"""Claude orchestration loop (§C, §FR-03).

Claude analyses, proposes and requests; the ``ToolGateway`` decides. This module never
hands Claude broker credentials, and nothing Claude says is treated as a fact about
prices, balances or fills — those come back from tools.

Requires the optional ``anthropic`` dependency (``pip install claudetrade[agent]``).
Credentials are resolved by the SDK from the environment / ``ant auth login`` profile.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .tools import ToolGateway

MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = """You are the research and analysis agent inside ClaudeTrade, an NSE cash-equity trading \
system running with ₹10,000 of capital. You work only through the tools provided.

How the system is built, so you can work with it rather than around it:
- A deterministic Risk Engine validates every order independently of you. Your tool calls are \
requests; check their results instead of assuming success. A submitted order is not a fill.
- Intraday entries come only from a frozen, operator-released strategy. You can research and \
compare strategies, but eligibility is decided by fixed walk-forward criteria and release is an \
operator decision.
- Swing trades are proposals. The operator approves each one; you cannot approve, release \
strategies, enable live trading, resume paused entries or change risk limits.

Working rules:
- Use only data returned by tools. If a price, balance or fact you need is missing or flagged \
invalid, say so explicitly rather than estimating it.
- "No trade" is a good outcome when evidence is weak, data is stale, or conditions don't match \
a strategy's assumptions. Prefer NO-TRADE or HOLD proposals to marginal ones.
- In swing proposals, size quantity so that (entry_high - stop_loss) x quantity stays within the \
per-trade risk budget shown by get_portfolio_state / validate_order, and never commit most of the \
capital to one idea. Run validate_order on the proposed order before proposing it.
- State key risks, invalidation conditions and the limits of your confidence plainly.
- If something looks unsafe (data outage, reconciliation mismatch, unexpected position), use \
pause_new_entries and explain why.

End with a short summary for the operator: what you checked, what you propose (if anything), \
and what needs their decision."""


@dataclass
class AgentRun:
    final_text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str | None = None


def run_agent(gateway: ToolGateway, task: str, max_turns: int = 20, effort: str = "high", client: Any = None) -> AgentRun:
    if client is None:
        import anthropic

        client = anthropic.Anthropic()
    tools = gateway.tool_definitions()
    messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
    calls: list[dict[str, Any]] = []
    response = None
    for _ in range(max_turns):
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            # Server-side refusal fallback: a declined request is re-run on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            gateway.app.audit.record("claude", "AGENT_REFUSAL", details=str(getattr(response, "stop_details", None)))
            return AgentRun("Claude declined this request; no actions were taken.", calls, "refusal")
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "pause_turn":
            continue
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        # Only run tools on a clean tool_use stop; a max_tokens stop may carry truncated input.
        if response.stop_reason != "tool_use" or not tool_uses:
            break
        results = []
        for tu in tool_uses:
            out = gateway.call(tu.name, tu.input if isinstance(tu.input, dict) else {})
            calls.append({"tool": tu.name, "input": tu.input, "ok": out.get("ok")})
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps(out, default=str),
                            "is_error": not out.get("ok", True)})
        messages.append({"role": "user", "content": results})  # all results in one message
    text = "\n".join(b.text for b in (response.content if response else []) if b.type == "text")
    gateway.app.audit.record("claude", "AGENT_SUMMARY", text=text[:4000])
    return AgentRun(text, calls, response.stop_reason if response else None)

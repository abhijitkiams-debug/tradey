"""Agent loop logic with a scripted fake client (no network, no API key)."""

from types import SimpleNamespace as NS

from claudetrade.agent import run_agent
from claudetrade.tools import ToolGateway


class FakeMessages:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def create(self, **kw):
        self.requests.append(kw)
        return self.script.pop(0)


def fake_client(script):
    m = FakeMessages(script)
    return NS(beta=NS(messages=m)), m


def test_tool_loop_runs_gateway_and_returns_summary(app):
    client, msgs = fake_client([
        NS(stop_reason="tool_use", content=[
            NS(type="tool_use", id="t1", name="get_portfolio_state", input={}),
            NS(type="tool_use", id="t2", name="validate_order",
               input={"symbol": "ALPHA", "side": "BUY", "quantity": 5, "mode": "SWING", "stop_loss": 490}),
        ]),
        NS(stop_reason="end_turn", content=[NS(type="text", text="No trade today: evidence is thin.")]),
    ])
    run = run_agent(ToolGateway(app, "paper"), "Review the portfolio", client=client)
    assert run.final_text == "No trade today: evidence is thin."
    assert [c["tool"] for c in run.tool_calls] == ["get_portfolio_state", "validate_order"]
    second = msgs.requests[1]["messages"]
    results = second[2]["content"]  # [user task, assistant tool_use, user tool_results, ...]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]  # all results in one user turn
    req = msgs.requests[0]
    assert req["model"] == "claude-opus-5-5" and req["thinking"] == {"type": "adaptive"}
    assert {t["name"] for t in req["tools"]} >= {"get_market_snapshot", "propose_swing_trade"}
    assert not app.execution.orders


def test_refusal_stops_without_actions(app):
    client, _ = fake_client([NS(stop_reason="refusal", content=[], stop_details=None)])
    run = run_agent(ToolGateway(app, "paper"), "x", client=client)
    assert run.stop_reason == "refusal" and not run.tool_calls


def test_truncated_tool_call_is_not_executed(app):
    client, _ = fake_client([NS(stop_reason="max_tokens", content=[
        NS(type="tool_use", id="t1", name="pause_new_entries", input={"reason": "partial"})])])
    run = run_agent(ToolGateway(app, "paper"), "x", client=client)
    assert not run.tool_calls and not app.controls.entries_paused

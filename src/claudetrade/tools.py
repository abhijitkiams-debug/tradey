"""Claude tool contract and gateway (§8).

Claude reaches the system only through these typed tools. The gateway:
* validates every input against the tool's JSON schema (unknown fields rejected);
* enforces a per-environment permission set;
* never exposes approval, release, live-authorisation, resume or config-change actions —
  those are operator-only methods with no tool binding;
* returns facts from system state (a tool call is a request, never proof of success).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from .app import ClaudeTrade
from .backtest import BacktestConfig, EligibilityCriteria, walk_forward
from .models import OrderRequest, OrderType, Product, Side, TradeMode
from .swing import SchemaError


class Permission(str, Enum):
    READ = "read"
    RESEARCH = "research"
    NON_EXECUTING = "non_executing"
    RESTRICTED = "restricted"
    SAFETY = "safety"


ENV_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "research": frozenset({Permission.READ, Permission.RESEARCH}),
    "paper": frozenset(Permission),
    # Live keeps the same surface; every RESTRICTED call still passes the Risk Engine and,
    # for swing entries, an operator approval.
    "live": frozenset(Permission),
}

SYMBOL = {"type": "string", "pattern": "^[A-Z0-9&-]{1,20}$"}
MODE = {"type": "string", "enum": ["INTRADAY", "SWING"]}

TOOLS: list[dict[str, Any]] = [
    {"name": "get_market_snapshot", "permission": Permission.READ,
     "description": "Validated quotes for the given symbols, with data age and any validation issues. "
                    "Use this instead of assuming prices; invalid data is reported, never filled in.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["symbols", "mode"],
                      "properties": {"symbols": {"type": "array", "items": SYMBOL, "maxItems": 50}, "mode": MODE}}},
    {"name": "get_portfolio_state", "permission": Permission.READ,
     "description": "Cash, available (uncommitted) capital, reservations, positions, pending orders, P&L and charges.",
     "input_schema": {"type": "object", "additionalProperties": False, "properties": {}}},
    {"name": "get_strategy_results", "permission": Permission.READ,
     "description": "Frozen strategy versions with eligibility status, failures and walk-forward evidence.",
     "input_schema": {"type": "object", "additionalProperties": False,
                      "properties": {"family": {"type": "string", "enum": ["ORB", "VWAP_PULLBACK", "MEAN_REVERSION"]}}}},
    {"name": "propose_strategy", "permission": Permission.RESEARCH,
     "description": "Run walk-forward validation for a strategy family on the loaded research dataset and freeze "
                    "the result as a new version. Eligibility is decided by fixed criteria, not by you. Research only: "
                    "does not release or activate anything.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["family", "rationale"],
                      "properties": {"family": {"type": "string", "enum": ["ORB", "VWAP_PULLBACK", "MEAN_REVERSION"]},
                                     "rationale": {"type": "string", "minLength": 10}}}},
    {"name": "validate_strategy", "permission": Permission.NON_EXECUTING,
     "description": "Run today's session validation for an operator-released strategy. You may add advisory flags "
                    "(e.g. material news); flags can only defer or reject, never approve.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["release_id"],
                      "properties": {"release_id": {"type": "string"},
                                     "advisory_flags": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "propose_swing_trade", "permission": Permission.NON_EXECUTING,
     "description": "Submit a structured swing proposal (schema swing-proposal/1). It is stored for the operator "
                    "to review; it cannot execute without their explicit approval. Use recommendation NO-TRADE or HOLD "
                    "when evidence is insufficient. Never invent prices: base entries on get_market_snapshot.",
     "input_schema": {"type": "object", "additionalProperties": False,
                      "required": ["symbol", "recommendation", "horizon_days", "rationale", "risks"],
                      "properties": {
                          "symbol": SYMBOL, "exchange": {"type": "string", "enum": ["NSE"]},
                          "recommendation": {"type": "string", "enum": ["BUY", "SELL", "HOLD", "NO-TRADE"]},
                          "entry_low": {"type": "number"}, "entry_high": {"type": "number"},
                          "quantity": {"type": "integer", "minimum": 0},
                          "stop_loss": {"type": "number"}, "target_low": {"type": "number"}, "target_high": {"type": "number"},
                          "horizon_days": {"type": "integer", "minimum": 5, "maximum": 31},
                          "rationale": {"type": "string"},
                          "evidence": {"type": "array", "items": {"type": "string"}},
                          "risks": {"type": "array", "items": {"type": "string"}},
                          "invalidation": {"type": "array", "items": {"type": "string"}},
                          "confidence_notes": {"type": "string"}}}},
    {"name": "validate_order", "permission": Permission.NON_EXECUTING,
     "description": "Dry-run the deterministic Risk Engine on a hypothetical order. Returns every check and reason. "
                    "Nothing is reserved or submitted.",
     "input_schema": {"type": "object", "additionalProperties": False,
                      "required": ["symbol", "side", "quantity", "mode"],
                      "properties": {"symbol": SYMBOL, "side": {"type": "string", "enum": ["BUY", "SELL"]},
                                     "quantity": {"type": "integer", "minimum": 1}, "mode": MODE,
                                     "limit_price": {"type": "number"}, "stop_loss": {"type": "number"},
                                     "is_exit": {"type": "boolean"}}}},
    {"name": "request_order", "permission": Permission.RESTRICTED,
     "description": "Request an order through the execution gateway. Permitted only for (a) exits that reduce an "
                    "existing position, or (b) a swing entry backed by an operator approval_id. Intraday entries come "
                    "only from the frozen strategy. The Risk Engine still validates every request.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["kind"],
                      "properties": {"kind": {"type": "string", "enum": ["exit", "approved_swing"]},
                                     "symbol": SYMBOL, "mode": MODE, "quantity": {"type": "integer", "minimum": 1},
                                     "approval_id": {"type": "string"}, "reason": {"type": "string"}}}},
    {"name": "get_order_status", "permission": Permission.READ,
     "description": "Broker-reconciled lifecycle of an order: status, fills, average price, rejection reason.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["client_request_id"],
                      "properties": {"client_request_id": {"type": "string"}}}},
    {"name": "cancel_order", "permission": Permission.RESTRICTED,
     "description": "Request cancellation of a working entry order. Exit orders cannot be cancelled from here.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["client_request_id"],
                      "properties": {"client_request_id": {"type": "string"}}}},
    {"name": "get_positions", "permission": Permission.READ,
     "description": "Reconcile with the broker, then return positions and any mismatches.",
     "input_schema": {"type": "object", "additionalProperties": False, "properties": {}}},
    {"name": "request_square_off", "permission": Permission.SAFETY,
     "description": "Start the intraday square-off workflow now (cancel entries, exit all intraday positions, verify flat).",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["reason"],
                      "properties": {"reason": {"type": "string", "minLength": 5}}}},
    {"name": "pause_new_entries", "permission": Permission.SAFETY,
     "description": "Stop all new entries. Only the operator can resume.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["reason"],
                      "properties": {"reason": {"type": "string", "minLength": 5}, "mode": MODE}}},
    {"name": "get_system_health", "permission": Permission.READ,
     "description": "Broker, market-data, reconciliation, kill-switch status and recent alerts.",
     "input_schema": {"type": "object", "additionalProperties": False, "properties": {}}},
]

FORBIDDEN_ACTIONS = ("approve", "release", "authorise_live", "resume_entries", "reset_kill_switch", "set_risk_config")


class ToolInputError(ValueError):
    pass


def validate_schema(schema: dict[str, Any], value: Any, path: str = "$") -> None:
    """Minimal JSON-schema subset validator (types, enum, required, additionalProperties,
    min/max, minLength, pattern, items, maxItems) so the gateway has no extra dependency."""
    import re

    t = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool}
    if t:
        if t in ("integer", "number") and isinstance(value, bool):
            raise ToolInputError(f"{path}: expected {t}")
        if not isinstance(value, types[t]):
            raise ToolInputError(f"{path}: expected {t}")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolInputError(f"{path}: must be one of {schema['enum']}")
    if t == "object":
        props = schema.get("properties", {})
        for r in schema.get("required", []):
            if r not in value:
                raise ToolInputError(f"{path}.{r}: required")
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            if extra:
                raise ToolInputError(f"{path}: unexpected fields {sorted(extra)}")
        for k, v in value.items():
            if k in props:
                validate_schema(props[k], v, f"{path}.{k}")
    if t == "array":
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ToolInputError(f"{path}: too many items")
        for i, v in enumerate(value):
            validate_schema(schema.get("items", {}), v, f"{path}[{i}]")
    if t in ("integer", "number"):
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolInputError(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolInputError(f"{path}: above maximum {schema['maximum']}")
    if t == "string":
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ToolInputError(f"{path}: too short")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise ToolInputError(f"{path}: invalid format")


_UNSUPPORTED_STRICT = {"minimum", "maximum", "minLength", "maxLength", "pattern", "maxItems", "minItems"}


def api_schema(schema: Any) -> Any:
    """Strict tool schemas don't accept numeric/string/array constraints; the gateway
    enforces those itself in ``validate_schema``, so they are stripped from what the API sees."""
    if isinstance(schema, dict):
        return {k: api_schema(v) for k, v in schema.items() if k not in _UNSUPPORTED_STRICT}
    if isinstance(schema, list):
        return [api_schema(v) for v in schema]
    return schema


def to_jsonable(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if is_dataclass(obj) and not isinstance(obj, type):
        return to_jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, float) and obj in (float("inf"), float("-inf")):
        return str(obj)
    return obj


class ToolGateway:
    def __init__(self, app: ClaudeTrade, environment: str = "paper", research_data: dict | None = None,
                 backtest_config: BacktestConfig | None = None, criteria: EligibilityCriteria | None = None):
        if environment not in ENV_PERMISSIONS:
            raise ValueError(environment)
        self.app = app
        self.env = environment
        self.allowed = ENV_PERMISSIONS[environment]
        self.research_data = research_data or {}
        self.bt_cfg = backtest_config or BacktestConfig(capital=float(app.risk_config.starting_capital),
                                                        risk_per_trade=float(app.risk_config.risk_per_trade),
                                                        max_position_value=float(app.risk_config.max_position_value),
                                                        max_concurrent=app.risk_config.max_concurrent_positions)
        self.criteria = criteria or EligibilityCriteria()
        self._spec = {t["name"]: t for t in TOOLS}
        self._handlers: dict[str, Callable[[dict], Any]] = {n: getattr(self, f"_t_{n}") for n in self._spec}

    def tool_definitions(self) -> list[dict[str, Any]]:
        """Anthropic Messages API tool definitions for the tools this environment permits."""
        return [{"name": t["name"], "description": t["description"], "input_schema": api_schema(t["input_schema"]),
                 "strict": True}
                for t in TOOLS if t["permission"] in self.allowed]

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        spec = self._spec.get(name)
        if spec is None:
            return self._deny(name, args, "unknown tool")
        if spec["permission"] not in self.allowed:
            return self._deny(name, args, f"tool not permitted in {self.env} environment")
        try:
            validate_schema(spec["input_schema"], args)
        except ToolInputError as e:
            return self._deny(name, args, f"invalid input: {e}")
        with self.app.lock:  # per call, so a long agent run never blocks the trading loop
            self.app.audit.record("claude", "TOOL_CALL", tool=name, args=to_jsonable(args))
            try:
                result = self._handlers[name](args)
            except (SchemaError, ToolInputError, KeyError, PermissionError, ValueError) as e:
                result = {"ok": False, "error": str(e)}
        out = to_jsonable(result)
        self.app.audit.record("gateway", "TOOL_RESULT", tool=name, ok=out.get("ok", True) if isinstance(out, dict) else True)
        return out

    def _deny(self, name: str, args: dict, reason: str) -> dict[str, Any]:
        self.app.audit.record("gateway", "TOOL_DENIED", tool=name, reason=reason)
        return {"ok": False, "error": reason}

    # ---- handlers -----------------------------------------------------------------------
    def _t_get_market_snapshot(self, a):
        mode = TradeMode(a["mode"])
        snap = self.app.market_data.snapshot(a["symbols"], mode)
        return {"ok": True, "as_of": self.app.clock(), "quotes": {
            s: {"valid": v.valid, "issues": list(v.issues), "age_seconds": v.age_seconds,
                "ltp": v.quote.ltp if v.quote else None, "bid": v.quote.bid if v.quote else None,
                "ask": v.quote.ask if v.quote else None, "prev_close": v.quote.prev_close if v.quote else None,
                "source": v.quote.source if v.quote else None, "source_ts": v.quote.source_ts if v.quote else None}
            for s, v in snap.items()}}

    def _t_get_portfolio_state(self, a):
        return {"ok": True, **self.app.dashboard()["portfolio"], "as_of": self.app.clock()}

    def _t_get_strategy_results(self, a):
        fam = a.get("family")
        return {"ok": True, "versions": [
            {"hash": h, "label": v.label, "status": v.status.value, "failures": list(v.eligibility_failures),
             "params": v.params_dict, "evidence": self.app.strategies.evidence.get(h)}
            for h, v in self.app.strategies.versions.items() if fam is None or v.family == fam],
            "releases": [asdict(r) for r in self.app.strategies.releases.values()]}

    def _t_propose_strategy(self, a):
        from .strategies import REGISTRY

        if not self.research_data:
            return {"ok": False, "error": "no research dataset loaded"}
        cls = REGISTRY.strategies[a["family"]]
        wf = walk_forward(cls, self.research_data, self.bt_cfg, self.app.costs)
        sv = self.app.strategies.freeze(wf, self.criteria, self.app.risk_config)
        self.app.audit.record("claude", "STRATEGY_PROPOSED", family=a["family"], rationale=a["rationale"], label=sv.label)
        return {"ok": True, "version_hash": sv.content_hash, "label": sv.label, "status": sv.status.value,
                "failures": list(sv.eligibility_failures), "summary": wf.summary(),
                "note": "Release for live/paper sessions requires an operator decision."}

    def _t_validate_strategy(self, a):
        rel = self.app.strategies.releases.get(a["release_id"])
        if rel is None:
            return {"ok": False, "error": "unknown release"}
        v = self.app.strategies.validate_for_session(rel, self.app.market_data, list(self.app.intraday.universe or self.app.market_data.instruments),
                                                     self.app.risk_config, self.app.session, a.get("advisory_flags"))
        return {"ok": True, "outcome": v.outcome.value, "reasons": list(v.reasons)}

    def _t_propose_swing_trade(self, a):
        p = self.app.swing.submit_proposal(dict(a), actor="claude")
        return {"ok": True, "proposal_id": p.proposal_id, "version": p.version, "hash": p.content_hash(),
                "reference_price": p.reference_price, "estimated_costs": p.estimated_costs,
                "max_loss_estimate": p.max_loss, "expires_at": p.expires_at, "status": "AWAITING_OPERATOR_APPROVAL"}

    def _order_from(self, a, client_id: str) -> OrderRequest:
        mode = TradeMode(a["mode"])
        lp = Decimal(str(a["limit_price"])) if "limit_price" in a else None
        return OrderRequest(
            client_request_id=client_id, symbol=a["symbol"], side=Side(a["side"]), quantity=a["quantity"],
            order_type=OrderType.LIMIT if lp else OrderType.MARKET,
            product=Product.MIS if mode is TradeMode.INTRADAY else Product.CNC, mode=mode, limit_price=lp,
            stop_loss=Decimal(str(a["stop_loss"])) if "stop_loss" in a else None,
            is_exit=a.get("is_exit", False), origin="claude")

    def _t_validate_order(self, a):
        d = self.app.risk.validate(self._order_from(a, f"dry-{uuid.uuid4().hex[:8]}"))
        return {"ok": True, "approved": d.approved, "reasons": d.rejection_reasons,
                "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in d.checks],
                "estimated_charges": d.estimated_cost, "would_reserve": d.reserved_amount, "dry_run": True}

    def _t_request_order(self, a):
        if a["kind"] == "approved_swing":
            if "approval_id" not in a:
                return {"ok": False, "error": "approval_id required"}
            r = self.app.swing.execute(a["approval_id"], actor="claude")
            if isinstance(r, str):
                return {"ok": False, "error": r}
            return self._result(r)
        for k in ("symbol", "mode", "quantity"):
            if k not in a:
                return {"ok": False, "error": f"{k} required for exit"}
        mode = TradeMode(a["mode"])
        pos = self.app.portfolio.position(a["symbol"], mode)
        if pos.quantity == 0:
            return {"ok": False, "error": "no open position to exit"}
        req = OrderRequest(
            client_request_id=f"cx-{uuid.uuid4().hex[:12]}", symbol=a["symbol"],
            side=Side.SELL if pos.quantity > 0 else Side.BUY, quantity=a["quantity"], order_type=OrderType.MARKET,
            product=Product.MIS if mode is TradeMode.INTRADAY else Product.CNC, mode=mode, is_exit=True, origin="claude")
        return self._result(self.app.execution.execute(req))

    def _result(self, r):
        o = r.order
        return {"ok": o is not None and o.status.value != "REJECTED", "message": r.message,
                "risk_approved": r.decision.approved, "risk_reasons": r.decision.rejection_reasons,
                "client_request_id": o.client_request_id if o else None,
                "status": o.status.value if o else None,
                "note": "Submission is not a fill. Use get_order_status to verify."}

    def _t_get_order_status(self, a):
        self.app.execution.sync()
        o = self.app.execution.orders.get(a["client_request_id"])
        if o is None:
            return {"ok": False, "error": "unknown order"}
        return {"ok": True, "status": o.status.value, "broker_order_id": o.broker_order_id,
                "quantity": o.request.quantity, "filled": o.filled_quantity, "average_price": o.average_price,
                "reject_reason": o.reject_reason, "updated_at": o.updated_at}

    def _t_cancel_order(self, a):
        o = self.app.execution.orders.get(a["client_request_id"])
        if o is not None and o.request.is_exit:
            return {"ok": False, "error": "exit orders (stops, square-off) cannot be cancelled by the agent"}
        msg = self.app.execution.cancel(a["client_request_id"], actor="claude")
        return {"ok": True, "message": msg}

    def _t_get_positions(self, a):
        mismatches = self.app.execution.reconcile_positions()
        return {"ok": True, "positions": self.app.dashboard()["portfolio"]["positions"], "mismatches": mismatches}

    def _t_request_square_off(self, a):
        self.app.audit.record("claude", "SQUARE_OFF_REQUESTED", reason=a["reason"])
        st = self.app.intraday.run_square_off()
        return {"ok": True, "verified_flat": st.verified_flat, "attempts": st.attempts, "last_error": st.last_error}

    def _t_pause_new_entries(self, a):
        mode = TradeMode(a["mode"]) if "mode" in a else None
        self.app.monitor.pause_entries("claude", a["reason"], mode)
        return {"ok": True, "paused": a.get("mode", "ALL"), "note": "Only the operator can resume entries."}

    def _t_get_system_health(self, a):
        return {"ok": True, **self.app.dashboard()["operations"], "as_of": self.app.clock()}


def dumps(result: dict[str, Any]) -> str:
    return json.dumps(result, default=str)

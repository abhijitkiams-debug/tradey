"""Wires the components to the append-only ``Store`` and restores them after a restart.

Event streams (audit, ledger events, fills) are written *before* they take effect in memory.
State objects (orders, approvals, proposals, strategy versions, controls, engine state) are
checkpointed after every audited action; unchanged snapshots are skipped.

Restore rebuilds memory from the store, then the caller reconciles with the broker. Two
things are deliberately NOT restored:
* live-trading authorisation — a restart must never silently re-enable live trading (§5);
* a resumed state for entries after a kill switch — the kill switch survives restarts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .broker.base import BrokerOrderState
from .ledger import LedgerEvent
from .lifecycle import Release, SessionOutcome, SessionValidation, StrategyStatus, StrategyVersion
from .models import (
    CheckResult,
    Fill,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Product,
    RiskDecision,
    Side,
    TradeMode,
)
from .store import Store
from .swing import Approval, ApprovalStatus, Recommendation, SwingProposal

if TYPE_CHECKING:
    from .app import ClaudeTrade


# ---- decoders -------------------------------------------------------------------------------
def request_from(d: dict[str, Any]) -> OrderRequest:
    return OrderRequest(**{**d, "side": Side(d["side"]), "order_type": OrderType(d["order_type"]),
                           "product": Product(d["product"]), "mode": TradeMode(d["mode"])})


def fill_from(d: dict[str, Any]) -> Fill:
    return Fill(**{**d, "side": Side(d["side"])})


def order_from(d: dict[str, Any]) -> Order:
    return Order(**{**d, "request": request_from(d["request"]), "status": OrderStatus(d["status"])})


def broker_state_from(d: dict[str, Any]) -> BrokerOrderState:
    return BrokerOrderState(**{**d, "status": OrderStatus(d["status"]), "fills": [fill_from(f) for f in d["fills"]]})


def decision_from(d: dict[str, Any]) -> RiskDecision:
    return RiskDecision(**{**d, "checks": tuple(CheckResult(**c) for c in d["checks"])})


def ledger_event_from(d: dict[str, Any]) -> LedgerEvent:
    return LedgerEvent(**{**d, "mode": TradeMode(d["mode"]) if d["mode"] else None})


def proposal_from(d: dict[str, Any]) -> SwingProposal:
    return SwingProposal(**{**d, "recommendation": Recommendation(d["recommendation"]),
                            "evidence": tuple(d["evidence"]), "risks": tuple(d["risks"]),
                            "invalidation": tuple(d["invalidation"])})


def strategy_version_from(d: dict[str, Any]) -> StrategyVersion:
    return StrategyVersion(**{**d, "params": tuple(tuple(p) for p in d["params"]), "status": StrategyStatus(d["status"]),
                              "eligibility_failures": tuple(d["eligibility_failures"])})


class IntegrityError(RuntimeError):
    pass


class Persistence:
    def __init__(self, store: Store):
        self.store = store
        self.app: ClaudeTrade | None = None
        self._checkpointing = False

    # ---- wiring ---------------------------------------------------------------------------
    def attach(self, app: ClaudeTrade) -> None:
        self.app = app
        app.audit.sink = self.store.append_audit
        app.audit.after_record = lambda ev: self.checkpoint()
        app.ledger.sink = lambda ev: self.store.put("ledger_event", f"{ev.seq:09d}", ev, dedupe=False)
        app.execution.on_fill = lambda o, f: self.store.put("fill", f.fill_id, {"fill": f, "mode": o.request.mode})
        app.execution.on_change = self.checkpoint

    def checkpoint(self) -> None:
        """Persist every changed state object. Cheap enough to run after each audited action."""
        if self.app is None or self._checkpointing:
            return
        self._checkpointing = True
        try:
            a, put = self.app, self.store.put
            ex = a.execution
            for cid in sorted(ex.dirty):
                if cid in ex.orders:
                    put("order", cid, ex.orders[cid])
            ex.dirty.clear()
            for cid, d in ex.decisions.items():
                put("decision", cid, d)
            for (sym, mode), p in a.portfolio.positions.items():
                put("position_meta", f"{sym}|{mode.value}", {"stop_loss": p.stop_loss, "target": p.target})
            for pid, versions in a.swing.proposals.items():
                for p in versions:
                    put("proposal", f"{pid}|{p.version:04d}", p)
            for aid, ap in a.swing.approvals.items():
                put("approval", aid, ap)
            for pid, reason in a.swing.declined.items():
                put("declined", pid, reason)
            for h, sv in a.strategies.versions.items():
                put("strategy_version", h, sv)
                put("strategy_evidence", h, a.strategies.evidence.get(h))
            for rid, r in a.strategies.releases.items():
                put("release", rid, r)
            c = a.controls
            put("controls", "global", {"kill_switch": c.kill_switch, "entries_paused": c.entries_paused,
                                       "paused_modes": sorted(m.value for m in c.paused_modes), "reasons": c.reasons})
            eng = a.intraday
            put("engine", "state", {
                "date": a.clock().date(),
                "strategy_hash": eng.strategy_version.content_hash if eng.strategy_version else None,
                "validation": eng.validation, "universe": eng.universe, "invalidated": eng.invalidated,
                "acted": sorted([list(x) for x in eng._acted_signals]),
                "square_off": vars(eng.square_off),
            })
            if hasattr(a.broker, "snapshot"):
                put("paper_broker", "state", a.broker.snapshot())
        finally:
            self._checkpointing = False

    # ---- restore --------------------------------------------------------------------------
    def has_state(self) -> bool:
        return bool(self.store.latest("ledger_event"))

    def restore(self, app: ClaudeTrade) -> dict[str, int]:
        s = self.store
        app.audit.load(s.audit_rows())
        app.ledger.load([ledger_event_from(v) for _, v in sorted(s.latest("ledger_event").items())])

        for h, d in s.latest("strategy_version").items():
            sv = strategy_version_from(d)
            if sv.content_hash != h:
                raise IntegrityError(f"strategy version {h[:10]} fails its hash check")
            app.strategies.versions[h] = sv
        app.strategies.evidence.update({h: v for h, v in s.latest("strategy_evidence").items() if v})
        for rid, d in s.latest("release").items():
            app.strategies.releases[rid] = Release(**d)

        orders = {cid: order_from(d) for cid, d in s.latest("order").items()}
        for o in orders.values():
            if o.status is OrderStatus.PENDING:
                # Crashed between our durable PENDING record and the broker's answer:
                # outcome unknown, so it must be reconciled by reference, never resubmitted.
                o.status = OrderStatus.UNKNOWN
        app.execution.orders.update(orders)
        app.execution.decisions.update({cid: decision_from(d) for cid, d in s.latest("decision").items()})
        for fid, d in s.all("fill"):
            f, mode = fill_from(d["fill"]), TradeMode(d["mode"])
            app.execution._seen_fill_ids.add(fid)
            app.portfolio.apply_fill(f, mode)
        for key, meta in s.latest("position_meta").items():
            sym, mode = key.split("|")
            pos = app.portfolio.position(sym, TradeMode(mode))
            pos.stop_loss, pos.target = meta["stop_loss"], meta["target"]

        props: dict[str, list[SwingProposal]] = {}
        for _key, d in s.all("proposal"):
            p = proposal_from(d)
            versions = props.setdefault(p.proposal_id, [])
            if all(v.version != p.version for v in versions):
                versions.append(p)
        for pid in props:
            props[pid].sort(key=lambda p: p.version)
        app.swing.proposals.update(props)
        for aid, d in s.latest("approval").items():
            ap = Approval(**{**d, "status": ApprovalStatus(d["status"])})
            p = next((v for v in props.get(ap.proposal_id, []) if v.version == ap.version), None)
            if ap.status is ApprovalStatus.ACTIVE and (p is None or p.content_hash() != ap.proposal_hash):
                ap.status, ap.note = ApprovalStatus.INVALIDATED, "proposal missing or altered at restore"
            app.swing.approvals[aid] = ap

        app.swing.declined.update(s.latest("declined"))
        c = s.latest("controls").get("global")
        if c:
            app.controls.kill_switch = c["kill_switch"]
            app.controls.entries_paused = c["entries_paused"]
            app.controls.paused_modes = {TradeMode(m) for m in c["paused_modes"]}
            app.controls.reasons = list(c["reasons"])

        eng = s.latest("engine").get("state")
        if eng and eng["date"] == app.clock().date() and eng["strategy_hash"] in app.strategies.versions:
            v = eng["validation"]
            val = SessionValidation(SessionOutcome(v["outcome"]), tuple(v["reasons"]), v["version_hash"], v["ts"]) if v else None
            if val is not None:
                app.intraday.strategy_version = app.strategies.versions[eng["strategy_hash"]]
                app.intraday.validation = val
                app.intraday.universe = list(eng["universe"])
            app.intraday.invalidated = eng["invalidated"]
            app.intraday._acted_signals = {(sym, idx) for sym, idx in eng["acted"]}
            for k, val_ in eng["square_off"].items():
                setattr(app.intraday.square_off, k, val_)

        snap = s.latest("paper_broker").get("state")
        if snap and hasattr(app.broker, "restore"):
            app.broker.restore(snap, lambda item: (broker_state_from(item["state"]), request_from(item["request"])))
        return {"orders": len(orders), "audit_events": len(app.audit.events()), "proposals": len(props),
                "strategy_versions": len(app.strategies.versions)}


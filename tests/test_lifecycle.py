from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta
from decimal import Decimal as D

import pytest

from claudetrade.backtest import BacktestConfig, EligibilityCriteria, walk_forward
from claudetrade.lifecycle import SessionOutcome, StrategyStatus
from claudetrade.models import IST
from claudetrade.strategies import MeanReversion
from claudetrade.synthetic import universe

LENIENT = EligibilityCriteria(min_oos_trades=0, min_oos_profit_factor=0, min_oos_net_pnl=-1e12,
                              max_oos_drawdown_pct=100, min_positive_fold_ratio=0, must_survive_stress=(),
                              must_survive_top_trade_removal=False)


@pytest.fixture(scope="module")
def wf():
    u = universe(n_days=18, start=date(2026, 6, 1))
    return walk_forward(MeanReversion, {"ALPHA": u["ALPHA"]}, BacktestConfig(), train_days=12, test_days=6, min_train_trades=1)


def test_freeze_is_immutable_and_hashed(app, wf):
    sv = app.strategies.freeze(wf, LENIENT, app.risk_config)
    assert sv.status is StrategyStatus.ELIGIBLE
    with pytest.raises(FrozenInstanceError):
        sv.params = ()  # frozen dataclass
    assert app.strategies.versions[sv.content_hash] is sv
    assert sv.instantiate().params == sv.params_dict


def test_ineligible_strategy_cannot_be_released(app, wf):
    sv = app.strategies.freeze(wf, EligibilityCriteria(min_oos_trades=10_000), app.risk_config)
    assert sv.status is StrategyStatus.INELIGIBLE
    with pytest.raises(PermissionError):
        app.strategies.release(sv.content_hash, app.clock().date(), "owner")


def test_session_validation_outcomes(app, wf, clock):
    sv = app.strategies.freeze(wf, LENIENT, app.risk_config)
    universe_ = ["ALPHA", "BRAVO", "CHARLIE", "DELTA"]

    tomorrow = app.strategies.release(sv.content_hash, clock().date() + timedelta(days=1), "owner")
    assert app.strategies.validate_for_session(tomorrow, app.market_data, universe_, app.risk_config, app.session).outcome is SessionOutcome.EXPIRED

    rel = app.strategies.release(sv.content_hash, clock().date(), "owner")
    v = app.strategies.validate_for_session(rel, app.market_data, universe_, app.risk_config, app.session)
    assert v.outcome is SessionOutcome.APPROVED, v.reasons

    # Claude's advisory flag can only downgrade
    v = app.strategies.validate_for_session(rel, app.market_data, universe_, app.risk_config, app.session,
                                            advisory_flags=["RBI policy surprise at 10:00"])
    assert v.outcome is SessionOutcome.DEFERRED

    # Changed risk limits invalidate the frozen strategy
    changed = replace(app.risk_config, risk_per_trade=D("250"))
    v = app.strategies.validate_for_session(rel, app.market_data, universe_, changed, app.session)
    assert v.outcome is SessionOutcome.REJECTED


def test_stale_data_defers_then_rejects_after_deadline(app, wf, clock):
    sv = app.strategies.freeze(wf, LENIENT, app.risk_config)
    rel = app.strategies.release(sv.content_hash, clock().date(), "owner")
    clock.advance(minutes=5)  # all quotes now stale
    u = ["ALPHA", "BRAVO", "CHARLIE", "DELTA"]
    assert app.strategies.validate_for_session(rel, app.market_data, u, app.risk_config, app.session).outcome is SessionOutcome.DEFERRED
    clock.set(datetime(2026, 10, 13, 10, 45, tzinfo=IST))
    assert app.strategies.validate_for_session(rel, app.market_data, u, app.risk_config, app.session).outcome is SessionOutcome.REJECTED


def test_code_change_rejects_frozen_version(app, wf, monkeypatch):
    sv = app.strategies.freeze(wf, LENIENT, app.risk_config)
    rel = app.strategies.release(sv.content_hash, app.clock().date(), "owner")
    import claudetrade.lifecycle as lc

    monkeypatch.setattr(lc, "code_version", lambda cls: "0.0.0+tampered")
    v = app.strategies.validate_for_session(rel, app.market_data, ["ALPHA"], app.risk_config, app.session)
    assert v.outcome is SessionOutcome.REJECTED and "code changed" in v.reasons[0]

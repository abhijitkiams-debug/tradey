from dataclasses import replace
from datetime import date

from claudetrade.replay_day import replay_day, synthetic_loader
from claudetrade.settings import ResearchSettings, Settings


def test_full_day_replay_runs_the_whole_pipeline(tmp_path):
    s = Settings(universe=("ALPHA", "BRAVO", "CHARLIE", "DELTA"), default_avg_daily_volume=1_000_000,
                 auto_paper=("ORB", "VWAP_PULLBACK", "MEAN_REVERSION"),
                 research=ResearchSettings(history_days=40, train_days=14, test_days=6, cache_dir=str(tmp_path / "h")))
    load, _ = synthetic_loader(s)
    lines = []
    out = replay_day(s, date(2026, 10, 30), load, db_dir=str(tmp_path / "replay"), progress=lines.append)
    desk = out.pop("_desk")
    assert {v["outcome"] for v in out["validations"]} == {"APPROVED"}
    assert len([r for r in out["strategies"] if r["mode"] == "PAPER"]) == 3
    assert out["verified_flat"] is True and out["open_intraday_positions"] == 0 and out["audit_chain_intact"]
    assert out["entries"] == out["exits"]  # everything opened was closed the same day
    # isolated: its own database, never the real paper book
    assert out["db"].startswith(str(tmp_path / "replay")) and desk.paper.app.persistence.store.path == out["db"]
    assert all("@v1#" in v.label for v in desk.versions().values())  # no 16:00 research inside a replay


def test_replay_is_fresh_each_run(tmp_path):
    s = Settings(universe=("ALPHA", "BRAVO"), default_avg_daily_volume=1_000_000, auto_paper=("ORB",),
                 research=ResearchSettings(history_days=40, train_days=14, test_days=6))
    load, _ = synthetic_loader(s)
    a = replay_day(s, date(2026, 10, 30), load, db_dir=str(tmp_path), progress=lambda m: None)
    b = replay_day(replace(s), date(2026, 10, 30), load, db_dir=str(tmp_path), progress=lambda m: None)
    a.pop("_desk"), b.pop("_desk")
    assert a["fills"] == b["fills"] and a["realised_pnl"] == b["realised_pnl"]  # deterministic, no carry-over

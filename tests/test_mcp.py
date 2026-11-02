import asyncio
from datetime import datetime

import pytest

pytest.importorskip("mcp")
from fake_groww import FakeGroww  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from claudetrade.clock import SimClock  # noqa: E402
from claudetrade.desk import StrategyMode, build_desk  # noqa: E402
from claudetrade.mcp_server import build_server  # noqa: E402
from claudetrade.models import IST  # noqa: E402
from claudetrade.server import create_app  # noqa: E402
from claudetrade.settings import Settings  # noqa: E402


@pytest.fixture
def env(tmp_path):
    clock = SimClock(datetime(2026, 11, 2, 11, 0, tzinfo=IST))
    s = Settings(paper_db_path=str(tmp_path / "p.db"), live_db_path=str(tmp_path / "l.db"), live_enabled=True,
                 universe=("ALPHA",), default_avg_daily_volume=1_000_000)
    desk = build_desk(s, clock=clock, groww_client=FakeGroww(), research_loader=lambda: {}, notifier=lambda a: None)
    client = TestClient(create_app(desk, token="mcp-token"))

    def call(method, path, body=None):
        r = client.request(method, path, json=body, headers={"Authorization": "Bearer mcp-token", "X-Operator": "claude-mcp"})
        data = r.json()
        return data if r.status_code < 400 else {"ok": False, "status": r.status_code, "error": data.get("detail")}

    return desk, build_server(call)


def tool(srv, name, **args):
    out = asyncio.run(srv.call_tool(name, args))
    return out


def names(srv):
    return {t.name for t in asyncio.run(srv.list_tools())}


def test_tool_surface_excludes_risk_adding_actions(env):
    _, srv = env
    n = names(srv)
    assert {"status", "book_details", "set_strategy_mode", "kill_switch", "pause_new_entries", "square_off",
            "reconcile", "live_master_off", "audit_trail", "research", "strategy_gate", "proposal"} == n
    for forbidden in ("approve", "execute", "resume", "reset", "authorise", "ack", "release"):
        assert not any(forbidden in x for x in n)


def test_status_and_controls_go_through_the_api(env):
    desk, srv = env
    tool(srv, "status")
    tool(srv, "set_strategy_mode", key="SWING", mode="PAPER", reason="operator asked for proposals")
    assert desk.mode("SWING") is StrategyMode.PAPER
    assert desk.toggles["SWING"].changed_by == "operator:claude-mcp"
    tool(srv, "pause_new_entries", reason="news risk", book="paper")
    assert desk.paper.app.controls.entries_paused
    tool(srv, "kill_switch", reason="drill", book="all")
    assert desk.paper.app.controls.kill_switch and desk.live.app.controls.kill_switch
    assert any(e.actor == "operator:claude-mcp" for e in desk.paper.app.audit.events("KILL_SWITCH_ON"))


def test_live_cannot_be_set_through_mcp(env):
    desk, srv = env
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="Input should be 'OFF' or 'PAPER'"):
        tool(srv, "set_strategy_mode", key="SWING", mode="LIVE", reason="try")
    assert desk.mode("SWING") is StrategyMode.OFF

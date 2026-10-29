import pytest

from claudetrade.broker import groww_trace


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body, self.ok = status, body, status < 400

    def json(self):
        return self._body


def test_trace_shows_codes_and_never_secrets(monkeypatch):
    import requests

    def fake_get(url, **kw):
        return FakeResp(403, {"status": "FAILURE", "error": {"code": "GA005", "message": "Access forbidden for this request."}})

    def fake_post(url, **kw):
        return FakeResp(200, {"token": "SECRET-ACCESS-TOKEN"})

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_post)
    lines = []
    undo = groww_trace.install(lines.append)
    try:
        requests.get("https://api.groww.in/v1/live-data/quote",
                     params={"exchange": "NSE", "segment": "CASH", "trading_symbol": "ITC", "token": "SECRET-PARAM"},
                     headers={"Authorization": "Bearer SECRET-BEARER", "x-request-id": "req-123"})
        requests.post("https://api.groww.in/v1/token/api/access", json={"key_type": "totp", "totp": "123456"},
                      headers={"Authorization": "Bearer SECRET-BEARER", "x-request-id": "req-456"})
        requests.get("https://example.com/not-groww")  # untouched
    finally:
        undo()
    out = "\n".join(lines)
    assert len(lines) == 2
    assert "/v1/live-data/quote?exchange=NSE&segment=CASH&trading_symbol=ITC -> 403 FAILURE GA005" in out
    assert "req-123" in out and "token exchange" in out
    for secret in ("SECRET", "123456", "Bearer"):
        assert secret not in out
    assert requests.get is fake_get  # restored


def test_trace_through_real_sdk(monkeypatch):
    growwapi = pytest.importorskip("growwapi")
    import requests

    def fake_get(url, **kw):
        if url.endswith("/changelog"):
            return FakeResp(200, {"status": "SUCCESS", "payload": {}})
        return FakeResp(403, {"status": "FAILURE", "error": {"code": "GA005", "message": "Access forbidden for this request."}})

    monkeypatch.setattr(requests, "get", fake_get)
    lines = []
    undo = groww_trace.install(lines.append)
    try:
        api = growwapi.GrowwAPI("SECRET-TOKEN")
        with pytest.raises(Exception) as e:
            api.get_quote(trading_symbol="ITC", exchange="NSE", segment="CASH")
        assert getattr(e.value, "code", "") == "GA005"
    finally:
        undo()
    quote = [ln for ln in lines if "live-data/quote" in ln]
    assert quote and "GA005" in quote[0] and "SECRET" not in "\n".join(lines)


def test_service_log_is_throttled(caplog):
    class E(Exception):
        code, msg = "GA005", "Access forbidden for this request."

    groww_trace._last_logged.clear()
    with caplog.at_level("WARNING", logger="claudetrade.groww"):
        for _ in range(5):
            groww_trace.log_error("get_quote(ITC)", E())
    assert len(caplog.records) == 1 and "GA005" in caplog.text and "Access forbidden" in caplog.text


def test_token_failure_shows_reason_but_success_never_parsed(monkeypatch):
    import requests

    responses = [FakeResp(400, {"error": {"code": "GA001", "displayMessage": "Invalid TOTP"}}),
                 FakeResp(200, {"token": "SECRET-ACCESS-TOKEN", "message": "SECRET-ACCESS-TOKEN"})]
    monkeypatch.setattr(requests, "post", lambda url, **kw: responses.pop(0))
    lines = []
    undo = groww_trace.install(lines.append)
    try:
        requests.post("https://api.groww.in/v1/token/api/access", json={"totp": "123456"})
        requests.post("https://api.groww.in/v1/token/api/access", json={"totp": "654321"})
    finally:
        undo()
    assert 'GA001 "Invalid TOTP"' in lines[0]
    assert "SECRET" not in lines[1] and "-> 200" in lines[1]

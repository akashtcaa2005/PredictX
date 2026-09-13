"""Dashboard: authentication, RBAC, CSRF, data assembly and analytics.

Fully offline. Live-account sync and market discovery are disabled so no test
ever touches the network; every engine database is a temporary fixture.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from polybot.telemetry.recorder import EngineRecorder

ADMIN_PASSWORD = "Adm1n!TestPassword"
VIEWER_PASSWORD = "V13wer!TestPassword"


# ======================================================================
# Fixtures
# ======================================================================
@pytest.fixture()
def env(tmp_path, monkeypatch):
    """An isolated data root plus a dashboard config with no network access."""
    monkeypatch.setenv("POLYBOT_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "k" * 48)
    monkeypatch.setenv("DASHBOARD_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("DASHBOARD_LIVE_SYNC", "false")
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    monkeypatch.setenv("DASHBOARD_CONTROL_ENABLED", "true")
    monkeypatch.setenv("MARKET_DISCOVERY_INTERVAL_S", "86400")
    monkeypatch.setenv("BROADCAST_INTERVAL_S", "3600")
    monkeypatch.setenv("VPS_INTERVAL_S", "3600")

    # polybot.config.paths reads DATA_ROOT at import time; reload the chain.
    import importlib

    import dashboard.config
    import dashboard.state
    import polybot.config.paths
    import polybot.config.settings

    importlib.reload(polybot.config.paths)
    importlib.reload(polybot.config.settings)
    importlib.reload(dashboard.config)
    importlib.reload(dashboard.state)
    return tmp_path


@pytest.fixture()
def seeded(env):
    """A live engine database with two orders, one round trip and equity."""
    from polybot.config.paths import mode_paths
    from polybot.orders.models import Action, Order, OrderIntent, OrderStatus

    paths = mode_paths("live").ensure()
    recorder = EngineRecorder(paths.db, mode="live", strategy_name="s1_cheap_buy")
    recorder.start()

    buy = Order(intent=OrderIntent(market_id="0xmarket", strategy_side="up", token_id="T-up",
                                   action=Action.BUY, price=0.40, size=10.0))
    buy.status = OrderStatus.FILLED
    buy.filled_size = 10.0
    recorder.record_order(buy)
    recorder.record_fill(buy, price=0.40, size=10.0)

    sell = Order(intent=OrderIntent(market_id="0xmarket", strategy_side="up", token_id="T-up",
                                    action=Action.SELL, price=0.55, size=10.0))
    sell.status = OrderStatus.FILLED
    sell.filled_size = 10.0
    recorder.record_order(sell)
    recorder.record_fill(sell, price=0.55, size=10.0, realized_pnl=1.5)

    now = time.time()
    for i in range(6):
        recorder.record_equity({"ts": now - (6 - i) * 86400, "balance": 100 + i,
                                "cash": 100 + i, "positions_value": 0.0,
                                "equity": 100 + i, "realized_pnl": float(i),
                                "unrealized_pnl": 0.0, "exposure": 0.0,
                                "open_orders": 0, "open_positions": 0})
    recorder.record_heartbeat({"mode": "live", "ts_epoch": now, "equity": 105.0,
                               "cash": 105.0, "positions_value": 0.0,
                               "pnl": {"realized": 1.5, "unrealized": 0.0, "total": 1.5},
                               "risk": {"halted": False}, "uptime_s": 90.0,
                               "strategy": {"name": "s1_cheap_buy", "ticks": 42},
                               "connections": [{"channel": "market", "connected": True}]})
    recorder.record_strategy_state([{
        "strategy": "s1_cheap_buy", "market_id": "0xmarket", "market_label": "Up/Down",
        "ts": now, "enabled": 1, "running": 1, "last_signal": "BUY up",
        "last_signal_ts": now, "current_signal": None, "confidence": None,
        "position_qty": 0.0, "entry_price": None, "exit_price": None,
        "stop_loss": None, "take_profit": None, "unrealized": 0.0,
        "ticks": 42, "errors": 0}])
    recorder.record_event("info", "startup", "engine started")

    deadline = time.time() + 5
    while not recorder._q.empty() and time.time() < deadline:
        time.sleep(0.02)
    time.sleep(0.7)
    recorder.stop()
    return env


@pytest.fixture()
def client(seeded):
    from fastapi.testclient import TestClient

    from dashboard.config import DashboardSettings
    from dashboard.server import create_app

    app = create_app(DashboardSettings.load())
    with TestClient(app) as test_client:
        yield test_client


def login(client, username="admin", password=ADMIN_PASSWORD):
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["csrf"]


# ======================================================================
# Authentication & session handling
# ======================================================================
def test_everything_requires_authentication(client):
    for path in ("/api/snapshot", "/api/engine/live/orders", "/api/system", "/api/users"):
        assert client.get(path).status_code == 401, path


def test_health_endpoint_is_public(client):
    assert client.get("/healthz").text == "ok"


def test_bad_credentials_are_rejected(client):
    assert client.post("/api/auth/login",
                       json={"username": "admin", "password": "nope"}).status_code == 401
    assert client.post("/api/auth/login",
                       json={"username": "ghost", "password": "nope"}).status_code == 401


def test_repeated_failures_trigger_a_lockout(client):
    for _ in range(7):
        client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    response = client.post("/api/auth/login",
                           json={"username": "admin", "password": ADMIN_PASSWORD})
    assert response.status_code == 429
    assert "try again" in response.json()["detail"]


def test_logout_revokes_the_session(client):
    csrf = login(client)
    assert client.get("/api/auth/me").status_code == 200
    client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    assert client.get("/api/auth/me").status_code == 401


def test_session_cookie_is_httponly(client):
    response = client.post("/api/auth/login",
                           json={"username": "admin", "password": ADMIN_PASSWORD})
    cookie = response.headers.get("set-cookie", "")
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie.lower() or "samesite=lax" in cookie.lower()


def test_weak_passwords_are_refused(client):
    csrf = login(client)
    response = client.post("/api/users", headers={"X-CSRF-Token": csrf},
                           json={"username": "weak", "password": "short", "role": "viewer"})
    assert response.status_code == 422 or response.status_code == 400


# ======================================================================
# CSRF & RBAC
# ======================================================================
def test_writes_without_a_csrf_token_are_refused(client):
    login(client)
    assert client.post("/api/control/service",
                       json={"target": "paper", "action": "start"}).status_code == 403
    assert client.post("/api/control/backup", json={"scope": "all"}).status_code == 403


def test_a_wrong_csrf_token_is_refused(client):
    login(client)
    response = client.post("/api/control/backup", json={"scope": "all"},
                           headers={"X-CSRF-Token": "not-the-token"})
    assert response.status_code == 403


def test_viewer_cannot_control_or_administer(client):
    csrf = login(client)
    client.post("/api/users", headers={"X-CSRF-Token": csrf},
                json={"username": "watcher", "password": VIEWER_PASSWORD, "role": "viewer"})
    client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})

    viewer_csrf = login(client, "watcher", VIEWER_PASSWORD)
    headers = {"X-CSRF-Token": viewer_csrf}
    assert client.get("/api/snapshot").status_code == 200
    assert client.post("/api/control/service", json={"target": "live", "action": "stop"},
                       headers=headers).status_code == 403
    assert client.post("/api/control/emergency-stop", json={"mode": "live"},
                       headers=headers).status_code == 403
    assert client.get("/api/users").status_code == 403
    assert client.get("/api/audit").status_code == 403


def test_privileged_actions_are_audited(client):
    csrf = login(client)
    client.post("/api/control/backup", json={"scope": "dashboard"},
                headers={"X-CSRF-Token": csrf})
    audit = client.get("/api/audit").json()["audit"]
    actions = {row["action"] for row in audit}
    assert "login" in actions
    assert "backup" in actions


def test_the_last_admin_cannot_be_removed(client):
    csrf = login(client)
    admin = next(u for u in client.get("/api/users").json()["users"] if u["role"] == "admin")
    response = client.delete(f"/api/users/{admin['id']}", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 409


# ======================================================================
# Control-plane input validation
# ======================================================================
def test_only_allowlisted_service_actions_are_accepted(client):
    csrf = login(client)
    headers = {"X-CSRF-Token": csrf}
    assert client.post("/api/control/service", json={"target": "paper", "action": "reboot"},
                       headers=headers).status_code == 400
    assert client.post("/api/control/service",
                       json={"target": "paper", "action": "start; rm -rf /"},
                       headers=headers).status_code == 400
    assert client.post("/api/control/service", json={"target": "root", "action": "start"},
                       headers=headers).status_code == 400


def test_log_reads_cannot_escape_the_log_directory(client):
    login(client)
    assert client.get("/api/system/logs?mode=live&file=../../env.txt").status_code == 400
    assert client.get("/api/system/logs?mode=live&file=/etc/passwd").status_code == 400
    assert client.get("/api/system/logs?mode=bogus&file=x.log").status_code == 404


def test_emergency_stop_writes_the_kill_switch(client, seeded):
    from polybot.config.paths import mode_paths

    csrf = login(client)
    response = client.post("/api/control/emergency-stop",
                           json={"mode": "paper", "reason": "test drill"},
                           headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    switch = mode_paths("paper").kill_switch
    assert switch.exists()
    payload = json.loads(switch.read_text())
    assert payload["reason"] == "test drill"
    assert payload["actor"] == "admin"

    cleared = client.post("/api/control/clear-kill-switch", json={"mode": "paper"},
                          headers={"X-CSRF-Token": csrf})
    assert cleared.status_code == 200
    assert not switch.exists()


# ======================================================================
# Data assembly
# ======================================================================
def test_snapshot_has_every_top_level_section(client):
    login(client)
    snapshot = client.get("/api/snapshot").json()
    for key in ("engines", "live_account", "markets", "vps", "notifications", "dashboard"):
        assert key in snapshot, key
    assert set(snapshot["engines"]) == {"paper", "live"}


def test_engine_view_reads_recorded_state(client):
    login(client)
    summary = client.get("/api/engine/live/summary").json()
    assert summary["available"] is True
    assert summary["orders"]["total"] == 2
    assert summary["heartbeat"]["strategy"]["name"] == "s1_cheap_buy"

    orders = client.get("/api/engine/live/orders").json()
    assert len(orders["orders"]) == 2
    assert orders["orders"][0]["filled_pct"] == 100.0

    trades = client.get("/api/engine/live/trades").json()["trades"]
    assert len(trades) == 1
    assert trades[0]["pnl"] == pytest.approx(1.5)


def test_a_stopped_engine_degrades_to_empty_not_an_error(client):
    login(client)
    summary = client.get("/api/engine/paper/summary").json()
    assert summary["available"] is False
    assert summary["orders"]["total"] == 0
    assert client.get("/api/engine/paper/trades").json()["trades"] == []
    assert client.get("/api/engine/paper/analytics").json()["trades"]["total_trades"] == 0


def test_analytics_are_computed_from_recorded_trades(client):
    login(client)
    report = client.get("/api/engine/live/analytics").json()
    assert report["trades"]["total_trades"] == 1
    assert report["trades"]["winning_trades"] == 1
    assert report["trades"]["win_rate"] == 100.0
    assert report["equity"]["samples"] == 6
    assert report["equity"]["max_drawdown"] == 0.0


def test_strategy_view_merges_state_and_performance(client):
    login(client)
    strategies = client.get("/api/engine/live/strategies").json()["strategies"]
    assert len(strategies) == 1
    strategy = strategies[0]
    assert strategy["name"] == "s1_cheap_buy"
    assert strategy["running"] is True
    assert strategy["total_trades"] == 1
    assert strategy["rank"] == 1
    assert strategy["confidence"] is None, "the compiled strategy publishes no confidence"


def test_journal_auto_imports_completed_trades(client):
    login(client)
    # The housekeeping loop imports on its own schedule; force it for the test.
    state = client.app.state.app_state
    state.journal.sync("live", state.readers["live"])

    entries = client.get("/api/journal/live").json()["entries"]
    assert len(entries) == 1
    assert entries[0]["pnl"] == pytest.approx(1.5)
    assert entries[0]["auto"] == 1


def test_journal_annotations_persist_and_validate(client):
    csrf = login(client)
    state = client.app.state.app_state
    state.journal.sync("live", state.readers["live"])
    entry_id = client.get("/api/journal/live").json()["entries"][0]["id"]
    headers = {"X-CSRF-Token": csrf}

    ok = client.patch(f"/api/journal/entry/{entry_id}", headers=headers,
                      json={"notes": "clean fill", "rating": 4, "confidence": 0.8})
    assert ok.status_code == 200
    assert ok.json()["entry"]["notes"] == "clean fill"

    bad = client.patch(f"/api/journal/entry/{entry_id}", headers=headers, json={"rating": 9})
    assert bad.status_code == 400


def test_exports_carry_the_recorded_rows(client):
    login(client)
    csv_export = client.get("/api/export/live/trades?format=csv")
    assert csv_export.status_code == 200
    assert "entry_price" in csv_export.text
    assert "attachment" in csv_export.headers["content-disposition"]

    json_export = client.get("/api/export/live/orders?format=json").json()
    assert len(json_export) == 2


def test_security_headers_are_present(client):
    response = client.get("/healthz")
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_notifications_can_be_listed_and_marked_read(client):
    csrf = login(client)
    state = client.app.state.app_state
    import asyncio

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        state.notifications.emit("warning", "unit_test", "Test alert", "body", force=True))

    data = client.get("/api/notifications").json()
    assert data["unread"] >= 1
    assert any(n["kind"] == "unit_test" for n in data["notifications"])

    client.post("/api/notifications/read", json={}, headers={"X-CSRF-Token": csrf})
    assert client.get("/api/notifications").json()["unread"] == 0


# ======================================================================
# WebSocket stream
# ======================================================================
def test_websocket_requires_a_session(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()


def test_websocket_rejects_a_foreign_origin(client):
    from starlette.websockets import WebSocketDisconnect

    login(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"origin": "https://evil.example"}) as socket:
            socket.receive_json()


def test_websocket_streams_a_snapshot(client):
    login(client)
    with client.websocket_connect("/ws") as socket:
        hello = socket.receive_json()
        assert hello["channel"] == "hello"
        assert hello["user"] == "admin"
        assert "engines" in hello["data"]

        socket.send_json({"action": "ping"})
        assert socket.receive_json()["channel"] == "pong"

        socket.send_json({"action": "snapshot"})
        pushed = socket.receive_json()
        assert pushed["channel"] == "snapshot"
        assert pushed["data"]["engines"]["live"]["orders"]["total"] == 2


# ======================================================================
# Secret handling
# ======================================================================
def test_config_endpoint_masks_secrets(client):
    login(client)
    config = client.get("/api/config").json()["dashboard"]
    assert config["secret_key"].startswith("set (")
    assert "k" * 48 not in json.dumps(config)


def test_mask_secrets_redacts_keys_and_tokens():
    from dashboard.security import mask_secrets

    masked = mask_secrets("key=0x" + "a" * 64 + " and api_secret=hunter2hunter2")
    assert "a" * 64 not in masked
    assert "hunter2hunter2" not in masked
    assert "redacted" in masked


def test_read_only_exchange_blocks_writes():
    from dashboard.live_sync import ReadOnlyExchange

    proxy = ReadOnlyExchange(object())
    for forbidden in ("place_order", "cancel", "cancel_all", "create_order", "post_order"):
        with pytest.raises(AttributeError, match="read-only"):
            getattr(proxy, forbidden)

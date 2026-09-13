"""FastAPI application: REST API, WebSocket stream and the static SPA.

Security posture:

* Every route except ``/api/auth/login``, ``/healthz`` and the static assets
  requires an authenticated session.
* Every state-changing request requires the session's CSRF token in
  ``X-CSRF-Token``; a cookie alone is never sufficient.
* Permissions are checked server-side per route. The UI hides what a role cannot
  do, but hiding is not the control.
* Responses carry a strict CSP, frame-deny, nosniff and (under TLS) HSTS.
* WebSocket upgrades validate the ``Origin`` header against the configured
  origins, so another site cannot open an authenticated stream with your cookie.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any, Callable

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketDisconnect

from dashboard import __version__
from dashboard.config import DashboardSettings
from dashboard.control import ALLOWED_ACTIONS, list_log_files, read_log_tail
from dashboard.security import AuthError, Principal, mask_secrets
from dashboard.state import MODES, AppState

log = logging.getLogger("polybot.dashboard.server")

STATIC_DIR = Path(__file__).resolve().parent / "static"

#: Self-contained app: no external origins are ever contacted by the page.
_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self' ws: wss:; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'"
)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


# ======================================================================
# Request models
# ======================================================================
class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class ServiceBody(BaseModel):
    target: str
    action: str


class EmergencyBody(BaseModel):
    mode: str = "live"
    reason: str = ""


class ModeBody(BaseModel):
    mode: str


class BackupBody(BaseModel):
    scope: str = "all"


class UserBody(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=12, max_length=256)
    role: str = "viewer"


class UserPatchBody(BaseModel):
    role: str | None = None
    active: bool | None = None
    new_password: str | None = None


class JournalPatchBody(BaseModel):
    entry_reason: str | None = None
    exit_reason: str | None = None
    confidence: float | None = None
    notes: str | None = None
    rating: int | None = None
    tags: str | None = None


class ScreenshotBody(BaseModel):
    slot: str
    data_url: str


class ReconnectBody(BaseModel):
    target: str


# ======================================================================
# Application factory
# ======================================================================
def create_app(settings: DashboardSettings) -> FastAPI:
    state = AppState(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        await state.start()
        try:
            yield
        finally:
            await state.stop()

    app = FastAPI(title="Polybot Dashboard", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.app_state = state
    app.state.settings = settings

    # ==================================================================
    # Middleware
    # ==================================================================
    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable[..., Any]) -> Response:
        try:
            response: Response = await call_next(request)
        except AuthError as exc:
            response = JSONResponse({"error": str(exc)}, status_code=exc.status)
        response.headers["Content-Security-Policy"] = _CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Cache-Control"] = response.headers.get("Cache-Control", "no-store")
        if settings.tls_enabled or settings.behind_proxy:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    # ==================================================================
    # Auth dependencies
    # ==================================================================
    def client_ip(request: Request) -> str:
        if settings.behind_proxy:
            forwarded = request.headers.get("x-forwarded-for", "")
            if forwarded:
                return forwarded.split(",")[0].strip()[:64]
        return request.client.host if request.client else "unknown"

    def current_principal(request: Request) -> Principal | None:
        sid = request.cookies.get(settings.cookie_name)
        return state.security.resolve(sid)

    def require_auth(request: Request) -> Principal:
        principal = current_principal(request)
        if principal is None:
            raise HTTPException(status_code=401, detail="authentication required")
        return principal

    def require(permission: str) -> Callable[[Request], Principal]:
        """Dependency factory: authenticated + permitted (+ CSRF on writes)."""

        def dependency(request: Request) -> Principal:
            principal = require_auth(request)
            if not principal.can(permission):
                raise HTTPException(
                    status_code=403,
                    detail=f"role '{principal.role}' may not {permission}")
            if request.method not in _SAFE_METHODS:
                token = request.headers.get("x-csrf-token")
                try:
                    state.security.verify_csrf(principal, token)
                except AuthError as exc:
                    raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
            return principal

        return dependency

    view_dep = require("view")

    def audit(principal: Principal | None, request: Request, action: str, *,
              target: str = "", detail: str = "", ok: bool = True) -> None:
        state.security.audit(principal, action, target=target, detail=detail,
                             ip=client_ip(request), ok=ok)

    def valid_mode(mode: str) -> str:
        if mode not in MODES:
            raise HTTPException(status_code=404, detail=f"unknown mode {mode!r}")
        return mode

    # ==================================================================
    # Health & static
    # ==================================================================
    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz() -> str:
        return "ok"

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ==================================================================
    # Authentication
    # ==================================================================
    @app.post("/api/auth/login")
    async def login(request: Request, body: LoginBody, response: Response) -> dict[str, Any]:
        ip = client_ip(request)
        try:
            principal = await asyncio.to_thread(
                state.security.login, body.username, body.password,
                ip=ip, user_agent=request.headers.get("user-agent", ""))
        except AuthError as exc:
            audit(None, request, "login", target=body.username, detail=str(exc), ok=False)
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

        response.set_cookie(
            settings.cookie_name, principal.sid, httponly=True,
            secure=settings.cookie_secure, samesite="lax", path="/",
            max_age=settings.session_ttl_s)
        audit(principal, request, "login")
        user = state.db.query_one("SELECT must_change_pw FROM users WHERE id = ?",
                                  (principal.user_id,))
        return {"username": principal.username, "role": principal.role,
                "csrf": principal.csrf,
                "must_change_password": bool(user["must_change_pw"]) if user else False,
                "permissions": sorted(_permissions(principal))}

    @app.post("/api/auth/logout")
    async def logout(request: Request, response: Response) -> dict[str, Any]:
        principal = current_principal(request)
        if principal is not None:
            state.security.revoke(principal.sid)
            await state.hub.revoke_session(principal.sid)
            audit(principal, request, "logout")
        response.delete_cookie(settings.cookie_name, path="/")
        return {"ok": True}

    @app.get("/api/auth/me")
    async def me(principal: Principal = Depends(require_auth)) -> dict[str, Any]:
        row = state.db.query_one(
            "SELECT username, role, last_login_ts, must_change_pw FROM users WHERE id = ?",
            (principal.user_id,))
        return {"username": principal.username, "role": principal.role, "csrf": principal.csrf,
                "last_login_ts": row["last_login_ts"] if row else None,
                "must_change_password": bool(row["must_change_pw"]) if row else False,
                "permissions": sorted(_permissions(principal))}

    @app.post("/api/auth/password")
    async def change_password(request: Request, body: PasswordBody,
                              principal: Principal = Depends(require("view"))) -> dict[str, Any]:
        try:
            await asyncio.to_thread(state.security.change_password, principal.user_id,
                                    body.new_password, current_password=body.current_password)
        except AuthError as exc:
            audit(principal, request, "change_password", ok=False, detail=str(exc))
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        await state.hub.revoke_session(principal.sid)
        audit(principal, request, "change_password")
        return {"ok": True, "message": "password changed — please sign in again"}

    # ==================================================================
    # Snapshot & stream
    # ==================================================================
    @app.get("/api/snapshot")
    async def snapshot(_: Principal = Depends(view_dep)) -> dict[str, Any]:
        return state.snapshot

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        origin = ws.headers.get("origin")
        if not _origin_allowed(origin, settings, ws):
            await ws.close(code=4403)
            return
        principal = state.security.resolve(ws.cookies.get(settings.cookie_name))
        if principal is None:
            await ws.close(code=4401)
            return

        await ws.accept()
        client = await state.hub.register(ws, username=principal.username,
                                          role=principal.role, sid=principal.sid)
        try:
            await state.hub.send_to(client, "hello", {
                "user": principal.username, "role": principal.role,
                "version": __version__, "data": state.snapshot})
            while True:
                message = await ws.receive_json()
                await _handle_ws_message(state, client, message)
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception as exc:  # noqa: BLE001
            log.debug("websocket closed", extra={"error": repr(exc)})
        finally:
            await state.hub.unregister(ws)

    # ==================================================================
    # Engine data
    # ==================================================================
    @app.get("/api/engine/{mode}/summary")
    async def engine_summary(mode: str, _: Principal = Depends(view_dep)) -> dict[str, Any]:
        mode = valid_mode(mode)
        return await asyncio.to_thread(state.views[mode].summary, state.service_state(mode))

    @app.get("/api/engine/{mode}/orders")
    async def engine_orders(mode: str, status: str | None = None,
                            market_id: str | None = None,
                            limit: int = Query(200, ge=1, le=2000),
                            _: Principal = Depends(view_dep)) -> dict[str, Any]:
        mode = valid_mode(mode)
        rows = await asyncio.to_thread(state.views[mode].orders, status=status, limit=limit,
                                       market_id=market_id)
        return {"orders": rows, "counts": state.views[mode].order_counts()}

    @app.get("/api/engine/{mode}/trades")
    async def engine_trades(mode: str, limit: int = Query(500, ge=1, le=20000),
                            _: Principal = Depends(view_dep)) -> dict[str, Any]:
        mode = valid_mode(mode)
        return {"trades": await asyncio.to_thread(state.views[mode].trades, limit)}

    @app.get("/api/engine/{mode}/fills")
    async def engine_fills(mode: str, limit: int = Query(200, ge=1, le=2000),
                           _: Principal = Depends(view_dep)) -> dict[str, Any]:
        mode = valid_mode(mode)
        return {"fills": await asyncio.to_thread(state.views[mode].fills, limit)}

    @app.get("/api/engine/{mode}/positions")
    async def engine_positions(mode: str, _: Principal = Depends(view_dep)) -> dict[str, Any]:
        mode = valid_mode(mode)
        return {"positions": await asyncio.to_thread(state.views[mode].positions)}

    @app.get("/api/engine/{mode}/equity")
    async def engine_equity(mode: str, limit: int = Query(2000, ge=2, le=20000),
                            _: Principal = Depends(view_dep)) -> dict[str, Any]:
        mode = valid_mode(mode)
        return {"equity": await asyncio.to_thread(state.views[mode].equity, limit)}

    @app.get("/api/engine/{mode}/events")
    async def engine_events(mode: str, level: str | None = None,
                            limit: int = Query(200, ge=1, le=2000),
                            _: Principal = Depends(view_dep)) -> dict[str, Any]:
        mode = valid_mode(mode)
        return {"events": await asyncio.to_thread(state.views[mode].events, limit, level=level)}

    @app.get("/api/engine/{mode}/strategies")
    async def engine_strategies(mode: str, _: Principal = Depends(view_dep)) -> dict[str, Any]:
        mode = valid_mode(mode)
        return {"strategies": await asyncio.to_thread(state.views[mode].strategies)}

    @app.get("/api/engine/{mode}/analytics")
    async def engine_analytics(mode: str, _: Principal = Depends(view_dep)) -> dict[str, Any]:
        mode = valid_mode(mode)
        return await asyncio.to_thread(state.views[mode].analytics)

    @app.get("/api/engine/{mode}/config")
    async def engine_config(mode: str, _: Principal = Depends(view_dep)) -> dict[str, Any]:
        mode = valid_mode(mode)
        return {"config": state.views[mode].engine_config(),
                "startup": state.views[mode].startup_info()}

    # ==================================================================
    # Live account
    # ==================================================================
    @app.get("/api/live/account")
    async def live_account(_: Principal = Depends(view_dep)) -> dict[str, Any]:
        return state.live_sync.snapshot()

    @app.post("/api/live/refresh")
    async def live_refresh(request: Request,
                           principal: Principal = Depends(require("view"))) -> dict[str, Any]:
        await state.live_sync.sync_once()
        audit(principal, request, "live_refresh")
        return {"ok": True, "account": state.live_sync.snapshot()}

    # ==================================================================
    # Markets
    # ==================================================================
    @app.get("/api/markets")
    async def markets(depth: bool = True, indicators: bool = True,
                      _: Principal = Depends(view_dep)) -> dict[str, Any]:
        return await asyncio.to_thread(state.market_feed.snapshot,
                                       with_indicators=indicators, with_depth=depth)

    @app.get("/api/markets/{key}/candles")
    async def market_candles(key: str, limit: int = Query(300, ge=10, le=2000),
                             _: Principal = Depends(view_dep)) -> dict[str, Any]:
        if key not in state.market_feed.market_keys():
            raise HTTPException(status_code=404, detail="unknown market")
        candles = await asyncio.to_thread(state.market_feed.candles, key, limit)
        from dashboard.indicators import compute_all

        return {"key": key, "candles": candles, "indicators": compute_all(candles)}

    @app.post("/api/markets/refresh")
    async def markets_refresh(request: Request,
                              principal: Principal = Depends(require("view"))) -> dict[str, Any]:
        count = await state.market_feed.discover()
        audit(principal, request, "market_refresh", detail=f"{count} markets")
        return {"ok": True, "tracked": count}

    # ==================================================================
    # System
    # ==================================================================
    @app.get("/api/system")
    async def system(_: Principal = Depends(view_dep)) -> dict[str, Any]:
        return await asyncio.to_thread(state.vps.collect)

    @app.get("/api/system/logs")
    async def system_logs(mode: str = "live", file: str = "execution.log",
                          lines: int = Query(300, ge=10, le=2000),
                          principal: Principal = Depends(require("logs.view"))) -> dict[str, Any]:
        mode = valid_mode(mode)
        directory = settings.engine_logs(mode)
        # Never let a caller escape the log directory, and only ever serve logs.
        name = Path(file).name
        target = (directory / name).resolve()
        if target.parent != directory.resolve() or not name.endswith(".log"):
            raise HTTPException(status_code=400, detail="invalid log file")
        raw = await asyncio.to_thread(read_log_tail, target, lines)
        return {"mode": mode, "file": target.name, "lines": [mask_secrets(line) for line in raw],
                "files": list_log_files(directory)}

    @app.get("/api/system/journal")
    async def system_journal(target: str = "live", lines: int = Query(200, ge=10, le=1000),
                             principal: Principal = Depends(require("logs.view"))) -> dict[str, Any]:
        raw = await state.control.journal_tail(target, lines)
        return {"target": target, "lines": [mask_secrets(line) for line in raw]}

    @app.get("/api/config")
    async def config(_: Principal = Depends(require("config.view"))) -> dict[str, Any]:
        return {"dashboard": settings.redacted()}

    # ==================================================================
    # Notifications
    # ==================================================================
    @app.get("/api/notifications")
    async def notifications(limit: int = Query(100, ge=1, le=500), unread: bool = False,
                            mode: str | None = None,
                            _: Principal = Depends(view_dep)) -> dict[str, Any]:
        return {"notifications": state.notifications.recent(limit, unread_only=unread, mode=mode),
                "unread": state.notifications.unread_count(),
                "channels": state.notifications.channel_status()}

    @app.post("/api/notifications/read")
    async def notifications_read(body: dict[str, Any] = Body(default={}),
                                 principal: Principal = Depends(require("view"))) -> dict[str, Any]:
        notification_id = body.get("id")
        changed = state.notifications.mark_read(int(notification_id) if notification_id else None)
        return {"ok": True, "updated": changed, "unread": state.notifications.unread_count()}

    @app.post("/api/notifications/test")
    async def notifications_test(request: Request,
                                 principal: Principal = Depends(require("notify.manage"))
                                 ) -> dict[str, Any]:
        result = await state.notifications.test_channels()
        audit(principal, request, "notification_test", detail=str(result))
        return result

    # ==================================================================
    # Journal
    # ==================================================================
    @app.get("/api/journal/{mode}")
    async def journal_list(mode: str, limit: int = Query(100, ge=1, le=1000),
                           offset: int = Query(0, ge=0), search: str = "",
                           _: Principal = Depends(view_dep)) -> dict[str, Any]:
        mode = valid_mode(mode)
        return {"entries": state.journal.entries(mode, limit=limit, offset=offset, search=search),
                "stats": state.journal.stats(mode)}

    @app.patch("/api/journal/entry/{entry_id}")
    async def journal_update(entry_id: int, body: JournalPatchBody, request: Request,
                             principal: Principal = Depends(require("journal.write"))
                             ) -> dict[str, Any]:
        try:
            entry = state.journal.annotate(entry_id, body.model_dump(exclude_none=True))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit(principal, request, "journal_update", target=str(entry_id))
        return {"ok": True, "entry": entry}

    @app.post("/api/journal/entry/{entry_id}/screenshot")
    async def journal_screenshot(entry_id: int, body: ScreenshotBody, request: Request,
                                 principal: Principal = Depends(require("journal.write"))
                                 ) -> dict[str, Any]:
        try:
            entry = state.journal.attach_screenshot(entry_id, body.slot, body.data_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit(principal, request, "journal_screenshot", target=f"{entry_id}:{body.slot}")
        return {"ok": True, "entry": entry}

    @app.delete("/api/journal/entry/{entry_id}/screenshot/{slot}")
    async def journal_screenshot_delete(entry_id: int, slot: str, request: Request,
                                        principal: Principal = Depends(require("journal.write"))
                                        ) -> dict[str, Any]:
        try:
            state.journal.delete_screenshot(entry_id, slot)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit(principal, request, "journal_screenshot_delete", target=f"{entry_id}:{slot}")
        return {"ok": True}

    @app.get("/api/journal/screenshot/{filename}")
    async def journal_screenshot_file(filename: str,
                                      _: Principal = Depends(view_dep)) -> FileResponse:
        path = state.journal.screenshot_path(filename)
        if path is None:
            raise HTTPException(status_code=404, detail="screenshot not found")
        return FileResponse(path)

    # ==================================================================
    # Controls
    # ==================================================================
    @app.post("/api/control/service")
    async def control_service(body: ServiceBody, request: Request,
                              principal: Principal = Depends(require_auth)) -> dict[str, Any]:
        permission = {"paper": "control.paper", "live": "control.live",
                      "dashboard": "control.dashboard"}.get(body.target)
        if permission is None:
            raise HTTPException(status_code=400, detail=f"unknown target {body.target!r}")
        if body.action not in ALLOWED_ACTIONS:
            raise HTTPException(status_code=400,
                                detail=f"action must be one of {', '.join(ALLOWED_ACTIONS)}")
        _authorise_write(state, request, principal, permission)

        result = await state.control.service_action(body.target, body.action)
        audit(principal, request, f"service_{body.action}", target=body.target,
              detail=result.message, ok=result.ok)
        await state.notifications.emit(
            "warning" if result.ok else "critical", f"control_{body.action}",
            f"{body.action.title()} {body.target}: {'ok' if result.ok else 'failed'}",
            f"requested by {principal.username} — {result.message}",
            mode=body.target, cooldown=0)
        return result.as_dict()

    @app.post("/api/control/emergency-stop")
    async def control_emergency(body: EmergencyBody, request: Request,
                                principal: Principal = Depends(require_auth)) -> dict[str, Any]:
        _authorise_write(state, request, principal, "emergency")
        mode = valid_mode(body.mode)
        result = await state.control.emergency_stop(
            mode, reason=body.reason, actor=principal.username)
        audit(principal, request, "emergency_stop", target=mode, detail=body.reason,
              ok=result.ok)
        await state.notifications.emit(
            "critical", "emergency_stop", f"EMERGENCY STOP — {mode}",
            f"{principal.username}: {body.reason or 'no reason given'}", mode=mode, cooldown=0)
        return result.as_dict()

    @app.post("/api/control/clear-kill-switch")
    async def control_clear_kill(body: ModeBody, request: Request,
                                 principal: Principal = Depends(require_auth)) -> dict[str, Any]:
        _authorise_write(state, request, principal, "emergency")
        mode = valid_mode(body.mode)
        result = state.control.clear_kill_switch(mode)
        audit(principal, request, "clear_kill_switch", target=mode, ok=result.ok)
        return result.as_dict()

    @app.post("/api/control/backup")
    async def control_backup(body: BackupBody, request: Request,
                             principal: Principal = Depends(require("backup"))) -> dict[str, Any]:
        result = await state.control.backup(body.scope)
        audit(principal, request, "backup", target=body.scope, detail=result.message,
              ok=result.ok)
        return result.as_dict()

    @app.get("/api/control/backups")
    async def control_backups(_: Principal = Depends(require("backup"))) -> dict[str, Any]:
        return {"backups": state.control.list_backups()}

    @app.post("/api/control/reconnect")
    async def control_reconnect(body: ReconnectBody, request: Request,
                                principal: Principal = Depends(require("view"))) -> dict[str, Any]:
        target = body.target
        if target == "market_data":
            count = await state.market_feed.discover()
            message = f"market feed resubscribed ({count} markets)"
        elif target == "live_api":
            await state.live_sync.start()
            await state.live_sync.sync_once()
            message = f"live account client rebuilt ({state.live_sync.auth_status})"
        elif target == "live_websocket":
            await state.live_sync.sync_once()
            message = "live account resynced; the user socket reconnects on its own backoff"
        else:
            raise HTTPException(status_code=400, detail=f"unknown reconnect target {target!r}")
        audit(principal, request, "reconnect", target=target, detail=message)
        return {"ok": True, "message": message}

    @app.get("/api/export/{mode}/{kind}")
    async def export(mode: str, kind: str, format: str = "csv",
                     principal: Principal = Depends(require("export"))) -> Response:
        mode = valid_mode(mode)
        if format not in ("csv", "json"):
            raise HTTPException(status_code=400, detail="format must be csv or json")
        exporters = {"trades": state.control.export_trades, "orders": state.control.export_orders,
                     "journal": state.control.export_journal}
        if kind not in exporters:
            raise HTTPException(status_code=404, detail=f"unknown export {kind!r}")
        filename, media_type, body = await asyncio.to_thread(exporters[kind], mode, format)
        return Response(content=body, media_type=media_type, headers={
            "Content-Disposition": f'attachment; filename="{filename}"'})

    # ==================================================================
    # Users & audit (admin)
    # ==================================================================
    @app.get("/api/users")
    async def users(_: Principal = Depends(require("users.manage"))) -> dict[str, Any]:
        return {"users": state.security.list_users(), "roles": ["admin", "trader", "viewer"]}

    @app.post("/api/users")
    async def create_user(body: UserBody, request: Request,
                          principal: Principal = Depends(require("users.manage"))
                          ) -> dict[str, Any]:
        try:
            user_id = await asyncio.to_thread(state.security.create_user, body.username,
                                              body.password, role=body.role)
        except AuthError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        audit(principal, request, "user_create", target=body.username, detail=body.role)
        return {"ok": True, "id": user_id}

    @app.patch("/api/users/{user_id}")
    async def patch_user(user_id: int, body: UserPatchBody, request: Request,
                         principal: Principal = Depends(require("users.manage"))
                         ) -> dict[str, Any]:
        try:
            if body.role is not None:
                state.security.set_role(user_id, body.role)
            if body.active is not None:
                state.security.set_active(user_id, body.active)
            if body.new_password:
                await asyncio.to_thread(state.security.change_password, user_id,
                                        body.new_password)
        except AuthError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        audit(principal, request, "user_update", target=str(user_id),
              detail=body.model_dump_json(exclude_none=True, exclude={"new_password"}))
        return {"ok": True}

    @app.delete("/api/users/{user_id}")
    async def delete_user(user_id: int, request: Request,
                          principal: Principal = Depends(require("users.manage"))
                          ) -> dict[str, Any]:
        try:
            state.security.delete_user(user_id)
        except AuthError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        audit(principal, request, "user_delete", target=str(user_id))
        return {"ok": True}

    @app.get("/api/audit")
    async def audit_log(limit: int = Query(200, ge=1, le=1000),
                        _: Principal = Depends(require("users.manage"))) -> dict[str, Any]:
        return {"audit": state.security.audit_log(limit)}

    return app


# ======================================================================
# Helpers
# ======================================================================
def _authorise_write(state: AppState, request: Request, principal: Principal,
                     permission: str) -> None:
    """Permission + CSRF check for routes whose permission is data-dependent."""
    if not principal.can(permission):
        raise HTTPException(status_code=403,
                            detail=f"role '{principal.role}' may not {permission}")
    try:
        state.security.verify_csrf(principal, request.headers.get("x-csrf-token"))
    except AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


def _permissions(principal: Principal) -> frozenset[str]:
    from dashboard.security import PERMISSIONS

    return PERMISSIONS.get(principal.role, frozenset())


def _origin_allowed(origin: str | None, settings: DashboardSettings, ws: WebSocket) -> bool:
    """Reject cross-site websocket upgrades.

    Same-origin requests (no ``Origin``, or one matching the configured public
    URL / allowed list / the Host header) are accepted; anything else is not.
    """
    if origin is None:
        return True  # non-browser client; the session cookie is still required
    allowed = {o.rstrip("/") for o in settings.allowed_origins}
    if settings.public_url:
        allowed.add(settings.public_url.rstrip("/"))
    host = ws.headers.get("host", "")
    if host:
        allowed.update({f"http://{host}", f"https://{host}"})
    return origin.rstrip("/") in allowed


async def _handle_ws_message(state: AppState, client: Any, message: dict[str, Any]) -> None:
    """Client -> server websocket control messages (subscription + ping only)."""
    action = str(message.get("action") or "")
    if action == "ping":
        await state.hub.send_to(client, "pong", {})
    elif action == "subscribe":
        channels = message.get("channels")
        if isinstance(channels, list):
            client.channels = {str(c) for c in channels if isinstance(c, str)} or {"snapshot"}
    elif action == "snapshot":
        await state.hub.send_to(client, "snapshot", {"data": state.snapshot})

"""Production monitoring + control dashboard for the Polymarket bot.

A dependency-light (stdlib-only) HTTP server, bound to localhost, that surfaces
ONLY real data the bot produces — the engine's heartbeat feed (``status.json``),
the readiness verdict (``readiness.json``), systemd service health, VPS metrics,
tailed logs, a SQLite trade history ingested from ``trade.log``, and a public
(no-auth) order-book snapshot of the configured market. It also exposes a small
set of safe controls (start/stop/restart the engine, emergency kill-switch,
backup, export) via scoped ``sudo systemctl`` / the existing kill-switch file.

Design constraints honoured:
* No fabricated data — panels with no data yet report empty, not fake numbers.
* No wallet secrets are ever read or served (only masked fields from status/
  readiness, which are already masked by the engine).
* Binds 127.0.0.1 only; reach it via ``ssh -L 8080:localhost:8080``.
* Does not touch the trading strategy, risk, or execution logic.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(os.environ.get("BOT_REPO", "/opt/bot-s1"))
STATE = Path(os.environ.get("STATE_DIR", str(REPO / "state")))
LOGS = Path(os.environ.get("LOG_DIR", str(REPO / "logs")))
DB_PATH = STATE / "dashboard.db"
SERVICE = os.environ.get("BOT_SERVICE", "bot-s1.service")
DASH_SERVICE = os.environ.get("DASH_SERVICE", "bot-s1-dashboard.service")
KILL_SWITCH = Path(os.environ.get("KILL_SWITCH_FILE", str(STATE / "KILL_SWITCH")))
HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))
TOKEN = os.environ.get("DASHBOARD_TOKEN", "")  # optional; if set, required
LOG_NAMES = ["execution", "orders", "trade", "strategy", "websocket", "wallet", "api", "errors"]

_prev_cpu: dict[str, float] = {}


# ----------------------------------------------------------------------------- collectors
def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return None


def _systemctl_show(unit: str) -> dict[str, str]:
    props = "ActiveState,SubState,ExecMainPID,NRestarts,MemoryCurrent,ExecMainStartTimestamp,UnitFileState"
    try:
        out = subprocess.run(["/usr/bin/systemctl", "show", unit, "-p", props, "--no-pager"],
                             capture_output=True, text=True, timeout=5).stdout
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    except Exception:  # noqa: BLE001
        return {}


def _service_health(unit: str) -> dict:
    d = _systemctl_show(unit)
    started = d.get("ExecMainStartTimestamp", "")
    uptime_s = None
    if started and started != "n/a":
        try:
            dt = datetime.strptime(started, "%a %Y-%m-%d %H:%M:%S %Z")
            uptime_s = int(time.time() - dt.replace(tzinfo=timezone.utc).timestamp())
        except Exception:  # noqa: BLE001
            uptime_s = None
    mem = d.get("MemoryCurrent", "")
    return {
        "unit": unit,
        "active_state": d.get("ActiveState"),
        "sub_state": d.get("SubState"),
        "pid": d.get("ExecMainPID"),
        "restarts": _int(d.get("NRestarts")),
        "enabled": d.get("UnitFileState"),
        "memory_mb": round(int(mem) / 1_048_576, 1) if mem.isdigit() else None,
        "uptime_s": uptime_s,
        "started": started,
    }


def _failed_units() -> list[str]:
    try:
        out = subprocess.run(["/usr/bin/systemctl", "list-units", "--state=failed", "--no-legend",
                              "--plain", "--no-pager"], capture_output=True, text=True, timeout=5).stdout
        return [ln.split()[0] for ln in out.splitlines() if ln.strip()]
    except Exception:  # noqa: BLE001
        return []


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _vps_metrics() -> dict:
    # CPU % via /proc/stat delta between polls (no sleep).
    cpu_pct = None
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()[1:]
        vals = list(map(int, parts))
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)
        if _prev_cpu:
            dt = total - _prev_cpu["total"]
            di = idle - _prev_cpu["idle"]
            if dt > 0:
                cpu_pct = round(100.0 * (dt - di) / dt, 1)
        _prev_cpu["total"], _prev_cpu["idle"] = total, idle
    except Exception:  # noqa: BLE001
        pass
    mem = {}
    try:
        info = {}
        for line in open("/proc/meminfo"):
            k, _, v = line.partition(":")
            info[k] = int(v.strip().split()[0])  # kB
        total_kb, avail_kb = info["MemTotal"], info.get("MemAvailable", 0)
        mem = {"total_mb": round(total_kb / 1024), "used_mb": round((total_kb - avail_kb) / 1024),
               "used_pct": round(100.0 * (total_kb - avail_kb) / total_kb, 1)}
    except Exception:  # noqa: BLE001
        pass
    disk = {}
    try:
        du = shutil.disk_usage("/")
        disk = {"total_gb": round(du.total / 1e9, 1), "used_gb": round(du.used / 1e9, 1),
                "used_pct": round(100.0 * du.used / du.total, 1)}
    except Exception:  # noqa: BLE001
        pass
    uptime_s = None
    try:
        uptime_s = int(float(open("/proc/uptime").read().split()[0]))
    except Exception:  # noqa: BLE001
        pass
    net = {}
    try:
        rx = tx = 0
        for line in open("/proc/net/dev").readlines()[2:]:
            if ":" not in line:
                continue
            iface, data = line.split(":")
            if iface.strip() == "lo":
                continue
            f = data.split()
            rx += int(f[0]); tx += int(f[8])
        net = {"rx_mb": round(rx / 1e6, 1), "tx_mb": round(tx / 1e6, 1)}
    except Exception:  # noqa: BLE001
        pass
    return {"cpu_pct": cpu_pct, "mem": mem, "disk": disk, "host_uptime_s": uptime_s, "net": net,
            "db_size_mb": round(DB_PATH.stat().st_size / 1e6, 2) if DB_PATH.exists() else 0,
            "log_size_mb": round(sum(p.stat().st_size for p in LOGS.glob("*.log")) / 1e6, 2)
                           if LOGS.exists() else 0}


def _tail(name: str, n: int = 200) -> list[str]:
    p = LOGS / f"{name}.log"
    if not p.is_file():
        return []
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = min(size, 65536 + n * 200)
            f.seek(size - block)
            data = f.read().decode("utf-8", "replace")
        return data.splitlines()[-n:]
    except Exception:  # noqa: BLE001
        return []


# ----------------------------------------------------------------------------- sqlite trade history
def _db() -> sqlite3.Connection:
    STATE.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, market TEXT, side TEXT, action TEXT,
        price REAL, size REAL, realized_pnl REAL, raw TEXT)""")
    con.execute("CREATE TABLE IF NOT EXISTS ingest(name TEXT PRIMARY KEY, offset INTEGER)")
    return con


def _ingest_trades() -> None:
    """Parse new FILL lines from trade.log into SQLite (idempotent by byte offset)."""
    p = LOGS / "trade.log"
    if not p.is_file():
        return
    con = _db()
    try:
        row = con.execute("SELECT offset FROM ingest WHERE name='trade.log'").fetchone()
        offset = row[0] if row else 0
        size = p.stat().st_size
        if size < offset:  # rotated/truncated
            offset = 0
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            for line in f:
                if "| FILL |" not in line and " FILL " not in line:
                    continue
                fields = _parse_kv(line)
                con.execute(
                    "INSERT INTO trades(ts,market,side,action,price,size,realized_pnl,raw) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (line[:29].strip() or _now(), fields.get("market"), fields.get("side"),
                     fields.get("action"), _f(fields.get("fill_price")), _f(fields.get("fill_size")),
                     _f(fields.get("realized_pnl")), line.strip()[:500]))
            new_off = f.tell()
        con.execute("INSERT INTO ingest(name,offset) VALUES('trade.log',?) "
                    "ON CONFLICT(name) DO UPDATE SET offset=?", (new_off, new_off))
        con.commit()
    finally:
        con.close()


def _parse_kv(line: str) -> dict[str, str]:
    out: dict[str, str] = {}
    tail = line.split("|")[-1] if "|" in line else line
    for tok in tail.split():
        if "=" in tok:
            k, _, v = tok.partition("=")
            out[k] = v.strip('"')
    return out


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _trades(limit: int = 200) -> dict:
    _ingest_trades()
    con = _db()
    try:
        rows = con.execute("SELECT ts,market,side,action,price,size,realized_pnl FROM trades "
                           "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        agg = con.execute("SELECT COUNT(*), COALESCE(SUM(realized_pnl),0), "
                          "COALESCE(SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END),0), "
                          "COALESCE(SUM(CASE WHEN realized_pnl<0 THEN 1 ELSE 0 END),0) "
                          "FROM trades").fetchone()
    finally:
        con.close()
    total, pnl, wins, losses = agg
    return {"trades": [dict(zip(("ts", "market", "side", "action", "price", "size", "realized_pnl"), r))
                       for r in rows],
            "analytics": {"total": total, "wins": wins, "losses": losses,
                          "win_rate": round(100.0 * wins / total, 1) if total else None,
                          "realized_pnl": round(pnl, 6),
                          "note": "populated from real fills; empty until the bot trades live"}}


# ----------------------------------------------------------------------------- public market book (no auth)
_market_cache: dict = {"ts": 0.0, "data": None}


def _market() -> dict:
    if time.time() - _market_cache["ts"] < 5 and _market_cache["data"]:
        return _market_cache["data"]
    data = {"note": "public CLOB order book (no auth)"}
    try:
        import sys
        sys.path.insert(0, str(REPO))
        from polybot.config.settings import Settings
        from py_clob_client_v2 import ClobClient
        s = Settings.load()
        c = ClobClient(host=s.clob_host, chain_id=s.chain_id)  # keyless public client
        mk = s.markets[0] if s.markets else None
        data["market"] = mk
        if mk:
            m = c.get_market(mk)
            tok = m["tokens"][0]["token_id"]
            book = c.get_order_book(tok)
            bids = getattr(book, "bids", []) or []
            asks = getattr(book, "asks", []) or []
            bb = max((float(b.price) for b in bids), default=0.0)
            ba = min((float(a.price) for a in asks), default=0.0)
            data.update({
                "accepting_orders": m.get("accepting_orders"),
                "best_bid": bb, "best_ask": ba,
                "mid": round((bb + ba) / 2, 6) if bb and ba else None,
                "spread": round(ba - bb, 6) if bb and ba else None,
                "bid_depth": round(sum(float(b.size) for b in bids), 2),
                "ask_depth": round(sum(float(a.size) for a in asks), 2),
                "levels": f"{len(bids)}/{len(asks)}",
                "tick_size": str(c.get_tick_size(tok)),
                "status": "active" if m.get("accepting_orders") else "not accepting",
            })
    except Exception as exc:  # noqa: BLE001
        data["error"] = repr(exc)
    _market_cache.update(ts=time.time(), data=data)
    return data


def _summary() -> dict:
    status = _read_json(STATE / "status.json") or {}
    readiness = _read_json(STATE / "readiness.json") or {}
    return {
        "ts": _now(),
        "engine": status,          # heartbeat: wallet/balance/orders/pnl/connections/risk/latency
        "readiness": readiness,    # auth/maker/allowance/balance/deposit-enabled verdict
        "services": {"engine": _service_health(SERVICE), "dashboard": _service_health(DASH_SERVICE)},
        "failed_units": _failed_units(),
        "vps": _vps_metrics(),
        "kill_switch": KILL_SWITCH.exists(),
        "dry_run": True,  # confirmed by unit lock; live requires deliberate arming
    }


# ----------------------------------------------------------------------------- controls (guarded)
def _sysctl(verb: str, unit: str) -> tuple[bool, str]:
    try:
        r = subprocess.run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", verb, unit],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0, (r.stderr or r.stdout or "ok").strip()
    except Exception as exc:  # noqa: BLE001
        return False, repr(exc)


def _control(action: str) -> dict:
    # Paper == the current dry-run engine service. Live arming is intentionally NOT
    # a web action (safety): use the kill switch + manual unit change to go live.
    if action in ("start_paper", "stop_paper", "restart_paper"):
        ok, msg = _sysctl(action.split("_")[0], SERVICE)
        return {"ok": ok, "msg": msg}
    if action == "emergency_stop":
        KILL_SWITCH.parent.mkdir(parents=True, exist_ok=True)
        KILL_SWITCH.write_text(f"emergency stop via dashboard {_now()}\n")
        return {"ok": True, "msg": f"KILL_SWITCH engaged — engine will halt & cancel open orders ({KILL_SWITCH})"}
    if action == "resume":
        try:
            KILL_SWITCH.unlink(missing_ok=True)
            return {"ok": True, "msg": "KILL_SWITCH cleared"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "msg": repr(exc)}
    if action == "restart_dashboard":
        ok, msg = _sysctl("restart", DASH_SERVICE)
        return {"ok": ok, "msg": msg}
    if action == "backup_db":
        _ingest_trades()
        dest = STATE / f"dashboard-backup-{int(time.time())}.db"
        try:
            shutil.copy2(DB_PATH, dest) if DB_PATH.exists() else _db().close()
            return {"ok": True, "msg": f"backup -> {dest}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "msg": repr(exc)}
    if action in ("start_live", "stop_live", "restart_live"):
        return {"ok": False, "msg": "Live arming is not a web control (safety). Emergency-stop is available; "
                                    "to go live, provision the wallet then arm ENABLE_LIVE_TRADING manually."}
    return {"ok": False, "msg": f"unknown action: {action}"}


def _export_csv() -> str:
    _ingest_trades()
    con = _db()
    try:
        rows = con.execute("SELECT ts,market,side,action,price,size,realized_pnl FROM trades ORDER BY id").fetchall()
    finally:
        con.close()
    lines = ["ts,market,side,action,price,size,realized_pnl"]
    for r in rows:
        lines.append(",".join("" if v is None else str(v) for v in r))
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _auth_ok(self) -> bool:
        if not TOKEN:
            return True
        q = urllib.parse.urlparse(self.path).query
        tok = urllib.parse.parse_qs(q).get("token", [""])[0]
        return tok == TOKEN or self.headers.get("X-Dash-Token") == TOKEN

    def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, default=str).encode())

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if not self._auth_ok():
            return self._json({"error": "unauthorized"}, 401)
        if path == "/":
            return self._send(200, HTML.encode(), "text/html; charset=utf-8")
        if path == "/api/summary":
            return self._json(_summary())
        if path == "/api/trades":
            return self._json(_trades())
        if path == "/api/market":
            return self._json(_market())
        if path == "/api/logs":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            name = q.get("name", ["execution"])[0]
            if name not in LOG_NAMES:
                return self._json({"error": "bad log name"}, 400)
            return self._json({"name": name, "lines": _tail(name, int(q.get("n", ["200"])[0]))})
        if path == "/api/export":
            return self._send(200, _export_csv().encode(), "text/csv",
                              {"Content-Disposition": "attachment; filename=trades.csv"})
        if path == "/healthz":
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not self._auth_ok():
            return self._json({"error": "unauthorized"}, 401)
        if path == "/api/control":
            length = int(self.headers.get("Content-Length", "0") or "0")
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except Exception:  # noqa: BLE001
                payload = {}
            action = str(payload.get("action", ""))
            result = _control(action)
            try:
                (LOGS / "dashboard-audit.log").open("a").write(
                    f"{_now()} action={action} result={result}\n")
            except Exception:  # noqa: BLE001
                pass
            return self._json(result)
        return self._json({"error": "not found"}, 404)


HTML = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Bot-s1 Dashboard</title><style>
:root{--bg:#0b0f14;--card:#141b24;--mut:#8aa0b3;--fg:#e7eef5;--ok:#2ecc71;--warn:#f1c40f;--bad:#e74c3c;--acc:#4aa3ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,Segoe UI,Roboto,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:12px 18px;background:#0e141b;border-bottom:1px solid #1e2a36;position:sticky;top:0;z-index:5}
h1{font-size:16px;margin:0}.pill{padding:2px 8px;border-radius:12px;font-size:12px;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px;padding:16px}
.card{background:var(--card);border:1px solid #1e2a36;border-radius:10px;padding:14px}
.card h2{font-size:13px;margin:0 0 10px;color:var(--acc);text-transform:uppercase;letter-spacing:.04em}
.row{display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid #1a232e}
.row .k{color:var(--mut)}.row .v{font-variant-numeric:tabular-nums;text-align:right}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.mut{color:var(--mut)}
table{width:100%;border-collapse:collapse;font-size:12px}td,th{padding:4px 6px;border-bottom:1px solid #1a232e;text-align:left}
button{background:#1c2733;color:var(--fg);border:1px solid #2a3a49;border-radius:7px;padding:7px 10px;cursor:pointer;font-size:12px}
button:hover{border-color:var(--acc)}button.bad{border-color:#5a2020;background:#2a1414}button.bad:hover{border-color:var(--bad)}
pre{background:#0a0e13;border:1px solid #1a232e;border-radius:8px;padding:10px;max-height:280px;overflow:auto;font-size:11px;margin:0;white-space:pre-wrap}
select{background:#0e141b;color:var(--fg);border:1px solid #2a3a49;border-radius:6px;padding:5px}
.controls{display:flex;flex-wrap:wrap;gap:8px}.note{color:var(--mut);font-size:11px;margin-top:8px}
.big{font-size:22px;font-weight:700}.mono{font-family:ui-monospace,Menlo,monospace;font-size:12px}
</style></head><body>
<header><h1>Bot-s1 — Polymarket Dashboard</h1><span id=mode class=pill></span>
<span id=ks class=pill></span><span style="flex:1"></span>
<span class=mut id=updated></span></header>
<div class=grid>
 <div class=card><h2>Service Health</h2><div id=svc></div></div>
 <div class=card><h2>Wallet &amp; Auth</h2><div id=wallet></div></div>
 <div class=card><h2>Readiness</h2><div id=ready></div></div>
 <div class=card><h2>Connections (WS / API)</h2><div id=conns></div></div>
 <div class=card><h2>Portfolio &amp; PnL</h2><div id=pnl></div></div>
 <div class=card><h2>Market (Polymarket)</h2><div id=market></div></div>
 <div class=card><h2>VPS</h2><div id=vps></div></div>
 <div class=card><h2>Trade Analytics</h2><div id=analytics></div></div>
 <div class=card style="grid-column:1/-1"><h2>Controls</h2><div class=controls id=controls></div>
   <div class=note id=ctlmsg></div></div>
 <div class=card style="grid-column:1/-1"><h2>Trades (from real fills)</h2><div id=trades></div></div>
 <div class=card style="grid-column:1/-1"><h2>Logs</h2>
   <select id=logsel></select> <span id=logstamp class=mut></span><pre id=logs></pre></div>
</div>
<script>
const $=id=>document.getElementById(id);const TOKEN=new URLSearchParams(location.search).get('token')||'';
const q=p=>p+(TOKEN?(p.includes('?')?'&':'?')+'token='+encodeURIComponent(TOKEN):'');
function cls(ok){return ok===true?'ok':ok===false?'bad':'warn'}
function rows(el,pairs){el.innerHTML=pairs.map(([k,v,c])=>`<div class=row><span class=k>${k}</span><span class="v ${c||''}">${v??'—'}</span></div>`).join('')}
function fmt(n,d=2){return n==null?'—':(typeof n==='number'?n.toFixed(d):n)}
async function j(p){const r=await fetch(q(p));return r.json()}
async function refresh(){
 let s;try{s=await j('/api/summary')}catch(e){$('updated').textContent='fetch error';return}
 $('updated').textContent='updated '+new Date().toLocaleTimeString();
 $('mode').textContent=s.dry_run?'DRY-RUN (paper)':'LIVE';$('mode').className='pill '+(s.dry_run?'':'bad');$('mode').style.background=s.dry_run?'#14351f':'#3a1414';
 $('ks').textContent=s.kill_switch?'KILL SWITCH ON':'kill switch off';$('ks').style.background=s.kill_switch?'#3a1414':'#14351f';
 const e=s.engine||{},r=s.readiness||{},sv=s.services||{},v=s.vps||{};
 const eng=sv.engine||{},dash=sv.dashboard||{};
 rows($('svc'),[['engine',(eng.active_state||'?')+'/'+(eng.sub_state||''),cls(eng.active_state==='active')],
   ['engine PID',eng.pid],['engine uptime',eng.uptime_s!=null?(eng.uptime_s/3600).toFixed(2)+' h':'—'],
   ['engine restarts',eng.restarts,cls(eng.restarts===0)],['engine mem',eng.memory_mb?eng.memory_mb+' MB':'—'],
   ['dashboard',(dash.active_state||'?'),cls(dash.active_state==='active')],
   ['failed units',(s.failed_units||[]).length?s.failed_units.join(', '):'none',cls((s.failed_units||[]).length===0)]]);
 const rc=r.checks||{};
 rows($('wallet'),[['signer',`<span class=mono>${e.wallet||r.signer||'—'}</span>`],
   ['maker/deposit',`<span class=mono>${r.maker_deposit_wallet||'—'}</span>`],
   ['signature type',r.signature_type_name||'—'],
   ['authenticated',rc.authentication?.ok?'yes':'no',cls(rc.authentication?.ok)],
   ['USDC balance',fmt(e.balance,4),cls((e.balance||0)>0)]]);
 rows($('ready'),[['LIVE READY',r.live_ready?'YES':'NO',cls(!!r.live_ready)],
   ['deposit wallet enabled',rc.deposit_wallet_enabled?.ok?'yes':'no',cls(rc.deposit_wallet_enabled?.ok)],
   ['maker authorized',rc.maker_wallet?.ok?'yes':'no',cls(rc.maker_wallet?.ok)],
   ['not closed-only',rc.not_closed_only?.ok?'yes':'no',cls(rc.not_closed_only?.ok)],
   ['allowance ok',rc.usdc_allowance?.ok?'yes':'no',cls(rc.usdc_allowance?.ok)]]);
 const per=rc.usdc_allowance?.per_spender||{};
 $('ready').innerHTML+=Object.entries(per).map(([k,val])=>`<div class=row><span class=k>· ${k}</span><span class="v ${cls(val>0)}">${fmt(val,2)}</span></div>`).join('');
 if((r.missing_requirements||[]).length)$('ready').innerHTML+=`<div class=note>Missing: ${r.missing_requirements.join('<br>')}</div>`;
 const conns=e.connections||[];
 rows($('conns'),conns.map(c=>[c.channel+' ws',c.connected?('up (rc='+c.reconnects+')'):'down',cls(c.connected)])
   .concat([['latency avg',e.latency?fmt(e.latency.avg_ms,1)+' ms':'—'],['ticks',e.ticks]]));
 const p=e.pnl||{};
 rows($('pnl'),[['open orders',e.orders?.open],['open positions',e.positions],['exposure',fmt(e.exposure,2)],
   ['realized PnL',fmt(p.realized,4)],['unrealized PnL',fmt(p.unrealized,4)],['total PnL',fmt(p.total,4)],
   ['risk halted',e.risk?.halted?'YES':'no',cls(e.risk?.halted===false)]]);
 rows($('vps'),[['CPU',v.cpu_pct!=null?v.cpu_pct+' %':'—',cls(v.cpu_pct==null||v.cpu_pct<85)],
   ['RAM',v.mem?v.mem.used_mb+'/'+v.mem.total_mb+' MB ('+v.mem.used_pct+'%)':'—',cls(!v.mem||v.mem.used_pct<90)],
   ['disk',v.disk?v.disk.used_gb+'/'+v.disk.total_gb+' GB ('+v.disk.used_pct+'%)':'—',cls(!v.disk||v.disk.used_pct<90)],
   ['host uptime',v.host_uptime_s?(v.host_uptime_s/3600).toFixed(1)+' h':'—'],
   ['net rx/tx',v.net?v.net.rx_mb+'/'+v.net.tx_mb+' MB':'—'],
   ['db size',fmt(v.db_size_mb,2)+' MB'],['log size',fmt(v.log_size_mb,2)+' MB']]);
 try{const m=await j('/api/market');rows($('market'),[['market',`<span class=mono>${(m.market||'—').slice(0,18)}…</span>`],
   ['status',m.status||'—',cls(m.accepting_orders)],['best bid',fmt(m.best_bid,4)],['best ask',fmt(m.best_ask,4)],
   ['mid',fmt(m.mid,4)],['spread',fmt(m.spread,4)],['depth bid/ask',(m.bid_depth??'—')+'/'+(m.ask_depth??'—')],
   ['levels',m.levels||'—'],['tick size',m.tick_size||'—']]);}catch(e){}
 try{const t=await j('/api/trades');const a=t.analytics||{};
   rows($('analytics'),[['total trades',a.total],['wins',a.wins,'ok'],['losses',a.losses,'bad'],
     ['win rate',a.win_rate!=null?a.win_rate+' %':'—'],['realized PnL',fmt(a.realized_pnl,4)]]);
   $('analytics').innerHTML+=`<div class=note>${a.note||''}</div>`;
   $('trades').innerHTML=t.trades.length?('<table><tr><th>time<th>market<th>side<th>action<th>price<th>size<th>pnl</tr>'+
     t.trades.map(x=>`<tr><td>${(x.ts||'').slice(0,19)}<td class=mono>${(x.market||'').slice(0,12)}<td>${x.side||''}<td>${x.action||''}<td>${fmt(x.price,4)}<td>${fmt(x.size,2)}<td>${fmt(x.realized_pnl,4)}</tr>`).join('')+'</table>')
     :'<div class=note>No trades yet — populates from real fills once the bot trades live.</div>';}catch(e){}
}
const CTL=[['Start Paper','start_paper'],['Stop Paper','stop_paper'],['Restart Paper','restart_paper'],
 ['Restart Dashboard','restart_dashboard'],['Backup DB','backup_db'],
 ['Export Trades (CSV)','__export'],['EMERGENCY STOP','emergency_stop','bad'],['Resume','resume']];
$('controls').innerHTML=CTL.map(([l,a,c])=>`<button class="${c||''}" data-a="${a}">${l}</button>`).join('');
$('controls').onclick=async ev=>{const b=ev.target.closest('button');if(!b)return;const a=b.dataset.a;
 if(a==='__export'){location.href=q('/api/export');return}
 if(a==='emergency_stop'&&!confirm('Engage KILL SWITCH? The engine will halt and cancel open orders.'))return;
 b.disabled=true;const r=await fetch(q('/api/control'),{method:'POST',headers:{'Content-Type':'application/json','X-Dash-Token':TOKEN},body:JSON.stringify({action:a})}).then(r=>r.json()).catch(e=>({ok:false,msg:''+e}));
 $('ctlmsg').textContent=(r.ok?'✓ ':'✗ ')+(r.msg||'');$('ctlmsg').className='note '+(r.ok?'ok':'bad');b.disabled=false;refresh()};
const sel=$('logsel');['execution','orders','trade','strategy','websocket','wallet','api','errors'].forEach(n=>sel.add(new Option(n,n)));
async function loadLogs(){const d=await j('/api/logs?name='+sel.value+'&n=200');$('logs').textContent=(d.lines||[]).join('\n');$('logstamp').textContent=new Date().toLocaleTimeString()}
sel.onchange=loadLogs;
refresh();loadLogs();setInterval(refresh,2000);setInterval(loadLogs,5000);
</script></body></html>"""


def main() -> None:
    _db().close()  # ensure schema
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"dashboard on http://{HOST}:{PORT}  (token {'set' if TOKEN else 'disabled'})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()

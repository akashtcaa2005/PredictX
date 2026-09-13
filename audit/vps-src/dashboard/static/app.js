/* Polybot dashboard SPA.
 *
 * One websocket carries a 1 Hz snapshot of everything that changes constantly
 * (engine state, prices, VPS vitals). Panels that need depth — full order books,
 * analytics, journal, logs — fetch on demand over REST so the steady-state
 * stream stays small.
 *
 * Live-updating panels re-render from the snapshot each tick; panels containing
 * form inputs render only when opened or explicitly refreshed, so typing is
 * never interrupted.
 */
(function () {
  'use strict';

  /* ================================================================== *
   * State
   * ================================================================== */
  const S = {
    user: null,
    csrf: null,
    permissions: new Set(),
    mode: localStorage.getItem('polybot.mode') || 'paper',
    tab: localStorage.getItem('polybot.tab') || 'overview',
    snap: null,
    ws: null,
    wsRetry: 0,
    connected: false,
    lastSnapshotAt: 0,
    detail: {},          // per-panel fetched data
    filters: { orders: 'all', logFile: 'execution.log', market: null, journalSearch: '' },
    charts: {},
    browserNotifications: localStorage.getItem('polybot.browserNotify') === '1',
    seenNotificationId: 0,
  };

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  /* ================================================================== *
   * Formatting
   * ================================================================== */
  const F = {
    num(v, d) {
      if (v === null || v === undefined || v === '' || Number.isNaN(Number(v))) return '—';
      const n = Number(v);
      return n.toLocaleString(undefined, { minimumFractionDigits: d ?? 2, maximumFractionDigits: d ?? 2 });
    },
    money(v, d) { return v === null || v === undefined ? '—' : '$' + F.num(v, d ?? 2); },
    price(v) { return v === null || v === undefined || v === 0 ? '—' : Number(v).toFixed(4); },
    pct(v, d) { return v === null || v === undefined ? '—' : F.num(v, d ?? 2) + '%'; },
    signed(v, d) {
      if (v === null || v === undefined) return '—';
      const n = Number(v);
      return (n > 0 ? '+' : '') + F.num(n, d ?? 2);
    },
    int(v) { return v === null || v === undefined ? '—' : Number(v).toLocaleString(); },
    bytes(v) {
      if (!v && v !== 0) return '—';
      const units = ['B', 'KB', 'MB', 'GB', 'TB'];
      let n = Number(v), i = 0;
      while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
      return n.toFixed(i ? 1 : 0) + ' ' + units[i];
    },
    dur(s) {
      if (s === null || s === undefined) return '—';
      s = Math.max(0, Math.floor(Number(s)));
      const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
      const m = Math.floor((s % 3600) / 60), sec = s % 60;
      if (d) return `${d}d ${h}h`;
      if (h) return `${h}h ${m}m`;
      if (m) return `${m}m ${sec}s`;
      return `${sec}s`;
    },
    time(ts) {
      if (!ts) return '—';
      return new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
    },
    datetime(ts) {
      if (!ts) return '—';
      const d = new Date(ts * 1000);
      return d.toLocaleDateString([], { month: 'short', day: '2-digit' }) + ' ' +
             d.toLocaleTimeString([], { hour12: false });
    },
    ago(ts) {
      if (!ts) return '—';
      return F.dur(Date.now() / 1000 - ts) + ' ago';
    },
    addr(a) { return !a ? '—' : (a.length > 14 ? a.slice(0, 8) + '…' + a.slice(-6) : a); },
    cls(v) { return Number(v) > 0 ? 'up' : Number(v) < 0 ? 'down' : 'dim'; },
  };

  function esc(text) {
    return String(text ?? '').replace(/[&<>"']/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  /* ================================================================== *
   * HTTP
   * ================================================================== */
  async function api(path, options) {
    const opts = Object.assign({ headers: {} }, options || {});
    opts.credentials = 'same-origin';
    if (opts.body !== undefined && typeof opts.body !== 'string') {
      opts.body = JSON.stringify(opts.body);
      opts.headers['Content-Type'] = 'application/json';
    }
    if (opts.method && !['GET', 'HEAD'].includes(opts.method) && S.csrf) {
      opts.headers['X-CSRF-Token'] = S.csrf;
    }
    const response = await fetch(path, opts);
    if (response.status === 401) { showLogin(); throw new Error('Not signed in'); }
    const type = response.headers.get('content-type') || '';
    const payload = type.includes('json') ? await response.json() : await response.text();
    if (!response.ok) {
      throw new Error((payload && payload.detail) || payload || `HTTP ${response.status}`);
    }
    return payload;
  }

  /* ================================================================== *
   * Toasts / modal
   * ================================================================== */
  function toast(message, kind, ms) {
    const el = document.createElement('div');
    el.className = 'toast ' + (kind || 'ok');
    el.textContent = message;
    $('#toasts').appendChild(el);
    setTimeout(() => el.remove(), ms || 5200);
  }

  function modal(html) {
    $('#modal-body').innerHTML = html;
    $('#modal').classList.add('open');
  }
  function closeModal() { $('#modal').classList.remove('open'); $('#modal-body').innerHTML = ''; }
  $('#modal').addEventListener('click', (e) => { if (e.target.id === 'modal') closeModal(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });

  function confirmAction(title, message, onConfirm, danger) {
    modal(`
      <h3>${esc(title)}</h3>
      <div class="${danger ? 'confirm-danger' : 'hint'}" style="margin:10px 0 16px">${message}</div>
      <div class="control-row" style="justify-content:flex-end">
        <button class="btn" data-act="cancel">Cancel</button>
        <button class="btn ${danger ? 'danger' : 'primary'}" data-act="ok">Confirm</button>
      </div>`);
    $('#modal-body [data-act="cancel"]').onclick = closeModal;
    $('#modal-body [data-act="ok"]').onclick = () => { closeModal(); onConfirm(); };
  }

  /* ================================================================== *
   * Auth
   * ================================================================== */
  function showLogin() {
    S.user = null;
    if (S.ws) { try { S.ws.close(); } catch (_) {} S.ws = null; }
    $('#login').style.display = 'grid';
    $('#app').classList.remove('ready');
    document.body.classList.add('locked');
  }

  function showApp() {
    $('#login').style.display = 'none';
    $('#app').classList.add('ready');
    document.body.classList.remove('locked');
  }

  $('#login-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = $('#login-btn');
    button.disabled = true;
    $('#login-error').textContent = '';
    try {
      const result = await api('/api/auth/login', {
        method: 'POST',
        body: { username: $('#u').value.trim(), password: $('#p').value },
      });
      applySession(result);
      $('#p').value = '';
      start();
      if (result.must_change_password) {
        toast('Please change your password from the account menu', 'warn', 9000);
      }
    } catch (error) {
      $('#login-error').textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });

  function applySession(result) {
    S.user = result.username;
    S.csrf = result.csrf;
    S.permissions = new Set(result.permissions || []);
    $('#user-label').textContent = `${result.username} · ${result.role}`;
  }

  const can = (permission) => S.permissions.has(permission);

  /* ================================================================== *
   * WebSocket
   * ================================================================== */
  function connect() {
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${scheme}://${location.host}/ws`);
    S.ws = ws;

    ws.onopen = () => { S.connected = true; S.wsRetry = 0; updateConnection(); };
    ws.onclose = () => {
      S.connected = false;
      updateConnection();
      S.wsRetry = Math.min(S.wsRetry + 1, 6);
      setTimeout(() => { if (S.user) connect(); }, 500 * Math.pow(2, S.wsRetry));
    };
    ws.onerror = () => ws.close();
    ws.onmessage = (event) => {
      let message;
      try { message = JSON.parse(event.data); } catch (_) { return; }
      if (message.channel === 'snapshot' || message.channel === 'hello') {
        if (message.data) onSnapshot(message.data);
        if (message.version) $('#version-tag').textContent = 'v' + message.version;
      } else if (message.channel === 'notifications' && message.notification) {
        onNotification(message.notification);
      }
    };
  }

  function updateConnection() {
    const dot = $('#conn-dot');
    dot.style.background = S.connected ? 'var(--c-up)' : 'var(--c-down)';
    dot.style.boxShadow = `0 0 12px ${S.connected ? 'var(--c-up)' : 'var(--c-down)'}`;
    dot.title = S.connected ? 'Connected to dashboard' : 'Disconnected — reconnecting…';
    if (!S.connected) setChip('chip-engine', 'off', 'offline');
  }

  function onSnapshot(data) {
    S.snap = data;
    S.lastSnapshotAt = Date.now();
    $('#version-tag').textContent = 'v' + data.version;
    updateChips();
    updateBadge(data.notifications ? data.notifications.unread : 0);
    const panel = PANELS[S.tab];
    if (panel && panel.tick) {
      try { panel.tick($('#main')); } catch (error) { console.error('tick failed', error); }
    }
  }

  function onNotification(notification) {
    if (notification.id <= S.seenNotificationId) return;
    S.seenNotificationId = notification.id;
    const kind = notification.severity === 'critical' ? 'err'
      : notification.severity === 'warning' ? 'warn' : 'ok';
    toast(`${notification.title}${notification.body ? ' — ' + notification.body : ''}`, kind,
          notification.severity === 'critical' ? 12000 : 6000);
    if (S.browserNotifications && Notification.permission === 'granted') {
      new Notification(notification.title, { body: notification.body || '', tag: 'polybot-' + notification.id });
    }
    if (S.tab === 'notifications') PANELS.notifications.load();
  }

  /* ================================================================== *
   * Top bar
   * ================================================================== */
  function setChip(id, ledClass, text, title) {
    const chip = $('#' + id);
    if (!chip) return;
    chip.querySelector('.led').className = 'led ' + (ledClass || '');
    chip.querySelector('span').textContent = text;
    if (title) chip.title = title;
  }

  function updateChips() {
    const snapshot = S.snap;
    if (!snapshot) return;
    const engine = (snapshot.engines || {})[S.mode] || {};
    const service = engine.service || {};
    const engineLed = engine.live ? 'on' : (service.healthy ? 'warn' : 'off');
    const engineText = engine.live ? `${S.mode} running`
      : service.healthy ? `${S.mode} starting` : `${S.mode} stopped`;
    setChip('chip-engine', engineLed, engineText,
            `heartbeat ${engine.heartbeat_age_s === null || engine.heartbeat_age_s === undefined
              ? 'never' : engine.heartbeat_age_s + 's ago'}`);

    const account = snapshot.live_account || {};
    const status = account.status || {};
    if (S.mode === 'live') {
      setChip('chip-api', status.api_ok ? 'on' : 'off', 'CLOB', status.auth_status || '');
      setChip('chip-ws', status.websocket_connected ? 'on' : 'warn', 'user WS',
              status.websocket_connected ? 'connected' : 'polling fallback');
    } else {
      const feed = (snapshot.markets || {}).websocket || {};
      setChip('chip-api', 'on', 'public', 'public market data');
      setChip('chip-ws', feed.connected ? 'on' : 'warn', 'market WS',
              feed.connected ? 'connected' : 'REST fallback');
    }
    $('#chip-clock').textContent = new Date().toLocaleTimeString([], { hour12: false });
  }

  function updateBadge(count) {
    const badge = $('#notif-badge');
    badge.hidden = !count;
    badge.textContent = count > 99 ? '99+' : count;
  }

  setInterval(() => {
    $('#chip-clock').textContent = new Date().toLocaleTimeString([], { hour12: false });
    if (S.user && S.connected && Date.now() - S.lastSnapshotAt > 8000) {
      setChip('chip-engine', 'warn', 'stale feed');
    }
  }, 1000);

  /* ================================================================== *
   * Rendering helpers
   * ================================================================== */
  function card(title, body, actions) {
    return `<div class="card"><h3>${esc(title)}${actions ? `<span class="actions">${actions}</span>` : ''}</h3>${body}</div>`;
  }

  function kpi(label, value, sub, cls, sparkId) {
    return `<div class="card kpi">
      <div class="label">${esc(label)}</div>
      <div class="value ${cls || ''}">${value}</div>
      ${sub ? `<div class="sub">${sub}</div>` : ''}
      ${sparkId ? `<canvas id="${sparkId}"></canvas>` : ''}
    </div>`;
  }

  function stat(label, value, cls) {
    return `<div class="stat"><div class="k">${esc(label)}</div><div class="v ${cls || ''}">${value}</div></div>`;
  }

  function table(columns, rows, emptyText) {
    if (!rows.length) return `<div class="table-empty">${esc(emptyText || 'Nothing here yet')}</div>`;
    const head = columns.map((c) => `<th class="${c.num ? 'num' : ''}">${esc(c.label)}</th>`).join('');
    const body = rows.map((row) =>
      '<tr>' + columns.map((c) => `<td class="${c.num ? 'num' : ''}">${c.render(row)}</td>`).join('') + '</tr>'
    ).join('');
    return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
  }

  const pill = (value, extra) =>
    `<span class="pill ${String(value ?? '').toLowerCase()} ${extra || ''}">${esc(value ?? '—')}</span>`;

  function draw(id, fn, config) {
    const canvas = document.getElementById(id);
    if (canvas) fn(canvas, config);
  }

  const engineOf = () => ((S.snap && S.snap.engines) || {})[S.mode] || {};
  const otherEngineOf = () => ((S.snap && S.snap.engines) || {})[S.mode === 'live' ? 'paper' : 'live'] || {};

  /* ================================================================== *
   * Panels
   * ================================================================== */
  const PANELS = {};

  /* ---------------------------- Overview ---------------------------- */
  PANELS.overview = {
    label: 'Overview', icon: '◎', group: 'Trading',
    render(root) { root.innerHTML = '<div id="overview-root"></div>'; this.tick(root); },
    tick() {
      const host = $('#overview-root');
      if (!host || !S.snap) return;
      const engine = engineOf();
      const pnl = engine.pnl || {};
      const account = S.snap.live_account || {};
      const wallet = account.wallet || {};
      const series = (engine.equity_series || []).map((p) => p.equity);
      const isLive = S.mode === 'live';
      const cash = isLive ? (wallet.balance ?? engine.cash) : engine.cash;
      const equity = isLive
        ? ((account.portfolio || {}).total_value ?? engine.equity)
        : engine.equity;

      host.innerHTML = `
        <div class="grid g4" style="margin-bottom:var(--gap)">
          ${kpi('Portfolio value', F.money(equity), `cash ${F.money(cash)}`, '', 'spark-equity')}
          ${kpi('Unrealised PnL', F.signed(pnl.unrealized), `${engine.open_positions || 0} open position(s)`, F.cls(pnl.unrealized))}
          ${kpi('Realised PnL', F.signed(pnl.realized), 'since engine start', F.cls(pnl.realized))}
          ${kpi('Exposure', F.money(engine.exposure), `${(engine.orders || {}).open || 0} working order(s)`)}
        </div>

        <div class="grid g2" style="margin-bottom:var(--gap)">
          ${card('Equity curve', '<canvas class="chart" id="chart-equity"></canvas>')}
          ${card('Engine & services', engineStatusBlock(engine))}
        </div>

        <div class="grid g2" style="margin-bottom:var(--gap)">
          ${card('Open positions', positionsTable(engine.positions || []))}
          ${card('Working orders', ordersTable((engine.open_orders || []).slice(0, 12)))}
        </div>

        <div class="grid g2">
          ${card('Recent trades', tradesTable((engine.recent_trades || []).slice(0, 10)))}
          ${card('Engine events', eventsList(engine.recent_events || []))}
        </div>`;

      draw('spark-equity', Charts.spark, { values: series.slice(-60) });
      draw('chart-equity', Charts.line, {
        series: [{ values: series, label: 'Equity', fill: true }],
        xLabels: (engine.equity_series || []).map((p) => Charts.fmtTime(p.ts)),
        emptyText: 'No equity samples yet — start the engine',
      });
    },
  };

  function engineStatusBlock(engine) {
    const heartbeat = engine.heartbeat || {};
    const service = engine.service || {};
    const risk = engine.risk || {};
    const services = ((S.snap || {}).vps || {}).services || [];
    return `<div class="stats" style="margin-bottom:12px">
        ${stat('Engine', engine.live ? '<span class="up">running</span>' : '<span class="down">stopped</span>')}
        ${stat('Heartbeat', engine.heartbeat_age_s == null ? '—' : engine.heartbeat_age_s + 's ago')}
        ${stat('Uptime', F.dur(heartbeat.uptime_s))}
        ${stat('Strategy', esc((heartbeat.strategy || {}).name || '—'))}
        ${stat('Ticks', F.int((heartbeat.strategy || {}).ticks))}
        ${stat('Risk', risk.halted ? '<span class="down">HALTED</span>' : '<span class="up">ok</span>')}
        ${stat('Kill switch', engine.kill_switch ? '<span class="down">ARMED</span>' : 'clear')}
        ${stat('Restarts', F.int(service.restarts))}
      </div>
      ${risk.halted ? `<div class="confirm-danger" style="margin-bottom:10px">Halt reason: ${esc(risk.halt_reason || 'unknown')}</div>` : ''}
      <div class="table-wrap"><table><tbody>
        ${services.map((s) => `<tr>
          <td>${esc(s.name)}</td>
          <td>${pill(s.available ? s.active : 'not installed')}</td>
          <td class="dim">${esc(s.sub || '')}</td>
          <td class="num dim">${s.memory_bytes ? F.bytes(s.memory_bytes) : '—'}</td>
          <td class="num dim">${s.uptime_s ? F.dur(s.uptime_s) : '—'}</td>
        </tr>`).join('')}
      </tbody></table></div>`;
  }

  function positionsTable(rows) {
    return table([
      { label: 'Market', render: (r) => esc(r.market_label || (r.market_id || '').slice(0, 14)) },
      { label: 'Side', render: (r) => pill(r.side) },
      { label: 'Qty', num: true, render: (r) => F.num(r.qty, 0) },
      { label: 'Avg', num: true, render: (r) => F.price(r.avg_price) },
      { label: 'Mark', num: true, render: (r) => F.price(r.mark_price) },
      { label: 'Unreal.', num: true, render: (r) => `<span class="${F.cls(r.unrealized)}">${F.signed(r.unrealized, 4)}</span>` },
    ], rows, 'No open positions');
  }

  function ordersTable(rows) {
    return table([
      { label: 'Time', render: (r) => F.time(r.ts_created) },
      { label: 'Market', render: (r) => esc(r.market_label || (r.market_id || '').slice(0, 12)) },
      { label: 'Side', render: (r) => pill(r.action) + ' ' + esc(r.side || '') },
      { label: 'Price', num: true, render: (r) => F.price(r.price) },
      { label: 'Size', num: true, render: (r) => F.num(r.size, 0) },
      { label: 'Filled', num: true, render: (r) =>
        `<div class="bar ${r.filled_pct >= 100 ? 'up' : 'warn'}"><i style="width:${Math.min(100, r.filled_pct || 0)}%"></i></div>` },
      { label: 'Status', render: (r) => pill(r.status) },
    ], rows, 'No working orders');
  }

  function tradesTable(rows) {
    return table([
      { label: 'Closed', render: (r) => F.datetime(r.exit_ts) },
      { label: 'Market', render: (r) => esc(r.market_label || (r.market_id || '').slice(0, 12)) },
      { label: 'Side', render: (r) => pill(r.side) },
      { label: 'Qty', num: true, render: (r) => F.num(r.qty, 0) },
      { label: 'In → Out', num: true, render: (r) => `${F.price(r.entry_price)} → ${F.price(r.exit_price)}` },
      { label: 'Held', num: true, render: (r) => F.dur(r.duration_s) },
      { label: 'PnL', num: true, render: (r) => `<span class="${F.cls(r.pnl)}">${F.signed(r.pnl, 4)}</span>` },
    ], rows, 'No completed trades yet');
  }

  function eventsList(events) {
    if (!events.length) return '<div class="table-empty">No engine events</div>';
    return events.map((e) => `<div class="notif ${esc(e.level)}">
      <div class="sev"></div>
      <div class="body">
        <div class="msg">${esc(e.message)}</div>
        <div class="meta">${esc(e.kind)} · ${F.datetime(e.ts)}</div>
      </div></div>`).join('');
  }

  /* ---------------------------- Wallet ---------------------------- */
  PANELS.wallet = {
    label: 'Wallet', icon: '◈', group: 'Trading',
    render(root) { root.innerHTML = '<div id="wallet-root"></div>'; this.tick(root); },
    tick() {
      const host = $('#wallet-root');
      if (!host || !S.snap) return;
      host.innerHTML = S.mode === 'live' ? liveWallet() : paperWallet();
    },
  };

  function readinessCard(engine) {
    const r = engine.readiness || {};
    if (!r || !Object.keys(r).length) {
      return card('Trading readiness',
        `<div class="table-empty">No readiness report yet — it is written by the engine
         on startup to <code>state/readiness.json</code>.</div>`);
    }
    const checks = r.checks || {};
    const missing = r.missing_requirements || r.missing || [];
    return card('Trading readiness', `
      <div class="control-row" style="margin-bottom:10px">
        <strong>Live ready:</strong>
        ${r.live_ready ? '<span class="pill ok">YES</span>' : '<span class="pill rejected">NO</span>'}
        <span class="hint">signer ${F.addr(r.signer)} · maker ${F.addr(r.maker_deposit_wallet)} · ${esc(r.signature_type_name || '')}</span>
      </div>
      <div class="table-wrap"><table><tbody>
        ${Object.entries(checks).map(([name, c]) => `<tr>
          <td>${esc(name.replace(/_/g, ' '))}</td>
          <td>${c.advisory
                ? `<span class="pill">${c.ok ? 'armed' : 'dry-run'}</span>`
                : (c.ok ? '<span class="pill ok">ok</span>' : '<span class="pill rejected">blocked</span>')}</td>
          <td class="dim">${esc(String(c.detail ?? (c.per_spender ? JSON.stringify(c.per_spender) : (c.balance_usdc ?? ''))).slice(0, 120))}</td>
        </tr>`).join('')}
      </tbody></table></div>
      ${(missing || []).length ? `<div class="confirm-danger" style="margin-top:10px">
        <strong>Missing:</strong><ul style="margin:6px 0 0 16px;padding:0">
        ${missing.map((m) => `<li>${esc(m)}</li>`).join('')}</ul></div>` : ''}
      <div class="hint" style="margin-top:8px">Reported ${F.ago(r._file_ts)}</div>`);
  }

  function liveWallet() {
    const account = S.snap.live_account || {};
    const wallet = account.wallet || {};
    const portfolio = account.portfolio || {};
    const status = account.status || {};
    const allowances = Object.entries(wallet.allowances || {});

    return `
      <div class="grid g4" style="margin-bottom:var(--gap)">
        ${kpi('USDC balance', F.money(wallet.balance), 'collateral held')}
        ${kpi('Available', F.money(wallet.available), `locked ${F.money(wallet.locked)}`)}
        ${kpi('Buying power', F.money(wallet.buying_power), 'min(cash, allowance)')}
        ${kpi('Portfolio value', F.money(portfolio.total_value), `positions ${F.money(portfolio.positions_value)}`)}
      </div>

      <div class="grid g2" style="margin-bottom:var(--gap)">
        ${card('Account identity', `<div class="stats">
          ${stat('Deposit wallet', `<span title="${esc(wallet.deposit_wallet)}">${F.addr(wallet.deposit_wallet)}</span>`)}
          ${stat('Signer wallet', `<span title="${esc(wallet.signer)}">${F.addr(wallet.signer)}</span>`)}
          ${stat('Signature type', wallet.signature_type ?? '—')}
          ${stat('Credentials', esc(wallet.creds_source || '—'))}
          ${stat('Builder program', wallet.builder_enabled ? '<span class="up">enabled</span>' : 'disabled')}
          ${stat('Authentication', status.authenticated ? '<span class="up">authenticated</span>' : `<span class="down">${esc(status.auth_status || 'not connected')}</span>`)}
        </div>`, `<button class="btn sm" data-act="refresh-live">Resync</button>`)}

        ${card('Allowance status', `
          <div class="stats" style="margin-bottom:10px">
            ${stat('Allowance OK', wallet.allowance_ok ? '<span class="up">yes</span>' : '<span class="down">no</span>')}
            ${stat('Lowest approval', wallet.allowance_min === null ? '—' : F.money(wallet.allowance_min))}
            ${stat('Required minimum', F.money(wallet.min_allowance))}
          </div>
          ${allowances.length ? `<div class="table-wrap"><table><thead><tr><th>Spender</th><th class="num">Approved</th></tr></thead><tbody>
            ${allowances.map(([spender, value]) => `<tr><td title="${esc(spender)}">${F.addr(spender)}</td>
              <td class="num">${value === null ? '<span class="up">unlimited</span>' : F.money(value)}</td></tr>`).join('')}
          </tbody></table></div>` : '<div class="table-empty">No allowance data from the exchange</div>'}
          ${!wallet.allowance_ok && wallet.address ? `<div class="confirm-danger" style="margin-top:10px">
            Buy orders will be rejected until USDC spending is approved for this wallet in the Polymarket UI.</div>` : ''}
        `)}
      </div>

      <div style="margin-bottom:var(--gap)">${readinessCard(engineOf())}</div>

      <div class="grid g2" style="margin-bottom:var(--gap)">
        ${card('Exchange connectivity', `<div class="stats">
          ${stat('CLOB API', status.api_ok ? '<span class="up">reachable</span>' : '<span class="down">unreachable</span>')}
          ${stat('User websocket', status.websocket_connected ? '<span class="up">connected</span>' : '<span class="warn">polling</span>')}
          ${stat('Last sync', F.ago(status.last_sync_ts))}
          ${stat('Sync latency', F.num(status.sync_latency_ms, 0) + ' ms')}
          ${stat('Syncs', F.int(status.sync_count))}
          ${stat('Errors', `<span class="${status.error_count ? 'warn' : ''}">${F.int(status.error_count)}</span>`)}
          ${stat('CLOB p95', F.num((status.clob_latency || {}).p95_ms, 0) + ' ms')}
          ${stat('Fees paid', F.money(account.fees_paid, 4))}
        </div>${status.last_error ? `<div class="hint" style="margin-top:10px">Last error: ${esc(status.last_error)}</div>` : ''}`)}

        ${card('Account PnL', `<div class="stats">
          ${stat('Unrealised', `<span class="${F.cls(portfolio.unrealized_pnl)}">${F.signed(portfolio.unrealized_pnl, 4)}</span>`)}
          ${stat('Realised', `<span class="${F.cls(portfolio.realized_pnl)}">${F.signed(portfolio.realized_pnl, 4)}</span>`)}
          ${stat('Total', `<span class="${F.cls(portfolio.total_pnl)}">${F.signed(portfolio.total_pnl, 4)}</span>`)}
          ${stat('Exposure (cost)', F.money(portfolio.exposure))}
          ${stat('Open positions', F.int(portfolio.open_positions))}
          ${stat('Open orders', F.int(portfolio.open_orders))}
          ${stat('Order notional', F.money(portfolio.open_order_notional))}
        </div><div class="hint" style="margin-top:10px">Read live from your Polymarket account, independent of the bot.</div>`)}
      </div>

      ${card('Account positions (all markets)', table([
        { label: 'Market', render: (p) => esc(p.title || p.market_id.slice(0, 18)) },
        { label: 'Outcome', render: (p) => pill(p.outcome) },
        { label: 'Size', num: true, render: (p) => F.num(p.size, 2) },
        { label: 'Avg', num: true, render: (p) => F.price(p.avg_price) },
        { label: 'Current', num: true, render: (p) => F.price(p.current_price) },
        { label: 'Value', num: true, render: (p) => F.money(p.current_value) },
        { label: 'PnL', num: true, render: (p) => `<span class="${F.cls(p.unrealized_pnl)}">${F.signed(p.unrealized_pnl, 4)}</span>` },
        { label: 'PnL %', num: true, render: (p) => `<span class="${F.cls(p.percent_pnl)}">${F.pct(p.percent_pnl)}</span>` },
      ], account.positions || [], status.authenticated ? 'No open positions on this account' : 'Not connected to the live account'))}`;
  }

  function paperWallet() {
    const engine = engineOf();
    const heartbeat = engine.heartbeat || {};
    const config = heartbeat.config || {};
    return `
      <div class="grid g4" style="margin-bottom:var(--gap)">
        ${kpi('Virtual cash', F.money(engine.cash), 'simulated collateral')}
        ${kpi('Positions value', F.money(engine.positions_value), `${engine.open_positions || 0} position(s)`)}
        ${kpi('Equity', F.money(engine.equity), 'cash + positions')}
        ${kpi('Total PnL', F.signed((engine.pnl || {}).total), 'realised + unrealised', F.cls((engine.pnl || {}).total))}
      </div>
      ${card('Paper account', `<div class="stats">
        ${stat('Wallet', 'simulated — no key loaded')}
        ${stat('Exposure', F.money(engine.exposure))}
        ${stat('Open orders', F.int((engine.orders || {}).open))}
        ${stat('Engine', engine.live ? '<span class="up">running</span>' : '<span class="down">stopped</span>')}
        ${stat('Uptime', F.dur(heartbeat.uptime_s))}
        ${stat('Kill switch', engine.kill_switch ? '<span class="down">ARMED</span>' : 'clear')}
      </div>
      <div class="hint" style="margin-top:12px">
        The paper engine runs the real strategy against real order books with virtual money.
        It has its own database, logs and configuration, and every wallet credential is deleted
        from its process environment at startup — it cannot reach real funds.
      </div>`)}
      <div style="height:var(--gap)"></div>
      ${card('Open positions', positionsTable(engine.positions || []))}`;
  }

  /* ---------------------------- Markets ---------------------------- */
  PANELS.markets = {
    label: 'Markets', icon: '▤', group: 'Trading',
    render(root) {
      root.innerHTML = `
        <div class="card" style="margin-bottom:var(--gap)">
          <div class="control-row">
            <strong>Live crypto markets</strong>
            <span class="hint" id="markets-meta"></span>
            <div class="spacer" style="flex:1"></div>
            <button class="btn sm" data-act="refresh-markets">Rediscover</button>
          </div>
        </div>
        <div id="markets-root" class="grid g-wide"></div>`;
      this.load();
    },
    async load() {
      try {
        S.detail.markets = await api('/api/markets?depth=true&indicators=true');
      } catch (error) { toast(error.message, 'err'); }
      this.paint();
    },
    tick() {
      // Prices come from the 1 Hz snapshot; books/indicators refresh on a slower beat.
      if (!this._last || Date.now() - this._last > 6000) { this._last = Date.now(); this.load(); }
      else this.paintCountdowns();
    },
    paint() {
      const host = $('#markets-root');
      if (!host) return;
      const data = S.detail.markets || { markets: [] };
      const discovery = data.discovery || {};
      const meta = $('#markets-meta');
      if (meta) {
        meta.textContent = `${data.count || 0} tracked · ${(data.assets || []).join(', ')} · ` +
          `${(data.durations || []).join(', ')} · feed ${(data.websocket || {}).connected ? 'live' : 'REST fallback'}` +
          (discovery.error ? ` · discovery error: ${discovery.error}` : '');
      }
      if (!data.markets || !data.markets.length) {
        host.innerHTML = `<div class="card"><div class="table-empty">
          No matching markets are open right now.<br>
          Tracking ${esc((data.assets || []).join(', '))} over ${esc((data.durations || []).join(', '))} windows.
          ${discovery.error ? '<br>Discovery error: ' + esc(discovery.error) : ''}
        </div></div>`;
        return;
      }
      host.innerHTML = data.markets.map(marketCard).join('');
      data.markets.forEach((market) => {
        draw('depth-' + cssId(market.key), Charts.depth, {
          bids: (market.up.bids || []), asks: (market.up.asks || []),
        });
        const indicators = market.indicators || {};
        if (indicators.series) {
          draw('mchart-' + cssId(market.key), Charts.line, {
            series: [
              { values: indicators.series.close, label: 'Mid', fill: true },
              { values: indicators.series.ema9, label: 'EMA9', width: 1 },
              { values: indicators.series.ema21, label: 'EMA21', width: 1 },
            ],
            xLabels: (indicators.series.ts || []).map(Charts.fmtTime),
            emptyText: 'Collecting candles…',
          });
        }
      });
    },
    paintCountdowns() {
      const data = S.detail.markets;
      if (!data) return;
      (data.markets || []).forEach((market) => {
        const el = document.getElementById('cd-' + cssId(market.key));
        if (!el || !market.settlement_ts) return;
        const remaining = market.settlement_ts - Date.now() / 1000;
        el.textContent = remaining > 0 ? F.dur(remaining) : 'settling';
        el.classList.toggle('urgent', remaining > 0 && remaining < 60);
      });
    },
  };

  const cssId = (key) => String(key).replace(/[^a-zA-Z0-9_-]/g, '_');

  function marketCard(market) {
    const indicators = market.indicators || {};
    const up = market.up || {}, down = market.down || {};
    const id = cssId(market.key);
    const ind = (label, value, cls) =>
      `<div class="ind"><div class="k">${label}</div><div class="v ${cls || ''}">${value}</div></div>`;

    return `<div class="card market-card">
      <div class="market-head">
        <div><span class="asset">${esc(market.asset)}</span> <span class="dur">${esc(market.duration)}</span></div>
        <div>${pill(market.status)}</div>
      </div>
      <div class="hint" title="${esc(market.question)}" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(market.question || market.slug)}</div>

      <div class="control-row" style="justify-content:space-between">
        <div class="market-price">${F.price(market.price)}</div>
        <div style="text-align:right">
          <div class="${F.cls(market.price_change_pct)}">${F.signed(market.price_change_pct, 2)}%</div>
          <div class="countdown" id="cd-${id}">${market.time_remaining_s == null ? '—' : F.dur(market.time_remaining_s)}</div>
        </div>
      </div>

      <div class="stats">
        ${stat('Bid', F.price(up.bid))}
        ${stat('Ask', F.price(up.ask))}
        ${stat('Mid', F.price(up.mid))}
        ${stat('Spread', up.spread == null ? '—' : up.spread.toFixed(4))}
        ${stat('Liquidity', F.money(market.liquidity, 0))}
        ${stat('Volume', F.money(market.volume, 0))}
        ${stat('24h vol', F.money(market.volume_24h, 0))}
        ${stat('Tick', String(market.tick_size))}
      </div>

      <canvas class="chart short" id="mchart-${id}"></canvas>

      <div class="book">
        <div class="bids">
          <div class="side-title">Bids · ${esc(market.up_outcome)}</div>
          ${bookLadder(up.bids || [], 'bids')}
        </div>
        <div class="asks">
          <div class="side-title">Asks · ${esc(market.up_outcome)}</div>
          ${bookLadder(up.asks || [], 'asks')}
        </div>
      </div>
      <canvas class="chart short" id="depth-${id}" style="height:80px"></canvas>

      <div class="ind-grid">
        ${ind('EMA 9', F.price(indicators.ema9))}
        ${ind('EMA 21', F.price(indicators.ema21))}
        ${ind('EMA 50', F.price(indicators.ema50))}
        ${ind('Trend', esc(indicators.ema_alignment || '—'),
              indicators.ema_alignment === 'bullish' ? 'up' : indicators.ema_alignment === 'bearish' ? 'down' : '')}
        ${ind('RSI', F.num(indicators.rsi, 1),
              indicators.rsi > 70 ? 'down' : indicators.rsi < 30 ? 'up' : '')}
        ${ind('MACD', F.num(indicators.macd, 4))}
        ${ind('Signal', F.num(indicators.macd_signal, 4))}
        ${ind('Hist', F.num(indicators.macd_histogram, 4), F.cls(indicators.macd_histogram))}
        ${ind('ATR', F.num(indicators.atr, 4))}
        ${ind('VWAP', F.price(indicators.vwap))}
        ${ind('BB upper', F.price(indicators.bb_upper))}
        ${ind('BB lower', F.price(indicators.bb_lower))}
        ${ind('%B', F.num(indicators.bb_percent_b, 2))}
        ${ind('Volatility', indicators.volatility_pct == null ? '—' : F.pct(indicators.volatility_pct))}
        ${ind('Vol Δ', F.num((indicators.volume_delta || {}).delta, 1), F.cls((indicators.volume_delta || {}).delta))}
        ${ind('Momentum', indicators.momentum_pct == null ? '—' : F.pct(indicators.momentum_pct), F.cls(indicators.momentum_pct))}
        ${ind('ADX', F.num(indicators.trend_strength, 1))}
        ${ind('Bars', F.int(indicators.bars))}
      </div>
      <div class="hint">Settles ${market.settlement_ts ? F.datetime(market.settlement_ts) : '—'} · min size ${market.min_order_size} · ${market.neg_risk ? 'neg-risk' : 'standard'}</div>
      <div class="hint">Candles reconstructed from the Polymarket order-book midpoint (Polymarket publishes no OHLCV); volume from observed trade prints. Indicators are computed on these reconstructed bars.</div>
    </div>`;
  }

  function bookLadder(levels, side) {
    if (!levels.length) return '<div class="faint">no depth</div>';
    const max = Math.max(...levels.map((l) => l.size), 1e-9);
    return levels.slice(0, 6).map((l) => `<div class="lvl">
      <div class="depth-fill" style="width:${(l.size / max * 100).toFixed(1)}%"></div>
      <span>${l.price.toFixed(3)}</span><span>${F.num(l.size, 0)}</span>
    </div>`).join('');
  }

  /* ---------------------------- Strategies ---------------------------- */
  PANELS.strategies = {
    label: 'Strategies', icon: '⌁', group: 'Trading',
    render(root) { root.innerHTML = '<div id="strategies-root"></div>'; this.tick(root); },
    tick() {
      const host = $('#strategies-root');
      if (!host || !S.snap) return;
      const strategies = engineOf().strategies || [];
      if (!strategies.length) {
        host.innerHTML = `<div class="card"><div class="table-empty">
          No strategy state recorded yet — start the ${esc(S.mode)} engine.</div></div>`;
        return;
      }
      host.innerHTML = `
        ${card('Strategy performance ranking', '<canvas class="chart" id="chart-strat"></canvas>')}
        <div style="height:var(--gap)"></div>
        <div class="grid g-wide">${strategies.map(strategyCard).join('')}</div>`;
      draw('chart-strat', Charts.hbars, {
        items: strategies.map((s) => ({ label: s.name, value: s.total_pnl })),
        emptyText: 'No completed trades yet',
      });
    },
  };

  function strategyCard(strategy) {
    return `<div class="card">
      <h3>${esc(strategy.name)} <span class="actions">
        ${pill(strategy.enabled ? 'enabled' : 'disabled')} ${pill(strategy.running ? 'active' : 'inactive')}
        <span class="pill">#${strategy.rank}</span></span></h3>
      <div class="stats" style="margin-bottom:12px">
        ${stat('Current signal', esc(strategy.current_signal || '—'))}
        ${stat('Last signal', esc(strategy.last_signal || '—'))}
        ${stat('Signal at', F.time(strategy.last_signal_ts))}
        ${stat('Confidence', strategy.confidence == null ? '<span class="faint">not published</span>' : F.pct(strategy.confidence * 100))}
        ${stat('Position', F.num(strategy.position_qty, 0))}
        ${stat('Current PnL', `<span class="${F.cls(strategy.current_pnl)}">${F.signed(strategy.current_pnl, 4)}</span>`)}
        ${stat('Total PnL', `<span class="${F.cls(strategy.total_pnl)}">${F.signed(strategy.total_pnl, 4)}</span>`)}
        ${stat('Win rate', F.pct(strategy.win_rate))}
        ${stat('Accuracy', '<span class="faint">not provided by strategy</span>')}
        ${stat('Avg win', F.signed(strategy.average_win, 4))}
        ${stat('Avg loss', F.signed(-Math.abs(strategy.average_loss || 0), 4))}
        ${stat('Trades', F.int(strategy.total_trades))}
        ${stat('Profit factor', strategy.profit_factor == null ? '—' : F.num(strategy.profit_factor, 2))}
        ${stat('Ticks', F.int(strategy.ticks))}
        ${stat('Errors', `<span class="${strategy.errors ? 'down' : ''}">${F.int(strategy.errors)}</span>`)}
      </div>
      ${table([
        { label: 'Market', render: (m) => esc(m.label || (m.market_id || '').slice(0, 14)) },
        { label: 'Signal', render: (m) => pill(m.signal || '—') },
        { label: 'Qty', num: true, render: (m) => F.num(m.position_qty, 0) },
        { label: 'Entry', num: true, render: (m) => F.price(m.entry_price) },
        { label: 'Exit', num: true, render: (m) => F.price(m.exit_price) },
        { label: 'Stop', num: true, render: (m) => m.stop_loss == null ? '—' : F.price(m.stop_loss) },
        { label: 'Target', num: true, render: (m) => m.take_profit == null ? '—' : F.price(m.take_profit) },
        { label: 'PnL', num: true, render: (m) => `<span class="${F.cls(m.unrealized)}">${F.signed(m.unrealized, 4)}</span>` },
      ], strategy.markets || [], 'No markets bound')}
    </div>`;
  }

  /* ---------------------------- Orders ---------------------------- */
  PANELS.orders = {
    label: 'Orders', icon: '≡', group: 'Trading',
    render(root) {
      root.innerHTML = `
        <div class="card" style="margin-bottom:var(--gap)">
          <div class="control-row" id="order-filters"></div>
        </div>
        <div id="orders-root"></div>`;
      this.load();
    },
    async load() {
      const filter = S.filters.orders;
      const query = filter === 'all' ? '' : `status=${encodeURIComponent(filter)}&`;
      try {
        S.detail.orders = await api(`/api/engine/${S.mode}/orders?${query}limit=500`);
      } catch (error) { toast(error.message, 'err'); return; }
      this.paint();
    },
    tick() {
      if (!this._last || Date.now() - this._last > 4000) { this._last = Date.now(); this.load(); }
    },
    paint() {
      const data = S.detail.orders || { orders: [], counts: {} };
      const counts = data.counts || {};
      const filters = [
        ['all', 'All', counts.total], ['open', 'Open', counts.open], ['pending', 'Pending', counts.pending],
        ['filled', 'Filled', counts.filled], ['cancelled', 'Cancelled', counts.canceled],
        ['rejected', 'Rejected', (counts.rejected || 0) + (counts.failed || 0)],
      ];
      const bar = $('#order-filters');
      if (bar) {
        bar.innerHTML = filters.map(([key, label, n]) =>
          `<button class="btn sm ${S.filters.orders === key ? 'primary' : ''}" data-filter="${key}">${label} (${n || 0})</button>`
        ).join('') + `<div style="flex:1"></div>
          <span class="hint">Partial fills: ${counts.partial || 0}</span>
          ${can('export') ? `<a class="btn sm" href="/api/export/${S.mode}/orders?format=csv">Export CSV</a>` : ''}`;
        $$('#order-filters [data-filter]').forEach((button) => {
          button.onclick = () => { S.filters.orders = button.dataset.filter; PANELS.orders.load(); };
        });
      }

      $('#orders-root').innerHTML = card(`${S.mode} orders`, table([
        { label: 'Order ID', render: (o) => `<span title="${esc(o.exchange_order_id || o.client_id)}">${esc((o.exchange_order_id || o.client_id || '').slice(0, 14))}</span>` },
        { label: 'Time', render: (o) => F.datetime(o.ts_created) },
        { label: 'Market', render: (o) => `<span title="${esc(o.market_id)}">${esc(o.market_label || (o.market_id || '').slice(0, 12))}</span>` },
        { label: 'Dir', render: (o) => pill(o.action) + ' ' + esc(o.side || '') },
        { label: 'Type', render: (o) => esc(o.order_type || '—') },
        { label: 'Price', num: true, render: (o) => F.price(o.price) },
        { label: 'Qty', num: true, render: (o) => F.num(o.size, 0) },
        { label: 'Filled %', num: true, render: (o) => F.pct(o.filled_pct, 1) },
        { label: 'Remaining', num: true, render: (o) => F.num(o.remaining_size, 0) },
        { label: 'Avg fill', num: true, render: (o) => F.price(o.avg_fill_price) },
        { label: 'Fees', num: true, render: (o) => F.num(o.fees, 4) },
        { label: 'Exec ms', num: true, render: (o) => o.execution_ms == null ? '—' : F.num(o.execution_ms, 0) },
        { label: 'Status', render: (o) => pill(o.status) + (o.simulated ? ' <span class="pill">sim</span>' : '') },
        { label: 'Exchange / error', render: (o) => o.last_error
            ? `<span class="down" title="${esc(o.last_error)}">${esc(o.last_error.slice(0, 46))}</span>`
            : `<span class="faint" title="${esc(o.exchange_response || '')}">${esc((o.exchange_response || '—').slice(0, 34))}</span>` },
      ], data.orders || [], 'No orders recorded for this filter'));
    },
  };

  /* ---------------------------- Trades ---------------------------- */
  PANELS.trades = {
    label: 'Trades', icon: '⇄', group: 'Trading',
    render(root) { root.innerHTML = '<div id="trades-root"><div class="card"><div class="table-empty">Loading…</div></div></div>'; this.load(); },
    async load() {
      try {
        const [trades, fills] = await Promise.all([
          api(`/api/engine/${S.mode}/trades?limit=1000`),
          api(`/api/engine/${S.mode}/fills?limit=200`),
        ]);
        S.detail.trades = trades.trades || [];
        S.detail.fills = fills.fills || [];
      } catch (error) { toast(error.message, 'err'); return; }
      this.paint();
    },
    tick() { if (!this._last || Date.now() - this._last > 8000) { this._last = Date.now(); this.load(); } },
    paint() {
      const trades = S.detail.trades || [];
      const fills = S.detail.fills || [];
      $('#trades-root').innerHTML = `
        ${card('Completed round trips', table([
          { label: 'Opened', render: (t) => F.datetime(t.entry_ts) },
          { label: 'Closed', render: (t) => F.datetime(t.exit_ts) },
          { label: 'Market', render: (t) => esc(t.market_label || (t.market_id || '').slice(0, 14)) },
          { label: 'Side', render: (t) => pill(t.side) },
          { label: 'Strategy', render: (t) => esc(t.strategy || '—') },
          { label: 'Qty', num: true, render: (t) => F.num(t.qty, 0) },
          { label: 'Entry', num: true, render: (t) => F.price(t.entry_price) },
          { label: 'Exit', num: true, render: (t) => F.price(t.exit_price) },
          { label: 'Held', num: true, render: (t) => F.dur(t.duration_s) },
          { label: 'Fees', num: true, render: (t) => F.num(t.fees, 4) },
          { label: 'PnL', num: true, render: (t) => `<span class="${F.cls(t.pnl)}">${F.signed(t.pnl, 4)}</span>` },
          { label: 'Return', num: true, render: (t) => `<span class="${F.cls(t.return_pct)}">${F.pct(t.return_pct)}</span>` },
          { label: 'R', num: true, render: (t) => t.r_multiple == null ? '—' : F.num(t.r_multiple, 2) },
        ], trades, 'No completed trades yet'),
        can('export') ? `<a class="btn sm" href="/api/export/${S.mode}/trades?format=csv">CSV</a>
                         <a class="btn sm" href="/api/export/${S.mode}/trades?format=json">JSON</a>` : '')}
        <div style="height:var(--gap)"></div>
        ${card('Raw fills', table([
          { label: 'Time', render: (f) => F.datetime(f.ts) },
          { label: 'Order', render: (f) => esc((f.exchange_order_id || f.client_id || '').slice(0, 14)) },
          { label: 'Market', render: (f) => esc((f.market_id || '').slice(0, 14)) },
          { label: 'Side', render: (f) => pill(f.action) + ' ' + esc(f.side || '') },
          { label: 'Price', num: true, render: (f) => F.price(f.price) },
          { label: 'Size', num: true, render: (f) => F.num(f.size, 0) },
          { label: 'Fee', num: true, render: (f) => F.num(f.fee, 4) },
          { label: 'Realised', num: true, render: (f) => `<span class="${F.cls(f.realized_pnl)}">${F.signed(f.realized_pnl, 4)}</span>` },
        ], fills, 'No fills recorded'))}`;
    },
  };

  /* ---------------------------- Analytics ---------------------------- */
  PANELS.analytics = {
    label: 'Analytics', icon: '∑', group: 'Insight',
    render(root) { root.innerHTML = '<div id="analytics-root"><div class="card"><div class="table-empty">Loading…</div></div></div>'; this.load(); },
    async load() {
      try { S.detail.analytics = await api(`/api/engine/${S.mode}/analytics`); }
      catch (error) { toast(error.message, 'err'); return; }
      this.paint();
    },
    tick() { if (!this._last || Date.now() - this._last > 15000) { this._last = Date.now(); this.load(); } },
    paint() {
      const report = S.detail.analytics;
      if (!report) return;
      const t = report.trades, e = report.equity, p = report.pnl;
      $('#analytics-root').innerHTML = `
        <div class="grid g4" style="margin-bottom:var(--gap)">
          ${kpi('Net PnL', F.signed(t.net_pnl, 4), `${t.total_trades} trades`, F.cls(t.net_pnl))}
          ${kpi('Win rate', F.pct(t.win_rate), `${t.winning_trades}W / ${t.losing_trades}L`)}
          ${kpi('Profit factor', t.profit_factor == null ? '—' : F.num(t.profit_factor, 2), 'gross win ÷ gross loss')}
          ${kpi('Max drawdown', F.money(e.max_drawdown, 4), F.pct(e.max_drawdown_pct), 'down')}
        </div>

        <div class="grid g2" style="margin-bottom:var(--gap)">
          ${card('Trade statistics', `<div class="stats">
            ${stat('Total trades', F.int(t.total_trades))}
            ${stat('Winning', `<span class="up">${F.int(t.winning_trades)}</span>`)}
            ${stat('Losing', `<span class="down">${F.int(t.losing_trades)}</span>`)}
            ${stat('Win rate', F.pct(t.win_rate))}
            ${stat('Loss rate', F.pct(t.loss_rate))}
            ${stat('Average trade', F.signed(t.average_trade, 4))}
            ${stat('Average win', `<span class="up">${F.signed(t.average_win, 4)}</span>`)}
            ${stat('Average loss', `<span class="down">${F.signed(-t.average_loss, 4)}</span>`)}
            ${stat('Avg holding', F.dur(t.average_holding_time_s))}
            ${stat('Median holding', F.dur(t.median_holding_time_s))}
            ${stat('Largest win', `<span class="up">${F.signed(t.largest_win, 4)}</span>`)}
            ${stat('Largest loss', `<span class="down">${F.signed(t.largest_loss, 4)}</span>`)}
            ${stat('Expectancy', F.signed(t.expectancy, 4))}
            ${stat('Avg R multiple', t.average_r_multiple == null ? '—' : F.num(t.average_r_multiple, 3))}
            ${stat('Risk/reward', t.risk_reward_ratio == null ? '—' : F.num(t.risk_reward_ratio, 2))}
            ${stat('Total fees', F.num(t.total_fees, 4))}
          </div>`)}

          ${card('Risk & streaks', `<div class="stats">
            ${stat('Sharpe', e.sharpe_ratio == null ? '—' : F.num(e.sharpe_ratio, 2))}
            ${stat('Sortino', e.sortino_ratio == null ? '—' : F.num(e.sortino_ratio, 2))}
            ${stat('Calmar', e.calmar_ratio == null ? '—' : F.num(e.calmar_ratio, 2))}
            ${stat('Volatility (ann.)', e.volatility_pct == null ? '—' : F.pct(e.volatility_pct))}
            ${stat('Max drawdown', F.money(e.max_drawdown, 4))}
            ${stat('Max DD %', F.pct(e.max_drawdown_pct))}
            ${stat('Current DD', F.money(e.current_drawdown, 4))}
            ${stat('Total return', `<span class="${F.cls(e.total_return_pct)}">${F.pct(e.total_return_pct)}</span>`)}
            ${stat('Current win streak', `<span class="up">${t.current_win_streak}</span>`)}
            ${stat('Current loss streak', `<span class="down">${t.current_loss_streak}</span>`)}
            ${stat('Longest win streak', F.int(t.longest_win_streak))}
            ${stat('Longest loss streak', F.int(t.longest_loss_streak))}
            ${stat('Peak equity', F.money(e.peak_equity))}
            ${stat('Samples', F.int(e.samples))}
          </div>
          <div class="hint" style="margin-top:10px">Sharpe, Sortino and annualised volatility are computed from day-over-day equity returns; they read “—” until at least ${e.min_daily_returns ?? 20} daily returns exist (currently ${e.daily_returns ?? 0}). Annualising fewer samples would be misleading.</div>`)}
        </div>

        <div class="grid g4" style="margin-bottom:var(--gap)">
          ${kpi('Today', F.signed(p.today, 4), 'realised', F.cls(p.today))}
          ${kpi('This week', F.signed(p.this_week, 4), 'realised', F.cls(p.this_week))}
          ${kpi('This month', F.signed(p.this_month, 4), 'realised', F.cls(p.this_month))}
          ${kpi('All time', F.signed(p.total, 4), `7d ${F.signed(p.rolling_7d, 2)} · 30d ${F.signed(p.rolling_30d, 2)}`, F.cls(p.total))}
        </div>

        <div class="grid g2" style="margin-bottom:var(--gap)">
          ${card('Daily PnL', '<canvas class="chart" id="an-daily"></canvas>')}
          ${card('Trade PnL distribution', '<canvas class="chart" id="an-dist"></canvas>')}
        </div>

        <div class="grid g2">
          ${card('Performance by strategy', table([
            { label: '#', render: (r) => r.rank },
            { label: 'Strategy', render: (r) => esc(r.name) },
            { label: 'Trades', num: true, render: (r) => F.int(r.trades) },
            { label: 'Win rate', num: true, render: (r) => F.pct(r.win_rate) },
            { label: 'Net PnL', num: true, render: (r) => `<span class="${F.cls(r.net_pnl)}">${F.signed(r.net_pnl, 4)}</span>` },
            { label: 'PF', num: true, render: (r) => r.profit_factor == null ? '—' : F.num(r.profit_factor, 2) },
          ], report.by_strategy || [], 'No trades yet'))}

          ${card('Performance by market', table([
            { label: '#', render: (r) => r.rank },
            { label: 'Market', render: (r) => esc(String(r.name).slice(0, 22)) },
            { label: 'Trades', num: true, render: (r) => F.int(r.trades) },
            { label: 'Win rate', num: true, render: (r) => F.pct(r.win_rate) },
            { label: 'Net PnL', num: true, render: (r) => `<span class="${F.cls(r.net_pnl)}">${F.signed(r.net_pnl, 4)}</span>` },
            { label: 'Avg', num: true, render: (r) => F.signed(r.average_trade, 4) },
          ], report.by_market || [], 'No trades yet'))}
        </div>`;

      draw('an-daily', Charts.bars, {
        values: (p.daily || []).map((d) => d.pnl),
        xLabels: (p.daily || []).map((d) => d.period.slice(5)),
        emptyText: 'No realised PnL yet',
      });
      draw('an-dist', Charts.histogram, {
        counts: (report.distribution || {}).counts || [],
        bins: (report.distribution || {}).bins || [],
        xLabels: ((report.distribution || {}).bins || []).map((b) => F.num(b, 2)),
        emptyText: 'No trades to distribute',
      });
    },
  };

  /* ---------------------------- Charts ---------------------------- */
  PANELS.charts = {
    label: 'Charts', icon: '◫', group: 'Insight',
    render(root) {
      root.innerHTML = `<div class="grid g2">
        ${card('Equity curve', '<canvas class="chart tall" id="c-equity"></canvas>')}
        ${card('Portfolio growth (%)', '<canvas class="chart tall" id="c-growth"></canvas>')}
        ${card('Balance history (cash)', '<canvas class="chart" id="c-balance"></canvas>')}
        ${card('Drawdown', '<canvas class="chart" id="c-dd"></canvas>')}
        ${card('Rolling win rate (20 trades)', '<canvas class="chart" id="c-winrate"></canvas>')}
        ${card('Trade timeline', '<canvas class="chart" id="c-timeline"></canvas>')}
        ${card('Daily PnL', '<canvas class="chart" id="c-daily"></canvas>')}
        ${card('Weekly PnL', '<canvas class="chart" id="c-weekly"></canvas>')}
        ${card('Monthly PnL', '<canvas class="chart" id="c-monthly"></canvas>')}
        ${card('Trade distribution', '<canvas class="chart" id="c-dist"></canvas>')}
        ${card('Market performance', '<canvas class="chart" id="c-markets"></canvas>')}
        ${card('Strategy performance', '<canvas class="chart" id="c-strats"></canvas>')}
        ${card('Order flow (last 60 orders)', '<canvas class="chart" id="c-flow"></canvas>')}
        ${card('Portfolio allocation', '<canvas class="chart" id="c-alloc"></canvas>')}
      </div>`;
      this.load();
    },
    async load() {
      try {
        const [analytics, equity, orders] = await Promise.all([
          api(`/api/engine/${S.mode}/analytics`),
          api(`/api/engine/${S.mode}/equity?limit=3000`),
          api(`/api/engine/${S.mode}/orders?limit=60`),
        ]);
        S.detail.analytics = analytics;
        S.detail.equity = equity.equity || [];
        S.detail.flowOrders = orders.orders || [];
      } catch (error) { toast(error.message, 'err'); return; }
      this.paint();
    },
    tick() { if (!this._last || Date.now() - this._last > 12000) { this._last = Date.now(); this.load(); } },
    paint() {
      const report = S.detail.analytics;
      const equity = S.detail.equity || [];
      if (!report) return;
      const engine = engineOf();
      const labels = equity.map((r) => Charts.fmtTime(r.ts));
      const values = equity.map((r) => r.equity);
      const start = values.length ? values[0] : 0;

      draw('c-equity', Charts.line, {
        series: [{ values, label: 'Equity', fill: true }], xLabels: labels,
        emptyText: 'No equity samples yet',
      });
      draw('c-growth', Charts.line, {
        series: [{ values: values.map((v) => (start ? (v / start - 1) * 100 : 0)), label: 'Growth %', fill: true }],
        xLabels: labels, baseline: 0, formatY: (v) => v.toFixed(1) + '%',
        emptyText: 'No equity samples yet',
      });
      draw('c-balance', Charts.line, {
        series: [{ values: equity.map((r) => r.cash), label: 'Cash', fill: true, color: 'var(--c-accent-2)' }],
        xLabels: labels, emptyText: 'No balance samples yet',
      });
      draw('c-dd', Charts.line, {
        series: [{ values: (report.equity.drawdown_series || []).map((d) => d.drawdown_pct),
                   label: 'Drawdown %', fill: true, color: 'var(--c-down)' }],
        xLabels: (report.equity.drawdown_series || []).map((d) => Charts.fmtTime(d.ts)),
        baseline: 0, formatY: (v) => v.toFixed(1) + '%', emptyText: 'No drawdown yet',
      });
      draw('c-winrate', Charts.line, {
        series: [{ values: (report.win_rate_history || []).map((w) => w.win_rate), label: 'Win rate %' }],
        xLabels: (report.win_rate_history || []).map((w) => '#' + w.trade),
        formatY: (v) => v.toFixed(0) + '%', emptyText: 'Not enough trades yet',
      });
      draw('c-timeline', Charts.timeline, {
        points: (S.detail.analyticsTrades || []).length ? S.detail.analyticsTrades
          : (engine.recent_trades || []).map((t) => ({ ts: t.exit_ts, value: t.pnl })),
        emptyText: 'No trades yet',
      });
      draw('c-daily', Charts.bars, {
        values: (report.pnl.daily || []).map((d) => d.pnl),
        xLabels: (report.pnl.daily || []).map((d) => d.period.slice(5)), emptyText: 'No daily PnL yet',
      });
      draw('c-weekly', Charts.bars, {
        values: (report.pnl.weekly || []).map((d) => d.pnl),
        xLabels: (report.pnl.weekly || []).map((d) => d.period.slice(5)), emptyText: 'No weekly PnL yet',
      });
      draw('c-monthly', Charts.bars, {
        values: (report.pnl.monthly || []).map((d) => d.pnl),
        xLabels: (report.pnl.monthly || []).map((d) => d.period), emptyText: 'No monthly PnL yet',
      });
      draw('c-dist', Charts.histogram, {
        counts: report.distribution.counts, bins: report.distribution.bins,
        xLabels: (report.distribution.bins || []).map((b) => F.num(b, 2)), emptyText: 'No trades yet',
      });
      draw('c-markets', Charts.hbars, {
        items: (report.by_market || []).map((m) => ({ label: String(m.name).slice(0, 18), value: m.net_pnl })),
        emptyText: 'No trades yet',
      });
      draw('c-strats', Charts.hbars, {
        items: (report.by_strategy || []).map((m) => ({ label: m.name, value: m.net_pnl })),
        emptyText: 'No trades yet',
      });

      const flow = (S.detail.flowOrders || []).slice().reverse();
      draw('c-flow', Charts.bars, {
        values: flow.map((o) => (o.action === 'BUY' ? 1 : -1) * (o.price * o.size)),
        xLabels: flow.map((o) => F.time(o.ts_created)),
        emptyText: 'No orders yet',
      });
      const positions = engine.positions || [];
      draw('c-alloc', Charts.donut, {
        items: positions.map((p) => ({
          label: `${(p.market_label || p.market_id || '').slice(0, 12)} ${p.side}`,
          value: Math.abs(p.qty * (p.mark_price || p.avg_price)),
        })).concat(engine.cash > 0 ? [{ label: 'Cash', value: engine.cash }] : []),
        emptyText: 'Nothing allocated',
      });
    },
  };

  /* ---------------------------- Journal ---------------------------- */
  PANELS.journal = {
    label: 'Journal', icon: '✎', group: 'Insight',
    render(root) {
      root.innerHTML = `
        <div class="card" style="margin-bottom:var(--gap)">
          <div class="control-row">
            <input id="journal-search" placeholder="Search notes, tags, markets…" style="max-width:280px" value="${esc(S.filters.journalSearch)}">
            <button class="btn sm" data-act="journal-search">Search</button>
            <div style="flex:1"></div>
            <span class="hint" id="journal-stats"></span>
            ${can('export') ? `<a class="btn sm" href="/api/export/${S.mode}/journal?format=csv">Export CSV</a>` : ''}
          </div>
        </div>
        <div id="journal-root"><div class="card"><div class="table-empty">Loading…</div></div></div>`;
      $('[data-act="journal-search"]').onclick = () => {
        S.filters.journalSearch = $('#journal-search').value.trim();
        this.load();
      };
      $('#journal-search').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { S.filters.journalSearch = e.target.value.trim(); this.load(); }
      });
      this.load();
    },
    async load() {
      try {
        S.detail.journal = await api(
          `/api/journal/${S.mode}?limit=100&search=${encodeURIComponent(S.filters.journalSearch)}`);
      } catch (error) { toast(error.message, 'err'); return; }
      this.paint();
    },
    tick() { /* form inputs live here — never auto re-render */ },
    paint() {
      const data = S.detail.journal || { entries: [], stats: {} };
      const stats = data.stats || {};
      const statsEl = $('#journal-stats');
      if (statsEl) {
        statsEl.textContent = `${stats.total || 0} entries · ${stats.annotated || 0} annotated · ` +
          `${stats.with_screenshots || 0} with screenshots` +
          (stats.average_rating ? ` · avg rating ${stats.average_rating}` : '');
      }
      const host = $('#journal-root');
      if (!data.entries.length) {
        host.innerHTML = `<div class="card"><div class="table-empty">
          No journal entries yet — they are created automatically when a trade closes.</div></div>`;
        return;
      }
      host.innerHTML = `<div class="grid g-wide">${data.entries.map(journalEntry).join('')}</div>`;
      $$('#journal-root [data-entry]').forEach(bindJournalEntry);
    },
  };

  function journalEntry(entry) {
    const editable = can('journal.write');
    return `<div class="journal-entry" data-entry="${entry.id}">
      <div class="row">
        <strong>${esc(entry.market_label || (entry.market_id || '').slice(0, 16))}</strong>
        ${pill(entry.side)}
        <span class="pill">${esc(entry.strategy || '—')}</span>
        <div style="flex:1"></div>
        <span class="${F.cls(entry.pnl)} mono">${F.signed(entry.pnl, 4)}</span>
      </div>
      <div class="row hint mono">
        ${F.datetime(entry.entry_ts)} → ${F.datetime(entry.exit_ts)} ·
        ${F.num(entry.qty, 0)} @ ${F.price(entry.entry_price)} → ${F.price(entry.exit_price)} ·
        held ${F.dur(entry.duration_s)}
      </div>
      <div class="row">
        <div style="flex:1;min-width:180px">
          <label class="hint">Entry reason</label>
          <input data-field="entry_reason" value="${esc(entry.entry_reason || '')}" ${editable ? '' : 'disabled'}>
        </div>
        <div style="flex:1;min-width:180px">
          <label class="hint">Exit reason</label>
          <input data-field="exit_reason" value="${esc(entry.exit_reason || '')}" ${editable ? '' : 'disabled'}>
        </div>
      </div>
      <div>
        <label class="hint">Notes</label>
        <textarea data-field="notes" ${editable ? '' : 'disabled'}>${esc(entry.notes || '')}</textarea>
      </div>
      <div class="row">
        <div style="min-width:150px">
          <label class="hint">Confidence (0–1)</label>
          <input data-field="confidence" type="number" min="0" max="1" step="0.05"
                 value="${entry.confidence ?? ''}" ${editable ? '' : 'disabled'}>
        </div>
        <div style="min-width:150px">
          <label class="hint">Tags</label>
          <input data-field="tags" value="${esc(entry.tags || '')}" ${editable ? '' : 'disabled'}>
        </div>
        <div>
          <label class="hint">Performance rating</label>
          <div class="stars" data-rating="${entry.rating || 0}">
            ${[1, 2, 3, 4, 5].map((n) => `<button data-star="${n}" class="${(entry.rating || 0) >= n ? 'on' : ''}" ${editable ? '' : 'disabled'}>★</button>`).join('')}
          </div>
        </div>
        <div style="flex:1"></div>
        ${editable ? '<button class="btn sm primary" data-act="save">Save</button>' : ''}
      </div>
      ${editable ? `<div class="row">
        <label class="btn sm">Entry screenshot<input type="file" accept="image/*" data-shot="entry_shot" hidden></label>
        <label class="btn sm">Exit screenshot<input type="file" accept="image/*" data-shot="exit_shot" hidden></label>
      </div>` : ''}
      <div class="row">
        ${entry.entry_shot ? `<img class="shot" style="max-width:46%" src="/api/journal/screenshot/${esc(entry.entry_shot)}" alt="entry screenshot">` : ''}
        ${entry.exit_shot ? `<img class="shot" style="max-width:46%" src="/api/journal/screenshot/${esc(entry.exit_shot)}" alt="exit screenshot">` : ''}
      </div>
    </div>`;
  }

  function bindJournalEntry(node) {
    const id = node.dataset.entry;
    const stars = $('.stars', node);
    if (stars) {
      $$('button[data-star]', stars).forEach((button) => {
        button.onclick = () => {
          const value = Number(button.dataset.star);
          stars.dataset.rating = value;
          $$('button[data-star]', stars).forEach((b) =>
            b.classList.toggle('on', Number(b.dataset.star) <= value));
        };
      });
    }
    const save = $('[data-act="save"]', node);
    if (save) {
      save.onclick = async () => {
        const body = {};
        $$('[data-field]', node).forEach((input) => {
          const value = input.value.trim();
          if (input.dataset.field === 'confidence') {
            if (value !== '') body.confidence = Number(value);
          } else body[input.dataset.field] = value;
        });
        const rating = Number(stars && stars.dataset.rating);
        if (rating) body.rating = rating;
        save.disabled = true;
        try {
          await api(`/api/journal/entry/${id}`, { method: 'PATCH', body });
          toast('Journal entry saved');
        } catch (error) { toast(error.message, 'err'); }
        finally { save.disabled = false; }
      };
    }
    $$('[data-shot]', node).forEach((input) => {
      input.onchange = async () => {
        const file = input.files && input.files[0];
        if (!file) return;
        if (file.size > 2 * 1024 * 1024) { toast('Screenshot must be under 2 MB', 'err'); return; }
        const reader = new FileReader();
        reader.onload = async () => {
          try {
            await api(`/api/journal/entry/${id}/screenshot`, {
              method: 'POST', body: { slot: input.dataset.shot, data_url: reader.result },
            });
            toast('Screenshot attached');
            PANELS.journal.load();
          } catch (error) { toast(error.message, 'err'); }
        };
        reader.readAsDataURL(file);
      };
    });
  }

  /* ---------------------------- Logs ---------------------------- */
  PANELS.logs = {
    label: 'Logs', icon: '❐', group: 'Operations', perm: 'logs.view',
    render(root) {
      root.innerHTML = `
        <div class="card" style="margin-bottom:var(--gap)">
          <div class="control-row">
            <select id="log-file" style="max-width:200px"></select>
            <select id="log-lines" style="max-width:120px">
              <option value="200">200 lines</option><option value="500">500 lines</option>
              <option value="1000">1000 lines</option><option value="2000">2000 lines</option>
            </select>
            <button class="btn sm" data-act="log-refresh">Refresh</button>
            <label class="hint"><input type="checkbox" id="log-follow" style="width:auto" checked> auto-refresh</label>
            <div style="flex:1"></div>
            <button class="btn sm" data-act="journalctl">systemd journal</button>
          </div>
        </div>
        <div class="card"><pre class="logs" id="log-body">Loading…</pre></div>`;
      $('[data-act="log-refresh"]').onclick = () => this.load();
      $('[data-act="journalctl"]').onclick = () => this.loadJournal();
      $('#log-file').onchange = (e) => { S.filters.logFile = e.target.value; this.load(); };
      this.load();
    },
    async load() {
      const lines = ($('#log-lines') || {}).value || 300;
      try {
        const data = await api(
          `/api/system/logs?mode=${S.mode}&file=${encodeURIComponent(S.filters.logFile)}&lines=${lines}`);
        const select = $('#log-file');
        if (select) {
          select.innerHTML = (data.files || []).map((f) =>
            `<option value="${esc(f.name)}" ${f.name === data.file ? 'selected' : ''}>${esc(f.name)} (${F.bytes(f.bytes)})</option>`
          ).join('') || `<option value="${esc(S.filters.logFile)}">${esc(S.filters.logFile)}</option>`;
        }
        paintLog(data.lines, `${S.mode}/${data.file}`);
      } catch (error) { toast(error.message, 'err'); }
    },
    async loadJournal() {
      try {
        const data = await api(`/api/system/journal?target=${S.mode}&lines=400`);
        paintLog(data.lines, `journalctl ${S.mode}`);
      } catch (error) { toast(error.message, 'err'); }
    },
    tick() {
      const follow = $('#log-follow');
      if (follow && follow.checked && (!this._last || Date.now() - this._last > 5000)) {
        this._last = Date.now();
        this.load();
      }
    },
  };

  function paintLog(lines, title) {
    const body = $('#log-body');
    if (!body) return;
    if (!lines || !lines.length) { body.textContent = `(${title}: no output)`; return; }
    body.innerHTML = lines.map((line) => {
      const level = /\b(CRITICAL|ERROR)\b/.test(line) ? 'lvl-critical'
        : /\bWARNING\b/.test(line) ? 'lvl-warning' : '';
      return `<span class="${level}">${esc(line)}</span>`;
    }).join('\n');
    const wrapper = body.parentElement;
    if (wrapper) body.scrollTop = body.scrollHeight;
  }

  /* ---------------------------- System ---------------------------- */
  PANELS.system = {
    label: 'System', icon: '▥', group: 'Operations',
    render(root) { root.innerHTML = '<div id="system-root"></div>'; this.tick(root); },
    tick() {
      const host = $('#system-root');
      if (!host || !S.snap) return;
      const vps = S.snap.vps || {};
      const cpu = vps.cpu || {}, memory = vps.memory || {}, disk = vps.disk || {};
      const network = vps.network || {}, uptime = vps.uptime || {}, storage = vps.storage || {};
      const process = vps.process || {};
      const dashboard = S.snap.dashboard || {};

      host.innerHTML = `
        <div class="grid g4" style="margin-bottom:var(--gap)">
          ${kpi('CPU', F.pct(cpu.percent, 1), `${cpu.cores} vCPU · load ${cpu.load_1m}`, cpu.percent > 85 ? 'down' : '')}
          ${kpi('Memory', F.pct(memory.percent, 1), `${F.bytes(memory.used)} of ${F.bytes(memory.total)}`, memory.percent > 88 ? 'down' : '')}
          ${kpi('Disk', F.pct(disk.percent, 1), `${F.bytes(disk.free)} free`, disk.percent > 90 ? 'down' : '')}
          ${kpi('VPS uptime', F.dur(uptime.vps_uptime_s), `dashboard ${F.dur(uptime.dashboard_uptime_s)}`)}
        </div>

        <div class="grid g2" style="margin-bottom:var(--gap)">
          ${card('Services', table([
            { label: 'Service', render: (s) => esc(s.unit) },
            { label: 'State', render: (s) => pill(s.available ? s.active : 'not installed') },
            { label: 'Sub', render: (s) => esc(s.sub || '—') },
            { label: 'Enabled', render: (s) => esc(s.enabled || '—') },
            { label: 'PID', num: true, render: (s) => s.pid || '—' },
            { label: 'Restarts', num: true, render: (s) => F.int(s.restarts) },
            { label: 'Memory', num: true, render: (s) => s.memory_bytes ? F.bytes(s.memory_bytes) : '—' },
            { label: 'Uptime', num: true, render: (s) => s.uptime_s ? F.dur(s.uptime_s) : '—' },
          ], vps.services || [], 'No services found'))}

          ${card('Network', `<div class="stats">
            ${stat('Received', F.bytes(network.bytes_recv))}
            ${stat('Sent', F.bytes(network.bytes_sent))}
            ${stat('RX rate', F.bytes(network.rx_bytes_per_s) + '/s')}
            ${stat('TX rate', F.bytes(network.tx_bytes_per_s) + '/s')}
            ${stat('Errors in/out', `${F.int(network.errors_in)} / ${F.int(network.errors_out)}`)}
            ${stat('Drops in/out', `${F.int(network.drop_in)} / ${F.int(network.drop_out)}`)}
            ${stat('Connections', network.connections === -1 ? 'n/a' : F.int(network.connections))}
          </div>`)}
        </div>

        <div class="grid g2">
          ${card('Storage', `<div class="stats">
            ${stat('Paper database', F.bytes(storage.paper_db_bytes))}
            ${stat('Live database', F.bytes(storage.live_db_bytes))}
            ${stat('Dashboard database', F.bytes(storage.dashboard_db_bytes))}
            ${stat('Paper logs', F.bytes(storage.paper_log_bytes))}
            ${stat('Live logs', F.bytes(storage.live_log_bytes))}
            ${stat('Backups', F.bytes(storage.backup_bytes))}
            ${stat('Total tracked', F.bytes(storage.total_bytes))}
            ${stat('Data root', `<span title="${esc(storage.data_root)}">${esc((storage.data_root || '').split('/').pop())}</span>`)}
          </div>`)}

          ${card('Dashboard process', `<div class="stats">
            ${stat('PID', process.pid || '—')}
            ${stat('CPU', F.pct(process.cpu_percent, 1))}
            ${stat('Memory', F.bytes(process.memory_rss))}
            ${stat('Threads', F.int(process.threads))}
            ${stat('Open files', F.int(process.open_files))}
            ${stat('Connected clients', F.int(dashboard.clients))}
            ${stat('Broadcasts', F.int(dashboard.broadcasts))}
            ${stat('Frames sent', F.int(dashboard.messages_sent))}
          </div>`)}
        </div>`;
    },
  };

  /* ---------------------------- Notifications ---------------------------- */
  PANELS.notifications = {
    label: 'Alerts', icon: '⚑', group: 'Operations',
    render(root) {
      root.innerHTML = `
        <div class="card" style="margin-bottom:var(--gap)">
          <div class="control-row">
            <button class="btn sm" data-act="notif-read">Mark all read</button>
            <button class="btn sm" data-act="notif-browser">Enable browser notifications</button>
            ${can('notify.manage') ? '<button class="btn sm" data-act="notif-test">Send test alert</button>' : ''}
            <div style="flex:1"></div>
            <span class="hint" id="notif-channels"></span>
          </div>
        </div>
        <div id="notif-root"></div>`;
      $('[data-act="notif-read"]').onclick = async () => {
        await api('/api/notifications/read', { method: 'POST', body: {} });
        this.load();
      };
      $('[data-act="notif-browser"]').onclick = async () => {
        if (!('Notification' in window)) { toast('This browser has no notification API', 'err'); return; }
        const permission = await Notification.requestPermission();
        S.browserNotifications = permission === 'granted';
        localStorage.setItem('polybot.browserNotify', S.browserNotifications ? '1' : '0');
        toast(S.browserNotifications ? 'Browser notifications enabled'
                                     : 'Browser notifications blocked', S.browserNotifications ? 'ok' : 'warn');
      };
      const test = $('[data-act="notif-test"]');
      if (test) test.onclick = async () => {
        try {
          const result = await api('/api/notifications/test', { method: 'POST', body: {} });
          toast('Test sent via: ' + (result.delivered || []).join(', '),
                Object.keys(result.errors || {}).length ? 'warn' : 'ok', 9000);
        } catch (error) { toast(error.message, 'err'); }
      };
      this.load();
    },
    async load() {
      try { S.detail.notifications = await api('/api/notifications?limit=200'); }
      catch (error) { toast(error.message, 'err'); return; }
      this.paint();
    },
    tick() { if (!this._last || Date.now() - this._last > 10000) { this._last = Date.now(); this.load(); } },
    paint() {
      const data = S.detail.notifications || { notifications: [], channels: {} };
      const channels = data.channels || {};
      const channelsEl = $('#notif-channels');
      if (channelsEl) {
        channelsEl.textContent = ['browser', 'email', 'telegram', 'discord']
          .map((name) => {
            const channel = channels[name] || {};
            return `${name}: ${channel.enabled ? (channel.error ? 'error' : 'on') : 'off'}`;
          }).join(' · ') + ` · min severity ${channels.min_severity || '—'}`;
      }
      $('#notif-root').innerHTML = card('Alert history',
        (data.notifications || []).length
          ? data.notifications.map((n) => `<div class="notif ${esc(n.severity)} ${n.read ? '' : 'unread'}">
              <div class="sev"></div>
              <div class="body">
                <div class="title">${esc(n.title)}</div>
                ${n.body ? `<div class="msg">${esc(n.body)}</div>` : ''}
                <div class="meta">${esc(n.kind)} · ${esc(n.mode || 'system')} · ${F.datetime(n.ts)}${n.delivered ? ' · sent: ' + esc(n.delivered) : ''}</div>
              </div></div>`).join('')
          : '<div class="table-empty">No alerts recorded</div>');
    },
  };

  /* ---------------------------- Controls ---------------------------- */
  PANELS.controls = {
    label: 'Controls', icon: '⚙', group: 'Operations',
    render(root) {
      root.innerHTML = '<div id="controls-root"></div>';
      this.tick(root);
    },
    tick() {
      const host = $('#controls-root');
      if (!host || !S.snap) return;
      // Avoid clobbering a reason the user is mid-typing.
      const typed = $('#stop-reason') ? $('#stop-reason').value : '';
      const engines = S.snap.engines || {};
      const controlEnabled = (S.snap.dashboard || {}).control_enabled;

      host.innerHTML = `
        ${!controlEnabled ? '<div class="card confirm-danger" style="margin-bottom:var(--gap)">Service control is disabled in the dashboard configuration (DASHBOARD_CONTROL_ENABLED=false).</div>' : ''}
        <div class="grid g3" style="margin-bottom:var(--gap)">
          ${serviceControlCard('paper', engines.paper || {}, 'control.paper')}
          ${serviceControlCard('live', engines.live || {}, 'control.live')}
          ${card('Dashboard', `
            <div class="stats" style="margin-bottom:12px">
              ${stat('Clients', F.int((S.snap.dashboard || {}).clients))}
              ${stat('Uptime', F.dur(S.snap.uptime_s))}
              ${stat('Live sync', (S.snap.dashboard || {}).live_sync_enabled ? 'enabled' : 'disabled')}
            </div>
            <div class="control-row">
              <button class="btn" data-svc="dashboard" data-action="restart" ${can('control.dashboard') ? '' : 'disabled'}>Restart dashboard</button>
              <button class="btn" data-act="reconnect" data-target="live_api">Restart live API client</button>
              <button class="btn" data-act="reconnect" data-target="live_websocket">Restart websocket</button>
              <button class="btn" data-act="reconnect" data-target="market_data">Refresh market data</button>
            </div>`)}
        </div>

        <div class="grid g2" style="margin-bottom:var(--gap)">
          ${card('Emergency stop — LIVE trading', `
            <p class="hint">Writes the kill switch so the running engine cancels every open order,
              then stops the service. Use this first; do not simply stop the unit, which would leave
              resting orders on the book.</p>
            <input id="stop-reason" placeholder="Reason (recorded in the audit log)" value="${esc(typed)}" style="margin-bottom:10px">
            <div class="control-row">
              <button class="btn danger" data-act="emergency" data-mode="live" ${can('emergency') ? '' : 'disabled'}>EMERGENCY STOP LIVE</button>
              <button class="btn warn" data-act="emergency" data-mode="paper" ${can('emergency') ? '' : 'disabled'}>Stop paper</button>
              <button class="btn" data-act="clear-kill" data-mode="live" ${can('emergency') ? '' : 'disabled'}>Clear live kill switch</button>
              <button class="btn" data-act="clear-kill" data-mode="paper" ${can('emergency') ? '' : 'disabled'}>Clear paper kill switch</button>
            </div>
            <div class="hint" style="margin-top:10px">
              Live kill switch: ${(engines.live || {}).kill_switch ? '<span class="down">ARMED</span>' : 'clear'} ·
              Paper: ${(engines.paper || {}).kill_switch ? '<span class="down">ARMED</span>' : 'clear'}
            </div>`)}

          ${card('Data & backups', `
            <div class="control-row" style="margin-bottom:10px">
              <button class="btn" data-act="backup" data-scope="all" ${can('backup') ? '' : 'disabled'}>Back up all databases</button>
              <button class="btn" data-act="backup" data-scope="dashboard" ${can('backup') ? '' : 'disabled'}>Dashboard only</button>
            </div>
            ${can('export') ? `<div class="control-row">
              <a class="btn sm" href="/api/export/${S.mode}/trades?format=csv">Export trades</a>
              <a class="btn sm" href="/api/export/${S.mode}/orders?format=csv">Export orders</a>
              <a class="btn sm" href="/api/export/${S.mode}/journal?format=csv">Export journal</a>
            </div>` : ''}
            <div id="backup-list" class="hint" style="margin-top:12px"></div>`)}
        </div>`;

      $$('#controls-root [data-svc]').forEach((button) => {
        button.onclick = () => serviceAction(button.dataset.svc, button.dataset.action);
      });
      $$('#controls-root [data-act="reconnect"]').forEach((button) => {
        button.onclick = async () => {
          button.disabled = true;
          try {
            const result = await api('/api/control/reconnect', {
              method: 'POST', body: { target: button.dataset.target } });
            toast(result.message);
          } catch (error) { toast(error.message, 'err'); }
          finally { button.disabled = false; }
        };
      });
      $$('#controls-root [data-act="emergency"]').forEach((button) => {
        button.onclick = () => {
          const mode = button.dataset.mode;
          const reason = ($('#stop-reason') || {}).value || '';
          confirmAction(`Emergency stop — ${mode}`,
            `This cancels every open ${mode} order and stops the engine.` +
            (mode === 'live' ? ' <strong>This affects real money.</strong>' : ''),
            async () => {
              try {
                const result = await api('/api/control/emergency-stop', {
                  method: 'POST', body: { mode, reason } });
                toast(result.message, result.ok ? 'warn' : 'err', 10000);
              } catch (error) { toast(error.message, 'err'); }
            }, mode === 'live');
        };
      });
      $$('#controls-root [data-act="clear-kill"]').forEach((button) => {
        button.onclick = async () => {
          try {
            const result = await api('/api/control/clear-kill-switch', {
              method: 'POST', body: { mode: button.dataset.mode } });
            toast(result.message, result.ok ? 'ok' : 'err');
          } catch (error) { toast(error.message, 'err'); }
        };
      });
      $$('#controls-root [data-act="backup"]').forEach((button) => {
        button.onclick = async () => {
          button.disabled = true;
          try {
            const result = await api('/api/control/backup', {
              method: 'POST', body: { scope: button.dataset.scope } });
            toast(result.message, result.ok ? 'ok' : 'err');
            PANELS.controls.loadBackups();
          } catch (error) { toast(error.message, 'err'); }
          finally { button.disabled = false; }
        };
      });
      if (can('backup') && (!this._backupsAt || Date.now() - this._backupsAt > 20000)) {
        this._backupsAt = Date.now();
        this.loadBackups();
      }
    },
    async loadBackups() {
      try {
        const data = await api('/api/control/backups');
        const host = $('#backup-list');
        if (!host) return;
        host.innerHTML = (data.backups || []).length
          ? 'Recent backups: ' + data.backups.slice(0, 6).map((b) =>
              `${esc(b.name)} (${F.bytes(b.bytes)})`).join(' · ')
          : 'No backups yet.';
      } catch (_) { /* permission or transient error: leave the panel as-is */ }
    },
  };

  function serviceControlCard(mode, engine, permission) {
    const service = engine.service || {};
    const allowed = can(permission);
    return card(`${mode} engine`, `
      <div class="stats" style="margin-bottom:12px">
        ${stat('State', service.available ? pill(service.active) : pill('not installed'))}
        ${stat('Engine', engine.live ? '<span class="up">reporting</span>' : '<span class="down">silent</span>')}
        ${stat('Uptime', service.uptime_s ? F.dur(service.uptime_s) : '—')}
        ${stat('Restarts', F.int(service.restarts))}
        ${stat('Memory', service.memory_bytes ? F.bytes(service.memory_bytes) : '—')}
        ${stat('Open orders', F.int((engine.orders || {}).open))}
      </div>
      <div class="control-row">
        <button class="btn" data-svc="${mode}" data-action="start" ${allowed ? '' : 'disabled'}>Start</button>
        <button class="btn" data-svc="${mode}" data-action="stop" ${allowed ? '' : 'disabled'}>Stop</button>
        <button class="btn" data-svc="${mode}" data-action="restart" ${allowed ? '' : 'disabled'}>Restart</button>
      </div>
      ${!allowed ? '<div class="hint" style="margin-top:8px">Your role cannot control this engine.</div>' : ''}`);
  }

  function serviceAction(target, action) {
    const run = async () => {
      try {
        const result = await api('/api/control/service', { method: 'POST', body: { target, action } });
        toast(result.message, result.ok ? 'ok' : 'err', result.ok ? 5000 : 12000);
      } catch (error) { toast(error.message, 'err', 10000); }
    };
    if (target === 'live' && action !== 'stop') {
      confirmAction('Start live trading engine',
        'The live engine trades with <strong>real funds</strong>. Continue?', run, true);
    } else run();
  }

  /* ---------------------------- Admin ---------------------------- */
  PANELS.admin = {
    label: 'Admin', icon: '⚿', group: 'Operations', perm: 'users.manage',
    render(root) {
      root.innerHTML = '<div id="admin-root"><div class="card"><div class="table-empty">Loading…</div></div></div>';
      this.load();
    },
    async load() {
      try {
        const [users, audit] = await Promise.all([api('/api/users'), api('/api/audit?limit=200')]);
        S.detail.users = users;
        S.detail.audit = audit.audit || [];
      } catch (error) { toast(error.message, 'err'); return; }
      this.paint();
    },
    tick() { /* contains forms — refresh only on demand */ },
    paint() {
      const users = (S.detail.users || {}).users || [];
      $('#admin-root').innerHTML = `
        ${card('Users', table([
          { label: 'User', render: (u) => esc(u.username) },
          { label: 'Role', render: (u) => `<select data-user-role="${u.id}" style="width:auto">
              ${['admin', 'trader', 'viewer'].map((r) => `<option ${u.role === r ? 'selected' : ''}>${r}</option>`).join('')}
            </select>` },
          { label: 'Active', render: (u) => `<button class="btn sm ${u.active ? '' : 'warn'}" data-user-toggle="${u.id}" data-active="${u.active}">${u.active ? 'active' : 'disabled'}</button>` },
          { label: 'Created', render: (u) => F.datetime(u.created_ts) },
          { label: 'Last login', render: (u) => u.last_login_ts ? F.ago(u.last_login_ts) : 'never' },
          { label: '', render: (u) => `<button class="btn sm danger" data-user-delete="${u.id}" data-name="${esc(u.username)}">Delete</button>` },
        ], users, 'No users'),
        '<button class="btn sm primary" data-act="new-user">Add user</button>')}

        <div style="height:var(--gap)"></div>
        ${card('Audit log', table([
          { label: 'When', render: (a) => F.datetime(a.ts) },
          { label: 'Actor', render: (a) => esc(a.actor) },
          { label: 'Role', render: (a) => esc(a.role) },
          { label: 'Action', render: (a) => esc(a.action) },
          { label: 'Target', render: (a) => esc(a.target || '—') },
          { label: 'Result', render: (a) => pill(a.ok ? 'ok' : 'failed') },
          { label: 'IP', render: (a) => esc(a.ip || '—') },
          { label: 'Detail', render: (a) => `<span class="faint" title="${esc(a.detail || '')}">${esc((a.detail || '').slice(0, 50))}</span>` },
        ], S.detail.audit || [], 'No audit entries'))}`;

      $('[data-act="new-user"]').onclick = () => {
        modal(`<h3>Add user</h3>
          <label class="hint">Username</label><input id="nu-name">
          <label class="hint">Password (12+ chars, 3 character classes)</label><input id="nu-pass" type="password">
          <label class="hint">Role</label>
          <select id="nu-role"><option>viewer</option><option>trader</option><option>admin</option></select>
          <div class="error-text" id="nu-err"></div>
          <div class="control-row" style="justify-content:flex-end">
            <button class="btn" data-act="cancel">Cancel</button>
            <button class="btn primary" data-act="ok">Create</button></div>`);
        $('#modal-body [data-act="cancel"]').onclick = closeModal;
        $('#modal-body [data-act="ok"]').onclick = async () => {
          try {
            await api('/api/users', { method: 'POST', body: {
              username: $('#nu-name').value.trim(), password: $('#nu-pass').value,
              role: $('#nu-role').value } });
            closeModal(); toast('User created'); PANELS.admin.load();
          } catch (error) { $('#nu-err').textContent = error.message; }
        };
      };

      $$('[data-user-role]').forEach((select) => {
        select.onchange = async () => {
          try {
            await api(`/api/users/${select.dataset.userRole}`, {
              method: 'PATCH', body: { role: select.value } });
            toast('Role updated'); PANELS.admin.load();
          } catch (error) { toast(error.message, 'err'); PANELS.admin.load(); }
        };
      });
      $$('[data-user-toggle]').forEach((button) => {
        button.onclick = async () => {
          try {
            await api(`/api/users/${button.dataset.userToggle}`, {
              method: 'PATCH', body: { active: button.dataset.active !== '1' } });
            PANELS.admin.load();
          } catch (error) { toast(error.message, 'err'); }
        };
      });
      $$('[data-user-delete]').forEach((button) => {
        button.onclick = () => confirmAction('Delete user',
          `Permanently delete <strong>${esc(button.dataset.name)}</strong> and revoke their sessions?`,
          async () => {
            try {
              await api(`/api/users/${button.dataset.userDelete}`, { method: 'DELETE' });
              toast('User deleted'); PANELS.admin.load();
            } catch (error) { toast(error.message, 'err'); }
          }, true);
      });
    },
  };

  /* ================================================================== *
   * Navigation
   * ================================================================== */
  const ORDER = ['overview', 'wallet', 'markets', 'strategies', 'orders', 'trades',
                 'analytics', 'charts', 'journal', 'logs', 'system', 'notifications',
                 'controls', 'admin'];

  function buildNav() {
    const nav = $('#nav');
    let lastGroup = null;
    nav.innerHTML = ORDER.filter((id) => {
      const panel = PANELS[id];
      return panel && (!panel.perm || can(panel.perm));
    }).map((id) => {
      const panel = PANELS[id];
      let prefix = '';
      if (panel.group !== lastGroup) { prefix = `<div class="nav-group">${panel.group}</div>`; lastGroup = panel.group; }
      return `${prefix}<button data-tab="${id}" class="${S.tab === id ? 'active' : ''}">
        <span class="ico">${panel.icon}</span><span>${panel.label}</span></button>`;
    }).join('');
    $$('#nav [data-tab]').forEach((button) => {
      button.onclick = () => go(button.dataset.tab);
    });
  }

  function go(tab) {
    if (!PANELS[tab] || (PANELS[tab].perm && !can(PANELS[tab].perm))) tab = 'overview';
    S.tab = tab;
    localStorage.setItem('polybot.tab', tab);
    $$('#nav [data-tab]').forEach((b) => b.classList.toggle('active', b.dataset.tab === tab));
    const main = $('#main');
    main.innerHTML = '';
    const panel = PANELS[tab];
    panel._last = 0;
    try { panel.render(main); } catch (error) { console.error(error); main.innerHTML =
      `<div class="card"><div class="table-empty">Panel failed to render: ${esc(error.message)}</div></div>`; }
    bindPanelActions();
  }

  function bindPanelActions() {
    const refreshMarkets = $('[data-act="refresh-markets"]');
    if (refreshMarkets) refreshMarkets.onclick = async () => {
      refreshMarkets.disabled = true;
      try {
        const result = await api('/api/markets/refresh', { method: 'POST', body: {} });
        toast(`Rediscovered ${result.tracked} market(s)`);
        PANELS.markets.load();
      } catch (error) { toast(error.message, 'err'); }
      finally { refreshMarkets.disabled = false; }
    };
    const refreshLive = $('[data-act="refresh-live"]');
    if (refreshLive) refreshLive.onclick = async () => {
      try { await api('/api/live/refresh', { method: 'POST', body: {} }); toast('Live account resynced'); }
      catch (error) { toast(error.message, 'err'); }
    };
  }

  /* Mode switch */
  $$('.mode-switch button').forEach((button) => {
    button.onclick = () => {
      S.mode = button.dataset.mode;
      localStorage.setItem('polybot.mode', S.mode);
      $$('.mode-switch button').forEach((b) => b.classList.toggle('active', b.dataset.mode === S.mode));
      S.detail = {};
      go(S.tab);
      updateChips();
    };
  });

  /* Theme */
  $('#btn-theme').onclick = () => {
    const root = document.documentElement;
    const light = root.classList.toggle('theme-light');
    root.classList.toggle('theme-dark', !light);
    localStorage.setItem('polybot.theme', light ? 'light' : 'dark');
    if (PANELS[S.tab].paint) PANELS[S.tab].paint();
    else PANELS[S.tab].tick($('#main'));
  };

  $('#btn-bell').onclick = () => go('notifications');

  $('#btn-user').onclick = () => {
    modal(`<h3>Account</h3>
      <div class="stats" style="margin-bottom:14px">
        ${stat('User', esc(S.user))}
        ${stat('Permissions', S.permissions.size + ' granted')}
      </div>
      <label class="hint">Current password</label><input id="pw-cur" type="password">
      <label class="hint">New password (12+ chars, 3 character classes)</label><input id="pw-new" type="password">
      <div class="error-text" id="pw-err"></div>
      <div class="control-row" style="justify-content:space-between">
        <button class="btn danger" data-act="logout">Sign out</button>
        <div>
          <button class="btn" data-act="cancel">Close</button>
          <button class="btn primary" data-act="ok">Change password</button>
        </div>
      </div>`);
    $('#modal-body [data-act="cancel"]').onclick = closeModal;
    $('#modal-body [data-act="logout"]').onclick = async () => {
      try { await api('/api/auth/logout', { method: 'POST', body: {} }); } catch (_) {}
      closeModal(); showLogin();
    };
    $('#modal-body [data-act="ok"]').onclick = async () => {
      try {
        await api('/api/auth/password', { method: 'POST', body: {
          current_password: $('#pw-cur').value, new_password: $('#pw-new').value } });
        closeModal();
        toast('Password changed — please sign in again', 'warn');
        showLogin();
      } catch (error) { $('#pw-err').textContent = error.message; }
    };
  };

  /* Redraw charts when the viewport changes size. */
  let resizeTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      const panel = PANELS[S.tab];
      if (panel && panel.paint) panel.paint();
      else if (panel && panel.tick) panel.tick($('#main'));
    }, 180);
  });

  /* ================================================================== *
   * Boot
   * ================================================================== */
  function start() {
    $$('.mode-switch button').forEach((b) => b.classList.toggle('active', b.dataset.mode === S.mode));
    buildNav();
    showApp();
    go(S.tab);
    connect();
    api('/api/snapshot').then(onSnapshot).catch(() => {});
  }

  (async function boot() {
    if (localStorage.getItem('polybot.theme') === 'light') {
      document.documentElement.classList.add('theme-light');
      document.documentElement.classList.remove('theme-dark');
    }
    try {
      const session = await api('/api/auth/me');
      applySession(session);
      start();
    } catch (_) {
      showLogin();
    }
  })();
})();

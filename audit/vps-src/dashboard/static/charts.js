/* Minimal canvas charting for the polybot dashboard.
 *
 * No external libraries: the page runs under a strict CSP that blocks every
 * outside origin, and a 2 vCPU VPS should not be shipping a megabyte of chart
 * framework to a phone. Everything here is 2D canvas, device-pixel-ratio aware,
 * and redraws only the charts whose data actually changed.
 *
 * Supported marks: line/area, bars (incl. diverging), histogram, donut,
 * horizontal bars, and a scatter timeline.
 */
(function (global) {
  'use strict';

  const CSS = getComputedStyle(document.documentElement);
  const readVar = (name, fallback) => (CSS.getPropertyValue(name) || '').trim() || fallback;

  const theme = () => ({
    text: readVar('--c-text-dim', '#8b97a8'),
    grid: readVar('--c-grid', 'rgba(255,255,255,0.06)'),
    axis: readVar('--c-border', 'rgba(255,255,255,0.12)'),
    up: readVar('--c-up', '#2ecc8f'),
    down: readVar('--c-down', '#f0616d'),
    accent: readVar('--c-accent', '#5b9dff'),
    accent2: readVar('--c-accent-2', '#b07bff'),
    warn: readVar('--c-warn', '#f5b83d'),
    surface: readVar('--c-surface', '#141a23'),
  });

  const PALETTE = () => {
    const t = theme();
    return [t.accent, t.up, t.warn, t.accent2, t.down, '#4fd1c5', '#f78fb3', '#9ab0c9'];
  };

  /** Prepare a canvas for crisp drawing; returns the 2D context and CSS size. */
  function prepare(canvas) {
    const dpr = Math.min(global.devicePixelRatio || 1, 2);
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(1, Math.round(rect.width));
    const height = Math.max(1, Math.round(canvas.clientHeight || rect.height || 180));
    if (canvas.width !== width * dpr || canvas.height !== height * dpr) {
      canvas.width = width * dpr;
      canvas.height = height * dpr;
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    return { ctx, width, height };
  }

  function niceScale(min, max, ticks) {
    if (!isFinite(min) || !isFinite(max)) return { min: 0, max: 1, step: 0.25 };
    if (min === max) { const pad = Math.abs(min) * 0.1 || 1; min -= pad; max += pad; }
    const range = max - min;
    const rough = range / Math.max(1, ticks);
    const mag = Math.pow(10, Math.floor(Math.log10(rough)));
    const norm = rough / mag;
    const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
    return { min: Math.floor(min / step) * step, max: Math.ceil(max / step) * step, step };
  }

  function fmtNumber(value, digits) {
    if (value === null || value === undefined || Number.isNaN(value)) return '—';
    const abs = Math.abs(value);
    if (abs >= 1e9) return (value / 1e9).toFixed(2) + 'B';
    if (abs >= 1e6) return (value / 1e6).toFixed(2) + 'M';
    if (abs >= 1e4) return (value / 1e3).toFixed(1) + 'k';
    if (digits !== undefined) return value.toFixed(digits);
    if (abs >= 100) return value.toFixed(1);
    if (abs >= 1) return value.toFixed(2);
    if (abs === 0) return '0';
    return value.toFixed(4);
  }

  function fmtTime(ts) {
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  /* ------------------------------------------------------------------ *
   * Axis frame shared by the cartesian charts
   * ------------------------------------------------------------------ */
  function drawFrame(ctx, box, scale, opts) {
    const t = theme();
    ctx.save();
    ctx.strokeStyle = t.grid;
    ctx.fillStyle = t.text;
    ctx.lineWidth = 1;
    ctx.font = '10px ui-monospace, SFMono-Regular, Menlo, monospace';
    ctx.textBaseline = 'middle';

    const steps = Math.round((scale.max - scale.min) / scale.step);
    for (let i = 0; i <= steps; i++) {
      const value = scale.min + scale.step * i;
      const y = box.y + box.h - ((value - scale.min) / (scale.max - scale.min)) * box.h;
      ctx.beginPath();
      ctx.moveTo(box.x, Math.round(y) + 0.5);
      ctx.lineTo(box.x + box.w, Math.round(y) + 0.5);
      ctx.stroke();
      ctx.textAlign = 'right';
      ctx.fillText(opts.formatY ? opts.formatY(value) : fmtNumber(value), box.x - 6, y);
    }

    if (opts.xLabels && opts.xLabels.length) {
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      const count = Math.min(opts.xLabels.length, Math.max(2, Math.floor(box.w / 90)));
      for (let i = 0; i < count; i++) {
        const idx = Math.round((opts.xLabels.length - 1) * (count === 1 ? 0 : i / (count - 1)));
        const x = box.x + (box.w * (count === 1 ? 0 : i / (count - 1)));
        ctx.fillText(opts.xLabels[idx], Math.min(box.x + box.w - 20, Math.max(box.x + 20, x)),
                     box.y + box.h + 6);
      }
    }
    ctx.restore();
  }

  function boxFor(width, height, opts) {
    const left = opts.padLeft === undefined ? 52 : opts.padLeft;
    const bottom = opts.xLabels && opts.xLabels.length ? 26 : 12;
    return { x: left, y: 10, w: Math.max(10, width - left - 12), h: Math.max(10, height - 10 - bottom) };
  }

  /* ------------------------------------------------------------------ *
   * Line / area chart — several series, shared y-axis
   * ------------------------------------------------------------------ */
  function line(canvas, config) {
    const { ctx, width, height } = prepare(canvas);
    const series = (config.series || []).filter((s) => s.values && s.values.length);
    if (!series.length) return empty(ctx, width, height, config.emptyText);

    const box = boxFor(width, height, config);
    const all = [];
    series.forEach((s) => s.values.forEach((v) => { if (v !== null && isFinite(v)) all.push(v); }));
    if (!all.length) return empty(ctx, width, height, config.emptyText);

    let min = Math.min(...all), max = Math.max(...all);
    if (config.baseline !== undefined) { min = Math.min(min, config.baseline); max = Math.max(max, config.baseline); }
    const scale = niceScale(min, max, 4);
    drawFrame(ctx, box, scale, config);

    const count = Math.max(...series.map((s) => s.values.length));
    const xAt = (i) => box.x + (count <= 1 ? box.w / 2 : (box.w * i) / (count - 1));
    const yAt = (v) => box.y + box.h - ((v - scale.min) / (scale.max - scale.min)) * box.h;
    const palette = PALETTE();

    series.forEach((s, si) => {
      const colour = s.color || palette[si % palette.length];
      const pts = s.values.map((v, i) => (v === null || !isFinite(v) ? null : [xAt(i), yAt(v)]));

      if (s.fill) {
        const gradient = ctx.createLinearGradient(0, box.y, 0, box.y + box.h);
        gradient.addColorStop(0, hexToRgba(colour, 0.28));
        gradient.addColorStop(1, hexToRgba(colour, 0));
        ctx.fillStyle = gradient;
        ctx.beginPath();
        let started = false;
        pts.forEach((p) => { if (p) { started ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]); started = true; } });
        if (started) {
          const last = pts.filter(Boolean).pop();
          const first = pts.find(Boolean);
          ctx.lineTo(last[0], box.y + box.h);
          ctx.lineTo(first[0], box.y + box.h);
          ctx.closePath();
          ctx.fill();
        }
      }

      ctx.strokeStyle = colour;
      ctx.lineWidth = s.width || 1.75;
      ctx.lineJoin = 'round';
      if (s.dashed) ctx.setLineDash([4, 4]); else ctx.setLineDash([]);
      ctx.beginPath();
      let drawing = false;
      pts.forEach((p) => {
        if (!p) { drawing = false; return; }
        if (!drawing) { ctx.moveTo(p[0], p[1]); drawing = true; } else ctx.lineTo(p[0], p[1]);
      });
      ctx.stroke();
      ctx.setLineDash([]);

      const lastPoint = pts.filter(Boolean).pop();
      if (lastPoint && s.marker !== false) {
        ctx.fillStyle = colour;
        ctx.beginPath();
        ctx.arc(lastPoint[0], lastPoint[1], 2.6, 0, Math.PI * 2);
        ctx.fill();
      }
    });

    if (config.baseline !== undefined) {
      ctx.save();
      ctx.strokeStyle = theme().axis;
      ctx.setLineDash([3, 4]);
      const y = yAt(config.baseline);
      ctx.beginPath(); ctx.moveTo(box.x, y); ctx.lineTo(box.x + box.w, y); ctx.stroke();
      ctx.restore();
    }
    legend(ctx, width, series, palette);
  }

  /* ------------------------------------------------------------------ *
   * Vertical bars — diverging around zero when values are signed
   * ------------------------------------------------------------------ */
  function bars(canvas, config) {
    const { ctx, width, height } = prepare(canvas);
    const values = config.values || [];
    if (!values.length) return empty(ctx, width, height, config.emptyText);

    const box = boxFor(width, height, config);
    const min = Math.min(0, ...values);
    const max = Math.max(0, ...values);
    const scale = niceScale(min, max, 4);
    drawFrame(ctx, box, scale, config);

    const t = theme();
    const slot = box.w / values.length;
    const barWidth = Math.max(1, Math.min(28, slot * 0.68));
    const yAt = (v) => box.y + box.h - ((v - scale.min) / (scale.max - scale.min)) * box.h;
    const zero = yAt(0);

    values.forEach((v, i) => {
      const x = box.x + slot * i + (slot - barWidth) / 2;
      const y = yAt(v);
      ctx.fillStyle = config.color || (v >= 0 ? t.up : t.down);
      const h = Math.max(1, Math.abs(zero - y));
      ctx.fillRect(x, Math.min(y, zero), barWidth, h);
    });
  }

  /** Frequency histogram (all bars one colour, x is a value axis). */
  function histogram(canvas, config) {
    const { ctx, width, height } = prepare(canvas);
    const counts = config.counts || [];
    if (!counts.length) return empty(ctx, width, height, config.emptyText);
    const box = boxFor(width, height, config);
    const scale = niceScale(0, Math.max(...counts), 4);
    drawFrame(ctx, box, scale, config);

    const t = theme();
    const slot = box.w / counts.length;
    counts.forEach((n, i) => {
      const h = ((n - scale.min) / (scale.max - scale.min)) * box.h;
      const bin = config.bins ? config.bins[i] : 0;
      ctx.fillStyle = bin >= 0 ? t.up : t.down;
      ctx.fillRect(box.x + slot * i + slot * 0.12, box.y + box.h - h,
                   Math.max(1, slot * 0.76), Math.max(1, h));
    });
  }

  /** Horizontal ranked bars — used for per-strategy / per-market performance. */
  function hbars(canvas, config) {
    const { ctx, width, height } = prepare(canvas);
    const items = (config.items || []).slice(0, config.limit || 10);
    if (!items.length) return empty(ctx, width, height, config.emptyText);

    const t = theme();
    const labelWidth = Math.min(140, Math.max(70, width * 0.32));
    const rowHeight = Math.min(30, Math.max(16, (height - 8) / items.length));
    const chartWidth = width - labelWidth - 62;
    const max = Math.max(...items.map((i) => Math.abs(i.value)), 1e-9);

    ctx.font = '11px system-ui, sans-serif';
    ctx.textBaseline = 'middle';
    items.forEach((item, i) => {
      const y = 6 + rowHeight * i + rowHeight / 2;
      ctx.fillStyle = t.text;
      ctx.textAlign = 'left';
      ctx.fillText(truncate(ctx, item.label, labelWidth - 8), 2, y);

      const w = (Math.abs(item.value) / max) * chartWidth;
      ctx.fillStyle = item.value >= 0 ? t.up : t.down;
      ctx.fillRect(labelWidth, y - rowHeight * 0.3, Math.max(1, w), rowHeight * 0.6);

      ctx.fillStyle = t.text;
      ctx.textAlign = 'left';
      ctx.fillText(fmtNumber(item.value), labelWidth + w + 6, y);
    });
  }

  /** Donut — portfolio allocation. */
  function donut(canvas, config) {
    const { ctx, width, height } = prepare(canvas);
    const items = (config.items || []).filter((i) => i.value > 0);
    if (!items.length) return empty(ctx, width, height, config.emptyText);

    const total = items.reduce((sum, i) => sum + i.value, 0);
    const cx = width * 0.34, cy = height / 2;
    const radius = Math.min(width * 0.3, height * 0.42);
    const inner = radius * 0.62;
    const palette = PALETTE();
    let angle = -Math.PI / 2;

    items.forEach((item, i) => {
      const sweep = (item.value / total) * Math.PI * 2;
      ctx.fillStyle = item.color || palette[i % palette.length];
      ctx.beginPath();
      ctx.arc(cx, cy, radius, angle, angle + sweep);
      ctx.arc(cx, cy, inner, angle + sweep, angle, true);
      ctx.closePath();
      ctx.fill();
      angle += sweep;
    });

    const t = theme();
    ctx.fillStyle = t.text;
    ctx.font = '11px system-ui, sans-serif';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    const legendX = width * 0.64;
    items.slice(0, 8).forEach((item, i) => {
      const y = cy - Math.min(items.length, 8) * 9 + i * 18 + 9;
      ctx.fillStyle = item.color || palette[i % palette.length];
      ctx.fillRect(legendX, y - 4, 8, 8);
      ctx.fillStyle = t.text;
      const pct = ((item.value / total) * 100).toFixed(1);
      ctx.fillText(truncate(ctx, `${item.label} ${pct}%`, width - legendX - 24), legendX + 14, y);
    });
  }

  /** Scatter timeline — one dot per trade, coloured and sized by PnL. */
  function timeline(canvas, config) {
    const { ctx, width, height } = prepare(canvas);
    const points = config.points || [];
    if (!points.length) return empty(ctx, width, height, config.emptyText);

    const box = boxFor(width, height, config);
    const values = points.map((p) => p.value);
    const scale = niceScale(Math.min(0, ...values), Math.max(0, ...values), 4);
    drawFrame(ctx, box, scale, config);

    const times = points.map((p) => p.ts);
    const tMin = Math.min(...times), tMax = Math.max(...times);
    const span = Math.max(1e-6, tMax - tMin);
    const t = theme();
    const maxAbs = Math.max(...values.map(Math.abs), 1e-9);

    const zeroY = box.y + box.h - ((0 - scale.min) / (scale.max - scale.min)) * box.h;
    ctx.strokeStyle = t.axis;
    ctx.setLineDash([3, 4]);
    ctx.beginPath(); ctx.moveTo(box.x, zeroY); ctx.lineTo(box.x + box.w, zeroY); ctx.stroke();
    ctx.setLineDash([]);

    points.forEach((p) => {
      const x = box.x + ((p.ts - tMin) / span) * box.w;
      const y = box.y + box.h - ((p.value - scale.min) / (scale.max - scale.min)) * box.h;
      const r = 2 + (Math.abs(p.value) / maxAbs) * 4;
      ctx.fillStyle = hexToRgba(p.value >= 0 ? t.up : t.down, 0.75);
      ctx.beginPath();
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fill();
    });
  }

  /** Compact sparkline with no axes, for KPI tiles. */
  function spark(canvas, config) {
    const { ctx, width, height } = prepare(canvas);
    const values = (config.values || []).filter((v) => v !== null && isFinite(v));
    if (values.length < 2) return;
    const min = Math.min(...values), max = Math.max(...values);
    const span = max - min || 1;
    const colour = config.color || (values[values.length - 1] >= values[0] ? theme().up : theme().down);
    const xAt = (i) => (width * i) / (values.length - 1);
    const yAt = (v) => height - 2 - ((v - min) / span) * (height - 4);

    const gradient = ctx.createLinearGradient(0, 0, 0, height);
    gradient.addColorStop(0, hexToRgba(colour, 0.3));
    gradient.addColorStop(1, hexToRgba(colour, 0));
    ctx.fillStyle = gradient;
    ctx.beginPath();
    ctx.moveTo(0, height);
    values.forEach((v, i) => ctx.lineTo(xAt(i), yAt(v)));
    ctx.lineTo(width, height);
    ctx.closePath();
    ctx.fill();

    ctx.strokeStyle = colour;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    values.forEach((v, i) => (i ? ctx.lineTo(xAt(i), yAt(v)) : ctx.moveTo(xAt(i), yAt(v))));
    ctx.stroke();
  }

  /** Order-book depth ladder: cumulative size either side of the mid. */
  function depth(canvas, config) {
    const { ctx, width, height } = prepare(canvas);
    const bids = config.bids || [], asks = config.asks || [];
    if (!bids.length && !asks.length) return empty(ctx, width, height, config.emptyText);

    const t = theme();
    const cumulative = (levels) => {
      let total = 0;
      return levels.map((l) => ({ price: l.price, size: (total += l.size) }));
    };
    const cb = cumulative(bids), ca = cumulative(asks);
    const maxSize = Math.max(1e-9, ...cb.map((l) => l.size), ...ca.map((l) => l.size));
    const half = width / 2;

    const draw = (levels, colour, fromRight) => {
      if (!levels.length) return;
      ctx.fillStyle = hexToRgba(colour, 0.22);
      ctx.strokeStyle = colour;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(half, height);
      levels.forEach((l, i) => {
        const x = fromRight ? half + (half * (i + 1)) / levels.length
                            : half - (half * (i + 1)) / levels.length;
        const y = height - (l.size / maxSize) * (height - 6);
        ctx.lineTo(x, y);
      });
      const lastX = fromRight ? width : 0;
      ctx.lineTo(lastX, height);
      ctx.closePath();
      ctx.fill();
      ctx.stroke();
    };
    draw(cb, t.up, false);
    draw(ca, t.down, true);
  }

  /* ------------------------------------------------------------------ */
  function legend(ctx, width, series, palette) {
    const labelled = series.filter((s) => s.label);
    if (labelled.length < 2) return;
    const t = theme();
    ctx.font = '10px system-ui, sans-serif';
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'left';
    let x = 56;
    labelled.forEach((s, i) => {
      const colour = s.color || palette[series.indexOf(s) % palette.length];
      ctx.fillStyle = colour;
      ctx.fillRect(x, 4, 8, 3);
      ctx.fillStyle = t.text;
      ctx.fillText(s.label, x + 12, 6);
      x += 22 + ctx.measureText(s.label).width;
      if (x > width - 60) return;
    });
  }

  function empty(ctx, width, height, text) {
    ctx.fillStyle = theme().text;
    ctx.font = '11px system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(text || 'No data yet', width / 2, height / 2);
  }

  function truncate(ctx, text, maxWidth) {
    let value = String(text ?? '');
    if (ctx.measureText(value).width <= maxWidth) return value;
    while (value.length > 1 && ctx.measureText(value + '…').width > maxWidth) {
      value = value.slice(0, -1);
    }
    return value + '…';
  }

  function hexToRgba(colour, alpha) {
    if (colour.startsWith('rgba')) return colour;
    if (colour.startsWith('rgb(')) {
      return colour.replace('rgb(', 'rgba(').replace(/\)\s*$/, `, ${alpha})`);
    }
    const hex = colour.replace('#', '');
    const full = hex.length === 3 ? hex.split('').map((c) => c + c).join('') : hex;
    const num = parseInt(full, 16);
    return `rgba(${(num >> 16) & 255}, ${(num >> 8) & 255}, ${num & 255}, ${alpha})`;
  }

  global.Charts = { line, bars, histogram, hbars, donut, timeline, spark, depth,
                    fmtNumber, fmtTime, theme };
})(window);

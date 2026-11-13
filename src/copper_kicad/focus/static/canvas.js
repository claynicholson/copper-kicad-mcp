/* Copper Canvas — point at a design so an AI can act on it.
 * Coordinates: KiCad's SVG user units are millimetres, so the only transform is the view map below
 * (a port of focus/geometry.py ViewMap — keep the two in step; tests round-trip through both). */
(() => {
  'use strict';
  const TOKEN = new URLSearchParams(location.search).get('t') || '';
  const $ = (id) => document.getElementById(id);
  const view = $('view'), stage = $('stage'), overlay = $('overlay'), ctx = overlay.getContext('2d');
  const ACCENT = '#C8783C';
  const LAYER_COLOR = {
    'F.Cu': '#C83434', 'B.Cu': '#4D7FC4', 'In1.Cu': '#7FC87F', 'In2.Cu': '#CE7D2C', 'In3.Cu': '#4FCBCB', 'In4.Cu': '#DB628B',
    'F.SilkS': '#F2EDA1', 'B.SilkS': '#E8B2A7', 'F.Fab': '#AFAFAF', 'B.Fab': '#585D84', 'Edge.Cuts': '#D0D2CD',
  };
  const DEFAULT_ON = {
    top: ['B.Cu', 'F.Cu', 'F.SilkS', 'Edge.Cuts'], bottom: ['F.Cu', 'B.Cu', 'B.SilkS', 'Edge.Cuts'],
  };

  const S = {
    tabs: [], tab: null, scene: null, focus: null, chip: '', seq: -1, hover: null, drag: null, pan: null,
    vm: { view: [0, 0, 297, 210], scale: 1, ox: 0, oy: 0, mirror: false },
    layersOn: { top: new Set(DEFAULT_ON.top), bottom: new Set(DEFAULT_ON.bottom) },
    imgs: new Map(), space: false, mouse: null, loading: 0, online: true, stamps: {},
  };

  // ── view map (== geometry.ViewMap) ─────────────────────────────────────
  function mmToScreen(x, y, vm = S.vm) {
    let lx = x - vm.view[0];
    if (vm.mirror) lx = vm.view[2] - lx;
    return [vm.ox + lx * vm.scale, vm.oy + (y - vm.view[1]) * vm.scale];
  }
  function screenToMm(sx, sy, vm = S.vm) {
    let lx = (sx - vm.ox) / vm.scale;
    if (vm.mirror) lx = vm.view[2] - lx;
    return [vm.view[0] + lx, vm.view[1] + (sy - vm.oy) / vm.scale];
  }
  function fit() {
    const w = view.clientWidth, h = view.clientHeight, m = 24, v = S.vm.view;
    if (w < 80 || h < 80) { S.fitted = false; return; }      // not laid out yet (hidden tab): fit on first resize
    const s = Math.max(1e-6, Math.min((w - 2 * m) / v[2], (h - 2 * m) / v[3]));
    S.vm.scale = s; S.vm.ox = (w - v[2] * s) / 2; S.vm.oy = (h - v[3] * s) / 2;
    S.fitScale = s; S.fitted = true; S.userView = false;
    layout();
  }
  function zoomAt(sx, sy, factor) {
    const max = Math.min(320, 48000 / S.vm.view[2]), min = (S.fitScale || 1) / 4;
    const s = Math.min(max, Math.max(min, S.vm.scale * factor));
    factor = s / S.vm.scale;
    S.vm.ox = sx - (sx - S.vm.ox) * factor; S.vm.oy = sy - (sy - S.vm.oy) * factor; S.vm.scale = s;
    S.userView = true;
    layout();
  }

  // ── geometry (== focus/resolver.py pick_item / pick_pin) ────────────────
  const distBox = (p, b) => Math.hypot(Math.max(b[0] - p[0], 0, p[0] - b[2]), Math.max(b[1] - p[1], 0, p[1] - b[3]));
  function distSeg(p, a, b) {
    const dx = b[0] - a[0], dy = b[1] - a[1], L2 = dx * dx + dy * dy;
    if (L2 <= 1e-12) return Math.hypot(p[0] - a[0], p[1] - a[1]);
    const t = Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2));
    return Math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy));
  }
  const cmp = (a, b) => { for (let i = 0; i < a.length; i++) { if (a[i] !== b[i]) return a[i] < b[i] ? -1 : 1; } return 0; };
  function pickItem(pt, radius, side, prefer) {
    let best = null, key = null;
    const pcb = !!S.scene && S.scene.document === 'pcb', grow = (S.scene && S.scene.pad_grow) || 0.08;
    for (const it of (S.scene ? S.scene.items : [])) {
      if (side && it.side && it.side !== side) continue;
      const d = distBox(pt, it.bbox);
      if (d > radius) continue;
      const b = it.bbox, c = [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2];
      const onPad = d === 0 && pcb && it.pins.some((p) => p[7] && padHit(p, pt, grow));
      const k = [Math.round(d * 1e4) / 1e4, (prefer && it.side && it.side !== prefer) ? 1 : 0, onPad ? 0 : 1,
        d === 0 ? (b[2] - b[0]) * (b[3] - b[1]) : 0, Math.hypot(pt[0] - c[0], pt[1] - c[1])];
      if (!key || cmp(k, key) < 0) { best = it; key = k; }
    }
    return best;
  }
  function padHit(p, pt, grow) {   // == resolver.pad_hit
    let dx = pt[0] - p[2], dy = pt[1] - p[3];
    if (p[9]) { const a = p[9] * Math.PI / 180, c = Math.cos(a), s = Math.sin(a); [dx, dy] = [dx * c - dy * s, dx * s + dy * c]; }
    return Math.abs(dx) <= p[7] + grow && Math.abs(dy) <= p[8] + grow;
  }
  function padTol(it, pin) {
    let best = null;
    for (const q of it.pins) {
      if (q === pin || (q[2] === pin[2] && q[3] === pin[3])) continue;
      const d = Math.hypot(q[2] - pin[2], q[3] - pin[3]);
      best = best === null ? d : Math.min(best, d);
    }
    return best === null ? 1.0 : Math.max(0.3, Math.min(1.5, 0.45 * best));
  }
  function pickPin(it, pt) {
    const sch = S.scene.document === 'schematic';
    let best = null, bd = 1e9;
    const grow = S.scene.pad_grow || 0.08;
    for (const p of it.pins) {
      let d, ok;
      if (sch) { d = distSeg(pt, [p[2], p[3]], [p[5], p[6]]); ok = d <= S.scene.pin_tol; }
      else {
        d = Math.hypot(pt[0] - p[2], pt[1] - p[3]);
        ok = p[7] ? padHit(p, pt, grow) : d <= padTol(it, p);
      }
      if (ok && d < bd) { best = p; bd = d; }
    }
    if (!best) return null;
    const b = it.bbox, cx = (b[0] + b[2]) / 2, cy = (b[1] + b[3]) / 2, n = it.pins.length;
    if (sch && n <= 4 && Math.hypot(pt[0] - cx, pt[1] - cy) <= bd) return null;
    if (!sch && isBodyPad(it, best)) return null;   // exposed / thermal pad under a package = the part
    return best;
  }
  function isBodyPad(it, pad) {   // == resolver.is_body_pad
    const e = 0.05, others = it.pins.filter((q) => q[0] !== pad[0]);
    if (!(others.some((q) => q[2] < pad[2] - e) && others.some((q) => q[2] > pad[2] + e)
      && others.some((q) => q[3] < pad[3] - e) && others.some((q) => q[3] > pad[3] + e))) return false;
    if (it.pins.filter((q) => q[0] === pad[0]).length > 1) return true;
    const area = (pad[7] || 0) * (pad[8] || 0);
    const areas = it.pins.map((q) => (q[7] || 0) * (q[8] || 0)).sort((x, y) => x - y);
    return area > 0 && area > 2 * areas[Math.floor(areas.length / 2)];
  }
  function pinLabel(it, p) {
    const name = p[1];
    if (name && it.pins.filter((q) => q[1] === name).length === 1) return `${it.ref}.${name}`;
    return `${it.ref}.${p[0]}`;
  }
  const pointRadius = () => Math.max(0.3, Math.min(S.scene ? S.scene.radius * 2 : 5, 26 / S.vm.scale));
  function sideArgs(box) {
    if (!S.tab || S.tab.doc !== 'pcb') return {};
    const on = S.layersOn[S.tab.side], other = S.tab.side === 'top' ? 'B.' : 'F.';
    const far = [...on].filter((l) => l.startsWith(other));
    if (!far.length) return { side: S.tab.side };
    // a box means "these parts I can see": parts on the far side only join in when that side's silkscreen
    // or fab layer is shown (its dimmed copper alone does not show which parts are there)
    if (box && !far.some((l) => !l.endsWith('.Cu'))) return { side: S.tab.side };
    return { prefer: S.tab.side };
  }

  // ── server ─────────────────────────────────────────────────────────────
  async function call(path, body) {
    const r = await fetch(path, {
      method: body ? 'POST' : 'GET', cache: 'no-store',
      headers: body ? { 'X-Copper-Token': TOKEN, 'Content-Type': 'application/json' } : { 'X-Copper-Token': TOKEN },
      body: body ? JSON.stringify(body) : undefined,
    });
    const j = await r.json().catch(() => ({ error: 'HTTP', message: `HTTP ${r.status}` }));
    if (!r.ok) throw Object.assign(new Error(j.message || `HTTP ${r.status}`), { info: j });
    return j;
  }
  const svgUrl = (q) => `/svg?${new URLSearchParams({ ...q, t: TOKEN })}`;

  // ── tabs ───────────────────────────────────────────────────────────────
  const tabId = (t) => (t.doc === 'pcb' ? `pcb-${t.side}` : `sch-${t.sheet}`);
  function buildTabs(docs) {
    const tabs = [];
    if (docs.schematic) for (const s of docs.schematic.sheets) tabs.push({ doc: 'schematic', sheet: s.index, name: s.name, title: s.title });
    if (docs.pcb) { tabs.push({ doc: 'pcb', side: 'top' }); tabs.push({ doc: 'pcb', side: 'bottom' }); }
    const sig = JSON.stringify(tabs);
    if (sig === S.tabSig) return;
    S.tabSig = sig; S.tabs = tabs;
    renderTabs();
  }
  function renderTabs() {
    const nav = $('tabs');
    nav.textContent = '';
    const sheets = S.tabs.filter((t) => t.doc === 'schematic');
    const mk = (label, t, title) => {
      const b = document.createElement('button');
      b.type = 'button'; b.textContent = label; b.title = title || '';
      b.setAttribute('aria-selected', String(!!S.tab && tabId(S.tab) === tabId(t)));
      b.addEventListener('click', () => openTab(t));
      nav.appendChild(b);
      return b;
    };
    if (sheets.length) {
      const cur = S.tab && S.tab.doc === 'schematic' ? S.tab : null;
      mk('Schematic', cur || sheets[0], 'Schematic');
      if (sheets.length > 1) {
        const wrap = document.createElement('span');
        if (cur) wrap.className = 'on';
        const sel = document.createElement('select');
        sel.title = 'Sheet'; sel.setAttribute('aria-label', 'Schematic sheet');
        for (const t of sheets) {
          const o = document.createElement('option');
          o.value = String(t.sheet); o.textContent = t.name === '/' ? 'Root' : t.name;
          if (cur && cur.sheet === t.sheet) o.selected = true;
          sel.appendChild(o);
        }
        sel.addEventListener('change', () => openTab(sheets.find((t) => String(t.sheet) === sel.value)));
        wrap.appendChild(sel); nav.appendChild(wrap);
      }
    }
    for (const t of S.tabs.filter((x) => x.doc === 'pcb')) mk(t.side === 'top' ? 'PCB top' : 'PCB bottom', t, 'Board seen from the ' + t.side);
  }
  function renderLayers() {
    const box = $('layers');
    box.textContent = '';
    if (!S.tab || S.tab.doc !== 'pcb' || !S.scene || !S.scene.layers) return;
    const on = S.layersOn[S.tab.side];
    for (const ly of [...S.scene.layers].reverse()) {
      const b = document.createElement('button');
      b.type = 'button'; b.title = `Show / hide ${ly}`;
      b.setAttribute('aria-pressed', String(on.has(ly)));
      const sw = document.createElement('i');
      sw.style.setProperty('--sw', LAYER_COLOR[ly] || '#888');
      b.appendChild(sw); b.appendChild(document.createTextNode(ly));
      b.addEventListener('click', () => { if (on.has(ly)) on.delete(ly); else on.add(ly); renderLayers(); layout(); });
      box.appendChild(b);
    }
  }

  async function openTab(t, keepView) {
    if (!t) return;
    const same = S.tab && tabId(S.tab) === tabId(t);
    S.tab = t; S.hover = null;
    try { history.replaceState(null, '', `${location.pathname}${location.search}#${tabId(t)}`); } catch (e) { /* ignore */ }
    renderTabs();
    view.classList.toggle('pcb', t.doc === 'pcb');
    const my = ++S.loading;
    setLive();
    let sc;
    try {
      sc = await call(`/api/scene?${new URLSearchParams(t.doc === 'pcb' ? { doc: 'pcb' } : { doc: 'schematic', sheet: String(t.sheet) })}`);
    } catch (e) {
      if (my !== S.loading) return;
      S.scene = null; clearStage(); showMsg(e.info || { message: String(e.message || e) });
      S.stamps[t.doc] = null;   // retry on the next poll (the file may have been mid-write)
      S.loading = 0; setLive(); draw();
      return;
    }
    if (my !== S.loading) return;
    showMsg(null);
    const sameView = S.scene && same && JSON.stringify(S.scene.view) === JSON.stringify(sc.view);
    S.scene = sc;
    S.byRef = new Map();
    for (const it of sc.items) { if (!S.byRef.has(it.ref)) S.byRef.set(it.ref, []); S.byRef.get(it.ref).push(it); }
    S.vm.view = sc.view; S.vm.mirror = t.doc === 'pcb' && t.side === 'bottom';
    S.stamps[t.doc] = sc.stamp;
    buildImages(sc, t);
    renderLayers();
    if (keepView && sameView && S.fitted) layout(); else fit();
    S.loading = 0; setLive();
    updateHover();
  }

  // ── images: KiCad's SVGs, positioned by the view map ───────────────────
  function clearStage() { stage.textContent = ''; S.imgs = new Map(); S.paper = null; }
  function buildImages(sc, t) {
    const old = [...stage.children];
    const fresh = new Map();
    let paper = null;
    if (t.doc === 'schematic') {
      paper = document.createElement('div'); paper.className = 'paper';
      stage.appendChild(paper);
    }
    const srcs = t.doc === 'schematic'
      ? [['sheet', svgUrl({ doc: 'schematic', sheet: String(t.sheet), v: sc.stamp })]]
      : sc.layers.map((ly) => [ly, svgUrl({ doc: 'pcb', layer: ly, v: sc.stamp })]);
    let pending = srcs.length;
    const done = () => { if (--pending <= 0) for (const el of old) el.remove(); };
    for (const [key, src] of srcs) {
      const im = new Image();
      im.alt = ''; im.draggable = false; im.decoding = 'async';
      im.addEventListener('load', done); im.addEventListener('error', done);
      im.src = src;
      stage.appendChild(im);
      fresh.set(key, im);
    }
    if (!srcs.length) for (const el of old) el.remove();
    S.imgs = fresh; S.paper = paper;
    stage.classList.toggle('mirror', S.vm.mirror);
  }
  function layout() {
    const vm = S.vm, w = vm.view[2] * vm.scale, h = vm.view[3] * vm.scale;
    const place = (el) => { el.style.left = `${vm.ox}px`; el.style.top = `${vm.oy}px`; el.style.width = `${w}px`; el.style.height = `${h}px`; };
    if (S.paper) place(S.paper);
    if (S.tab && S.tab.doc === 'pcb' && S.scene && S.scene.layers) {
      const on = S.layersOn[S.tab.side], bottom = S.tab.side === 'bottom';
      const order = [...S.scene.layers];
      if (bottom) { const edge = order.filter((l) => l === 'Edge.Cuts'); const rest = order.filter((l) => l !== 'Edge.Cuts').reverse(); order.splice(0, order.length, ...rest, ...edge); }
      order.forEach((ly, i) => {
        const im = S.imgs.get(ly);
        if (!im) return;
        place(im);
        im.style.zIndex = String(i + 1);
        im.classList.toggle('off', !on.has(ly));
        const far = ly !== 'Edge.Cuts' && ly.startsWith(bottom ? 'F.' : 'B.');
        im.style.opacity = far ? '0.42' : (ly.startsWith('In') ? '0.6' : '0.92');
      });
    } else {
      for (const im of S.imgs.values()) place(im);
    }
    draw();
  }

  // ── overlay ────────────────────────────────────────────────────────────
  function resize() {
    const dpr = window.devicePixelRatio || 1;
    overlay.width = Math.max(1, Math.round(view.clientWidth * dpr));
    overlay.height = Math.max(1, Math.round(view.clientHeight * dpr));
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  function rectPx(b) {
    const a = mmToScreen(b[0], b[1]), c = mmToScreen(b[2], b[3]);
    return [Math.min(a[0], c[0]), Math.min(a[1], c[1]), Math.abs(c[0] - a[0]), Math.abs(c[1] - a[1])];
  }
  function focusHere() {
    const f = S.focus, t = S.tab;
    if (!f || !t || !S.scene || f.document !== t.doc) return null;
    if (t.doc === 'schematic' && f.sheet !== t.name) return null;
    return f;
  }
  function draw() {
    ctx.clearRect(0, 0, view.clientWidth, view.clientHeight);
    if (!S.scene) return;
    const f = focusHere(), pcb = S.tab.doc === 'pcb';
    const ink = pcb ? '#FFB27A' : ACCENT;                 // lighter on copper, with a dark halo underneath
    const stroke = (path, w, dash) => {
      ctx.setLineDash(dash || []);
      if (pcb) { ctx.strokeStyle = 'rgba(16,16,20,0.85)'; ctx.lineWidth = w + 2.5; path(); ctx.stroke(); }
      ctx.strokeStyle = ink; ctx.lineWidth = w; path(); ctx.stroke();
      ctx.setLineDash([]);
    };
    const rect = (r, pad) => () => { ctx.beginPath(); ctx.rect(r[0] - pad, r[1] - pad, r[2] + 2 * pad, r[3] + 2 * pad); };
    ctx.lineJoin = 'round';
    if (f) {
      ctx.fillStyle = pcb ? 'rgba(255,178,122,0.20)' : 'rgba(200,120,60,0.16)';
      const pinRefs = new Set(f.mode === 'box' ? [] : (f.pins || []).map((p) => p.slice(0, p.indexOf('.'))));
      for (const ref of f.refs || []) {
        for (const it of (S.byRef.get(ref) || [])) {
          const r = rectPx(it.bbox);
          if (pinRefs.has(ref)) stroke(rect(r, 2), 1);     // only a pin of this part is meant: thin outline, no fill
          else { ctx.fillRect(r[0] - 2, r[1] - 2, r[2] + 4, r[3] + 4); stroke(rect(r, 2), 2); }
        }
      }
      for (const pl of f.mode === 'box' ? [] : (f.pins || [])) {
        const i = pl.indexOf('.'), its = S.byRef.get(pl.slice(0, i)) || [], pn = pl.slice(i + 1);
        for (const it of its) for (const p of it.pins) if (p[0] === pn || (p[1] && p[1] === pn)) {
          const q = mmToScreen(p[2], p[3]);
          stroke(() => { ctx.beginPath(); ctx.arc(q[0], q[1], 7, 0, 6.2832); }, 3);
        }
      }
      if (f.mode === 'box' && f.box_mm) stroke(rect(rectPx(f.box_mm), 0), 2, [7, 5]);
      if (f.mode === 'point' && f.point_mm) {
        const q = mmToScreen(f.point_mm[0], f.point_mm[1]);
        stroke(() => { ctx.beginPath(); ctx.moveTo(q[0] - 10, q[1]); ctx.lineTo(q[0] + 10, q[1]); ctx.moveTo(q[0], q[1] - 10); ctx.lineTo(q[0], q[1] + 10); }, 2);
      }
    }
    if (S.hover && !S.drag) {
      const r = rectPx(S.hover.it.bbox);
      ctx.strokeStyle = pcb ? '#F2F0EA' : '#3A3832'; ctx.lineWidth = 1.25; ctx.setLineDash([]);
      ctx.strokeRect(r[0] - 3.5, r[1] - 3.5, r[2] + 7, r[3] + 7);
      if (S.hover.pin) {
        const q = mmToScreen(S.hover.pin[2], S.hover.pin[3]);
        stroke(() => { ctx.beginPath(); ctx.arc(q[0], q[1], 5, 0, 6.2832); }, 2);
      }
    }
    if (S.drag && S.drag.box) {
      const d = S.drag, x = Math.min(d.x0, d.x1), y = Math.min(d.y0, d.y1), w = Math.abs(d.x1 - d.x0), h = Math.abs(d.y1 - d.y0);
      ctx.fillStyle = pcb ? 'rgba(255,178,122,0.14)' : 'rgba(200,120,60,0.12)';
      ctx.fillRect(x, y, w, h);
      stroke(rect([x, y, w, h], 0), 1.5, [6, 4]);
    }
  }

  // ── hover ──────────────────────────────────────────────────────────────
  function updateHover() {
    const tip = $('tip');
    if (!S.mouse || !S.scene || S.pan) { S.hover = null; tip.hidden = true; $('hover').textContent = ''; draw(); return; }
    const pt = screenToMm(S.mouse[0], S.mouse[1]);
    $('coord').textContent = `x ${pt[0].toFixed(2)}  y ${pt[1].toFixed(2)} mm`;
    const a = sideArgs();
    const it = pickItem(pt, pointRadius(), a.side, a.prefer);
    const pin = it ? pickPin(it, pt) : null;
    S.hover = it ? { it, pin } : null;
    if (!it || S.drag) { tip.hidden = true; $('hover').textContent = ''; draw(); return; }
    tip.textContent = '';
    const b = document.createElement('b'); b.textContent = pin ? pinLabel(it, pin) : it.ref; tip.appendChild(b);
    const v = document.createElement('span'); v.className = 'v'; v.textContent = it.value + (it.side === 'bottom' ? ' · bottom' : ''); tip.appendChild(v);
    const n = document.createElement('span'); n.className = 'n';
    let line;
    if (pin) {
      line = `pin ${pin[0]}${pin[1] ? ' ' + pin[1] : ''} · `;
      n.appendChild(document.createTextNode(line));
      const em = document.createElement('em'); em.textContent = pin[4] || 'no net'; n.appendChild(em);
      line += pin[4] || 'no net';
    } else {
      const nets = [...new Set(it.pins.map((p) => p[4]).filter((x) => x && !x.startsWith('unconnected-(')))];
      line = nets.length ? nets.slice(0, 6).join(', ') + (nets.length > 6 ? ` +${nets.length - 6}` : '') : `${it.pins.length} pins`;
      n.textContent = line;
    }
    tip.appendChild(n);
    tip.hidden = false;
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    tip.style.left = `${Math.min(view.clientWidth - tw - 6, S.mouse[0] + 16)}px`;
    tip.style.top = `${S.mouse[1] + 18 + th > view.clientHeight ? S.mouse[1] - th - 10 : S.mouse[1] + 18}px`;
    $('hover').textContent = `${b.textContent}  ${it.value}  —  ${line}`;
    draw();
  }

  // ── focus ──────────────────────────────────────────────────────────────
  function setFocus(f, chip, seq) {
    const changed = seq !== S.seq;
    S.focus = f || null; S.chip = chip || ''; if (seq !== undefined) S.seq = seq;
    const el = $('focus'), has = !!f;
    el.classList.toggle('empty', !has);
    $('chip').textContent = has ? (chip || '—') : 'nothing yet — click a part or drag a box';
    if (has && changed) { el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash'); }
    draw();
  }
  async function sendFocus(body) {
    if (!S.tab || !S.scene) return;
    const t = S.tab;
    const payload = { document: t.doc, ...(t.doc === 'schematic' ? { sheet: String(t.sheet) } : sideArgs(body.mode === 'box')), ...body };
    try {
      const r = await call('/api/focus', payload);
      setFocus(r.focus, r.chip, r.seq);
    } catch (e) { showMsg(e.info || { message: String(e.message || e) }, 2500); }
  }
  async function clearFocus() {
    try { const r = await call('/api/clear', {}); setFocus(null, '', r.seq); } catch (e) { /* offline */ }
  }

  // ── pointer ────────────────────────────────────────────────────────────
  const local = (e) => { const r = view.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
  view.addEventListener('pointerdown', (e) => {
    view.focus({ preventScroll: true });
    const p = local(e);
    if (e.button === 1 || e.button === 2 || (e.button === 0 && S.space)) {
      S.pan = { x: p[0], y: p[1], ox: S.vm.ox, oy: S.vm.oy };
      view.classList.add('panning');
    } else if (e.button === 0) {
      S.drag = { x0: p[0], y0: p[1], x1: p[0], y1: p[1], box: false, add: e.shiftKey };
    } else return;
    view.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  view.addEventListener('pointermove', (e) => {
    const p = local(e);
    S.mouse = p;
    if (S.pan) { S.vm.ox = S.pan.ox + p[0] - S.pan.x; S.vm.oy = S.pan.oy + p[1] - S.pan.y; S.userView = true; layout(); return; }
    if (S.drag) {
      S.drag.x1 = p[0]; S.drag.y1 = p[1];
      if (!S.drag.box && Math.hypot(p[0] - S.drag.x0, p[1] - S.drag.y0) > 4) S.drag.box = true;
    }
    updateHover();
  });
  const finish = (e) => {
    if (S.pan) { S.pan = null; view.classList.remove('panning'); updateHover(); return; }
    const d = S.drag;
    if (!d) return;
    S.drag = null;
    if (e.type === 'pointercancel') { draw(); return; }
    const add = d.add || e.shiftKey;
    if (d.box) {
      const a = screenToMm(d.x0, d.y0), b = screenToMm(d.x1, d.y1);
      sendFocus({ mode: 'box', box: [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[0], b[0]), Math.max(a[1], b[1])], add });
    } else {
      sendFocus({ mode: 'point', point: screenToMm(d.x0, d.y0), radius: pointRadius(), add });
    }
    updateHover();
  };
  view.addEventListener('pointerup', finish);
  view.addEventListener('pointercancel', finish);
  view.addEventListener('pointerleave', () => { if (!S.drag && !S.pan) { S.mouse = null; updateHover(); $('coord').textContent = ''; } });
  view.addEventListener('contextmenu', (e) => e.preventDefault());
  view.addEventListener('auxclick', (e) => e.preventDefault());
  view.addEventListener('wheel', (e) => {
    e.preventDefault();
    const p = local(e);
    const k = e.deltaMode === 1 ? 33 : 1;
    zoomAt(p[0], p[1], Math.exp(-e.deltaY * k * (e.ctrlKey ? 0.01 : 0.0016)));
    S.mouse = p; updateHover();
  }, { passive: false });
  view.addEventListener('dblclick', (e) => {
    if (!S.scene || S.tab.doc !== 'schematic') return;
    const pt = screenToMm(...local(e));
    const sub = (S.scene.subsheets || []).find((s) => pt[0] >= s.bbox[0] && pt[0] <= s.bbox[2] && pt[1] >= s.bbox[1] && pt[1] <= s.bbox[3]);
    if (!sub) return;
    const want = (S.tab.name === '/' ? '' : S.tab.name) + '/' + sub.name;
    openTab(S.tabs.find((t) => t.doc === 'schematic' && t.name === want));
  });
  window.addEventListener('keydown', (e) => {
    if (e.target instanceof HTMLSelectElement) return;
    if (e.key === 'Escape') { if (S.drag) { S.drag = null; draw(); } else clearFocus(); e.preventDefault(); }
    else if (e.key === 'f' || e.key === 'F' || e.key === 'Home') { fit(); updateHover(); }
    else if (e.key === ' ') { S.space = true; e.preventDefault(); }
    else if (e.key === '+' || e.key === '=') zoomAt(view.clientWidth / 2, view.clientHeight / 2, 1.3);
    else if (e.key === '-') zoomAt(view.clientWidth / 2, view.clientHeight / 2, 1 / 1.3);
  });
  window.addEventListener('keyup', (e) => { if (e.key === ' ') S.space = false; });
  window.addEventListener('blur', () => { S.space = false; });
  window.addEventListener('hashchange', () => { const t = S.tabs.find((x) => tabId(x) === location.hash.slice(1)); if (t && (!S.tab || tabId(t) !== tabId(S.tab))) openTab(t); });
  $('clear').addEventListener('click', (e) => { e.stopPropagation(); clearFocus(); });
  $('focus').addEventListener('click', () => {
    if (S.focus && S.chip && navigator.clipboard) navigator.clipboard.writeText(S.chip).catch(() => {});
  });
  new ResizeObserver(() => { resize(); if (S.scene) { if (!S.fitted || !S.userView) fit(); else layout(); } }).observe(view);

  // ── messages + live reload ─────────────────────────────────────────────
  let msgTimer = 0;
  function showMsg(info, ms) {
    const el = $('msg');
    clearTimeout(msgTimer);
    if (!info) { el.hidden = true; return; }
    el.textContent = '';
    const b = document.createElement('b'); b.textContent = info.message || 'Something went wrong'; el.appendChild(b);
    if (info.fix) el.appendChild(document.createTextNode(`Ask your AI: ${info.fix}`));
    el.hidden = false;
    if (ms) msgTimer = setTimeout(() => { el.hidden = true; }, ms);
  }
  function setLive() {
    const el = $('live');
    el.classList.toggle('off', !S.online);
    el.classList.toggle('busy', S.online && S.loading > 0);
    el.textContent = !S.online ? 'offline' : (S.loading > 0 ? 'loading' : 'live');
  }
  async function poll() {
    try {
      const st = await call('/api/state');
      if (!S.online) { S.online = true; setLive(); }
      $('proj').textContent = st.project;
      document.title = `${st.project} — Copper Canvas`;
      buildTabs(st.documents);
      if (!S.tab) {
        const want = location.hash.slice(1);
        openTab(S.tabs.find((t) => tabId(t) === want) || S.tabs[0]);
        if (!S.tabs.length) showMsg({ message: 'This project has no schematic or board yet.', fix: 'sch_apply(...) to start the schematic' });
      } else if (!S.loading) {
        const d = st.documents[S.tab.doc];
        const gone = !S.tabs.some((t) => tabId(t) === tabId(S.tab));
        if (gone) { S.tab = null; S.scene = null; clearStage(); }
        else if (d && d.stamp !== S.stamps[S.tab.doc]) { S.stamps[S.tab.doc] = d.stamp; openTab(S.tab, true); }
      }
      if (st.seq !== S.seq) setFocus(st.focus, st.chip, st.seq);
    } catch (e) {
      if (S.online) { S.online = false; setLive(); }
    }
  }

  window.copperCanvas = { state: S, mmToScreen, screenToMm, pickItem, pickPin, padHit, sideArgs, fit, zoomAt, openTab, poll };
  resize();
  poll();
  setInterval(poll, 1000);
})();

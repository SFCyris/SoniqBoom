/* SPDX-FileCopyrightText: 2026 S.F. Cyris
   SPDX-License-Identifier: AGPL-3.0-or-later

   Background: a procedurally-generated printed-circuit board, dimmed, retro.
   Components (ICs, capacitors, resistors, a connector) are placed with real
   PINS; copper traces are NETS that connect a pin on one component to a pin
   on a DIFFERENT component (greedy nearest-neighbour matching), routed out of
   each pin with a stub and a 90°/45° dog-leg — so the board actually wires the
   chips together instead of shorting a random grid. On load the nets retrace
   themselves (Tron reveal), a scan pulse re-lights them, and electrons flow
   chip → trace → chip, entering a component on one pin and leaving on another.

   The static board is painted once to an offscreen canvas and blitted each
   frame; only the glow + electrons animate. No dependencies, offline-safe.
   Honours prefers-reduced-motion and pauses when hidden. */
(function () {
  "use strict";
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var cv = document.getElementById("electrons");
  if (!cv) return;
  var ctx = cv.getContext("2d");

  // palette
  var GREEN_HI = "#123a22", GREEN_LO = "#081f12", GREEN_POUR = "rgba(28,120,66,0.16)";
  var COPPER = "#8a6422", COPPER_HI = "#b3852f";
  var PAD = "#c4c9cd";
  var SILK = "#cfe6d4";
  var IC_BODY = "#0c0e11", IC_EDGE = "#23262b", PIN = "#c2c7cc";
  var LIT = "120,230,170", SCAN = "120,210,255";
  var ELEC = ["255,162,74", "143,214,255", "120,255,140", "255,210,120"];
  var DIM = 0.5;

  var DPR = Math.min(window.devicePixelRatio || 1, 2);
  var W = 0, H = 0, P = 34;
  var comps = [], nets = [], vias = [], labels = [];
  var board = null, bctx = null, electrons = [];
  var t0 = 0, clock = 0;

  function rand(a, b) { return a + Math.random() * (b - a); }
  function ri(a, b) { return (rand(a, b + 1)) | 0; }
  function pick(a) { return a[(Math.random() * a.length) | 0]; }
  function chance(p) { return Math.random() < p; }
  function dist(a, b) { return Math.hypot(a.x - b.x, a.y - b.y); }

  // straight-then-45° router between two points
  function dogleg(ax, ay, bx, by) {
    var dx = bx - ax, dy = by - ay, adx = Math.abs(dx), ady = Math.abs(dy);
    if (adx < 3 || ady < 3) return [];
    var sx = dx < 0 ? -1 : 1, sy = dy < 0 ? -1 : 1;
    return adx > ady ? [{ x: ax + sx * (adx - ady), y: ay }]
                     : [{ x: ax, y: ay + sy * (ady - adx) }];
  }

  // ── component factories (each owns a pin list with outward normals) ──
  var icN = 0, capN = 0, resN = 0;
  function pin(comp, x, y, nx, ny) { return { comp: comp, x: x, y: y, nx: nx, ny: ny, used: false }; }

  function makeIC(x, y, w, h, quad) {
    var c = { t: "ic", x: x, y: y, w: w, h: h, quad: quad, pins: [], traces: [] };
    var nx = Math.max(3, Math.round(w / 12)), ny = Math.max(3, Math.round(h / 12)), i, px, py;
    for (i = 0; i < nx; i++) {
      px = x + 8 + i * ((w - 16) / (nx - 1));
      c.pins.push(pin(c, px, y - 3, 0, -1));      // top
      c.pins.push(pin(c, px, y + h + 3, 0, 1));   // bottom
    }
    if (quad) for (i = 0; i < ny; i++) {
      py = y + 8 + i * ((h - 16) / (ny - 1));
      c.pins.push(pin(c, x - 3, py, -1, 0));      // left
      c.pins.push(pin(c, x + w + 3, py, 1, 0));   // right
    }
    c.cx = x + w / 2; c.cy = y + h / 2;
    labels.push({ x: c.cx, y: y - 7, s: "IC" + (10 + icN++) });
    return c;
  }
  function makeCap(x, y, rad) {
    var c = { t: "cap", x: x, y: y, rad: rad, pins: [], traces: [], cx: x, cy: y };
    c.pins.push(pin(c, x - rad - 2, y, -1, 0));
    c.pins.push(pin(c, x + rad + 2, y, 1, 0));
    labels.push({ x: x, y: y - rad - 5, s: "C" + (120 + (capN++) * 2) });
    return c;
  }
  function makeRes(x, y, x2) {
    var c = { t: "res", x: x, y: y, x2: x2, pins: [], traces: [], cx: (x + x2) / 2, cy: y };
    c.pins.push(pin(c, x - 5, y, -1, 0));
    c.pins.push(pin(c, x2 + 5, y, 1, 0));
    labels.push({ x: c.cx, y: y - 8, s: "R" + (200 + resN++) });
    return c;
  }
  function makeConn(x, y, n, pitch) {
    var c = { t: "conn", x: x, y: y, n: n, pitch: pitch, pins: [], traces: [], cx: x + (n - 1) * pitch / 2, cy: y };
    for (var i = 0; i < n; i++) c.pins.push(pin(c, x + i * pitch, y, 0, -1));
    return c;
  }

  function overlaps(x, y, w, h, pad) {
    for (var i = 0; i < comps.length; i++) {
      var c = comps[i], cx0, cy0, cw, ch;
      if (c.t === "ic") { cx0 = c.x; cy0 = c.y; cw = c.w; ch = c.h; }
      else if (c.t === "cap") { cx0 = c.x - c.rad; cy0 = c.y - c.rad; cw = c.rad * 2; ch = c.rad * 2; }
      else if (c.t === "res") { cx0 = c.x; cy0 = c.y - 6; cw = c.x2 - c.x; ch = 12; }
      else { cx0 = c.x; cy0 = c.y - 6; cw = (c.n - 1) * c.pitch; ch = 12; }
      if (x < cx0 + cw + pad && x + w + pad > cx0 && y < cy0 + ch + pad && y + h + pad > cy0) return true;
    }
    return false;
  }

  function buildModel() {
    comps = []; nets = []; vias = []; labels = []; icN = capN = resN = 0;
    P = Math.max(26, Math.min(40, Math.round(Math.sqrt((W * H) / 1400))));

    // a connector along the lower area (row of pads, like a header)
    var connN = Math.max(6, Math.min(20, Math.round(W / (P * 1.6))));
    var connPitch = P * 0.9, connX = (W - (connN - 1) * connPitch) / 2;
    comps.push(makeConn(connX, H - P * 1.6, connN, connPitch));

    // scatter ICs, caps, resistors on a loose grid, avoiding overlap
    var step = P * 3.0;
    for (var gy = P * 1.6; gy < H - P * 3; gy += step) {
      for (var gx = P * 1.4; gx < W - P * 3; gx += step) {
        var x = gx + rand(-P * 0.4, P * 0.4), y = gy + rand(-P * 0.4, P * 0.4), r = Math.random();
        if (r < 0.34) {
          var w = ri(2, 4) * P * 0.62, h = ri(2, 3) * P * 0.62, quad = chance(0.45);
          if (!overlaps(x, y, w, h, P * 0.6)) comps.push(makeIC(x, y, w, h, quad));
        } else if (r < 0.55) {
          var rad = P * rand(0.5, 0.8);
          if (!overlaps(x - rad, y - rad, rad * 2, rad * 2, P * 0.5)) comps.push(makeCap(x, y, rad));
        } else if (r < 0.80) {
          var rl = P * rand(1.2, 1.7);
          if (!overlaps(x, y - 6, rl, 12, P * 0.5)) comps.push(makeRes(x, y, x + rl));
        }
      }
    }

    // ── nets: connect each pin to the nearest free pin on ANOTHER comp ──
    var pins = [];
    for (var ci = 0; ci < comps.length; ci++)
      for (var pi = 0; pi < comps[ci].pins.length; pi++) pins.push(comps[ci].pins[pi]);
    // shuffle for varied routing
    for (var s = pins.length - 1; s > 0; s--) { var j = (Math.random() * (s + 1)) | 0; var tmp = pins[s]; pins[s] = pins[j]; pins[j] = tmp; }

    var MAXD = P * 7, maxNets = Math.round((W * H) / 26000), made = 0;
    for (var a = 0; a < pins.length && made < maxNets; a++) {
      var p = pins[a]; if (p.used) continue;
      // limit fan per component so one chip doesn't hog every pin
      if (p.comp.traces.length >= (p.comp.t === "ic" ? 10 : p.comp.t === "conn" ? 14 : 2)) continue;
      var best = null, bd = MAXD;
      for (var b = 0; b < pins.length; b++) {
        var q = pins[b];
        if (q.used || q.comp === p.comp) continue;
        if (q.comp.traces.length >= (q.comp.t === "ic" ? 10 : q.comp.t === "conn" ? 14 : 2)) continue;
        var d = dist(p, q); if (d < bd) { bd = d; best = q; }
      }
      if (!best) continue;
      makeNet(p, best); made++;
    }

    // reveal order = diagonal wipe
    var md = W + H;
    for (var ni = 0; ni < nets.length; ni++) {
      var e = nets[ni], mid = e.pts[(e.pts.length / 2) | 0];
      e.revealAt = (mid.x + mid.y) / md;
    }
  }

  function makeNet(p, q) {
    var STUB = 7;
    var s1 = { x: p.x + p.nx * STUB, y: p.y + p.ny * STUB };
    var s2 = { x: q.x + q.nx * STUB, y: q.y + q.ny * STUB };
    var corners = dogleg(s1.x, s1.y, s2.x, s2.y);
    var pts = [{ x: p.x, y: p.y }, s1].concat(corners, [s2, { x: q.x, y: q.y }]);
    var len = 0;
    for (var k = 1; k < pts.length; k++) len += Math.hypot(pts[k].x - pts[k - 1].x, pts[k].y - pts[k - 1].y);
    var bus = chance(0.18);
    var net = { pts: pts, len: len, bus: bus, aComp: p.comp, bComp: q.comp, revealAt: 0 };
    net.i = nets.length; nets.push(net);
    p.comp.traces.push(net); q.comp.traces.push(net);
    p.used = q.used = true;
    for (var c = 0; c < corners.length; c++) vias.push(corners[c]);
    vias.push({ x: p.x, y: p.y }); vias.push({ x: q.x, y: q.y });
  }

  // ── static board ────────────────────────────────────────────────────
  function renderBoard() {
    board = document.createElement("canvas");
    board.width = Math.floor(W * DPR); board.height = Math.floor(H * DPR);
    bctx = board.getContext("2d"); bctx.setTransform(DPR, 0, 0, DPR, 0, 0);

    var gr = bctx.createRadialGradient(W * 0.5, H * 0.42, 0, W * 0.5, H * 0.5, Math.max(W, H) * 0.8);
    gr.addColorStop(0, GREEN_HI); gr.addColorStop(1, GREEN_LO);
    bctx.fillStyle = gr; bctx.fillRect(0, 0, W, H);

    bctx.strokeStyle = GREEN_POUR; bctx.lineWidth = 1; bctx.save(); bctx.globalAlpha = 0.5;
    for (var hx = -H; hx < W; hx += 7) { if (((hx / 7) | 0) % 3 === 0) continue; bctx.beginPath(); bctx.moveTo(hx, 0); bctx.lineTo(hx + H, H); bctx.stroke(); }
    bctx.restore();

    // copper traces
    bctx.lineJoin = "round"; bctx.lineCap = "round";
    for (var i = 0; i < nets.length; i++) drawTrace(bctx, nets[i], COPPER, nets[i].bus ? 3 : 1.8);
    for (var i2 = 0; i2 < nets.length; i2++) drawTrace(bctx, nets[i2], "rgba(179,133,47,0.5)", nets[i2].bus ? 1.1 : 0.6);

    // vias
    for (var v = 0; v < vias.length; v++) {
      bctx.fillStyle = COPPER_HI; bctx.beginPath(); bctx.arc(vias[v].x, vias[v].y, 2.6, 0, 6.2832); bctx.fill();
      bctx.fillStyle = GREEN_LO; bctx.beginPath(); bctx.arc(vias[v].x, vias[v].y, 1.1, 0, 6.2832); bctx.fill();
    }

    for (var ci = 0; ci < comps.length; ci++) drawComponent(bctx, comps[ci]);

    bctx.fillStyle = SILK; bctx.font = "9px ui-monospace, Menlo, monospace"; bctx.textAlign = "center";
    bctx.globalAlpha = 0.6;
    for (var li = 0; li < labels.length; li++) bctx.fillText(labels[li].s, labels[li].x, labels[li].y);
    bctx.globalAlpha = 1;

    bctx.strokeStyle = "rgba(207,230,212,0.22)"; bctx.lineWidth = 2; bctx.strokeRect(10, 10, W - 20, H - 20);
  }

  function drawTrace(c, e, color, lw) {
    c.strokeStyle = color; c.lineWidth = lw;
    c.beginPath(); c.moveTo(e.pts[0].x, e.pts[0].y);
    for (var p = 1; p < e.pts.length; p++) c.lineTo(e.pts[p].x, e.pts[p].y);
    c.stroke();
  }

  function drawComponent(c, m) {
    var i;
    // pins (silver) for every component
    c.fillStyle = PIN;
    for (i = 0; i < m.pins.length; i++) {
      var pn = m.pins[i];
      if (Math.abs(pn.nx) > Math.abs(pn.ny)) c.fillRect(pn.x - (pn.nx > 0 ? 0 : 4), pn.y - 1.5, 4, 3);
      else c.fillRect(pn.x - 1.5, pn.y - (pn.ny > 0 ? 0 : 4), 3, 4);
    }
    if (m.t === "ic") {
      c.fillStyle = IC_BODY; c.strokeStyle = IC_EDGE; c.lineWidth = 1;
      roundRect(c, m.x, m.y, m.w, m.h, 3); c.fill(); c.stroke();
      c.fillStyle = "rgba(255,255,255,0.05)"; c.fillRect(m.x + 1, m.y + 1, m.w - 2, 2);
      c.fillStyle = "rgba(207,230,212,0.5)"; c.beginPath(); c.arc(m.x + 6, m.y + 6, 1.6, 0, 6.2832); c.fill();
    } else if (m.t === "cap") {
      c.fillStyle = "#23262b"; c.beginPath(); c.arc(m.x, m.y, m.rad, 0, 6.2832); c.fill();
      var g = c.createRadialGradient(m.x - m.rad * 0.3, m.y - m.rad * 0.3, 1, m.x, m.y, m.rad);
      g.addColorStop(0, "#5a5f66"); g.addColorStop(0.6, "#3a3e44"); g.addColorStop(1, "#191b1f");
      c.fillStyle = g; c.beginPath(); c.arc(m.x, m.y, m.rad * 0.86, 0, 6.2832); c.fill();
      c.strokeStyle = "rgba(20,22,26,0.8)"; c.lineWidth = 1.4; c.beginPath();
      c.moveTo(m.x - m.rad * 0.5, m.y); c.lineTo(m.x + m.rad * 0.5, m.y);
      c.moveTo(m.x, m.y - m.rad * 0.5); c.lineTo(m.x, m.y + m.rad * 0.5); c.stroke();
      c.fillStyle = "rgba(180,190,200,0.5)"; c.beginPath();
      c.arc(m.x, m.y, m.rad * 0.86, Math.PI * 0.78, Math.PI * 1.22); c.lineTo(m.x, m.y); c.fill();
    } else if (m.t === "res") {
      var w = (m.x2 - m.x) - 6, mx = (m.x + m.x2) / 2;
      c.fillStyle = "#171a1e"; roundRect(c, mx - w / 2, m.y - 5, w, 10, 1.5); c.fill();
      c.fillStyle = "rgba(255,255,255,0.06)"; c.fillRect(mx - w / 2, m.y - 5, w, 2);
    } else if (m.t === "conn") {
      c.strokeStyle = "rgba(207,230,212,0.3)"; c.lineWidth = 1.5;
      c.strokeRect(m.x - m.pitch * 0.6, m.y - m.pitch * 0.6, (m.n - 1) * m.pitch + m.pitch * 1.2, m.pitch * 1.2);
      for (i = 0; i < m.n; i++) {
        c.fillStyle = PAD; c.beginPath(); c.arc(m.x + i * m.pitch, m.y, m.pitch * 0.34, 0, 6.2832); c.fill();
        c.fillStyle = "#191b1f"; c.beginPath(); c.arc(m.x + i * m.pitch, m.y, m.pitch * 0.15, 0, 6.2832); c.fill();
      }
    }
  }
  function roundRect(c, x, y, w, h, r) {
    c.beginPath(); c.moveTo(x + r, y);
    c.arcTo(x + w, y, x + w, y + h, r); c.arcTo(x + w, y + h, x, y + h, r);
    c.arcTo(x, y + h, x, y, r); c.arcTo(x, y, x + w, y, r); c.closePath();
  }

  // ── electrons flow chip → trace → chip ──────────────────────────────
  function spawnElectron() {
    var with_t = comps.filter(function (c) { return c.traces.length; });
    if (!with_t.length) return null;
    var comp = pick(with_t);
    return { fromComp: comp, edge: pick(comp.traces), seg: 0, t: Math.random(),
             speed: rand(80, 150), color: pick(ELEC), size: rand(1.2, 2.1) };
  }
  function segPts(e, seg, fwd) {
    var pts = e.pts, n = pts.length;
    return [fwd ? pts[seg] : pts[n - 1 - seg], fwd ? pts[seg + 1] : pts[n - 2 - seg]];
  }
  function advance(el, dt) {
    if (!el.edge) { var s = spawnElectron(); if (s) Object.assign(el, s); else el.edge = null; return; }
    var guard = 0;
    while (guard++ < 8) {
      var fwd = el.fromComp === el.edge.aComp, n = el.edge.pts.length;
      var ab = segPts(el.edge, el.seg, fwd);
      var slen = Math.hypot(ab[1].x - ab[0].x, ab[1].y - ab[0].y) || 1;
      el.t += (el.speed * dt) / slen;
      if (el.t < 1) return;
      el.t -= 1; el.seg++;
      if (el.seg < n - 1) continue;          // next segment, same trace
      // arrived at the far component — leave on a DIFFERENT trace
      var arr = fwd ? el.edge.bComp : el.edge.aComp;
      var opts = arr.traces.length > 1 ? arr.traces.filter(function (t) { return t !== el.edge; }) : [];
      if (!opts.length) { var sp = spawnElectron(); if (sp) Object.assign(el, sp); else el.edge = null; return; }
      el.fromComp = arr; el.edge = pick(opts); el.seg = 0; el.t = 0;
      return;
    }
  }
  function epos(el) {
    var fwd = el.fromComp === el.edge.aComp, ab = segPts(el.edge, el.seg, fwd);
    return { x: ab[0].x + (ab[1].x - ab[0].x) * el.t, y: ab[0].y + (ab[1].y - ab[0].y) * el.t };
  }

  function build() {
    W = cv.clientWidth || window.innerWidth; H = cv.clientHeight || window.innerHeight;
    cv.width = Math.floor(W * DPR); cv.height = Math.floor(H * DPR);
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    buildModel(); renderBoard();
    electrons = [];
    var count = Math.max(16, Math.min(56, Math.round((W * H) / 36000)));
    for (var i = 0; i < count; i++) { var e = spawnElectron(); if (e) electrons.push(e); }
  }

  var REVEAL = 2.8, PULSE_PERIOD = 9, PULSE_DUR = 4;
  function frame(ts) {
    if (!t0) t0 = ts;
    var now = (ts - t0) / 1000, dt = Math.min(0.05, now - clock); clock = now;

    ctx.globalAlpha = DIM; ctx.drawImage(board, 0, 0, W, H); ctx.globalAlpha = 1;
    ctx.fillStyle = "rgba(6,12,8,0.40)"; ctx.fillRect(0, 0, W, H);
    var vg = ctx.createRadialGradient(W * 0.5, H * 0.34, 0, W * 0.5, H * 0.4, Math.max(W, H) * 0.55);
    vg.addColorStop(0, "rgba(6,10,8,0.36)"); vg.addColorStop(1, "rgba(6,10,8,0)");
    ctx.fillStyle = vg; ctx.fillRect(0, 0, W, H);

    var reveal = reduce ? 1 : Math.min(1, now / REVEAL);
    var pulseD = -1;
    if (!reduce && now > REVEAL) { var ph = (now - REVEAL) % PULSE_PERIOD; if (ph < PULSE_DUR) pulseD = (ph / PULSE_DUR) * 1.2 - 0.05; }

    ctx.lineJoin = "round"; ctx.lineCap = "round";
    for (var i = 0; i < nets.length; i++) {
      var e = nets[i]; if (e.revealAt > reveal) continue;
      var ta = 0;
      if (reveal < 1) { var d = reveal - e.revealAt; if (d >= 0 && d < 0.06) ta = (1 - d / 0.06) * 0.9; }
      if (pulseD >= 0) { var mid = e.pts[(e.pts.length / 2) | 0]; var pd = Math.abs((mid.x + mid.y) / (W + H) - pulseD); if (pd < 0.10) ta = Math.max(ta, (1 - pd / 0.10) * 0.7); }
      if (ta <= 0.01) continue;
      var col = (pulseD >= 0 && reveal >= 1) ? SCAN : LIT;
      ctx.strokeStyle = "rgba(" + col + "," + ta.toFixed(3) + ")";
      ctx.lineWidth = (e.bus ? 3 : 1.8) + 0.6;
      ctx.shadowBlur = 8 * ta; ctx.shadowColor = "rgba(" + col + ",0.9)";
      drawTrace(ctx, e, ctx.strokeStyle, ctx.lineWidth); ctx.shadowBlur = 0;
    }

    if (reveal >= 0.25) {
      for (var m = 0; m < electrons.length; m++) {
        var el = electrons[m];
        if (!el.edge || el.edge.revealAt > reveal) { var s = spawnElectron(); if (s) Object.assign(el, s); continue; }
        advance(el, dt); if (!el.edge) continue;
        var p2 = epos(el);
        ctx.fillStyle = "rgba(" + el.color + ",1)"; ctx.shadowBlur = 9; ctx.shadowColor = "rgba(" + el.color + ",0.95)";
        ctx.beginPath(); ctx.arc(p2.x, p2.y, el.size, 0, 6.2832); ctx.fill();
        ctx.fillStyle = "rgba(255,255,255,0.85)"; ctx.beginPath(); ctx.arc(p2.x, p2.y, el.size * 0.45, 0, 6.2832); ctx.fill();
        ctx.shadowBlur = 0;
      }
    }
    raf = requestAnimationFrame(frame);
  }

  var raf = 0;
  function start() { if (!raf) raf = requestAnimationFrame(frame); }
  function stop() { cancelAnimationFrame(raf); raf = 0; }
  var rt;
  window.addEventListener("resize", function () { clearTimeout(rt); rt = setTimeout(function () { t0 = 0; build(); }, 220); });
  document.addEventListener("visibilitychange", function () { document.hidden ? stop() : start(); });

  build();
  if (reduce) requestAnimationFrame(function (ts) { t0 = ts - (REVEAL + 1) * 1000; frame(ts); });
  else start();
})();

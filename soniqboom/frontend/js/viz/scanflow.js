// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * viz/scanflow.js — live scan-pipeline visualization for the admin scan panel.
 *
 * Particles flow FS roots → Scanner → Store → AOF as a scan runs; the
 * emission rate tracks the REAL files/sec derived from successive
 * ``scan_progress`` WS payloads (admin.js feeds them via ``onProgress``).
 * When idle the pipeline is calm; during a hot full scan it streams.
 *
 * Canvas-based: nodes + edges + particles all drawn on one canvas so there
 * is zero per-frame DOM churn during a fast scan.
 */
import { registerViz, clamp } from './engine.js';

const NODES = [
  { id: 'fs',   label: 'FS roots', sub: 'walk',    x: 0.10, y: 0.5 },
  { id: 'scan', label: 'Scanner',  sub: 'extract', x: 0.38, y: 0.5 },
  { id: 'store', label: 'Store',   sub: 'in-mem',  x: 0.68, y: 0.32 },
  { id: 'aof',  label: 'AOF',      sub: 'durable', x: 0.68, y: 0.70 },
];
const EDGES = [['fs', 'scan'], ['scan', 'store'], ['scan', 'aof']];

export function mountScanFlow(host) {
  const cv = document.createElement('canvas');
  cv.setAttribute('aria-hidden', 'true');   // decorative — info is in the SR mirror
  host.appendChild(cv);
  const ctx = cv.getContext('2d');
  function size() {
    const dpr = window.devicePixelRatio || 1;
    cv.width = Math.max(1, host.clientWidth * dpr);
    cv.height = Math.max(1, host.clientHeight * dpr);
  }
  size();
  const ro = new ResizeObserver(size); ro.observe(host);

  const sr = document.createElement('div'); sr.className = 'viz-sr-only'; host.appendChild(sr);

  let rate = 0;                 // files/sec (smoothed)
  let lastProg = null;          // {processed, t}
  let emitAcc = 0;
  let particles = [];
  let nodeFlash = {};

  function nodeXY(id, W, H) {
    const n = NODES.find(n => n.id === id);
    return [n.x * W, n.y * H];
  }

  // Called by admin.js on each scan_progress WS event.
  function onProgress(prog) {
    const processed = Number(prog?.processed ?? prog?.done ?? 0);
    const now = performance.now();
    if (lastProg && processed >= lastProg.processed) {
      const dFiles = processed - lastProg.processed;
      const dt = (now - lastProg.t) / 1000;
      if (dt > 0.05) {
        const inst = dFiles / dt;
        rate = rate * 0.6 + inst * 0.4;   // EMA smoothing
      }
    }
    lastProg = { processed, t: now };
    const total = Number(prog?.total ?? 0);
    sr.textContent = total
      ? `Scanning: ${processed} of ${total} files, ${Math.round(rate)}/s`
      : `Scanning: ${processed} files`;
    // Decay rate if no progress arrives (scan finished) — handled in draw.
  }

  const ctl = registerViz({
    host, group: 'admin', fps: 30,
    draw(dt) {
      const dpr = window.devicePixelRatio || 1, W = cv.width, H = cv.height;
      ctx.clearRect(0, 0, W, H);
      // edges
      ctx.strokeStyle = '#243043'; ctx.lineWidth = 1.6 * dpr;
      for (const [a, b] of EDGES) {
        const [ax, ay] = nodeXY(a, W, H), [bx, by] = nodeXY(b, W, H);
        ctx.beginPath(); ctx.moveTo(ax + 30 * dpr, ay);
        ctx.bezierCurveTo(ax + 70 * dpr, ay, bx - 70 * dpr, by, bx - 30 * dpr, by);
        ctx.stroke();
      }
      // emission ∝ rate (with a gentle idle pulse so it's not dead when warm)
      const eRate = clamp(rate / 40, 0, 30);
      emitAcc += dt / 1000 * eRate;
      while (emitAcc >= 1) {
        emitAcc -= 1;
        particles.push({ edge: 0, u: 0, spd: 0.9, kind: 'req' });
        if (Math.random() < 0.85) particles.push({ edge: 1, u: -0.25, spd: 0.8, kind: 'res' });
        if (Math.random() < 0.35) particles.push({ edge: 2, u: -0.3, spd: 0.8, kind: 'evt' });
      }
      // rate decays toward 0 when progress stops arriving
      rate *= Math.pow(0.5, dt / 1500);
      // particles
      for (const p of particles) {
        p.u += dt / 1000 * p.spd;
        if (p.u >= 1) {
          p.dead = true;
          const tgt = EDGES[p.edge][1];
          nodeFlash[tgt] = 1;
        }
        if (p.u < 0) continue;
        const [a, b] = EDGES[p.edge];
        const [ax, ay] = nodeXY(a, W, H), [bx, by] = nodeXY(b, W, H);
        // approximate bezier point
        const t = clamp(p.u, 0, 1);
        const mx = ax + 70 * dpr, my = ay, nx = bx - 70 * dpr, ny = by;
        const x = _bez(ax + 30 * dpr, mx, nx, bx - 30 * dpr, t);
        const y = _bez(ay, my, ny, by, t);
        const col = p.kind === 'req' ? '107,200,240' : p.kind === 'res' ? '116,224,154' : '240,162,58';
        ctx.beginPath(); ctx.arc(x, y, 3 * dpr, 0, 7);
        ctx.fillStyle = `rgb(${col})`; ctx.shadowBlur = 5 * dpr; ctx.shadowColor = `rgb(${col})`;
        ctx.fill(); ctx.shadowBlur = 0;
      }
      particles = particles.filter(p => !p.dead);
      // nodes
      for (const n of NODES) {
        const [x, y] = nodeXY(n.id, W, H);
        const flash = nodeFlash[n.id] || 0;
        ctx.fillStyle = '#18212f';
        _roundRect(ctx, x - 30 * dpr, y - 18 * dpr, 60 * dpr, 36 * dpr, 8 * dpr); ctx.fill();
        ctx.strokeStyle = flash > 0 ? `rgba(107,200,240,${0.4 + flash * 0.6})` : '#243043';
        ctx.lineWidth = 1.5 * dpr; ctx.stroke();
        if (flash > 0) nodeFlash[n.id] = Math.max(0, flash - dt / 200);
        ctx.fillStyle = '#e6edf3'; ctx.font = `600 ${11 * dpr}px sans-serif`; ctx.textAlign = 'center';
        ctx.fillText(n.label, x, y - 1 * dpr);
        ctx.fillStyle = '#8a98ab'; ctx.font = `${9 * dpr}px sans-serif`;   // AA contrast on #18212f
        ctx.fillText(n.sub, x, y + 11 * dpr);
      }
      // rate readout
      ctx.fillStyle = 'rgba(116,224,154,0.9)'; ctx.font = `600 ${11 * dpr}px sans-serif`; ctx.textAlign = 'left';
      ctx.fillText(rate > 1 ? `${Math.round(rate).toLocaleString()} files/s` : 'idle', 12 * dpr, 18 * dpr);
    },
    freeze() {
      // Reduced-motion: paint the static pipeline (edges + nodes at rest, no
      // particles) so the panel shows the architecture instead of a blank box.
      const dpr = window.devicePixelRatio || 1, W = cv.width, H = cv.height;
      ctx.clearRect(0, 0, W, H);
      ctx.strokeStyle = '#243043'; ctx.lineWidth = 1.6 * dpr;
      for (const [a, b] of EDGES) {
        const [ax, ay] = nodeXY(a, W, H), [bx, by] = nodeXY(b, W, H);
        ctx.beginPath(); ctx.moveTo(ax + 30 * dpr, ay);
        ctx.bezierCurveTo(ax + 70 * dpr, ay, bx - 70 * dpr, by, bx - 30 * dpr, by);
        ctx.stroke();
      }
      for (const n of NODES) {
        const [x, y] = nodeXY(n.id, W, H);
        ctx.fillStyle = '#18212f';
        _roundRect(ctx, x - 30 * dpr, y - 18 * dpr, 60 * dpr, 36 * dpr, 8 * dpr); ctx.fill();
        ctx.strokeStyle = '#243043'; ctx.lineWidth = 1.5 * dpr; ctx.stroke();
        ctx.fillStyle = '#e6edf3'; ctx.font = `600 ${11 * dpr}px sans-serif`; ctx.textAlign = 'center';
        ctx.fillText(n.label, x, y - 1 * dpr);
        ctx.fillStyle = '#8a98ab'; ctx.font = `${9 * dpr}px sans-serif`;
        ctx.fillText(n.sub, x, y + 11 * dpr);
      }
    },
  });

  return {
    onProgress,
    unregister() { ctl.unregister(); ro.disconnect(); },
  };
}

function _bez(p0, p1, p2, p3, t) {
  const mt = 1 - t;
  return mt*mt*mt*p0 + 3*mt*mt*t*p1 + 3*mt*t*t*p2 + t*t*t*p3;
}
function _roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

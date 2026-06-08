// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * viz/ftplanes.js — FTP connection-pool lanes for the admin FTP panel.
 *
 * Each server's pool is shown as priority lanes (Stream / Scan); in-flight
 * transfers ride the lanes as moving dots, so pool saturation is visible at
 * a glance.  Driven by polling ``GET /api/admin/ftp-pool/status``.
 *
 * Tolerant of the exact payload shape: reads a per-pool ``in_use``/``active``
 * count and ``size`` if present, and degrades to calm idle lanes otherwise.
 */
import { registerViz, rand } from './engine.js';

export function mountFtpLanes(host, { pollMs = 2000 } = {}) {
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

  // lanes: [{label, prio, target (desired dot count), dots:[]}]
  let lanes = [];
  let pollTimer = null;

  function _laneCount(pool, key) {
    for (const k of key) {
      const v = pool && pool[k];
      if (typeof v === 'number') return v;
    }
    return 0;
  }

  async function poll() {
    // Don't poll a backgrounded tab or a host that isn't currently rendered
    // (admin section collapsed / navigated away) — offsetParent is null when
    // the host or an ancestor is display:none.
    if (document.hidden || host.offsetParent === null) return;
    try {
      const r = await fetch('/api/admin/ftp-pool/status', { credentials: 'same-origin' });
      if (!r.ok) return;
      const j = await r.json();
      const pools = Array.isArray(j) ? j : (j.servers || j.pools || []);
      const next = [];
      for (const pool of pools) {
        const label = pool.label || pool.host || pool.server || 'pool';
        // The live in-use count is nested under `pool.live` (the snapshot
        // from FtpConnectionPool.status()); it is null until the pool has
        // ever connected.  Lane capacities are `scan_budget`/`stream_budget`.
        // Fall back to flat/legacy keys so the harness + older shapes work.
        const live = pool.live || pool;
        const inUse = _laneCount(live, ['in_use', 'active', 'busy', 'in_flight']);
        const streamCap = _laneCount(pool, ['stream_budget', 'stream_size', 'stream', 'stream_workers']) || 0;
        const scanCap = _laneCount(pool, ['scan_budget', 'scan_size', 'scan', 'scan_workers']) || 0;
        // Use the REAL per-lane in-use breakdown the pool now reports, so a
        // scan-heavy reindex lights up the SCAN lane (not stream).  Fall back
        // to a stream-first split only for older backends without the field.
        let streamUse, scanUse;
        if (typeof live.in_use_stream === 'number' || typeof live.in_use_scan === 'number') {
          streamUse = live.in_use_stream || 0;
          scanUse = live.in_use_scan || 0;
        } else {
          streamUse = Math.min(inUse, Math.max(1, streamCap || 2));
          scanUse = Math.max(0, inUse - streamUse);
        }
        next.push({ label: `${label} · stream`, prio: true, target: streamUse, dots: [] });
        if (scanCap > 0 || scanUse > 0) {
          next.push({ label: `${label} · scan`, prio: false, target: scanUse, dots: [] });
        }
      }
      // Preserve existing dots where lane labels match (smooth transition).
      for (const nl of next) {
        const old = lanes.find(l => l.label === nl.label);
        if (old) nl.dots = old.dots;
      }
      lanes = next;
      const totalInFlight = lanes.reduce((a, l) => a + l.target, 0);
      sr.textContent = lanes.length
        ? `${lanes.length} pool lane(s), ${totalInFlight} transfer(s) in flight`
        : 'No FTP pools active.';
    } catch { /* ignore */ }
  }
  poll();
  pollTimer = setInterval(poll, pollMs);

  let spawnAcc = 0;
  const ctl = registerViz({
    host, group: 'admin', fps: 30,
    draw(dt) {
      const dpr = window.devicePixelRatio || 1, W = cv.width, H = cv.height;
      ctx.clearRect(0, 0, W, H);
      if (!lanes.length) {
        ctx.fillStyle = 'rgba(147,161,179,0.5)'; ctx.font = `${12 * dpr}px sans-serif`; ctx.textAlign = 'center';
        ctx.fillText('no active FTP pools', W / 2, H / 2);
        return;
      }
      spawnAcc += dt / 1000;
      const laneGap = H / (lanes.length + 1);
      lanes.forEach((lane, i) => {
        const ly = laneGap * (i + 1);
        // top up / drain dots toward target
        if (spawnAcc > 0.25 && lane.dots.length < lane.target) {
          lane.dots.push({ u: 0, spd: lane.prio ? rand(0.45, 0.65) : rand(0.2, 0.32), size: lane.prio ? rand(4, 6) : rand(3, 4.5) });
        }
        // rail
        ctx.strokeStyle = lane.prio ? 'rgba(107,200,240,0.28)' : 'rgba(147,161,179,0.18)';
        ctx.lineWidth = 1 * dpr; ctx.beginPath();
        ctx.moveTo(90 * dpr, ly); ctx.lineTo(W - 24 * dpr, ly); ctx.stroke();
        // label
        ctx.fillStyle = lane.prio ? 'rgba(107,200,240,0.85)' : 'rgba(147,161,179,0.7)';
        ctx.font = `${10 * dpr}px sans-serif`; ctx.textAlign = 'left';
        ctx.fillText(lane.label, 10 * dpr, ly - 7 * dpr);
        // dots
        for (const d of lane.dots) {
          d.u += dt / 1000 * d.spd;
          const dx = (90 + (W / dpr - 120) * d.u) * dpr;
          const c = lane.prio ? '107,200,240' : '147,161,179';
          ctx.beginPath(); ctx.arc(dx, ly, d.size * dpr, 0, 7);
          ctx.fillStyle = `rgb(${c})`; ctx.shadowBlur = 8 * dpr; ctx.shadowColor = `rgb(${c})`;
          ctx.fill(); ctx.shadowBlur = 0;
        }
        // recycle finished dots (loop while transfer "ongoing"); drain to target
        lane.dots = lane.dots.filter(d => d.u < 1);
        while (lane.dots.length > lane.target && lane.dots.length) lane.dots.pop();
      });
      if (spawnAcc > 0.25) spawnAcc = 0;
    },
    freeze() {
      // Reduced-motion: draw the lane rails + labels at rest (no moving dots).
      const dpr = window.devicePixelRatio || 1, W = cv.width, H = cv.height;
      ctx.clearRect(0, 0, W, H);
      if (!lanes.length) {
        ctx.fillStyle = 'rgba(147,161,179,0.6)'; ctx.font = `${12 * dpr}px sans-serif`;
        ctx.textAlign = 'center'; ctx.fillText('no active FTP pools', W / 2, H / 2);
        return;
      }
      const laneGap = H / (lanes.length + 1);
      lanes.forEach((lane, i) => {
        const ly = laneGap * (i + 1);
        ctx.strokeStyle = lane.prio ? 'rgba(107,200,240,0.28)' : 'rgba(147,161,179,0.18)';
        ctx.lineWidth = 1 * dpr; ctx.beginPath();
        ctx.moveTo(90 * dpr, ly); ctx.lineTo(W - 24 * dpr, ly); ctx.stroke();
        ctx.fillStyle = lane.prio ? 'rgba(107,200,240,0.85)' : 'rgba(147,161,179,0.7)';
        ctx.font = `${10 * dpr}px sans-serif`; ctx.textAlign = 'left';
        ctx.fillText(lane.label, 10 * dpr, ly - 7 * dpr);
      });
    },
  });

  return {
    unregister() { ctl.unregister(); ro.disconnect(); if (pollTimer) clearInterval(pollTimer); },
  };
}

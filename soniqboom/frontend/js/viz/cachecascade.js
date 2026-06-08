// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * viz/cachecascade.js — cache-hierarchy cascade for the admin Renderers tab.
 *
 * Requests drop in at the top and fall through the cache tiers; each tier
 * "catches" a request green at its REAL hit-rate, or the request misses red
 * to disk.  Driven by ``GET /api/admin/cache-stats`` (see core/cache_stats.py)
 * — the per-tier ``hit_rate`` is the resolve probability and the summed
 * ``rate_1s`` paces the drop cadence, so the animation reflects actual cache
 * behaviour rather than a synthetic model.  Only tiers that have seen traffic
 * are drawn (keeps it honest — an unexercised tier shows nothing, not a fake
 * 0%).
 *
 * Canvas (not per-frame DOM) to avoid reflow/GC churn during animation.
 */
import { registerViz, clamp, rand } from './engine.js';

const TIER_LABELS = {
  browse: 'Browse cache', scan_root: 'Scan-root sort', per_path: 'Per-path result',
  conversion: 'Conversion cache', art: 'Art cache', remote: 'Remote cache', zip: 'ZIP extract',
};

export function mountCacheCascade(host, { pollMs = 1500 } = {}) {
  const cv = document.createElement('canvas');
  host.appendChild(cv);
  cv.setAttribute('aria-hidden', 'true');   // decorative — info is in the SR mirror
  const ctx = cv.getContext('2d');
  function size() {
    const dpr = window.devicePixelRatio || 1;
    cv.width = Math.max(1, host.clientWidth * dpr);
    cv.height = Math.max(1, host.clientHeight * dpr);
  }
  size();
  const ro = new ResizeObserver(size); ro.observe(host);

  // a11y mirror
  const sr = document.createElement('div'); sr.className = 'viz-sr-only';
  sr.setAttribute('aria-live', 'off'); host.appendChild(sr);

  let tiers = [];          // [{key, label, hitRate, y0..}]
  let totalRate = 0;       // summed rate_1s
  let particles = [];
  let dropAcc = 0;
  let stat = { hit: 0, total: 0, depth: 0 };
  let pollTimer = null;
  let _diskFlash = 0;

  async function poll() {
    // Skip a backgrounded tab or a host that isn't currently rendered
    // (admin section collapsed / navigated away).
    if (document.hidden || host.offsetParent === null) return;
    try {
      const r = await fetch('/api/admin/cache-stats', { credentials: 'same-origin' });
      if (!r.ok) return;
      const j = await r.json();
      const active = Object.entries(j.tiers || {})
        .filter(([, d]) => (d.hits + d.misses) > 0)
        .map(([key, d]) => ({ key, label: TIER_LABELS[key] || key, hitRate: d.hit_rate, rate1s: d.rate_1s, hits: d.hits, misses: d.misses }));
      tiers = active;
      totalRate = active.reduce((a, t) => a + (t.rate1s || 0), 0);
      sr.textContent = active.length
        ? active.map(t => `${t.label}: ${Math.round(t.hitRate * 100)}% hit (${t.misses} to disk)`).join('; ')
        : 'No cache activity yet.';
    } catch { /* ignore — admin may be mid-restart */ }
  }
  poll();
  pollTimer = setInterval(poll, pollMs);

  function drop() {
    particles.push({ x: 0.5 + rand(-0.06, 0.06), y: -0.04, vy: 0.55, tier: 0, phase: 'down', kind: 'req' });
  }

  const ctl = registerViz({
    host, group: 'admin', fps: 30,
    draw(dt) {
      const dpr = window.devicePixelRatio || 1;
      const W = cv.width, H = cv.height;
      ctx.clearRect(0, 0, W, H);
      if (!tiers.length) {
        ctx.fillStyle = 'rgba(147,161,179,0.5)';
        ctx.font = `${12 * dpr}px sans-serif`; ctx.textAlign = 'center';
        ctx.fillText('waiting for cache activity…', W / 2, H / 2);
        return;
      }
      const n = tiers.length + 1;            // +1 for disk row
      const pad = 8 * dpr, rowH = (H - pad * 2) / n, gap = rowH * 0.18;
      const barX = Math.min(150 * dpr, W * 0.42), barW = W - barX - 10 * dpr;
      tiers.forEach((t, i) => {
        t.cy = pad + i * rowH + rowH / 2;
        t.top = pad + i * rowH + gap / 2;
        t.h = rowH - gap;
        // row
        ctx.fillStyle = t._flash > 0 ? `rgba(116,224,154,${0.10 + t._flash * 0.12})` : '#141c28';
        _roundRect(ctx, barX, t.top, barW, t.h, 6 * dpr);
        ctx.fill();
        ctx.strokeStyle = t._flash > 0 ? 'rgba(116,224,154,0.8)' : '#243043';
        ctx.lineWidth = 1.4 * dpr; ctx.stroke();
        if (t._flash > 0) t._flash = Math.max(0, t._flash - dt / 260);
        // label + live hit-rate
        ctx.fillStyle = 'rgba(147,161,179,0.9)'; ctx.font = `${10.5 * dpr}px sans-serif`;
        ctx.textAlign = 'right'; ctx.fillText(t.label, barX - 8 * dpr, t.cy + 4 * dpr);
        ctx.textAlign = 'left'; ctx.fillStyle = 'rgba(116,224,154,0.7)';
        ctx.fillText(`${Math.round(t.hitRate * 100)}%`, barX + 8 * dpr, t.cy + 4 * dpr);
      });
      // disk row
      const diskTop = pad + tiers.length * rowH + gap / 2, diskH = rowH - gap;
      const diskCy = diskTop + diskH / 2;
      ctx.fillStyle = _diskFlash > 0 ? `rgba(239,107,141,${0.12 + _diskFlash * 0.12})` : '#1a1016';
      _roundRect(ctx, barX, diskTop, barW, diskH, 6 * dpr); ctx.fill();
      ctx.strokeStyle = _diskFlash > 0 ? 'rgba(239,107,141,0.85)' : '#3a1f2a';
      ctx.lineWidth = 1.4 * dpr; ctx.stroke();
      if (_diskFlash > 0) _diskFlash = Math.max(0, _diskFlash - dt / 260);
      ctx.fillStyle = 'rgba(239,107,141,0.9)'; ctx.font = `${10.5 * dpr}px sans-serif`;
      ctx.textAlign = 'right'; ctx.fillText('Disk / fetch', barX - 8 * dpr, diskCy + 4 * dpr);

      // drop cadence — scale to real total rate, with a gentle floor so the
      // viz still breathes on a quiet but warm cache.
      const cadence = clamp(0.4 + totalRate * 0.5, 0.6, 8);
      dropAcc += dt / 1000 * cadence;
      while (dropAcc >= 1) { dropAcc -= 1; drop(); }

      const cxPix = barX + barW / 2;
      for (const p of particles) {
        if (p.phase === 'down') {
          p.y += p.vy * dt / 1000;
          const yPix = p.y * H;
          if (p.tier < tiers.length) {
            const t = tiers[p.tier];
            if (yPix >= t.cy) {
              if (Math.random() < t.hitRate) {
                t._flash = 1; p.phase = 'up'; p.kind = 'res'; p.vy = 0.9;
                stat.hit++; stat.total++; stat.depth += p.tier + 1;
              } else { p.tier++; }
            }
          } else {
            if (yPix >= diskCy) {
              _diskFlash = 1; p.phase = 'up'; p.kind = 'err'; p.vy = 0.45;
              stat.total++; stat.depth += tiers.length + 1;
            }
          }
          if (yPix > H + 12) p.dead = true;
        } else {
          p.y -= p.vy * dt / 1000;
          if (p.y < -0.05) p.dead = true;
        }
        if (!p.dead) {
          const col = p.kind === 'req' ? '107,200,240' : p.kind === 'res' ? '116,224,154' : '239,107,141';
          ctx.beginPath(); ctx.arc(cxPix + (p.x - 0.5) * barW * 0.6, p.y * H, 3.4 * dpr, 0, 7);
          ctx.fillStyle = `rgb(${col})`; ctx.shadowBlur = 6 * dpr; ctx.shadowColor = `rgb(${col})`;
          ctx.fill(); ctx.shadowBlur = 0;
        }
      }
      particles = particles.filter(p => !p.dead);
    },
    freeze() {
      // Reduced-motion: draw the tier rows + disk row at rest (no falling
      // particles) so the cascade structure + live hit-rates are still shown.
      const dpr = window.devicePixelRatio || 1, W = cv.width, H = cv.height;
      ctx.clearRect(0, 0, W, H);
      if (!tiers.length) {
        ctx.fillStyle = 'rgba(147,161,179,0.6)'; ctx.font = `${12 * dpr}px sans-serif`;
        ctx.textAlign = 'center'; ctx.fillText('cache cascade (paused)', W / 2, H / 2);
        return;
      }
      const n = tiers.length + 1, pad = 8 * dpr, rowH = (H - pad * 2) / n, gap = rowH * 0.18;
      const barX = Math.min(150 * dpr, W * 0.42), barW = W - barX - 10 * dpr;
      tiers.forEach((t, i) => {
        const top = pad + i * rowH + gap / 2, cy = pad + i * rowH + rowH / 2;
        ctx.fillStyle = '#141c28'; _roundRect(ctx, barX, top, barW, rowH - gap, 6 * dpr); ctx.fill();
        ctx.strokeStyle = '#243043'; ctx.lineWidth = 1.4 * dpr; ctx.stroke();
        ctx.fillStyle = 'rgba(147,161,179,0.9)'; ctx.font = `${10.5 * dpr}px sans-serif`;
        ctx.textAlign = 'right'; ctx.fillText(t.label, barX - 8 * dpr, cy + 4 * dpr);
        ctx.textAlign = 'left'; ctx.fillStyle = 'rgba(116,224,154,0.7)';
        ctx.fillText(`${Math.round(t.hitRate * 100)}%`, barX + 8 * dpr, cy + 4 * dpr);
      });
      const diskTop = pad + tiers.length * rowH + gap / 2, diskCy = diskTop + (rowH - gap) / 2;
      ctx.fillStyle = '#1a1016'; _roundRect(ctx, barX, diskTop, barW, rowH - gap, 6 * dpr); ctx.fill();
      ctx.strokeStyle = '#3a1f2a'; ctx.lineWidth = 1.4 * dpr; ctx.stroke();
      ctx.fillStyle = 'rgba(239,107,141,0.9)'; ctx.font = `${10.5 * dpr}px sans-serif`;
      ctx.textAlign = 'right'; ctx.fillText('Disk / fetch', barX - 8 * dpr, diskCy + 4 * dpr);
    },
  });

  return {
    unregister() { ctl.unregister(); ro.disconnect(); if (pollTimer) clearInterval(pollTimer); },
  };
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

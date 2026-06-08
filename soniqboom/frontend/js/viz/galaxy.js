// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * viz/galaxy.js — "Library Galaxy" view: every format a constellation.
 *
 * Each track is a star; stars cluster by format into glowing nebulae sized
 * by track count.  Driven by ``GET /api/library/formats``.  Usability-first:
 * a deliberately rich star field (thousands of points) drifting + twinkling,
 * with clickable cluster labels that filter the library to that format.
 *
 * Canvas, sampled to a star budget chosen for smooth 60 fps on a large
 * library — not a memory constraint, a frame-rate one (the visual must stay
 * fluid; memory is plentiful).
 */
import { registerViz, rand } from './engine.js';

// Stable hue per format family so the same format always lands the same colour.
const HUE = {
  SID: 280, PSID: 280,
  // Spread the tracker family across a wide hue band — the three biggest
  // (ProTracker, FastTracker 2, Impulse Tracker) were within 30° and read as
  // the same green for tracker-heavy libraries.
  ProTracker: 150, 'FastTracker 2': 95, 'Impulse Tracker': 188, 'ScreamTracker 3': 70,
  MOD: 150, XM: 95, IT: 188, S3M: 70,
  FLAC: 200, ALAC: 190, WAV: 210, AIFF: 210,
  MP3: 35, 'Ogg Vorbis': 45, Opus: 50, AAC: 30,
  DSD: 320, DSF: 320, DFF: 320,
  MIDI: 120, NSF: 100, SPC: 110,
};
function hueFor(fmt, i) {
  const f = String(fmt || '');
  if (HUE[f] != null) return HUE[f];
  const u = f.toUpperCase();
  for (const k in HUE) if (u.includes(k.toUpperCase())) return HUE[k];
  return (i * 53) % 360;                  // stable spread for unknowns
}

const STAR_BUDGET = 4000;                 // total rendered stars (fps-bound)

export function mountGalaxy(host, { onPickFormat } = {}) {
  const cv = document.createElement('canvas');
  cv.setAttribute('aria-hidden', 'true');   // decorative — info is in the legend
  host.appendChild(cv);
  const ctx = cv.getContext('2d');
  function size() {
    const dpr = window.devicePixelRatio || 1;
    cv.width = Math.max(1, host.clientWidth * dpr);
    cv.height = Math.max(1, host.clientHeight * dpr);
  }
  size();
  const ro = new ResizeObserver(size); ro.observe(host);

  // a11y: a real list of formats + counts (canvas is decorative).
  const sr = document.createElement('div');
  sr.className = 'galaxy-legend';
  host.appendChild(sr);

  let clusters = [];   // {fmt, count, hue, cx, cy, r, n}
  let stars = [];      // {cluster, x, y, tw, sp, base}
  let t = 0;

  function layout(formats) {
    const total = formats.reduce((a, f) => a + f.count, 0) || 1;
    // place clusters on a loose spiral so big ones spread out
    const N = formats.length;
    clusters = formats.map((f, i) => {
      const ang = i * 2.399963;                  // golden angle
      const rad = 0.08 + 0.40 * Math.sqrt(i / Math.max(1, N - 1));
      return {
        fmt: f.format, count: f.count, hue: hueFor(f.format, i),
        cx: 0.5 + Math.cos(ang) * rad,
        // Keep clusters in the top ~60% of the canvas so the bottom legend
        // band (which doubles as the SR mirror + filter chips) never occludes
        // a constellation or its label.
        cy: 0.30 + Math.sin(ang) * rad * 0.44,
        spread: 0.04 + 0.10 * Math.sqrt(f.count / total),
        n: Math.max(6, Math.round(STAR_BUDGET * (f.count / total))),
      };
    });
    stars = [];
    for (const c of clusters) {
      for (let i = 0; i < c.n; i++) {
        const a = Math.random() * 7, r = Math.pow(Math.random(), 0.55) * c.spread;
        stars.push({
          c, x: c.cx + Math.cos(a) * r, y: c.cy + Math.sin(a) * r * 0.85,
          tw: Math.random() * 7, sp: rand(0.4, 1.3),
        });
      }
    }
    // Hard cap total stars — guards against a pathological distinct-format
    // count ballooning the array (the per-cluster floor of 6 can stack).
    if (stars.length > STAR_BUDGET + 600) stars.length = STAR_BUDGET + 600;
    // legend (also the SR + click targets)
    sr.innerHTML = '';
    formats.forEach((f, i) => {
      const chip = document.createElement('button');
      chip.className = 'galaxy-chip';
      chip.style.setProperty('--gx-hue', hueFor(f.format, i));
      chip.textContent = `${f.format} · ${f.count.toLocaleString()}`;
      chip.addEventListener('click', () => onPickFormat && onPickFormat(f.format, f.count));
      sr.appendChild(chip);
    });
  }

  async function load() {
    try {
      const r = await fetch('/api/library/formats', { credentials: 'same-origin' });
      if (!r.ok) return;
      const formats = await r.json();
      if (Array.isArray(formats) && formats.length) {
        layout(formats);
      } else {
        // Empty library / pre-scan: show a real message (also the SR text)
        // instead of a silent black panel.
        clusters = []; stars = [];
        sr.innerHTML = '<span class="galaxy-empty">No formats indexed yet — run a library scan to populate the galaxy.</span>';
      }
    } catch { /* ignore */ }
  }
  load();

  const ctl = registerViz({
    host, group: 'library', fps: 60,
    draw(dt) {
      const dpr = window.devicePixelRatio || 1, W = cv.width, H = cv.height;
      t += dt / 1000;
      // soft trail fade for a nebula glow
      ctx.fillStyle = 'rgba(7,10,15,0.30)';
      ctx.fillRect(0, 0, W, H);
      for (const s of stars) {
        const tw = 0.45 + 0.55 * Math.sin(t * s.sp + s.tw);
        const x = s.x * W + Math.sin(t * 0.16 + s.tw) * 3 * dpr;
        const y = s.y * H + Math.cos(t * 0.14 + s.tw) * 3 * dpr;
        ctx.beginPath();
        ctx.arc(x, y, (0.7 + tw * 1.3) * dpr, 0, 7);
        ctx.fillStyle = `hsla(${s.c.hue}, 82%, ${52 + tw * 26}%, ${0.35 + tw * 0.55})`;
        ctx.fill();
      }
      // cluster labels
      ctx.textAlign = 'center';
      for (const c of clusters) {
        ctx.font = `${10.5 * dpr}px sans-serif`;
        ctx.fillStyle = `hsla(${c.hue}, 70%, 72%, 0.85)`;
        ctx.fillText(c.fmt, c.cx * W, (c.cy - c.spread) * H - 6 * dpr);
      }
    },
    freeze() {
      // static star field (no twinkle/drift)
      const dpr = window.devicePixelRatio || 1, W = cv.width, H = cv.height;
      ctx.fillStyle = '#070a0f'; ctx.fillRect(0, 0, W, H);
      for (const s of stars) {
        ctx.beginPath(); ctx.arc(s.x * W, s.y * H, 1.1 * dpr, 0, 7);
        ctx.fillStyle = `hsla(${s.c.hue}, 80%, 60%, 0.7)`; ctx.fill();
      }
    },
  });

  return {
    reload: load,
    unregister() { ctl.unregister(); ro.disconnect(); },
  };
}

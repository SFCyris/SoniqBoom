// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * eq/modern.js — the "Modern" look of the equalizer panel: a glowing response
 * curve over a gradient area fill (accent → amber → transparent) that breathes
 * with the music's loudness, a soft blurred spectrum of the playing audio (the
 * post-EQ spectrum in the accent, the pre-EQ one faint behind it) and a glow
 * under each band's knob from that band's level.  The knobs, labels and panel
 * surface are DOM/CSS: #eq-panel[data-eq-style="modern"] in app.css.
 * Colours come from CSS custom properties (light and dark theme) — `vars`.
 */

import { registerEqStyle, cssAlpha as _alpha } from './engine.js';

const GRID_FREQS = [20, 30, 40, 50, 60, 70, 80, 90, 100, 200, 300, 400, 500, 600, 700, 800, 900,
  1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000, 20000];
// Axis labels: the decades, faint, along the top of the plot (the band
// labels under the graph are the main frequency reference).
const LABEL_FREQS = [[20, '20 Hz'], [100, '100'], [1000, '1k'], [10000, '10k']];
const DB_LINES = [12, 6, 0, -6, -12];

// Low-resolution layer for the spectrum: drawn small, scaled up smoothed =
// a cheap blur (a canvas `filter: blur()` per frame costs far more).
let _spec = null, _specG = null;
const SPEC_SCALE = 0.25;

function _curvePath(g, s, db) {
  const n = s.curveFreqs.length;
  const top = s.plot.t - 4, bot = s.plot.t + s.plot.h + 4;
  for (let i = 0; i < n; i++) {
    const x = s.x(s.curveFreqs[i]);
    let y = s.y(db[i]);
    y = y < top ? top : y > bot ? bot : y;
    if (i) g.lineTo(x, y); else g.moveTo(x, y);
  }
}

// The spectrum's contour (smoothed through midpoints); `closed` adds the
// sides and the floor for a fill.
function _specPath(g, s, vals, scale, closed = true) {
  const { plot } = s;
  const base = (plot.t + plot.h) * scale;
  const n = s.specFreqs.length;
  g.beginPath();
  let px = 0, py = 0;
  for (let i = 0; i < n; i++) {
    const x = s.x(s.specFreqs[i]) * scale;
    const y = (plot.t + plot.h - vals[i] * plot.h * 0.92) * scale;
    if (!i) {
      if (closed) { g.moveTo(x, base); g.lineTo(x, y); } else g.moveTo(x, y);
    } else {
      g.quadraticCurveTo(px, py, (px + x) / 2, (py + y) / 2);
    }
    px = x; py = y;
  }
  g.lineTo(px, py);
  if (closed) { g.lineTo(px, base); g.closePath(); }
}

const modern = {
  name: 'modern',
  vars: {
    accent: '--eq-accent', amber: '--eq-amber', grid: '--eq-grid', gridStrong: '--eq-grid-strong',
    label: '--eq-label', pre: '--eq-spec-pre', preLine: '--eq-spec-pre-line', post: '--eq-spec-post', ghost: '--eq-ghost',
    plotBg: '--eq-plot-bg', glow: '--eq-glow', line: '--eq-line', lineMid: '--eq-line-mid',
  },

  drawGrid(g, s, T) {
    const { plot } = s;
    // plot surface
    if (T.plotBg) {
      g.fillStyle = T.plotBg;
      const r = 10;
      g.beginPath();
      if (g.roundRect) g.roundRect(plot.l, plot.t, plot.w, plot.h, r);
      else g.rect(plot.l, plot.t, plot.w, plot.h);
      g.fill();
    }
    g.save();
    g.beginPath(); g.rect(plot.l, plot.t, plot.w, plot.h); g.clip();
    g.lineWidth = 1;
    for (const f of GRID_FREQS) {
      const x = Math.round(s.x(f)) + 0.5;
      const major = f === 100 || f === 1000 || f === 10000;
      g.strokeStyle = major ? T.gridStrong : T.grid;
      g.globalAlpha = major ? 1 : 0.55;
      g.beginPath(); g.moveTo(x, plot.t); g.lineTo(x, plot.t + plot.h); g.stroke();
    }
    g.globalAlpha = 1;
    for (const db of DB_LINES) {
      const y = Math.round(s.y(db)) + 0.5;
      g.strokeStyle = db === 0 ? T.gridStrong : T.grid;
      g.setLineDash(db === 0 ? [] : [3, 4]);
      g.beginPath(); g.moveTo(plot.l, y); g.lineTo(plot.l + plot.w, y); g.stroke();
    }
    g.setLineDash([]);
    g.restore();
    // labels
    g.fillStyle = T.label;
    g.font = `500 10px ${T.font}`;
    g.textBaseline = 'middle';
    g.textAlign = 'right';
    for (const db of DB_LINES) {
      g.fillText(db > 0 ? `+${db}` : db < 0 ? `−${-db}` : '0', plot.l - 6, s.y(db));
    }
    g.textBaseline = 'top';
    g.font = `500 9.5px ${T.font}`;
    g.globalAlpha = 0.7;
    for (const [f, txt] of LABEL_FREQS) {
      const x = s.x(f);
      g.textAlign = 'left';
      g.fillText(txt, x + 5, plot.t + 5);
    }
    g.globalAlpha = 1;
  },

  drawFrame(g, s, T) {
    const { plot } = s;
    const accent = T.accent || '#f0722a', amber = T.amber || '#ffb347';
    g.save();
    g.beginPath(); g.rect(plot.l, plot.t - 6, plot.w, plot.h + 12); g.clip();

    // 1. spectrum (blurred): pre faint, post in the accent
    if (s.post || s.pre) {
      const sw = Math.max(1, Math.round(s.w * SPEC_SCALE)), sh = Math.max(1, Math.round(s.h * SPEC_SCALE));
      if (!_spec) { _spec = document.createElement('canvas'); _specG = _spec.getContext('2d'); }
      if (_spec.width !== sw || _spec.height !== sh) { _spec.width = sw; _spec.height = sh; }
      const sg = _specG;
      sg.clearRect(0, 0, sw, sh);
      if (s.pre) {
        _specPath(sg, s, s.pre, SPEC_SCALE);
        sg.fillStyle = T.pre || 'rgba(255,255,255,0.08)';
        sg.fill();
      }
      if (s.post) {
        _specPath(sg, s, s.post, SPEC_SCALE);
        const gr = sg.createLinearGradient(0, plot.t * SPEC_SCALE, 0, (plot.t + plot.h) * SPEC_SCALE);
        gr.addColorStop(0, _alpha(T.post || accent, 0.34));
        gr.addColorStop(0.6, _alpha(T.post || accent, 0.12));
        gr.addColorStop(1, _alpha(T.post || accent, 0));
        sg.fillStyle = gr;
        sg.fill();
      }
      g.imageSmoothingEnabled = true;
      g.imageSmoothingQuality = 'high';
      g.drawImage(_spec, 0, 0, sw, sh, 0, 0, s.w, s.h);
      // the post-EQ spectrum's outline, sharp, so its shape reads under the curve
      if (s.post) {
        _specPath(g, s, s.post, 1, false);
        g.strokeStyle = _alpha(T.post || accent, 0.32);
        g.lineWidth = 1;
        g.stroke();
      }
      if (s.pre) {
        _specPath(g, s, s.pre, 1, false);
        g.strokeStyle = T.preLine || 'rgba(255,255,255,0.22)';
        g.lineWidth = 1;
        g.setLineDash([2, 3]);
        g.stroke();
        g.setLineDash([]);
      }
    }

    // 2. band glows (under the knobs), from each band's post-EQ level
    for (const b of s.bands) {
      const lv = s.post ? b.level : 0;
      if (lv <= 0.02) continue;
      const r = 12 + 26 * lv;
      const gr = g.createRadialGradient(b.x, b.y, 0, b.x, b.y, r);
      gr.addColorStop(0, _alpha(T.glow || accent, 0.10 + 0.5 * lv));
      gr.addColorStop(1, _alpha(T.glow || accent, 0));
      g.fillStyle = gr;
      g.fillRect(b.x - r, b.y - r, r * 2, r * 2);
    }

    // 3. area fill under the curve — breathes with the loudness
    const pulse = s.motion && s.post ? s.loud : 0;
    const y0 = s.y(0);
    const fill = g.createLinearGradient(0, plot.t, 0, plot.t + plot.h);
    const a1 = (0.34 + 0.24 * pulse) * (1 - 0.55 * s.bypassMix);
    const z = Math.max(0.05, Math.min(0.8, (y0 - plot.t) / plot.h));
    fill.addColorStop(0, _alpha(accent, a1));
    fill.addColorStop(z, _alpha(amber, a1 * 0.42));
    fill.addColorStop(Math.min(1, z + 0.2), _alpha(amber, 0));
    g.beginPath();
    _curvePath(g, s, s.curveDb);
    g.lineTo(plot.l + plot.w, plot.t + plot.h + 6);
    g.lineTo(plot.l, plot.t + plot.h + 6);
    g.closePath();
    g.fillStyle = fill;
    g.fill();

    // 4. bypass: the curve that is bypassed, dashed and faint
    if (s.ghostDb) {
      g.beginPath();
      _curvePath(g, s, s.ghostDb);
      g.setLineDash([5, 5]);
      g.lineWidth = 1.5;
      g.strokeStyle = T.ghost || 'rgba(255,255,255,0.35)';
      g.globalAlpha = s.bypassMix;
      g.stroke();
      g.setLineDash([]);
      g.globalAlpha = 1;
    }

    // 5. the curve: soft glow + crisp gradient line
    const la = T.line || accent, lm = T.lineMid || amber;
    const line = g.createLinearGradient(plot.l, 0, plot.l + plot.w, 0);
    line.addColorStop(0, la);
    line.addColorStop(0.55, lm);
    line.addColorStop(1, la);
    g.beginPath();
    _curvePath(g, s, s.curveDb);
    g.lineJoin = 'round'; g.lineCap = 'round';
    g.strokeStyle = _alpha(la, 0.16 + 0.12 * pulse);
    g.lineWidth = 7;
    g.stroke();
    g.strokeStyle = line;
    g.lineWidth = 2.25;
    g.stroke();
    g.restore();
  },
};

registerEqStyle('modern', modern);
export default modern;

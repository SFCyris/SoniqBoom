// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * viz/signalchain.js — decode-pipeline visualization for the Track Info modal.
 *
 * Structural (NOT audio-reactive) so it never competes with the VU/waveform.
 * Shows the per-format decode chain for the playing track — e.g.
 * SID → libsidplay → PCM → ReplayGain → WebAudio → 🔊 — with the active
 * stage pulsing as a signal dot flows through.  Driven by the current
 * format + whether playback is active, via an accessor the caller passes.
 */
import { registerViz, svgEl, clamp } from './engine.js';

const VB_W = 440, VB_H = 120;

// Per-format decode chains.  Renderer names match the real backend dispatch
// (libsidplay / libopenmpt / ffmpeg / libgme).
const CHAINS = {
  SID:   ['SID', 'libsidplay', 'PCM', 'ReplayGain', 'WebAudio', '🔊'],
  MOD:   ['MOD', 'libopenmpt', 'PCM', 'ReplayGain', 'WebAudio', '🔊'],
  XM:    ['XM', 'libopenmpt', 'PCM', 'ReplayGain', 'WebAudio', '🔊'],
  IT:    ['IT', 'libopenmpt', 'PCM', 'ReplayGain', 'WebAudio', '🔊'],
  S3M:   ['S3M', 'libopenmpt', 'PCM', 'ReplayGain', 'WebAudio', '🔊'],
  // HivelyTracker (.hvl) and AHX (.ahx) are NOT decoded by libopenmpt —
  // .hvl uses the bundled HivelyTracker replay (hvl2wav), .ahx uses uade123.
  HVL:   ['HVL', 'hvl2wav', 'PCM', 'ReplayGain', 'WebAudio', '🔊'],
  AHX:   ['AHX', 'uade123', 'PCM', 'ReplayGain', 'WebAudio', '🔊'],
  FLAC:  ['FLAC', 'ffmpeg', 'PCM', 'ReplayGain', 'WebAudio', '🔊'],
  ALAC:  ['ALAC', 'ffmpeg', 'PCM', 'ReplayGain', 'WebAudio', '🔊'],
  MP3:   ['MP3', 'decode', 'PCM', 'ReplayGain', 'WebAudio', '🔊'],
  DSF:   ['DSF', 'ffmpeg DSD', 'PCM 24b', 'ReplayGain', 'WebAudio', '🔊'],
  DFF:   ['DFF', 'ffmpeg DSD', 'PCM 24b', 'ReplayGain', 'WebAudio', '🔊'],
  NSF:   ['NSF', 'libgme', 'PCM', 'ReplayGain', 'WebAudio', '🔊'],
  SPC:   ['SPC', 'libgme', 'PCM', 'ReplayGain', 'WebAudio', '🔊'],
  MIDI:  ['MIDI', 'fluidsynth', 'PCM', 'ReplayGain', 'WebAudio', '🔊'],
};
const TRACKER = new Set(['MOD', 'XM', 'IT', 'S3M', 'MPTM', 'MTM', 'UMX']);

function chainFor(fmt) {
  const F = String(fmt || '').toUpperCase();
  if (CHAINS[F]) return CHAINS[F];
  if (F.includes('SID')) return CHAINS.SID;
  // HivelyTracker / AHX must be checked BEFORE the generic TRACKER fallback —
  // "HivelyTracker" contains "TRACKER" but is decoded by hvl2wav, not libopenmpt.
  if (F === 'HIVELYTRACKER' || F.includes('HIVELY') || F === 'HVL') return CHAINS.HVL;
  if (F === 'AHX' || F === 'THX' || F.includes('ABYSS')) return CHAINS.AHX;
  if (TRACKER.has(F) || F.includes('TRACKER')) return CHAINS.MOD;
  if (F.includes('FLAC')) return CHAINS.FLAC;
  if (F.includes('DSD') || F.includes('DSF') || F.includes('DFF')) return CHAINS.DSF;
  if (F.includes('MIDI') || F === 'MID') return CHAINS.MIDI;
  // Generic lossy fallback.
  return [F || 'audio', 'decode', 'PCM', 'ReplayGain', 'WebAudio', '🔊'];
}

/**
 * Mount the signal chain into ``host``.
 * @param {HTMLElement} host
 * @param {() => {format: string, playing: boolean}} getState
 * @returns {{ unregister: ()=>void, rebuild: ()=>void }}
 */
export function mountSignalChain(host, getState) {
  host.classList.add('viz-signal-chain');
  const svg = svgEl('svg', {
    viewBox: `0 0 ${VB_W} ${VB_H}`,
    preserveAspectRatio: 'xMidYMid meet',
    'aria-hidden': 'true',
  });
  host.appendChild(svg);

  // A11y: a real ordered-list mirror for screen readers (SVG is decorative).
  const ol = document.createElement('ol');
  ol.className = 'viz-sr-only';
  host.appendChild(ol);

  let nodes = [];
  let curFmt = null;
  let t = 0;

  function render(fmt) {
    curFmt = fmt;
    svg.textContent = '';
    ol.textContent = '';
    const steps = chainFor(fmt);
    const n = steps.length;
    const pad = 26, gapw = (VB_W - pad * 2) / n;
    nodes = steps.map((label, i) => {
      const cx = pad + gapw * i + gapw / 2;
      const cy = VB_H / 2;
      const isOut = i === n - 1;
      if (i < n - 1) {
        const nx = pad + gapw * (i + 1) + gapw / 2;
        svg.appendChild(svgEl('line', {
          x1: cx + 24, y1: cy, x2: nx - 24, y2: cy,
          stroke: '#243043', 'stroke-width': 1.6,
        }));
      }
      const g = svgEl('g');
      const shape = isOut
        ? svgEl('circle', { cx, cy, r: 20, class: 'viz-node' })
        : svgEl('rect', { x: cx - 28, y: cy - 20, width: 56, height: 40, rx: 9, class: 'viz-node' });
      const tx = svgEl('text', {
        x: cx, y: isOut ? cy + 7 : cy + 4, 'text-anchor': 'middle',
        class: 'viz-node-label',
      });
      tx.textContent = isOut ? '🔊' : label;
      if (isOut) tx.setAttribute('style', 'font-size:17px');
      else if (label.length > 9) tx.setAttribute('style', 'font-size:9px');
      g.append(shape, tx);
      svg.appendChild(g);
      const li = document.createElement('li');
      li.textContent = label;
      ol.appendChild(li);
      return { shape, cx, cy };
    });
    ol.setAttribute('aria-label', 'Decode pipeline: ' + steps.join(' → '));
  }

  render(getState().format);

  const ctl = registerViz({
    host, group: 'nowPlaying', fps: 20,
    draw(dt) {
      const st = getState();
      if (st.format !== curFmt) render(st.format);
      if (!nodes.length) return;
      // Only flow while playing; when paused, light the output stage.
      const steps = nodes.length;
      if (!st.playing) {
        nodes.forEach((node, i) => node.shape.classList.toggle('active', i === steps - 1));
        [...svg.querySelectorAll('circle.viz-sig')].forEach(c => c.remove());
        return;
      }
      t += dt;
      const cycle = 2600, per = cycle / steps;
      const active = Math.floor((t % cycle) / per);
      nodes.forEach((node, i) => node.shape.classList.toggle('active', i === active));
      const localU = ((t % cycle) % per) / per;
      [...svg.querySelectorAll('circle.viz-sig')].forEach(c => c.remove());
      if (active < steps - 1) {
        const a = nodes[active], b = nodes[active + 1];
        const sx = a.cx + (b.cx - a.cx) * clamp(localU, 0, 1);
        svg.appendChild(svgEl('circle', {
          class: 'viz-sig', cx: sx, cy: a.cy, r: 4,
          fill: 'var(--viz-res, #74e09a)',
          filter: 'drop-shadow(0 0 6px var(--viz-res, #74e09a))',
        }));
      }
    },
    freeze() {
      // Static frame: output stage lit, no flowing dot.
      if (!nodes.length) render(getState().format);
      nodes.forEach((node, i) => node.shape.classList.toggle('active', i === nodes.length - 1));
      [...svg.querySelectorAll('circle.viz-sig')].forEach(c => c.remove());
    },
  });

  return {
    unregister() { ctl.unregister(); },
    rebuild() { render(getState().format); ctl.refresh(); },
  };
}

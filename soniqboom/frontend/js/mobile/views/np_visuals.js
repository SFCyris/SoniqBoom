// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * np_visuals.js — the phone Now Playing deck's meters and visuals (Retro hi-fi,
 * the phone's only look): the meter display between the cassette and the VFD,
 * the waveform in the seek slot, the tape counter, the reels' speed.
 *
 * The display (.m-npm) shows one of three things; a tap on its glass steps to
 * the next, a tap on a legend above it (METER / SPECTRUM / SCOPE — the lit one:
 * the next) picks that one, a long press (or the context-menu key) lists them
 * with "Turn meters off" (localStorage ``sb_m_np_meter`` keeps the pick).  A
 * change is said once: the glass's (status region #m-npm-said), the legends'
 * by their pressed state; the glass's name stays "Meter display", what it
 * shows is its description (#m-npm-desc):
 *   meter     a tracker / SID / chip / Amiga / Atari / PSF tune: one LED column
 *             per voice from its VU sidecar (vu/vumr.js — the desktop meter's
 *             data; until the sidecar exists the 32-column FFT spectrum, with
 *             the desktop's words for why); ordinary audio (MP3, FLAC, …): the
 *             twin L/R needle meters (vu/needles.js, Player.vuTaps()).
 *   spectrum  octave bars (third-octaves on a display ≥ 640 px), peak hold
 *             (viz/bands.js on a fan-out analyser of Player.analyser).
 *   scope     a phosphor oscilloscope trace (viz/phosphor.js, Player.analyser).
 * A station played through the audio graph (radio-player.js ``metered`` —
 * with the meters on) reads like ordinary audio; one played directly, outside
 * the graph, rests every view and says there is no level reading.  No Web
 * Audio: the same, except the per-voice LEDs (a sidecar needs no analyser).
 *
 * One rAF loop for everything that moves, at most 30 frames per second
 * (analyser reads ≤ 20 / s), running only while Now Playing is the open view,
 * the page is visible, the display is on screen, the Now-playing
 * visualizations are on (sb_viz_settings, as the desktop) and motion is not
 * reduced — and while music plays: after a pause it runs until the meters
 * have fallen back, then stops.  Analyser taps are connected only while their
 * view draws.  Reduced motion: one still frame 400 ms after a change, kept on
 * screen (needles and spectrum: the average of 5 readings, as the desktop
 * needles; LEDs and scope: one reading).  Visualizations
 * off: the display is replaced by a "Meters off" key that turns them on.
 *
 * The reels (CSS animation in mobile.css) turn at the speed a real cassette's
 * would — the emptier reel faster — via their animations' playbackRate; the
 * tape counter counts take-up reel turns.  Neither follows the visualization
 * setting (the reels never did); the reels stand still under reduced motion,
 * the counter follows the position.
 *
 *   mountNpVisuals({ root, np, ctx }) → { position(pos), state(), stats }
 */
import { Player } from '../../player.js';
import { MobileRadio } from '../radio-player.js';
import { Auth } from '../../auth.js';
import { isUadeAmigaTrack } from '../../utils.js';
import { setVizSettings } from '../../viz/engine.js';
import { createVumrFetcher, parseVUMR, vuEligible, vuKeyOf, vuLabelStep, vumrLevelsAt,
         fftColumnLevels, vuFallbackText, FFT_FALLBACK_BARS, SID_VU_RETRY_MS,
         AMIGA_VU_RETRY_MS, AMIGA_VU_SETTLE_S, AMIGA_VU_SETTLE_POLL_MS,
         AMIGA_VU_SETTLE_MAX_WAITS } from '../../vu/vumr.js';
import { ASPECT as NEEDLE_ASPECT, READ_MS, createNeedleState, readNeedleLevels, stepNeedles,
         drawNeedleFace, drawNeedle, needleAngle } from '../../vu/needles.js';
import { AN_THIRDS, AN_OCTAVES, bandBins, readBands, createBandState, stepBands } from '../../viz/bands.js';
import { createPhosphor } from '../../viz/phosphor.js';

const MODES = ['meter', 'spectrum', 'scope'];
const MODE_NAMES = { meter: 'Meter', spectrum: 'Spectrum', scope: 'Scope' };
const MODE_KEY = 'sb_m_np_meter';
const FRAME_MS = 33;                 // ≤ 30 frames / s
const LED_FALL = 3;                  // an LED column falls at most this much (full scale) / s
const LED_PITCH = 4, LED_SEG = 3;    // CSS px per segment, lit px of it
const LED_PK_HOLD_MS = 700, LED_PK_FALL = 0.6;
const PEAK_LAMP_MS = 500;
const AN_FFT = 4096;
const AN_NARROW_PX = 640;            // a display narrower than this gets octave bands (as the desktop)
const STILL_DELAY_MS = 400, STILL_READS = 5, STILL_GAP_MS = 60;
const LONG_PRESS_MS = 500, MOVE_TOLERANCE = 8;   // as gestures.js
const DPR_MAX = 2;
const WAVE_RETRY_MS = [2000, 4000, 8000, 15000, 30000];
// A waveform drawn while its audio renders: asked again this often, until the
// render has not moved for this many asks.
const WAVE_PARTIAL_MS = 1000;
const WAVE_PARTIAL_STALL = 20;
const WAVE_PARTIAL_MAX = 900;                  // (15 minutes of asks at most)
// The VFD's colours (mobile.css --np-vfd / -dim / the amber "on"): the glass
// stays dark in the light scheme too, so one palette.
const VFD = '#6ff7e9', VFD_DIM = '#46c9bd', AMBER = '#ffb12b';
const SCOPE_TOK = { trace: VFD, traceGlow: 'rgba(111,247,233,0.22)', traceCore: '#e9fffc', persist: 0.8, level: 0.95 };

const _rm = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
const reducedMotion = () => !!(_rm && _rm.matches);

// The Now-playing visualizations switch (the viz engine's sb_viz_settings, as
// app.js _nowPlayingVizOn reads it): read from storage so a change made in
// another tab counts too.
function vizOn() {
  try {
    const s = JSON.parse(localStorage.getItem('sb_viz_settings') || '{}');
    return s.enabled !== false && s.nowPlaying !== false;
  } catch (_) { return true; }
}

function readMode() {
  try { const m = localStorage.getItem(MODE_KEY); return MODES.includes(m) ? m : 'meter'; }
  catch (_) { return 'meter'; }
}

export function mountNpVisuals({ root, np, ctx }) {
  const box     = np.querySelector('#m-npm');
  const glass   = np.querySelector('#m-npm-glass');
  const leds    = np.querySelector('#m-npm-leds');
  const cvs     = np.querySelector('#m-npm-cvs');
  const noteEl  = np.querySelector('#m-npm-note');
  const offKey  = np.querySelector('#m-npm-off');
  const peakEl  = np.querySelector('#m-npm-peak');
  const countEl = np.querySelector('#m-npm-count');
  const said    = np.querySelector('#m-npm-said');
  const descEl  = np.querySelector('#m-npm-desc');
  const modeKeys = Array.from(np.querySelectorAll('.m-npm-mode'));
  const waveBox = np.querySelector('#m-npw');
  const waveCvs = Array.from(np.querySelectorAll('#m-npw canvas'));
  const reels   = Array.from(np.querySelectorAll('.m-np-reel i'));
  const g = cvs.getContext('2d');
  const stats = { frames: 0, ms: 0, maxMs: 0, reads: 0, draws: 0 };

  let mode = readMode();
  let viz = vizOn();                 // re-read on 'sb:viz-settings' / 'storage'
  let kind = 'none';                 // 'voices' | 'audio' | 'radio' | 'none'
  let raf = 0, lastFrame = 0, lastRead = 0, stillT = 0, peakUntil = 0, peakShown = false;
  let size = { w: 0, h: 0, dpr: 0 };
  let onScreen = true;

  // ── what the track wants ───────────────────────────────────────────────
  function _kindOf() {
    if (MobileRadio.active) return MobileRadio.metered ? 'audio' : 'radio';
    const t = Player.currentTrack;
    if (!t) return 'none';
    return vuEligible(t) ? 'voices' : 'audio';
  }
  const _active = () => root.classList.contains('active');
  // What the display shows now: 'leds' | 'needles' | 'spectrum' | 'scope'.
  function _view() {
    if (mode === 'spectrum') return 'spectrum';
    if (mode === 'scope') return 'scope';
    return kind === 'voices' ? 'leds' : 'needles';
  }
  const _analyser = () => Player.analyser || null;
  // What plays: the phone's radio while it has a station, else the library.
  const _playing = () => (MobileRadio.active ? MobileRadio.playing : Player.playing);

  // ── the per-voice sidecar (the desktop's data path, vu/vumr.js) ─────────
  const vuFetch = createVumrFetcher();
  // vuKey: the tune the sidecar was asked for ('' = ask); vuPhase: 'fetch' (the
  // first ask in flight), 'poll' (a ladder runs), 'done' (painted, or none comes).
  let vuKey = '', vuPhase = '', vumr = null, vuNote = '', pollKey = '', fftBuf = null;
  let ledN = 0, ledCols = [], ledLit = [], ledPk = [], ledLv = null, ledPkLv = null, ledHold = null, ledRows = 1;
  const ledShown = [];               // the segment counts on screen (−1 = unknown)

  function _resetVoices() {
    vuFetch.abort();
    vumr = null; vuNote = ''; pollKey = ''; vuKey = ''; vuPhase = '';
  }

  // Ask for the sidecar of the playing tune (only while the display can show
  // it: Now Playing open, visualizations on) — a miss paints the spectrum and
  // asks again on the SID / Amiga ladder.
  async function _acquire() {
    const t = Player.currentTrack;
    if (kind !== 'voices' || !t || !t.id) return;
    if (!_active() || !viz) return;                      // asked again when it shows
    const key = vuKeyOf(t);
    if (key === vuKey && vuPhase) return;
    vuKey = key; vuPhase = 'fetch'; vumr = null; pollKey = '';
    const primary = String(t.format || '').split('/')[0].trim() || 'this format';
    const amiga = isUadeAmigaTrack(t);
    _buildLeds(FFT_FALLBACK_BARS, false);
    vuNote = ''; _paintNote();
    const r = await vuFetch.fetch(t.id, t.subsong, { start: !amiga });
    if (key !== vuKey || key !== vuKeyOf(Player.currentTrack)) return;
    if (r && !r.unavailable) { _useVumr(r); return; }
    vuPhase = 'done';
    const unavailable = !!(r && r.unavailable);
    const willPoll = primary === 'SID' || (amiga && !unavailable);
    vuNote = willPoll ? 'Spectrum — per-voice meters loading…'
                      : vuFallbackText(primary, unavailable ? r.reason : '', Auth.isAdmin);
    _paintNote();
    if (willPoll) { pollKey = key; vuPhase = 'poll'; _poll(t, key, amiga ? AMIGA_VU_RETRY_MS : SID_VU_RETRY_MS, amiga, 0, 0); }
  }

  function _poll(t, key, ladder, amiga, attempt, waits) {
    if (attempt >= ladder.length) {
      if (pollKey === key) { pollKey = ''; vuPhase = 'done'; vuNote = vuFallbackText(String(t.format || '').split('/')[0].trim() || 'this format', '', Auth.isAdmin); _paintNote(); }
      return;
    }
    setTimeout(async () => {
      if (pollKey !== key) return;
      // The display went away (another view, visualizations off): stop; it asks
      // again from the start when it shows.
      if (!_active() || !viz) { pollKey = ''; vuKey = ''; vuPhase = ''; return; }
      // Amiga: the first ask starts the server's per-voice pass — only once the
      // track has really played a few seconds.
      if (amiga && attempt === 0 && !(Player.playing && (Player.currentTime || 0) >= AMIGA_VU_SETTLE_S)) {
        if (waits < AMIGA_VU_SETTLE_MAX_WAITS) _poll(t, key, ladder, amiga, 0, waits + 1);
        else { pollKey = ''; vuPhase = 'done'; }
        return;
      }
      const r = await vuFetch.fetch(t.id, t.subsong);
      if (pollKey !== key) return;
      if (r && r.unavailable) {
        pollKey = ''; vuPhase = 'done';
        vuNote = vuFallbackText(String(t.format || '').split('/')[0].trim(), r.reason, Auth.isAdmin);
        _paintNote();
        return;
      }
      if (r) { pollKey = ''; _useVumr(r); return; }
      _poll(t, key, ladder, amiga, attempt + 1, 0);
    }, waits > 0 ? AMIGA_VU_SETTLE_POLL_MS : ladder[attempt]);
  }

  function _useVumr(r) {
    vumr = r; vuNote = ''; pollKey = ''; vuPhase = 'done';
    _buildLeds(r.channels, true);
    _paintNote();
    _sync();
  }

  // The in-browser SID player (flag-gated, sid-wasm-player.js) renders the
  // voices with the audio and hands them over here.
  Player.on('sidwasmvu', ({ id, subsong, vumr: buf }) => {
    const key = `${id}:${Number.isInteger(subsong) ? subsong : 0}`;
    if (key !== vuKeyOf(Player.currentTrack) || !buf) return;
    const parsed = parseVUMR(buf);
    if (parsed) { vuKey = key; _useVumr(parsed); }
  });

  // ── LED columns (DOM: one column per voice, lit to --vu-level) ──────────
  function _buildLeds(n, labels) {
    ledN = n;
    leds.textContent = '';
    leds.dataset.labels = labels ? 'on' : 'off';
    leds.style.setProperty('--npm-n', String(n));
    ledCols = []; ledLit = []; ledPk = [];
    const frag = document.createDocumentFragment();
    for (let i = 0; i < n; i++) {
      const col = document.createElement('span');
      col.className = 'm-npm-col';
      const lit = document.createElement('span'); lit.className = 'm-npm-lit';
      const pk = document.createElement('span'); pk.className = 'm-npm-pk';
      col.append(lit, pk);
      if (labels) {
        const lab = document.createElement('span');
        lab.className = 'm-npm-lab';
        col.appendChild(lab);
      }
      frag.appendChild(col);
      ledCols.push(col); ledLit.push(lit); ledPk.push(pk);
    }
    leds.appendChild(frag);
    ledLv = new Float32Array(n); ledPkLv = new Float32Array(n); ledHold = new Float64Array(n);
    ledShown.length = 0;
    for (let i = 0; i < 2 * n; i++) ledShown.push(-1);
    _labelLeds();
    _layoutLeds();
  }

  // Channel 1 and every step-th voice keep their number, the rest a dot (as
  // the desktop meter, vu/vumr.js vuLabelStep).
  function _labelLeds() {
    if (leds.dataset.labels !== 'on' || !ledN) return;
    const w = leds.clientWidth || glass.clientWidth || 300;
    const step = vuLabelStep(ledN, w, 5.5);
    leds.dataset.labelStep = String(step);
    for (let i = 0; i < ledN; i++) {
      const lab = ledCols[i].lastChild;
      const n = i + 1;
      const dot = step > 1 && n !== 1 && n % step !== 0;
      lab.textContent = dot ? '·' : String(n);
      lab.classList.toggle('dot', dot);
    }
  }

  function _layoutLeds() {
    const col = ledCols[0];
    const h = col ? col.clientHeight : 0;
    const labH = col && col.lastChild && col.lastChild.classList.contains('m-npm-lab') ? col.lastChild.offsetHeight : 0;
    const usable = Math.max(LED_PITCH, h - labH);
    ledRows = Math.max(4, Math.floor(usable / LED_PITCH));
    leds.style.setProperty('--npm-rows', String(ledRows));
    for (let i = 0; i < ledShown.length; i++) ledShown[i] = -1;
  }

  // Levels 0…1 into ledLv (target), then the columns' ballistics and writes.
  const _ledTarget = new Float32Array(32);
  function _readLeds(now) {
    const n = ledN;
    if (!n) return false;
    const playing = Player.playing && kind === 'voices';
    let ok = false;
    if (playing && vumr) {
      vumrLevelsAt(vumr, Player.currentTime || 0, _ledTarget, n);
      ok = true;
    } else if (playing) {
      const an = Player.vuAnalyser || null;
      if (an && now - lastRead >= READ_MS) {
        if (!fftBuf || fftBuf.length !== an.frequencyBinCount) fftBuf = new Uint8Array(an.frequencyBinCount);
        an.getByteFrequencyData(fftBuf);
        fftColumnLevels(fftBuf, n, _ledTarget);
        lastRead = now; stats.reads++;
      }
      ok = !!an;
    }
    if (!ok) _ledTarget.fill(0, 0, n);
    return true;
  }

  function _stepLeds(dt, now, still) {
    const n = ledN;
    let up = false, hot = false;
    for (let i = 0; i < n; i++) {
      const t = _ledTarget[i];
      const lv = still || t >= ledLv[i] ? t : Math.max(t, ledLv[i] - LED_FALL * dt);
      ledLv[i] = lv;
      if (lv >= ledPkLv[i]) { ledPkLv[i] = lv; ledHold[i] = now + LED_PK_HOLD_MS; }
      else if (still) ledPkLv[i] = lv;
      else if (now > ledHold[i]) ledPkLv[i] = Math.max(lv, ledPkLv[i] - LED_PK_FALL * dt);
      if (lv > 0.002 || ledPkLv[i] > 0.002) up = true;
      if (lv >= 0.97) hot = true;
      const segs = Math.round(lv * ledRows);
      if (segs !== ledShown[i]) { ledShown[i] = segs; ledLit[i].style.setProperty('--vu-level', (segs / ledRows).toFixed(4)); }
      const pk = Math.round(ledPkLv[i] * ledRows);
      const pkShown = pk > segs ? pk : 0;
      if (pkShown !== ledShown[n + i]) { ledShown[n + i] = pkShown; ledPk[i].style.setProperty('--vu-peak', (pkShown / ledRows).toFixed(4)); }
    }
    if (hot && !still) peakUntil = now + PEAK_LAMP_MS;
    return up;
  }

  // ── needles (vu/needles.js) ─────────────────────────────────────────────
  const nd = createNeedleState();
  let taps = null, faces = [null, null], ndGeom = null, ndDrawn = [NaN, NaN], ndLamp = [null, null];
  // Two faces side by side, or one above the other on a tall display (an
  // iPad held sideways) — whichever gives the bigger face; at most 440 px wide.
  function _needleGeom() {
    const { w, h } = size;
    const pad = 6, gap = Math.max(8, Math.round(Math.min(w, h) * 0.04));
    const side = Math.min((w - gap - 2 * pad) / 2, (h - 2 * pad) / NEEDLE_ASPECT);
    const stack = Math.min(w - 2 * pad, (h - gap - 2 * pad) / 2 / NEEDLE_ASPECT);
    const stacked = stack > side * 1.15;
    const fw = Math.floor(Math.min(440, stacked ? stack : side));
    const fh = Math.round(fw * NEEDLE_ASPECT);
    if (stacked) {
      const x = Math.round((w - fw) / 2), y0 = Math.round((h - 2 * fh - gap) / 2);
      return { fw, fh, xs: [x, x], ys: [y0, y0 + fh + gap] };
    }
    const x0 = Math.round((w - 2 * fw - gap) / 2), y = Math.round((h - fh) / 2);
    return { fw, fh, xs: [x0, x0 + fw + gap], ys: [y, y] };
  }
  function _readNeedles(now) {
    if (now - lastRead < READ_MS) return;
    lastRead = now; stats.reads++;
    if (kind === 'audio' && _playing()) {
      if (!taps) taps = Player.vuTaps ? Player.vuTaps() : null;
      if (taps) {
        const el = MobileRadio.active ? MobileRadio.el : Player.audio;
        const vol = el ? Number(el.volume) : 1;
        const gain = vol > 0.001 && Number.isFinite(vol) ? 1 / Math.min(1, vol) : 0;
        readNeedleLevels(taps, nd, gain, now);
        return;
      }
    }
    nd.target[0] = nd.target[1] = 0;
  }
  function _drawNeedles(force) {
    if (!size.w) return;
    if (!ndGeom) ndGeom = _needleGeom();
    const G = ndGeom;
    if (G.fw < 40) return;
    const now = performance.now();
    for (let i = 0; i < 2; i++) {
      const lit = nd.lampUntil[i] > now;
      if (!force && Math.abs(nd.pos[i] - ndDrawn[i]) <= 0.0005 && lit === ndLamp[i]) continue;
      if (!faces[i]) faces[i] = drawNeedleFace(G.fw, G.fh, size.dpr, i);
      drawNeedle(g, faces[i], G.fw, G.fh, size.dpr, nd.pos[i], lit, G.xs[i], G.ys[i]);
      ndDrawn[i] = nd.pos[i]; ndLamp[i] = lit;
      stats.draws++;
    }
  }

  // ── spectrum (viz/bands.js) ─────────────────────────────────────────────
  let anTap = null, anFrom = null, anSpec = null, anBins = null, anBinsKey = '', bands = null, anGeom = null;
  function _anTapFor(an) {
    if (anFrom === an && anTap) return anTap;
    _anRelease();
    try {
      if (!anTap || anTap.context !== an.context) {
        anTap = an.context.createAnalyser();
        anTap.fftSize = AN_FFT;
        anTap.smoothingTimeConstant = 0;               // the bars have their own ballistics
      }
      an.connect(anTap);
      anFrom = an;
    } catch (_) { anFrom = null; return null; }
    return anTap;
  }
  function _anRelease() {
    if (anFrom && anTap) { try { anFrom.disconnect(anTap); } catch (_) {} }
    anFrom = null;
  }
  function _freqs() { return size.w >= AN_NARROW_PX ? AN_THIRDS : AN_OCTAVES; }
  function _readSpectrum(now) {
    const freqs = _freqs();
    if (!bands || bands.level.length !== freqs.length) bands = createBandState(freqs.length);
    if (now - lastRead < READ_MS) return;
    lastRead = now; stats.reads++;
    const an = _analyser();
    const live = an && _playing() && kind !== 'radio' && kind !== 'none';
    const tap = live ? _anTapFor(an) : null;
    if (!tap) { bands.target.fill(0); return; }
    if (!anSpec || anSpec.length !== tap.frequencyBinCount) anSpec = new Float32Array(tap.frequencyBinCount);
    tap.getFloatFrequencyData(anSpec);
    const key = `${freqs.length}@${tap.context.sampleRate}`;
    if (anBinsKey !== key) { anBins = bandBins(freqs, tap.context.sampleRate, AN_FFT); anBinsKey = key; }
    readBands(anSpec, anBins, freqs.length, bands.target);
  }
  // Static layer: unlit segments and the band labels.
  function _anLayout() {
    const { w, h, dpr } = size;
    const k = dpr, W = Math.round(w * k), H = Math.round(h * k);
    const freqs = _freqs(), n = freqs.length;
    const padX = Math.round(8 * k), labelH = Math.round(13 * k), top = Math.round(8 * k);
    const base = H - labelH;
    const slot = (W - 2 * padX) / n;
    const barW = Math.max(1, Math.floor(slot * 0.7));
    const xs = new Float32Array(n);
    for (let i = 0; i < n; i++) xs[i] = Math.round(padX + slot * i + (slot - barW) / 2);
    const pitch = Math.max(3, Math.round(LED_PITCH * k)), segH = Math.max(2, Math.round(LED_SEG * k));
    const rows = Math.max(4, Math.floor((base - top) / pitch));
    const seg = (color) => {
      const c = document.createElement('canvas');
      c.width = 1; c.height = pitch;
      const cg = c.getContext('2d');
      cg.fillStyle = color;
      cg.fillRect(0, pitch - segH, 1, segH);
      const pat = g.createPattern(c, 'repeat');
      if (pat.setTransform && typeof DOMMatrix === 'function') pat.setTransform(new DOMMatrix([1, 0, 0, 1, 0, base % pitch]));
      return pat;
    };
    const layer = document.createElement('canvas');
    layer.width = W; layer.height = H;
    const lg = layer.getContext('2d');
    lg.fillStyle = seg('rgba(111,247,233,0.10)');
    for (let i = 0; i < n; i++) lg.fillRect(xs[i], base - rows * pitch, barW, rows * pitch);
    lg.fillStyle = VFD_DIM;
    lg.font = `700 ${Math.round(9 * k)}px "DIN Alternate","DIN Condensed",Bahnschrift,"Arial Narrow",sans-serif`;
    lg.textAlign = 'center'; lg.textBaseline = 'bottom';
    for (let i = 0; i < n; i++) {
      const f = freqs[i];
      if (freqs === AN_THIRDS && !AN_OCTAVES.includes(f)) continue;
      lg.fillText(f >= 1000 ? `${f / 1000}k` : `${Math.floor(f)}`, xs[i] + barW / 2, H - Math.round(1 * k));
    }
    anGeom = { W, H, n, xs, barW, base, pitch, rows, layer, lit: seg(VFD), peak: seg(AMBER), freqs };
    return anGeom;
  }
  function _drawSpectrum() {
    if (!size.w || !size.h) return false;               // (not laid out yet: a 0 × 0 layer can't be drawn)
    const G = (anGeom && anGeom.W === cvs.width && anGeom.H === cvs.height && anGeom.freqs === _freqs()) ? anGeom : _anLayout();
    if (!bands || bands.level.length !== G.n) bands = createBandState(G.n);
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.clearRect(0, 0, G.W, G.H);
    g.drawImage(G.layer, 0, 0);
    const { xs, barW, base, pitch, rows } = G;
    g.fillStyle = G.lit;
    let hot = false;
    for (let i = 0; i < G.n; i++) {
      const lit = Math.round(bands.level[i] * rows);
      if (bands.level[i] >= 0.98) hot = true;
      if (lit) g.fillRect(xs[i], base - lit * pitch, barW, lit * pitch);
    }
    g.fillStyle = G.peak;
    for (let i = 0; i < G.n; i++) {
      const pk = Math.round(bands.peak[i] * rows);
      if (pk > Math.round(bands.level[i] * rows)) g.fillRect(xs[i], base - pk * pitch, barW, pitch);
    }
    stats.draws++;
    return hot;
  }

  // ── scope (viz/phosphor.js), auto-ranged to the recent peak ─────────────
  const phosphor = createPhosphor({ keep: 16, minAlpha: 0.03 });
  let scopeBuf = null, scopeGain = 1, scopeFlat = null, scopeLiveAt = 0;
  function _readScope(now) {
    if (now - lastRead < READ_MS) return false;
    lastRead = now; stats.reads++;
    const an = _analyser();
    const live = an && _playing() && kind !== 'radio' && kind !== 'none';
    if (!live) {
      if (!scopeFlat || scopeFlat.length !== 256) scopeFlat = new Uint8Array(256).fill(128);
      scopeBuf = scopeFlat;
      return true;
    }
    scopeLiveAt = now;
    if (!scopeBuf || scopeBuf === scopeFlat || scopeBuf.length !== an.fftSize) scopeBuf = new Uint8Array(an.fftSize);
    an.getByteTimeDomainData(scopeBuf);
    let pk = 0;
    for (let i = 0; i < scopeBuf.length; i++) { const d = Math.abs(scopeBuf[i] - 128); if (d > pk) pk = d; }
    if (pk >= 101) peakUntil = now + PEAK_LAMP_MS;            // ≈ −2 dBFS, the needles' PEAK
    // gain towards the trace filling ~85 % of the screen, at most ×4, eased
    const want = pk > 2 ? Math.min(4, 109 / pk) : scopeGain;
    scopeGain += (want - scopeGain) * (want < scopeGain ? 0.5 : 0.08);
    if (scopeGain !== 1) {
      for (let i = 0; i < scopeBuf.length; i++) {
        const v = 128 + (scopeBuf[i] - 128) * scopeGain;
        scopeBuf[i] = v < 0 ? 0 : v > 255 ? 255 : v;
      }
    }
    return true;
  }
  function _drawScope(now) {
    if (!scopeBuf || !size.w || !size.h) return;
    g.setTransform(1, 0, 0, 1, 0, 0);
    phosphor.draw(g, cvs.width, cvs.height, scopeBuf, SCOPE_TOK, size.dpr, now);
    stats.draws++;
  }

  // ── the loop ────────────────────────────────────────────────────────────
  function _shown() {
    return viz && _active() && !document.hidden && onScreen;
  }
  function _wanted() {
    return _shown() && !reducedMotion();
  }
  const _liveAudio = () => _playing() && (kind === 'voices' || kind === 'audio');
  // …and the view has something to read (a sidecar, an analyser, the taps):
  // without one the loop doesn't run on for nothing.
  function _readable() {
    const v = _view();
    if (v === 'leds') return !!vumr || !!Player.vuAnalyser;
    if (v === 'needles') return !!(taps || (Player.ctx && Player.analyser));
    return !!_analyser();
  }

  function _frame(now) {
    raf = 0;
    if (!_wanted()) { _release(); return; }
    if (now - lastFrame < FRAME_MS - 2) { raf = requestAnimationFrame(_frame); return; }
    const t0 = performance.now();
    const dt = lastFrame ? Math.min(0.1, (now - lastFrame) / 1000) : FRAME_MS / 1000;
    lastFrame = now;
    const v = _view();
    let moving = false;
    if (v === 'leds') {
      _readLeds(now);
      moving = _stepLeds(dt, now, false);
    } else if (v === 'needles') {
      _readNeedles(now);
      moving = stepNeedles(nd, dt);
      for (let i = 0; i < 2; i++) if (nd.lampUntil[i] > now) moving = true;
      _drawNeedles(false);
    } else if (v === 'spectrum') {
      _readSpectrum(now);
      moving = stepBands(bands, dt, now);
      if (_drawSpectrum()) peakUntil = now + PEAK_LAMP_MS;
    } else {
      const fresh = _readScope(now);
      if (fresh || phosphor.traces > 1) _drawScope(now);
      moving = now - scopeLiveAt < 400;               // the afterglow fades onto the flat trace
    }
    _paintPeak(now);
    const ms = performance.now() - t0;
    stats.frames++; stats.ms += ms; if (ms > stats.maxMs) stats.maxMs = ms;
    if ((_liveAudio() && _readable()) || moving || peakShown) raf = requestAnimationFrame(_frame);
    else _release();
  }

  function _paintPeak(now) {
    const on = peakUntil > now && _view() !== 'needles';   // (the needles light their own)
    if (on !== peakShown) { peakShown = on; peakEl.classList.toggle('on', on); }
  }

  // The taps and the spectrum's analyser are connected only while they draw.
  function _release() {
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    lastFrame = 0;
    if (taps && Player.releaseVuTaps) Player.releaseVuTaps();
    taps = null;
    _anRelease();
  }

  // Reduced motion: draw the current reading once, without motion — the needles
  // and the spectrum averaged over STILL_READS readings, the LEDs and the scope
  // the last one.  The taps are released when it is drawn (or given up).
  function _still() {
    clearTimeout(stillT);
    stillT = setTimeout(async () => {
      stillT = 0;
      if (!_shown()) return;
      const v = _view();
      const live = _liveAudio();
      const sum = [0, 0];
      let sumBands = null;
      for (let r = 0; r < (live ? STILL_READS : 1); r++) {
        if (r) await new Promise(res => setTimeout(res, STILL_GAP_MS));
        if (!_shown() || v !== _view()) { _release(); return; }   // left meanwhile
        lastRead = 0;
        const now = performance.now();
        if (v === 'leds') { _readLeds(now); }
        else if (v === 'needles') { _readNeedles(now); sum[0] += nd.target[0]; sum[1] += nd.target[1]; }
        else if (v === 'spectrum') {
          _readSpectrum(now);
          if (!sumBands) sumBands = new Float32Array(bands.target.length);
          for (let i = 0; i < sumBands.length; i++) sumBands[i] += bands.target[i];
        } else _readScope(now);
      }
      if (!_shown() || v !== _view()) { _release(); return; }
      const n = live ? STILL_READS : 1;
      const now = performance.now();
      if (v === 'leds') _stepLeds(0, now, true);
      else if (v === 'needles') {
        nd.pos[0] = sum[0] / n; nd.pos[1] = sum[1] / n;
        nd.lampUntil[0] = nd.lampUntil[1] = 0;     // a frozen lamp would read as "clipping now"
        _clear(); _drawNeedles(true);
      } else if (v === 'spectrum') {
        for (let i = 0; i < sumBands.length; i++) bands.level[i] = bands.peak[i] = sumBands[i] / n;
        _drawSpectrum();
      } else { phosphor.reset(); _drawScope(now); }
      peakUntil = 0; _paintPeak(now);
      if (reducedMotion()) _release();
    }, STILL_DELAY_MS);
  }

  function _clear() {
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.clearRect(0, 0, cvs.width, cvs.height);
  }

  // Re-decide what shows and whether the loop runs.
  function _sync() {
    const on = viz;
    box.dataset.viz = on ? 'on' : 'off';
    offKey.hidden = on;
    if (!on) offKey.textContent = _masterOff() ? 'Visualizations off · tap to turn them on' : 'Meters off · tap to turn them on';
    box.dataset.mode = mode;
    box.dataset.view = _view();
    box.dataset.kind = kind;
    if (!on) { _release(); clearTimeout(stillT); _resetVoices(); return; }
    if (!_shown()) { _release(); return; }
    if (kind === 'voices') _acquire();
    if (_view() === 'leds' && !ledN) _buildLeds(FFT_FALLBACK_BARS, false);
    if (_view() === 'leds' && ledN && leds.clientHeight && ledRows === 4 && Math.floor(leds.clientHeight / LED_PITCH) > 8) { _labelLeds(); _layoutLeds(); }
    _paintNote();
    if (reducedMotion()) { _release(); _still(); return; }
    if (!raf && (_liveAudio() || _view() === 'needles' && (nd.pos[0] > 0.001 || nd.pos[1] > 0.001))) {
      raf = requestAnimationFrame(_frame);
    } else if (!raf) {
      _restFrame();
    }
  }

  // Nothing plays: the display at rest (needles on the stop pin, empty bars, a
  // flat trace) — drawn once.
  function _restFrame() {
    const v = _view();
    const now = performance.now();
    if (v === 'leds') { _ledTarget.fill(0); if (ledN) _stepLeds(0, now, true); }
    else if (v === 'needles') { nd.pos[0] = nd.pos[1] = nd.target[0] = nd.target[1] = 0; _clear(); _drawNeedles(true); }
    else if (v === 'spectrum') { _readSpectrum(now); if (bands) { bands.level.fill(0); bands.peak.fill(0); } _drawSpectrum(); }
    else { lastRead = 0; _readScope(now); phosphor.reset(); _drawScope(now); }
    peakUntil = 0; _paintPeak(now);
  }

  function _paintNote() {
    const v = _view();
    let msg = '';
    if (kind === 'radio') msg = 'Live stream — no level reading';
    else if (kind === 'none') msg = '';
    else if (v === 'leds') msg = vumr ? `${vumr.channels} ${vumr.channels === 1 ? 'voice' : 'voices'}` : vuNote;
    else if (!_analyser() && _playing()) msg = 'No level reading in this browser';
    if (noteEl.textContent !== msg) noteEl.textContent = msg;
    const desc = `${MODE_NAMES[mode]}${msg ? `: ${msg.replace(/[….]+$/, '')}` : ''}. Tap for the next display; touch and hold for the list.`;
    if (descEl && descEl.textContent !== desc) descEl.textContent = desc;
    for (const el of modeKeys) {
      const on = el.dataset.mode === mode;
      el.classList.toggle('on', on);
      el.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
  }

  function setMode(m, announce = true) {
    if (!MODES.includes(m) || m === mode) return;
    mode = m;
    try { localStorage.setItem(MODE_KEY, m); } catch (_) {}
    _release();
    phosphor.reset(); anGeom = null; ndDrawn = [NaN, NaN]; lastRead = 0;
    _clear();
    // (emptied first: the same words written again are said again)
    if (announce && said) { said.textContent = ''; requestAnimationFrame(() => { said.textContent = MODE_NAMES[m]; }); }
    _sync();
  }

  // ── size (DPR capped at 2) ──────────────────────────────────────────────
  function _resize() {
    const r = glass.getBoundingClientRect();
    const w = Math.round(r.width), h = Math.round(r.height);
    const dpr = Math.min(DPR_MAX, window.devicePixelRatio || 1);
    if (w === size.w && h === size.h && dpr === size.dpr) return;
    size = { w, h, dpr };
    cvs.width = Math.max(1, Math.round(w * dpr)); cvs.height = Math.max(1, Math.round(h * dpr));
    cvs.style.width = w + 'px'; cvs.style.height = h + 'px';
    faces = [null, null]; ndGeom = null; ndDrawn = [NaN, NaN]; anGeom = null; phosphor.reset();
    _labelLeds(); _layoutLeds();
    if (!raf && _shown()) (reducedMotion() ? _still() : _restOrResume());
  }
  function _restOrResume() { if (_liveAudio()) _sync(); else _restFrame(); }

  if ('ResizeObserver' in window) {
    new ResizeObserver(() => _resize()).observe(glass);
    // the columns are laid out (segment count, label thinning) when they show:
    // hidden (another display picked) they measure 0
    new ResizeObserver(() => { if (leds.clientHeight) { _labelLeds(); _layoutLeds(); } }).observe(leds);
  } else window.addEventListener('resize', _resize);
  if ('IntersectionObserver' in window) {
    new IntersectionObserver((es) => {
      const on = es.some(e => e.isIntersecting);
      if (on === onScreen) return;
      onScreen = on;
      _sync();
    }, { threshold: 0 }).observe(glass);
  }

  // ── touch: tap = next display, long press = the list ────────────────────
  let press = null, suppressClick = false, sheetAt = 0;
  // The click that ends a long press goes wherever the finger lifts — over the
  // sheet that just opened, its backdrop would close it: swallow it, once.
  document.addEventListener('click', (e) => {
    if (!press0 || performance.now() - liftAt > 400) return;     // only the click of that lift
    press0 = false;
    if (e.target === glass || glass.contains(e.target)) return;   // (the glass's own handler skips it)
    e.preventDefault(); e.stopPropagation();
  }, true);
  let press0 = false, liftAt = -1e9;   // a long press opened the sheet; when its finger lifted
  function _openSheet() {
    if (performance.now() - sheetAt < 800) return;
    sheetAt = performance.now();
    press0 = true;
    const actions = MODES.map(m => ({
      label: `${MODE_NAMES[m]}${m === mode ? '  ✓' : ''}`,
      onSelect: () => setMode(m),
    }));
    actions.push({ label: 'Turn meters off', onSelect: () => _patchViz({ nowPlaying: false }) });
    ctx.showSheet({ title: 'Meter display', actions });
  }
  glass.addEventListener('pointerdown', (e) => {
    suppressClick = false; press0 = false;
    if (e.button !== 0) return;
    press = { id: e.pointerId, x: e.clientX, y: e.clientY,
              timer: setTimeout(() => { press = null; suppressClick = true; _openSheet(); }, LONG_PRESS_MS) };
  });
  glass.addEventListener('pointermove', (e) => {
    if (!press || e.pointerId !== press.id) return;
    if (Math.abs(e.clientX - press.x) > MOVE_TOLERANCE || Math.abs(e.clientY - press.y) > MOVE_TOLERANCE) {
      clearTimeout(press.timer); press = null;
    }
  });
  const _endPress = () => { if (press) { clearTimeout(press.timer); press = null; } };
  glass.addEventListener('pointerup', () => { _endPress(); if (press0) liftAt = performance.now(); });
  glass.addEventListener('pointercancel', _endPress);
  glass.addEventListener('contextmenu', (e) => { e.preventDefault(); _endPress(); suppressClick = true; _openSheet(); });
  glass.addEventListener('click', () => {
    if (suppressClick) { suppressClick = false; return; }
    setMode(MODES[(MODES.indexOf(mode) + 1) % MODES.length]);
  });
  // A legend picks its display (the lit one: the next — a phone held sideways
  // shows only that one); its pressed state is what a screen reader says.
  for (const el of modeKeys) {
    el.addEventListener('click', () => {
      const m = el.dataset.mode;
      const had = document.activeElement === el;
      setMode(m === mode ? MODES[(MODES.indexOf(mode) + 1) % MODES.length] : m, m === mode);
      // sideways the key just used hides: the focus moves to the one now lit
      const lit = modeKeys.find(k => k.dataset.mode === mode);
      if (had && lit && lit !== el && lit.offsetParent !== null) lit.focus();
    });
  }
  // The key turns on what is off: the Now-playing group, and the master switch
  // too when that is what is off (its label says which).
  offKey.addEventListener('click', () => _patchViz(_masterOff() ? { enabled: true, nowPlaying: true } : { nowPlaying: true }));
  function _masterOff() {
    try { return (JSON.parse(localStorage.getItem('sb_viz_settings') || '{}') || {}).enabled === false; }
    catch (_) { return false; }
  }
  // setVizSettings merges into the settings the page loaded with: start from
  // the stored ones so a change another tab made isn't written back.
  function _patchViz(patch) {
    let cur = {};
    try { cur = JSON.parse(localStorage.getItem('sb_viz_settings') || '{}') || {}; } catch (_) { cur = {}; }
    setVizSettings({ ...cur, ...patch });
  }

  // ── what re-decides ─────────────────────────────────────────────────────
  function _onTrack() {
    const k = _kindOf();
    const key = vuKeyOf(Player.currentTrack);
    if (k !== kind || (k === 'voices' && key !== vuKey)) {
      kind = k;
      if (k !== 'voices' || key !== vuKey) _resetVoices();
      if (k === 'voices') _buildLeds(FFT_FALLBACK_BARS, false);
      _release(); _clear(); phosphor.reset(); ndDrawn = [NaN, NaN];
    }
    _counter(MobileRadio.active ? 0 : (Player.currentTime || 0));
    _sync();
    _wave();
  }
  Player.on('trackchange', _onTrack);
  MobileRadio.on('change', _onTrack);
  document.addEventListener('soniqboom:track-refreshed', (e) => {
    const cur = Player.currentTrack;
    if (cur && e && e.detail && e.detail.id === cur.id) _onTrack();
  });
  Player.on('statechange', () => { if (reducedMotion()) _still(); else _sync(); _reels(); });
  MobileRadio.on('state', () => { _sync(); _reels(); });
  Player.on('timeupdate', ({ current }) => { if (!MobileRadio.active) _counter(current); });
  // (a view shown again: its reels' animations are new ones, at rate 1)
  new MutationObserver(() => { _sync(); if (_active()) { _wave(); reelRate = [NaN, NaN]; _reels(); } })
    .observe(root, { attributes: true, attributeFilter: ['class'] });
  document.addEventListener('visibilitychange', () => _sync());
  window.addEventListener('sb:viz-settings', () => { viz = vizOn(); _sync(); });
  window.addEventListener('storage', (e) => {
    if (!e || e.key === 'sb_viz_settings' || e.key === null) { viz = vizOn(); _sync(); }
  });
  if (_rm) {
    const onRm = () => { reelRate = [NaN, NaN]; _reels(); _sync(); };
    if (_rm.addEventListener) _rm.addEventListener('change', onRm);
    else if (_rm.addListener) _rm.addListener(onRm);
  }

  // ── tape counter: turns of the take-up reel (a C60's numbers) ───────────
  // Tape at 4.76 cm/s onto an 11 mm hub, 12 µm a turn; the counter's gear
  // halves the turns — a 3-minute song reads about 058, a 30-minute side 424.
  let countShown = '';
  function _counter(sec) {
    const L = 4.76 * Math.max(0, Number(sec) || 0);
    const turns = (Math.sqrt(1.21 + L * 0.0012 / Math.PI) - 1.1) / 0.0012;
    const s = String(Math.floor(turns / 2) % 1000).padStart(3, '0');
    if (s === countShown) return;
    countShown = s;
    const ds = countEl.children;
    for (let i = 0; i < 3; i++) if (ds[i].textContent !== s[i]) ds[i].textContent = s[i];
  }

  // ── reels: the emptier one turns faster (constant tape speed) ───────────
  let reelPos = 0, reelRate = [NaN, NaN];
  function _reels() {
    const radio = MobileRadio.active;
    const p = radio ? 0.5 : reelPos;
    const packs = [0.4 + 0.6 * (1 - p), 0.4 + 0.6 * p];       // as mobile.css --pack
    reels.forEach((el, i) => {
      const rate = +(0.7 / packs[i]).toFixed(2);
      if (Math.abs(rate - reelRate[i]) < 0.02) return;
      const anims = el.getAnimations ? el.getAnimations() : [];
      for (const a of anims) { try { a.playbackRate = rate; } catch (_) {} }
      if (anims.length) reelRate[i] = rate;
    });
  }

  // ── waveform in the seek slot (GET /api/tracks/{id}/waveform) ───────────
  let waveFor = '', waveAbort = null, waveTimer = 0, waveData = null;
  let waveProg = -1, waveStill = 0, waveAsks = 0;   // a waveform drawn while its audio renders
  function _waveAgain(key, attempt, ms) {
    waveTimer = setTimeout(() => { if (waveFor === key) _wave(attempt); }, ms);
  }
  function _wave(attempt = 0) {
    const t = MobileRadio.active ? null : Player.currentTrack;
    const key = t && t.id ? `${t.id}~${Number.isInteger(t.subsong) ? t.subsong : ''}` : '';
    if (!attempt && key === waveFor) { if (waveData) _drawWave(); return; }
    if (!attempt) {
      waveFor = key; waveData = null; waveProg = -1; waveStill = 0; waveAsks = 0;
      clearTimeout(waveTimer); waveTimer = 0;
      if (waveAbort) { try { waveAbort.abort(); } catch (_) {} waveAbort = null; }
      waveBox.hidden = true;
      np.classList.remove('has-wave');
    }
    if (!key || !_active()) { if (!_active()) waveFor = ''; return; }   // asked when the view shows
    const ctl = (typeof AbortController === 'function') ? new AbortController() : null;
    waveAbort = ctl;
    const ss = Number.isInteger(t.subsong) ? `?subsong=${t.subsong}` : '';
    // ``partial=1``: while the audio renders, the part rendered so far
    // (points not rendered yet null) — asked again each second while it moves.
    fetch(`/api/tracks/${encodeURIComponent(t.id)}/waveform${ss ? `${ss}&partial=1` : '?partial=1'}`,
          ctl ? { cache: 'no-cache', signal: ctl.signal } : { cache: 'no-cache' })
      .then(r => (r.ok && r.status !== 204) ? r.json() : null)
      .then((data) => {
        if (waveAbort === ctl) waveAbort = null;
        if (waveFor !== key) return;
        if (data && data.pending) {                   // the audio is still being rendered
          if (waveData && ++waveStill < WAVE_PARTIAL_STALL) _waveAgain(key, attempt || 1, WAVE_PARTIAL_MS);
          else if (attempt < WAVE_RETRY_MS.length) _waveAgain(key, attempt + 1, WAVE_RETRY_MS[attempt]);
          return;
        }
        const partial = !!(data && data.partial);
        if (partial) {
          const p = Number(data.progress) || 0;
          // (a render running past its expected length stays at 1 until it ends)
          waveStill = (p > waveProg || p >= 1) ? 0 : waveStill + 1;
          waveProg = p;
          if (waveStill < WAVE_PARTIAL_STALL && ++waveAsks <= WAVE_PARTIAL_MAX) _waveAgain(key, attempt || 1, WAVE_PARTIAL_MS);
          else if (attempt < WAVE_RETRY_MS.length) _waveAgain(key, attempt + 1, WAVE_RETRY_MS[attempt]);
        }
        let arr = null;
        if (data && Array.isArray(data.waveform)) arr = data.waveform;
        else if (data && data.waveform && typeof data.waveform === 'object') arr = data.waveform.peaks || data.waveform.rms || null;
        if (!arr || !arr.length) {
          // no waveform after all: bars drawn while it rendered don't stay
          waveData = null; waveBox.hidden = true; np.classList.remove('has-wave');
          return;
        }
        // √ curve: quiet passages stay visible, peaks still stand out (as the desktop bar)
        // (a point not rendered yet: -1, no bar)
        waveData = arr.map(v => (v == null ? -1 : Math.sqrt(Math.max(0, Math.min(1, Number(v) || 0)))));
        if (!partial && waveData.every(v => v < 0.01)) {
          waveData = null; waveBox.hidden = true; np.classList.remove('has-wave');
          return;
        }
        waveBox.hidden = false;
        np.classList.add('has-wave');
        _drawWave();
      })
      .catch(() => { if (waveAbort === ctl) waveAbort = null; });
  }
  function _drawWave() {
    if (!waveData) return;
    const r = waveBox.getBoundingClientRect();
    const w = Math.round(r.width), h = Math.round(r.height);
    if (!w || !h) return;
    const dpr = Math.min(DPR_MAX, window.devicePixelRatio || 1);
    const colours = ['rgba(111,247,233,0.30)', AMBER];       // unplayed, played
    waveCvs.forEach((c, i) => {
      c.width = Math.round(w * dpr); c.height = Math.round(h * dpr);
      const wg = c.getContext('2d');
      wg.setTransform(dpr, 0, 0, dpr, 0, 0);
      wg.clearRect(0, 0, w, h);
      wg.fillStyle = colours[i];
      const step = 3, bars = Math.floor((w - 4) / step), mid = h / 2, amp = h / 2 - 3;
      for (let b = 0; b < bars; b++) {
        const v = waveData[Math.min(waveData.length - 1, Math.floor(b / bars * waveData.length))];
        if (v < 0) continue;
        const bh = Math.max(1, Math.round(v * amp));
        wg.fillRect(2 + b * step, Math.round(mid - bh), 2, bh * 2);
      }
    });
  }
  if ('ResizeObserver' in window) new ResizeObserver(() => { if (waveData) _drawWave(); }).observe(waveBox);

  // Initial state
  kind = _kindOf();
  if (kind === 'voices') _buildLeds(FFT_FALLBACK_BARS, false);
  _counter(Player.currentTime || 0);

  const api = {
    /** The tape's position 0…1 (nowplaying.js setScrub): the reels' speed. */
    position(p) { reelPos = Math.max(0, Math.min(1, Number(p) || 0)); _reels(); },
    stats,
    /** Debug / verification: what the display shows and its readings. */
    state: () => ({
      mode, kind, view: _view(), running: !!raf, viz, reducedMotion: reducedMotion(),
      shown: _shown(), vumr: vumr ? { channels: vumr.channels, rateHz: vumr.rateHz, frames: vumr.frames } : null,
      note: noteEl.textContent, leds: ledN, ledLevels: ledLv ? Array.from(ledLv.slice(0, ledN), v => +v.toFixed(3)) : [],
      needles: { pos: nd.pos.map(v => +v.toFixed(3)), angleDeg: nd.pos.map(p => +(needleAngle(p) * 180 / Math.PI).toFixed(2)) },
      bands: bands ? Array.from(bands.level, v => +v.toFixed(3)) : [],
      taps: !!taps, spectrumTap: !!anFrom, size: { ...size }, counter: countShown, reelRate: reelRate.slice(),
      wave: !!waveData,
    }),
    setMode,
  };
  window.SoniqBoom = window.SoniqBoom || {};
  window.SoniqBoom.npVisuals = api;
  _sync();
  // (the view is built on its first showing, already active: no class change
  // asks for the playing track's waveform then)
  if (_active()) _wave();
  return api;
}

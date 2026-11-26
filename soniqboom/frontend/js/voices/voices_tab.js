// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * voices/voices_tab.js — the Track Info panel's Voices tab: one row per voice
 * of the tune that is playing — its name, its oscilloscope, Mute and Solo.
 * The audio and the scope data: voices/mixer.js.
 *
 * The tab shows for a C64 SID, a libgme chiptune and an AdLib / OPL tune
 * (mute, solo and scopes — each family's note says how it mutes).  It works
 * on the playing tune; for another tune it says to play it.
 *
 * Scopes are a now-playing visualization: registered with the viz engine
 * (viz/engine.js, group ``nowPlaying``) — drawn only while the tab shows, the
 * page is visible and the switches are on; with them off the scopes are
 * hidden (Mute and Solo stay); with reduced motion they freeze on one frame.
 * The scope data is fetched only while they draw.
 */
import { Player } from '../player.js';
import { registerViz, vizGroupEnabled, prefersReducedMotion } from '../viz/engine.js';
import { TRACKER_FORMAT_NAMES, CHIP_FORMAT_NAMES, ADLIB_FORMAT_NAMES } from '../utils.js';
import { createVoiceMixer, allMask, audibleMask } from './mixer.js';

const POLL_MS = 3000;
const POLL_MAX = 60;                 // ~3 minutes of "Reading the voices…" at most
const SCOPE_MS = 26;                 // the time a scope shows

export const mixer = createVoiceMixer({ player: Player });

/** What the Voices tab offers for ``track``: 'mixer' (mute / solo / scopes)
 *  or '' (no tab). */
export function voicesKind(track) {
  if (!track || track.station) return '';
  const p = String(track.format || '').split('/')[0].trim();
  if (p === 'SID' || ADLIB_FORMAT_NAMES.has(p)) return 'mixer';
  if (CHIP_FORMAT_NAMES.has(p) && !TRACKER_FORMAT_NAMES.has(p)) return 'mixer';
  return '';
}

const _FAMILY_NOTE = {
  gme: 'Console chip (libgme): Mute and Solo play the tune again with those voices switched off inside the emulation.',
  adlib: 'AdLib / OPL (AdPlug, Nuked OPL3): Mute and Solo play the tune again without those FM channels in the mix.',
  sid: 'C64 SID (sidplayfp): Mute and Solo play the tune again with those voices switched off; the SID’s filter and mixer act on the voices left. sidplayfp starts each render after a random power-on delay, so the muted render can sit a fraction of a millisecond or more from the saved one. On a tune for two or three SID chips the keys act on the first chip’s voices.',
};

export function mountVoicesTab({ overlay, tabBtn, pane, tabInfo }) {
  if (!overlay || !tabBtn || !pane) return null;
  pane.setAttribute('role', 'tabpanel');
  pane.setAttribute('aria-labelledby', tabBtn.id);
  pane.tabIndex = 0;
  pane.innerHTML = `
    <div class="ti-voices">
      <div class="ti-voices-head">
        <span class="ti-voices-status" aria-live="polite"></span>
        <button type="button" class="ti-voices-reset" hidden>All voices</button>
      </div>
      <div class="ti-voices-list" role="group" aria-label="Voices"></div>
      <p class="ti-voices-note"></p>
    </div>`;
  const statusEl = pane.querySelector('.ti-voices-status');
  const resetBtn = pane.querySelector('.ti-voices-reset');
  const listEl = pane.querySelector('.ti-voices-list');
  const noteEl = pane.querySelector('.ti-voices-note');

  let shownTrack = null;          // the track the panel displays
  let active = false;             // the Voices tab is the current one
  let infoKey = '', info = null, pollTimer = null, rows = [], polls = 0;
  let muted = 0, soloed = 0;
  const peaks = [];
  let buf = null;

  const playing = () => Player.currentTrack;
  const keyOf = (t) => t ? `${t.id}:${Number.isInteger(t.subsong) ? t.subsong : ''}` : '';
  const sameTune = (a, b) => !!a && !!b && a.id === b.id
    && (Number.isInteger(a.subsong) ? a.subsong : null) === (Number.isInteger(b.subsong) ? b.subsong : null);

  function setStatus(t) { statusEl.textContent = t || ''; }

  function syncTab() {
    const kind = voicesKind(shownTrack);
    tabBtn.hidden = !kind;
    if (!kind && active && tabInfo) tabInfo.click();
  }

  async function loadInfo() {
    clearTimeout(pollTimer); pollTimer = null;
    const t = shownTrack;
    if (!active || !t) return;
    if (!sameTune(t, playing())) {
      render(null, 'Play this tune to mute, solo and watch its voices.');
      return;
    }
    const key = keyOf(t);
    if (mixer.track && keyOf(mixer.track) === key && mixer.info && infoKey === key) {
      render(mixer.info, '');
      return;
    }
    setStatus('Reading the voices…');
    let res = null;
    try {
      const q = Number.isInteger(t.subsong) ? `?subsong=${t.subsong}` : '';
      const r = await fetch(`/api/tracks/${encodeURIComponent(t.id)}/voices${q}`, { credentials: 'include' });
      res = r.ok ? await r.json() : null;
    } catch (_) { res = null; }
    if (shownTrack !== t || !active) return;
    if (!res) { render(null, 'The voices could not be read.'); return; }
    if (res.pending) {
      if (++polls > POLL_MAX) { render(null, 'The voices are not ready yet — open this tab again later.'); return; }
      setStatus('Reading the voices…');
      pollTimer = setTimeout(loadInfo, POLL_MS);
      return;
    }
    if (!res.voices || !res.voices.length || !res.mute) {
      const why = res.reason === 'not-playing' ? 'Play this tune to mute, solo and watch its voices.'
        : res.reason === 'skipped' ? 'Voice mixing is not available for this tune.'
        : 'Voice mixing is not available on this server for this tune.';
      render(null, why);
      return;
    }
    if (!sameTune(t, playing())) { render(null, 'Play this tune to mute, solo and watch its voices.'); return; }
    infoKey = key;
    muted = 0; soloed = 0;
    mixer.attach(playing(), res);
    render(res, '');
  }

  function render(inf, message) {
    info = inf;
    listEl.textContent = '';
    rows = [];
    resetBtn.hidden = true;
    noteEl.textContent = inf ? (_FAMILY_NOTE[inf.family] || '') + ' The saved render is never changed.' : '';
    if (!inf) { setStatus(message); showScopes(); return; }
    const frag = document.createDocumentFragment();
    inf.voices.forEach((name, v) => {
      const row = document.createElement('div');
      row.className = 'ti-voice';
      row.dataset.voice = String(v);
      row.innerHTML = `
        <span class="ti-voice-n">${v + 1}</span>
        <span class="ti-voice-name"></span>
        <canvas class="ti-voice-scope" aria-hidden="true"></canvas>
        <button type="button" class="ti-voice-btn ti-voice-m" aria-pressed="false">M</button>
        <button type="button" class="ti-voice-btn ti-voice-s" aria-pressed="false">S</button>`;
      row.querySelector('.ti-voice-name').textContent = name;
      const m = row.querySelector('.ti-voice-m');
      const s = row.querySelector('.ti-voice-s');
      m.title = `Mute ${name}`; m.setAttribute('aria-label', `Mute ${name}`);
      s.title = `Solo ${name}`; s.setAttribute('aria-label', `Solo ${name}`);
      m.addEventListener('click', () => { muted ^= (1 << v); apply(); });
      s.addEventListener('click', () => { soloed ^= (1 << v); apply(); });
      frag.appendChild(row);
      rows.push({ row, m, s, canvas: row.querySelector('.ti-voice-scope') });
      peaks[v] = 0.05;
    });
    listEl.appendChild(frag);
    paintButtons();
    paintState();
    showScopes();
  }

  function apply() {
    mixer.setAudible(audibleMask(rows.length, muted, soloed));
    paintButtons();
  }

  function paintButtons() {
    const heard = audibleMask(rows.length, muted, soloed);
    rows.forEach(({ row, m, s }, v) => {
      m.setAttribute('aria-pressed', muted & (1 << v) ? 'true' : 'false');
      s.setAttribute('aria-pressed', soloed & (1 << v) ? 'true' : 'false');
      row.classList.toggle('ti-voice-off', !(heard & (1 << v)));
    });
    resetBtn.hidden = !(muted || soloed);
  }

  function paintState() {
    if (!info) return;
    const st = mixer.state;
    setStatus(st === 'preparing' && mixer.audible !== allMask(rows.length)
      ? (Player.playing ? 'Preparing the mix…' : 'Muted voices play when the tune plays')
      : st === 'error' ? 'The mix could not be played — all voices are heard.'
      : st === 'on' ? 'Playing with voices muted'
      : `${rows.length} voices`);
  }

  resetBtn.addEventListener('click', () => { muted = 0; soloed = 0; apply(); });
  mixer.onChange(() => {
    paintState();
    // A new track (the mixer detached): the rows were for the old tune.
    if (!mixer.track && info && active) { info = null; infoKey = ''; loadInfo(); }
  });

  // ── scopes ───────────────────────────────────────────────────────────────
  const scopesOn = () => vizGroupEnabled('nowPlaying');
  function syncScopeVisibility() { listEl.classList.toggle('ti-voices-noscope', !scopesOn()); }

  // One frame of every voice's scope; true when there was data to draw.
  function drawScopes() {
    if (!info || !rows.length) return false;
    const sr = info.scope_rate || 22050;
    const win = Math.max(64, Math.round(sr * SCOPE_MS / 1000));
    const need = win * 2;
    if (!buf || buf.length !== need) buf = new Float32Array(need);
    const style = getComputedStyle(listEl);
    const ink = style.getPropertyValue('--ti-scope-ink').trim() || '#6ff7e9';
    const dim = style.getPropertyValue('--ti-scope-dim').trim() || 'rgba(255,255,255,0.15)';
    let any = false;
    rows.forEach(({ canvas }, v) => {
      const dpr = window.devicePixelRatio || 1;
      const w = Math.max(1, Math.round(canvas.clientWidth * dpr));
      const h = Math.max(1, Math.round(canvas.clientHeight * dpr));
      if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
      const c = canvas.getContext('2d');
      c.clearRect(0, 0, w, h);
      c.fillStyle = dim;
      c.fillRect(0, Math.floor(h / 2), w, Math.max(1, Math.round(dpr)));
      if (!mixer.scopeWindow(v, need, buf)) return;
      any = true;
      // Trigger: the first rising zero crossing in the first half.
      let t0 = 0;
      for (let i = 1; i < win; i++) { if (buf[i - 1] < 0 && buf[i] >= 0) { t0 = i; break; } }
      let pk = 0;
      for (let i = t0; i < t0 + win; i++) { const a = Math.abs(buf[i]); if (a > pk) pk = a; }
      // Auto-gain, slow to shrink so quiet passages don't jump.
      peaks[v] = Math.max(pk, peaks[v] * 0.97, 0.002);
      const g = 0.45 * h / peaks[v];
      c.strokeStyle = ink;
      c.lineWidth = Math.max(1, dpr * 1.2);
      c.beginPath();
      for (let i = 0; i < win; i++) {
        const x = (i / (win - 1)) * w;
        const y = h / 2 - buf[t0 + i] * g;
        if (i) c.lineTo(x, y); else c.moveTo(x, y);
      }
      c.stroke();
    });
    return any;
  }

  // Reduced motion: one still frame (its data asked once), then nothing.
  let stillTimer = null;
  function stillFrame() {
    clearTimeout(stillTimer);
    if (!active || !scopesOn() || !info) return;
    mixer.wantScopes(true);
    let tries = 0;
    const step = () => {
      if (!active || !prefersReducedMotion()) { mixer.wantScopes(active && scopesOn()); return; }
      if (drawScopes() || ++tries > 15) { mixer.wantScopes(false); return; }
      stillTimer = setTimeout(step, 300);
    };
    step();
  }

  const scopes = registerViz({
    host: listEl, group: 'nowPlaying', fps: 30, hideWhenOff: false,
    draw: () => {
      if (!active || !scopesOn()) return;
      mixer.wantScopes(true);
      drawScopes();
    },
    freeze: () => {
      // Switches off: the scopes hide (Mute / Solo stay); reduced motion: still.
      syncScopeVisibility();
      mixer.wantScopes(false);
      if (active && prefersReducedMotion()) stillFrame();
    },
  });
  function showScopes() {
    syncScopeVisibility();
    scopes.refresh();
    if (active && prefersReducedMotion()) stillFrame();
  }
  window.addEventListener('sb:viz-settings', showScopes);

  // ── the panel's events ───────────────────────────────────────────────────
  overlay.addEventListener('trackinfo:render', (e) => {
    const t = e?.detail?.track || null;
    shownTrack = t;
    syncTab();
    if (active) loadInfo();
  });
  overlay.addEventListener('trackinfo:tab', (e) => {
    active = e?.detail?.tab === 'voices';
    if (!active) { mixer.wantScopes(false); clearTimeout(pollTimer); }
    else { polls = 0; loadInfo(); }
    showScopes();
  });
  overlay.addEventListener('trackinfo:close', () => {
    active = false;
    mixer.wantScopes(false);
    clearTimeout(pollTimer);
    showScopes();
  });
  Player.on('trackchange', () => { if (active) setTimeout(loadInfo, 0); });
  Player.on('statechange', () => paintState());

  return { mixer, refresh: loadInfo };
}

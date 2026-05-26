// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * sync.js — multi-room sync engine.
 *
 * Responsibilities:
 *  - WebSocket lifecycle + reconnect
 *  - Cristian-style clock-skew probes (ping/pong, EWMA over last 8 samples)
 *  - Track-change barrier (master `prepare` → slave `ready` → server `play_at`)
 *  - Steady-state drift correction (tuned to avoid stuttering):
 *      <20 ms   no-op
 *      20–1000 ms   proportional playbackRate (up to ±3 %)
 *      1000–2500 ms hard seek, but only after 2 consecutive samples agree
 *      >2500 ms     immediate hard seek
 *    Fresh `play_at` barrier disables drift eval for a 2 s grace window so
 *    the audio output pipeline can settle without being seeked mid-buffer.
 *    After any hard seek, a 1.5 s cooldown prevents back-to-back seeks.
 *    Drift is EWMA-smoothed to absorb single noisy samples.
 *
 * Wraps the existing `Player` singleton from `player.js` as the audio engine.
 */
import { Player } from '../player.js';

const SKEW_SAMPLES         = 8;
const PING_INTERVAL        = 2000;    // ms between probes
const DRIFT_NOOP           = 20;      // ms — inside this, do nothing
const DRIFT_RATE_MAX       = 1000;    // ms — up to here we rate-correct only
const DRIFT_REBARRIER      = 2500;    // ms — above this, immediate hard seek
const RATE_DELTA           = 0.03;    // ±3 % — ~52 cents, barely audible on transients
const RATE_SATURATE_MS     = 1500;    // drift that maxes out rate correction
const POST_BARRIER_GRACE   = 2000;    // ms — skip drift eval after a barrier fires
const POST_SEEK_COOLDOWN   = 1500;    // ms — skip further seeks right after one
const HARD_SEEK_CONSECUTIVE = 2;      // consecutive out-of-band samples before seek
const DRIFT_EWMA_ALPHA     = 0.2;     // smoothing factor (more inertia = less jumpy)
const DRIFT_OUTLIER_MS     = 600;     // raw samples this far from EWMA are discarded
const DRIFT_CAL_SAMPLES    = 3;       // first N samples after grace set the baseline
const DRIFT_CAL_MAX_MS     = 1000;    // reject implausible baselines (real desync, not offset)

// iPadOS ≥ 13 masquerades as MacIntel — detect via touch points.
const IS_MOBILE = /iPad|iPhone|iPod|Android/i.test(navigator.userAgent)
  || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

// Barrier tuning. Mobile Safari underreports "ready" state and benefits from
// a deeper preload cushion before it actually plays without glitching.
const BARRIER_MARGIN_DESKTOP  = 400;  // ms — base schedule lead after all acks
const BARRIER_MARGIN_MOBILE   = 700;
const BARRIER_MARGIN_FALLBACK = 200;  // used when no slaves are listening
const SLAVE_BUFFER_TARGET_S   = IS_MOBILE ? 7 : 5;   // seconds of headroom before ready
const SLAVE_BUFFER_MAX_WAIT   = IS_MOBILE ? 4000 : 2000;  // ms cap on the extra top-up
const MASTER_BARRIER_TIMEOUT  = 4000; // ms — release play_at even if some slaves didn't ack

/** Seconds of contiguous buffered audio ahead of currentTime. */
function _bufferedAhead(a) {
  if (!a.buffered || !a.buffered.length) return 0;
  const t = a.currentTime || 0;
  for (let i = 0; i < a.buffered.length; i++) {
    if (a.buffered.start(i) <= t && a.buffered.end(i) >= t) {
      return a.buffered.end(i) - t;
    }
  }
  return a.buffered.end(a.buffered.length - 1);
}

/** Top up the preload headroom past Player.playTrack's own wait. */
function _extraBufferWait(audio, targetSec, maxWaitMs) {
  if (targetSec <= 0) return Promise.resolve();
  if (_bufferedAhead(audio) >= targetSec) return Promise.resolve();
  return new Promise((resolve) => {
    const start = Date.now();
    const tick = () => {
      if (_bufferedAhead(audio) >= targetSec) return resolve();
      if (Date.now() - start >= maxWaitMs) return resolve();
      setTimeout(tick, 100);
    };
    tick();
  });
}

function newClientId() {
  if (crypto?.randomUUID) return crypto.randomUUID();
  return `c-${Math.random().toString(36).slice(2, 10)}-${Date.now()}`;
}

// Persist the clientId across reloads so reconnects rejoin under the
// same identity — without this, every F5 leaks a ghost slave into the
// room (Perf #2 flagged this for the 3-room load).
function persistentClientId() {
  try {
    let id = localStorage.getItem('sb_mr_client_id');
    if (!id) {
      id = newClientId();
      localStorage.setItem('sb_mr_client_id', id);
    }
    return id;
  } catch {
    return newClientId();
  }
}

function nowMs() { return Date.now(); }

class SyncEngine extends EventTarget {
  constructor() {
    super();
    this.role        = null;            // 'master' | 'slave'
    this.clientId    = persistentClientId();
    this.roomId      = null;
    this.roomName    = null;
    this.label       = 'Device';
    this.masterId    = null;
    this.clients     = [];

    this.ws          = null;
    this._wsUrl      = null;
    this._reconnectDelay = 1000;
    this._reconnectTimer = null;
    this._pingTimer  = null;

    // Skew tracking (EWMA over last N samples)
    this._skewSamples = [];
    this.skewMs      = 0;
    this.rttMs       = 0;

    // Slave-only drift state
    this._ewmaDrift       = null;   // null means "no sample yet"
    this._hardSeekStreak  = 0;
    this._graceUntil      = 0;      // skip drift eval while Date.now() < this
    this._cooldownUntil   = 0;      // skip further seeks while Date.now() < this
    this._lastState       = null;
    this.lastDrift        = 0;
    // Per-device intrinsic `currentTime` bias (Safari iOS reports ~600 ms
    // behind the audible output; Edge ~0 ms). Learned from the first few
    // samples after each barrier and subtracted from future drift readings.
    this._driftBaseline     = null;
    this._calibrationSamples = [];

    // Master-only: pending barrier waiting for slave `ready` acks.
    // Shape: { barrierId, expected: Set<clientId>, received: Set<clientId>,
    //          resolve, timeoutId }
    this._pendingBarrier  = null;

    // True while the user is deliberately leaving — suppresses auto-reconnect
    // that would otherwise re-fetch last_state and restart the paused track.
    this._intentionalClose = false;

    // Expose live state for the console
    window.__mrDebug = this;
  }

  async connect({ roomId, roomName, role, label }) {
    this.roomId = roomId || null;
    this.roomName = roomName || 'Room';
    this.role = role;
    this.label = label || 'Device';
    this._intentionalClose = false;

    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    this._wsUrl = `${proto}//${location.host}/api/multiroom/ws`;
    await this._openSocket();
  }

  _openSocket() {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(this._wsUrl);
      this.ws = ws;

      ws.addEventListener('open', () => {
        this._reconnectDelay = 1000;
        ws.send(JSON.stringify({
          type:        'hello',
          ts:          nowMs(),
          client_id:   this.clientId,
          room_id:     this.roomId,
          room_name:   this.roomName,
          role_wanted: this.role,
          label:       this.label,
        }));
        this._startPings();
        resolve();
      });

      ws.addEventListener('message', (ev) => this._onMessage(ev));

      ws.addEventListener('close', () => {
        this._stopPings();
        this.dispatchEvent(new CustomEvent('disconnected'));
        if (!this._intentionalClose) this._scheduleReconnect();
      });

      ws.addEventListener('error', (err) => {
        // Errors are followed by close; don't reject the promise after open.
        if (ws.readyState === WebSocket.CONNECTING) reject(err);
      });
    });
  }

  _scheduleReconnect() {
    if (this._reconnectTimer) return;
    const delay = this._reconnectDelay;
    this._reconnectDelay = Math.min(this._reconnectDelay * 2, 10000);
    this._reconnectTimer = setTimeout(() => {
      this._reconnectTimer = null;
      this._openSocket().catch(() => this._scheduleReconnect());
    }, delay);
  }

  close() {
    this._intentionalClose = true;
    this._cancelPendingBarrier();
    this._stopPings();
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      try { this.ws.send(JSON.stringify({ type: 'bye', ts: nowMs() })); } catch { /* ignore */ }
      this.ws.close();
    }
    this.ws = null;
    // Reset drift-correction state so a later rejoin doesn't inherit stale values.
    this._ewmaDrift          = null;
    this._hardSeekStreak     = 0;
    this._graceUntil         = 0;
    this._cooldownUntil      = 0;
    this._lastState          = null;
    this._driftBaseline      = null;
    this._calibrationSamples = [];
    this.masterId            = null;
    this.clients             = [];
    try { if (Player.audio.playbackRate !== 1) Player.audio.playbackRate = 1; } catch { /* ignore */ }
  }

  _send(obj) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
    this.ws.send(JSON.stringify({ ts: nowMs(), ...obj }));
    return true;
  }

  // ── Clock skew probes ──────────────────────────────────────────────────

  _startPings() {
    this._stopPings();
    this._pingTimer = setInterval(() => this._ping(), PING_INTERVAL);
    this._ping();  // first probe immediately
  }

  _stopPings() {
    if (this._pingTimer) { clearInterval(this._pingTimer); this._pingTimer = null; }
  }

  _ping() {
    this._send({
      type: 'ping',
      nonce: Math.random().toString(36).slice(2),
      clientMonoMs: performance.now(),
    });
  }

  _handlePong(msg) {
    const t3 = performance.now();
    const t0 = msg.clientMonoMs;
    if (typeof t0 !== 'number') return;
    const rtt = t3 - t0;
    // Cristian's: estimate server_now at reply ≈ serverMonoMs, client_now at reply ≈ t3
    // skew = (server clock - client clock) in "Date.now()" space ≈ ts - (t0 + t3)/2 offset
    // We actually want offset between server Date.now() and client Date.now() so we can
    // convert server-stamped epoch timestamps into the local clock.
    const serverTs = msg.ts;               // server's Date.now() when it replied
    const clientTsAtReply = Date.now() - (t3 - t0) * 0.5;
    const skew = serverTs - clientTsAtReply;

    this._skewSamples.push({ rtt, skew });
    if (this._skewSamples.length > SKEW_SAMPLES) this._skewSamples.shift();

    // EWMA weighting newest samples heavier; trim the 25% worst-RTT ones.
    const sorted = [...this._skewSamples].sort((a, b) => a.rtt - b.rtt);
    const keep = sorted.slice(0, Math.max(1, Math.ceil(sorted.length * 0.75)));
    let accumS = 0, accumR = 0;
    for (const s of keep) { accumS += s.skew; accumR += s.rtt; }
    this.skewMs = accumS / keep.length;
    this.rttMs  = accumR / keep.length;
  }

  /** Convert a server-epoch ms (Date.now on server) into local Date.now ms. */
  serverToLocal(serverEpochMs) {
    return serverEpochMs - this.skewMs;
  }

  // ── Message routing ────────────────────────────────────────────────────

  _onMessage(ev) {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    const t = msg.type;

    if (t === 'pong') { this._handlePong(msg); return; }

    if (t === 'welcome') {
      this.role      = msg.your_role;
      this.clientId  = msg.client_id || this.clientId;
      this.roomId    = msg.room_id;
      this.roomName  = msg.room_name;
      this.masterId  = msg.master_id;
      this.clients   = msg.clients || [];
      this.dispatchEvent(new CustomEvent('welcome', { detail: msg }));
      // If we joined an already-playing room, the master's last_state is attached.
      if (msg.last_state && this.role === 'slave' && msg.last_state.track) {
        // Apply it after a short delay so the UI can render first.
        setTimeout(() => this._applyInitialState(msg.last_state), 50);
      }
      return;
    }

    if (t === 'roster') {
      this.clients = msg.clients || [];
      if (this.role === 'master') this._reconcileBarrierRoster();
      this.dispatchEvent(new CustomEvent('roster', { detail: msg }));
      return;
    }

    if (t === 'ready' && this.role === 'master') {
      this._onReady(msg);
      return;
    }

    if (t === 'master_changed') {
      this.masterId = msg.master_id;
      if (this.masterId === null && this.role === 'slave') {
        // Master left — pause and offer takeover
        try { Player.audio.pause(); } catch { /* ignore */ }
      }
      this.dispatchEvent(new CustomEvent('master_changed', { detail: msg }));
      return;
    }

    if (t === 'error') {
      this.dispatchEvent(new CustomEvent('sync_error', { detail: msg }));
      return;
    }

    // Slave-specific events
    if (this.role === 'slave') {
      if (t === 'prepare')  { this._slavePrepare(msg);  return; }
      if (t === 'play_at')  { this._slavePlayAt(msg);   return; }
      if (t === 'state')    { this._slaveState(msg);    return; }
      if (t === 'seek')     { this._slaveSeek(msg);     return; }
      if (t === 'pause')    { this._slavePause(msg);    return; }
    }
  }

  // ── Master → API ───────────────────────────────────────────────────────
  // Called by master.js when the user picks a track.

  async masterPlayTrack(track, { seek = 0 } = {}) {
    if (this.role !== 'master') return;
    const barrierId = `b-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;

    // Snapshot the slaves expected to ack this barrier. If any leave before
    // acking, _reconcileBarrierRoster will drop them from the expected set.
    const slaveIds = new Set(
      this.clients
        .filter(c => c.role === 'slave' && c.client_id !== this.clientId)
        .map(c => c.client_id)
    );

    // Any previous barrier is void — release it and start fresh.
    this._cancelPendingBarrier();

    // 1) Tell slaves to preload the new track
    this._send({
      type:      'prepare',
      trackId:   track.id,
      path:      track.path || null,
      seek,
      barrierId,
      track,
    });

    // 2) Register the pending barrier before doing anything else that awaits.
    const readyPromise = (slaveIds.size === 0)
      ? Promise.resolve('empty')
      : new Promise((resolve) => {
          const pending = {
            barrierId,
            expected: slaveIds,
            received: new Set(),
            resolve,
            timeoutId: setTimeout(() => {
              if (this._pendingBarrier === pending) {
                this._pendingBarrier = null;
                resolve('timeout');
              }
            }, MASTER_BARRIER_TIMEOUT),
          };
          this._pendingBarrier = pending;
        });

    // 3) Load + buffer locally via the Player
    await Player.playTrack(track);
    Player.audio.pause();
    if (seek > 0) {
      try { Player.audio.currentTime = seek; } catch { /* ignore */ }
    }

    // 4) Wait for all current slaves to ack (or timeout)
    const outcome = await readyPromise;

    // 5) Schedule a synchronized start for everyone. If some slaves timed out,
    //    give them a tiny extra margin — they'll catch up via drift correction.
    const baseMargin = IS_MOBILE ? BARRIER_MARGIN_MOBILE
                     : (outcome === 'empty' ? BARRIER_MARGIN_FALLBACK : BARRIER_MARGIN_DESKTOP);
    const startServerEpoch = Date.now() + baseMargin + Math.max(this.rttMs, 40);
    this._send({
      type:            'play_at',
      serverEpochMs:   startServerEpoch,
      positionAtStart: seek,
    });
    // Schedule our own audio.play() on the same wall clock.  Clear any
    // previous scheduled start so a rapid track-skip can't fire two
    // ``audio.play()`` calls against the new src in succession (Perf #2
    // caught the race after barrier cancellation).
    if (this._scheduledPlayTimer) {
      clearTimeout(this._scheduledPlayTimer);
      this._scheduledPlayTimer = null;
    }
    const delay = startServerEpoch - Date.now();
    this._scheduledPlayTimer = setTimeout(() => {
      this._scheduledPlayTimer = null;
      Player.audio.play().catch(() => {});
      this._emitStateUpdate();
    }, Math.max(0, delay));
  }

  masterPlayPause() {
    if (this.role !== 'master') return;
    if (Player.audio.paused) {
      // Slaves already hold the track buffered — no barrier needed, just a
      // small scheduling lead so setTimeout fires reliably on all clients.
      const startEpoch = Date.now() + BARRIER_MARGIN_FALLBACK;
      this._send({ type: 'play_at',
                   serverEpochMs:   startEpoch,
                   positionAtStart: Player.audio.currentTime });
      const delay = startEpoch - Date.now();
      setTimeout(() => {
        Player.audio.play().catch(() => {});
        this._emitStateUpdate();
      }, Math.max(0, delay));
    } else {
      const pauseEpoch = Date.now() + 30;
      this._send({ type: 'pause', serverEpochMs: pauseEpoch });
      setTimeout(() => {
        Player.audio.pause();
        this._emitStateUpdate();
      }, Math.max(0, pauseEpoch - Date.now()));
    }
  }

  _onReady(msg) {
    const pending = this._pendingBarrier;
    if (!pending || pending.barrierId !== msg.barrierId) return;
    if (!msg.clientId) return;
    pending.received.add(msg.clientId);
    this._maybeCompleteBarrier();
  }

  _reconcileBarrierRoster() {
    const pending = this._pendingBarrier;
    if (!pending) return;
    const currentSlaves = new Set(
      this.clients
        .filter(c => c.role === 'slave' && c.client_id !== this.clientId)
        .map(c => c.client_id)
    );
    for (const cid of [...pending.expected]) {
      if (!currentSlaves.has(cid)) pending.expected.delete(cid);
    }
    this._maybeCompleteBarrier();
  }

  _maybeCompleteBarrier() {
    const pending = this._pendingBarrier;
    if (!pending) return;
    // Completion condition: every still-expected slave has acked. An empty
    // expected set (everyone who was asked has since left) also resolves.
    let allAcked = true;
    for (const cid of pending.expected) {
      if (!pending.received.has(cid)) { allAcked = false; break; }
    }
    if (allAcked) {
      clearTimeout(pending.timeoutId);
      this._pendingBarrier = null;
      pending.resolve('all');
    }
  }

  _cancelPendingBarrier() {
    const pending = this._pendingBarrier;
    if (!pending) return;
    clearTimeout(pending.timeoutId);
    this._pendingBarrier = null;
    pending.resolve('cancelled');
    // Also drop any already-scheduled audio.play() — see masterPlayTrack
    // above.  Without this a skip mid-barrier can leave the previous
    // timer queued and fire play() against the new track right after we
    // schedule the *new* play.
    if (this._scheduledPlayTimer) {
      clearTimeout(this._scheduledPlayTimer);
      this._scheduledPlayTimer = null;
    }
  }

  masterSeek(pct) {
    if (this.role !== 'master') return;
    const dur = Player.audio.duration || 0;
    if (!dur) return;
    const pos = (pct / 100) * dur;
    const epoch = Date.now() + 60;
    // Local seek immediately so the master UI mirrors quickly
    try { Player.audio.currentTime = pos; } catch { /* ignore */ }
    this._send({ type: 'seek', position: pos, serverEpochMs: epoch });
    this._emitStateUpdate();
  }

  _emitStateUpdate() {
    if (this.role !== 'master') return;
    // Stamp the moment of sampling in server wall-clock. Slaves subtract their
    // own skew to get the sample time in their local clock, which removes
    // master→server→slave relay jitter from the drift calculation.
    this._send({
      type:            'state_update',
      trackId:         Player.currentTrackId,
      position:        Player.audio.currentTime || 0,
      playing:         !Player.audio.paused,
      duration:        Player.audio.duration || 0,
      track:           Player.currentTrack || null,
      sampledAtServer: Date.now() + this.skewMs,
    });
  }

  takeMaster() {
    this._send({ type: 'take_master' });
  }

  // ── Slave handlers ─────────────────────────────────────────────────────

  async _slavePrepare(msg) {
    const track = msg.track;
    if (!track) return;
    // Load the same track locally; pause once ready.
    await Player.playTrack(track);
    Player.audio.pause();
    if (msg.seek > 0) {
      try { Player.audio.currentTime = msg.seek; } catch { /* ignore */ }
    }
    // Mobile Safari's readyState lies — top up headroom before acking so the
    // master's play_at doesn't arrive while we're still underrun-prone.
    await _extraBufferWait(Player.audio, SLAVE_BUFFER_TARGET_S, SLAVE_BUFFER_MAX_WAIT);
    this._send({ type: 'ready', trackId: track.id, barrierId: msg.barrierId });
  }

  _slavePlayAt(msg) {
    const localTarget = this.serverToLocal(msg.serverEpochMs);
    const delay = localTarget - Date.now();
    // Position sync: master passes positionAtStart so we can align
    const pos = msg.positionAtStart || 0;
    try { if (Math.abs(Player.audio.currentTime - pos) > 0.05) Player.audio.currentTime = pos; } catch { /* ignore */ }
    setTimeout(() => {
      Player.audio.play().catch(() => {
        this.dispatchEvent(new CustomEvent('autoplay_blocked'));
      });
      // Give the output pipeline a moment to settle before drift kicks in.
      this._graceUntil        = Date.now() + POST_BARRIER_GRACE;
      this._cooldownUntil     = 0;
      this._ewmaDrift         = null;
      this._hardSeekStreak    = 0;
      this._driftBaseline     = null;
      this._calibrationSamples = [];
    }, Math.max(0, delay));
  }

  _slaveState(msg) {
    this._lastState = msg;
    this._evaluateDrift(msg);
    this.dispatchEvent(new CustomEvent('state', { detail: msg }));
  }

  _slaveSeek(msg) {
    const delay = Math.max(0, this.serverToLocal(msg.serverEpochMs) - Date.now());
    setTimeout(() => {
      try { Player.audio.currentTime = msg.position || 0; } catch { /* ignore */ }
      // After a seek the output pipeline re-primes — the learned baseline
      // may no longer apply, so recalibrate.
      this._graceUntil         = Date.now() + POST_BARRIER_GRACE;
      this._ewmaDrift          = null;
      this._hardSeekStreak     = 0;
      this._driftBaseline      = null;
      this._calibrationSamples = [];
    }, delay);
  }

  _slavePause(msg) {
    const delay = Math.max(0, this.serverToLocal(msg.serverEpochMs) - Date.now());
    setTimeout(() => {
      try { Player.audio.pause(); } catch { /* ignore */ }
    }, delay);
  }

  async _applyInitialState(state) {
    if (!state || !state.track) return;

    // Freeze drift evaluation while we load so no in-flight `state` frames
    // accidentally seed the calibration baseline with load-phase noise.
    this._graceUntil = Date.now() + 60000;

    try {
      await Player.playTrack(state.track);
      Player.audio.pause();

      // Master is paused — park at the reported position; no sync needed.
      if (!state.playing) {
        try { Player.audio.currentTime = state.position || 0; } catch { /* ignore */ }
        this._graceUntil = 0;
        return;
      }

      // Top up buffer the same way the slave would for a real prepare/ready.
      await _extraBufferWait(Player.audio, SLAVE_BUFFER_TARGET_S, SLAVE_BUFFER_MAX_WAIT);

      // Synthetic barrier: schedule a start instant, compute where master
      // WILL be at that instant (not where master was when the welcome was
      // built), seek there, then play. This eliminates the "whatever the
      // slave was doing during load becomes the baseline" lottery.
      const margin = IS_MOBILE ? BARRIER_MARGIN_MOBILE : BARRIER_MARGIN_DESKTOP;
      const startLocal = Date.now() + margin;
      const elapsed = (typeof state.sampledAtServer === 'number')
        ? Math.max(0, (startLocal - (state.sampledAtServer - this.skewMs)) / 1000)
        : margin / 1000;
      const targetPos = Math.max(0, (state.position || 0) + elapsed);
      try { Player.audio.currentTime = targetPos; } catch { /* ignore */ }

      const delay = Math.max(0, startLocal - Date.now());
      setTimeout(() => {
        Player.audio.play().catch(() => {
          this.dispatchEvent(new CustomEvent('autoplay_blocked'));
        });
        // Mirror the post-barrier reset so calibration runs cleanly on a
        // freshly-aligned slave.
        this._graceUntil         = Date.now() + POST_BARRIER_GRACE;
        this._cooldownUntil      = 0;
        this._ewmaDrift          = null;
        this._hardSeekStreak     = 0;
        this._driftBaseline      = null;
        this._calibrationSamples = [];
      }, delay);
    } catch {
      // If load fails, unfreeze so subsequent state frames can re-trigger.
      this._graceUntil = 0;
    }
  }

  // ── Drift evaluation (slave) ───────────────────────────────────────────

  _evaluateDrift(state) {
    if (!state || state.trackId !== Player.currentTrackId) return;
    if (!state.playing) { this._resetRate(); return; }

    const now = Date.now();
    if (now < this._graceUntil) return;       // post-barrier settle window

    // Raw drift: positive = slave is ahead of master.
    // Prefer the master-stamped sample time (converted to slave local) so the
    // relay latency from master→server→slave doesn't leak into the drift.
    // Fall back to the server forward time for older frames or before skew
    // has settled.
    const localTs = (typeof state.sampledAtServer === 'number')
      ? state.sampledAtServer - this.skewMs
      : this.serverToLocal(state.ts);
    const elapsed  = Math.max(0, (now - localTs) / 1000);
    const expected = state.position + elapsed;
    const actual   = Player.audio.currentTime || 0;
    const raw      = (actual - expected) * 1000;

    // Calibration phase: collect the first few post-grace samples to learn
    // the device's intrinsic `currentTime`-vs-audible offset. Safari on iOS
    // reports ~600 ms of pipeline lag that's a measurement artifact, not real
    // desync — baselining it avoids chasing a phantom.
    if (this._driftBaseline === null) {
      this._calibrationSamples.push(raw);
      this.lastDrift = raw;
      this.dispatchEvent(new CustomEvent('drift', { detail: { driftMs: raw, calibrating: true } }));
      if (this._calibrationSamples.length >= DRIFT_CAL_SAMPLES) {
        const mean = this._calibrationSamples.reduce((a, b) => a + b, 0)
                     / this._calibrationSamples.length;
        // Ignore implausibly large baselines — that's genuine desync we
        // should correct, not a platform offset.
        this._driftBaseline = Math.abs(mean) <= DRIFT_CAL_MAX_MS ? mean : 0;
        this._ewmaDrift = 0;  // seed EWMA at the new zero-point
      }
      return;
    }

    const adjusted = raw - this._driftBaseline;

    // EWMA-smooth to dampen noisy per-sample spikes from Wi-Fi / scheduler jitter.
    // Reject single-sample spikes more than DRIFT_OUTLIER_MS away from the
    // running estimate — they're almost always a GC pause or packet burst,
    // not a real position change.
    if (this._ewmaDrift === null) {
      this._ewmaDrift = adjusted;
    } else if (Math.abs(adjusted - this._ewmaDrift) <= DRIFT_OUTLIER_MS) {
      this._ewmaDrift = DRIFT_EWMA_ALPHA * adjusted + (1 - DRIFT_EWMA_ALPHA) * this._ewmaDrift;
    }
    // else: drop the sample; EWMA retains previous value.

    const driftMs = this._ewmaDrift;
    const abs     = Math.abs(driftMs);
    this.lastDrift = driftMs;
    this.dispatchEvent(new CustomEvent('drift', { detail: { driftMs, calibrating: false } }));

    // Tier 0 — locked, snap rate back to 1.0.
    if (abs < DRIFT_NOOP) {
      this._hardSeekStreak = 0;
      this._resetRate();
      return;
    }

    // Tier 3 — egregious offset: skip the streak, seek immediately.
    if (abs > DRIFT_REBARRIER) {
      this._hardSeek(expected);
      return;
    }

    // Tier 2 — out-of-band but still recoverable: require N in a row before seeking.
    if (abs > DRIFT_RATE_MAX) {
      this._hardSeekStreak++;
      if (this._hardSeekStreak >= HARD_SEEK_CONSECUTIVE
          && now >= this._cooldownUntil) {
        this._hardSeek(expected);
      } else {
        // Keep rate pinned at the edge so we're still closing the gap.
        this._applyProportionalRate(driftMs);
      }
      return;
    }

    // Tier 1 — in-band (20..1000 ms): proportional rate correction only.
    // Hold the corrected rate until drift closes below DRIFT_NOOP; no auto-reset.
    this._hardSeekStreak = 0;
    this._applyProportionalRate(driftMs);
  }

  _applyProportionalRate(driftMs) {
    // Positive drift (we're ahead) → slow down; negative → speed up.
    const sign = driftMs > 0 ? -1 : 1;
    const mag  = Math.min(RATE_DELTA, Math.abs(driftMs) / RATE_SATURATE_MS * RATE_DELTA);
    const targetRate = 1 + sign * mag;
    try {
      // Safari re-primes the audio graph on every playbackRate change, which
      // manifests as a brief click/drop. Deadband at 0.3 % keeps us stable
      // between drift samples instead of nudging the rate twice a second.
      if (Math.abs(Player.audio.playbackRate - targetRate) > 0.003) {
        Player.audio.playbackRate = targetRate;
      }
    } catch { /* ignore */ }
  }

  _hardSeek(expectedPos) {
    try { Player.audio.currentTime = expectedPos; } catch { /* ignore */ }
    this._resetRate();
    this._ewmaDrift      = 0;
    this._hardSeekStreak = 0;
    this._cooldownUntil  = Date.now() + POST_SEEK_COOLDOWN;
  }

  _resetRate() {
    try { if (Player.audio.playbackRate !== 1) Player.audio.playbackRate = 1; } catch { /* ignore */ }
  }
}

export const Sync = new SyncEngine();

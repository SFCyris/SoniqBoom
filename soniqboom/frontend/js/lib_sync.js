// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * lib_sync.js — which library generation this page last synced to.
 *
 * The server's ``lib_gen`` token moves whenever the lists, counts or All
 * Tracks may have changed, and on a server restart.  It comes with the
 * WebSocket's connect snapshot (``snapshot: true``), with every
 * ``scan_progress`` frame with ``running: false`` and with a
 * ``catalogue_changed`` event — the frames after which the page re-reads its
 * view.  Comparing a snapshot's token with the last one seen tells a
 * (re)connecting page whether it missed a change while disconnected.
 *
 * Before its first sync point the page is loading its data: every browse
 * response says the generation it was served at (``X-SB-Lib-Gen``,
 * ``watchBootLoads``).  The first snapshot re-reads when one of them differs
 * from it (a change landed between a first load and the connect), and a first
 * load answered only after it, at another generation, asks for one re-read.
 */

export function createLibSync() {
  let gen = null;
  let decided = false;            // the first sync point was seen
  const bootGens = new Set();     // generations the first loads were served at
  let lateReread = false;         // a late first load asked for its re-read
  return {
    /** The last token seen, or null before the first. */
    get gen() { return gen; },
    /** No sync point seen yet: the page's first loads are being tracked. */
    get booting() { return !decided; },
    /**
     * A browse response's ``X-SB-Lib-Gen`` for a request made while booting.
     * Before the first sync point it is remembered; after it, a token other
     * than the synced one returns true once (the caller re-reads its view).
     */
    served(token) {
      if (typeof token !== 'string' || !token) return false;
      if (!decided) {
        // (two different ones already decide a first snapshot: no need for more)
        bootGens.add(token);
        if (bootGens.size > 2) bootGens.delete(bootGens.values().next().value);
        return false;
      }
      if (token === gen || lateReread) return false;
      lateReread = true;
      return true;
    },
    /**
     * Remember ``token`` as synced (a scan's completion frame, a library
     * change event).  Returns whether it moved from the last one (always
     * true without a usable token: treat as changed).
     */
    note(token) {
      decided = true;
      if (typeof token !== 'string' || !token) return true;
      const moved = token !== gen;
      gen = token;
      return moved;
    },
    /**
     * A connect snapshot's token: ``'first'`` (nothing synced yet and the
     * page's first loads were served at this generation), ``'same'``
     * (nothing changed), ``'changed'`` (a change was missed — or landed
     * during the first loads: re-read), or ``'unknown'`` (no token: treat as
     * changed).  The token is remembered.
     */
    snapshot(token) {
      decided = true;
      if (typeof token !== 'string' || !token) return 'unknown';
      const prev = gen;
      gen = token;
      if (prev === null) {
        for (const g of bootGens) if (g !== token) return 'changed';
        return 'first';
      }
      return prev === token ? 'same' : 'changed';
    },
  };
}

/**
 * Track the page's first loads: ``fetch`` (on ``target``) is wrapped while
 * ``sync`` is booting — each ``/api/`` response reports its ``X-SB-Lib-Gen``
 * to ``sync.served``; ``onStale()`` runs when one answered after the first
 * sync point at another generation.  Requests made after the first sync point
 * are not tracked, and the wrapper takes itself out once the tracked ones
 * have all answered (unless something wrapped ``fetch`` after it).
 */
export function watchBootLoads(sync, onStale, target = globalThis) {
  const inner = target.fetch;
  if (typeof inner !== 'function') return;
  let pending = 0;
  const unwrap = () => {
    if (!sync.booting && pending === 0 && target.fetch === wrapped) target.fetch = inner;
  };
  const wrapped = function bootLoadFetch(input, init) {
    const p = inner.call(target, input, init);
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    if (!sync.booting || !/(^|\/\/[^/]+)\/api\//.test(url)) { unwrap(); return p; }
    pending++;
    return p.then((r) => {
      pending--;
      try {
        if (sync.served(r && r.headers ? r.headers.get('X-SB-Lib-Gen') : null)) onStale();
      } catch (_) { /* never break the caller's request */ }
      unwrap();
      return r;
    }, (e) => { pending--; unwrap(); throw e; });
  };
  target.fetch = wrapped;
}

/**
 * What a connect snapshot with no scan running asks of the page.
 * ``change``: LibSync.snapshot()'s answer; ``wasScanning``: the page showed a
 * scan in progress before the socket dropped; ``embedding``: the snapshot is
 * the embedding phase.
 *   reread    — re-read counts, the open view and the folder tree
 *   showDone  — the scan the page showed has ended: badge "Done", spinner off
 *   pollFacet — a scan ended: watch for its Scene-groups data
 */
export function snapshotPlan(change, { wasScanning = false, embedding = false } = {}) {
  const reread = change === 'changed' || change === 'unknown' || (wasScanning && change !== 'same');
  return {
    reread,
    showDone: wasScanning && !embedding,
    pollFacet: reread && wasScanning && !embedding,
  };
}

export const LibSync = createLibSync();

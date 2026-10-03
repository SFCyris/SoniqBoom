/**
 * tunes.js — helpers for the tune lists of multi-tune files (desktop Track
 * Info picker, mobile Now Playing).  A separate module on purpose: a page
 * still controlled by an older service worker can serve a cached utils.js
 * without these names, and a named import of a missing export fails to link.
 */
import { subsongStart, subsongWireToTune } from './utils.js';

// A queue entry for one tune of a multi-tune file: the file's track object
// plus the wire in ``subsong`` (what the player forwards as ?subsong=), the
// tune count / start song it was picked under, and a "Tune N" label.
// ``duration``: the file's stored length is its default tune's (``def``,
// 1-based: the one a plain play plays — the start song unless given), so
// another tune carries its own (``lengths``, in tune order — HVSC
// Songlengths) or none until it plays.  Shared by the desktop track-info
// picker and the mobile Now Playing tune list.
export function subsongVirtualTrack(base, wire, { count = 0, start = 1, lengths = null, def = null } = {}) {
  const tune = subsongWireToTune(wire, start, count);
  const own = Array.isArray(lengths) ? Number(lengths[tune - 1]) : 0;
  const isDef = tune === subsongStart(def ?? start, count);
  return { ...base, subsong: wire,
           duration: own > 0 ? own : (isDef ? base.duration : 0),
           subsongTotal: count,
           subsongStart: subsongStart(start, count),
           subsongLabel: `Tune ${tune}` };
}
// Console-rip formats that can hold several tunes (libgme): their track-info
// panel shows the tune list even though they have no module details.
export const TUNE_CHIP_FORMAT_NAMES = new Set(['NSF', 'NSFe', 'GBS', 'AY', 'SAP', 'KSS', 'HES']);

// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * utils.js — Shared utility functions used across multiple modules.
 * No imports from other SoniqBoom modules (prevents circular deps).
 */

/**
 * Tracker format names as reported by mutagen / openmpt123.
 * Used both for VU-meter detection and for placeholder emoji selection.
 */
export const TRACKER_FORMAT_NAMES = new Set([
  'ProTracker', 'ScreamTracker 3', 'FastTracker 2', 'Impulse Tracker',
  'MultiTracker', 'OctaMED', 'Composer 669', 'DigiBooster Pro',
  'AHX', 'HivelyTracker', 'UltraTracker', 'ScreamTracker 2',
  'Farandole', 'ASYLUM/DMP', 'General DigiMusic', 'Imago Orpheus',
  'Oktalyzer', 'SoundFX', 'Grave Composer', 'DSIK',
]);

/** File extension fallback list (lower-case, no dot). */
const TRACKER_EXTS = new Set([
  'mod', 's3m', 'xm', 'it', 'mtm', 'med', 'oct', '669',
  'dbm', 'ahx', 'hvl', 'ult', 'stm', 'far', 'amf',
  'gdm', 'imf', 'okt', 'sfx', 'wow', 'dsm',
]);

/**
 * Return the format-appropriate placeholder emoji for a track with no art.
 *
 *   🕹️  SID / PSID         (C64 chiptune)
 *   🎼  MIDI               (MIDI music)
 *   💾  Tracker / module   (MOD, S3M, XM, IT …)
 *   🎵  FLAC               (lossless, compressed)
 *   🎧  ALAC               (Apple Lossless)
 *   💿  WAV / AIFF         (uncompressed PCM)
 *   🔊  Everything else    (MP3, OGG, Opus, AAC …)
 */
export function artPlaceholderEmoji(track) {
  const fmt   = track?.format || '';     // raw mutagen format name — see metadata.py FORMAT_NAMES
  const fmtUp = fmt.toUpperCase();

  // SID / PSID
  if (fmtUp === 'SID' || fmtUp === 'PSID') return '\u{1F579}\uFE0F'; // 🕹️

  // MIDI
  if (fmtUp === 'MID' || fmtUp === 'MIDI') return '\u{1F3BC}';       // 🎼

  // Tracker — primary check via mutagen format name (exact, case-sensitive)
  if (TRACKER_FORMAT_NAMES.has(fmt)) return '\u{1F4BE}';              // 💾

  // Tracker — fallback: check file extension
  const ext = ((track?.path || '').split('.').pop() || '').toLowerCase();
  if (TRACKER_EXTS.has(ext)) return '\u{1F4BE}';                      // 💾

  // FLAC — lossless compressed
  if (fmtUp === 'FLAC') return '\u{1F3B5}';                           // 🎵

  // ALAC — Apple Lossless (stored as "ALAC" by metadata.py)
  if (fmtUp === 'ALAC') return '\u{1F3A7}';                           // 🎧

  // WAV / AIFF — uncompressed PCM
  if (fmtUp === 'WAV' || fmtUp === 'WAVE' || fmtUp === 'AIFF') return '\u{1F4BF}'; // 💿

  return '\u{1F50A}'; // 🔊 default (MP3, OGG, Opus, AAC, WavPack …)
}

// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Performance guards for the per-render work the 2.3.0 desktop changes added
// (a regression a listener could notice fails here first).  Budgets are ~10×
// what a 2024 laptop measures, so a loaded CI box doesn't flake; the browser
// before/after numbers (v2.2.0 vs now, 50k tracks, 30k genres) are in the
// handover note.
//   * the Genres view's one-track filter over 30,506 genres (every open and
//     every toggle — no request, no library read);
//   * an edit of a 10,000-entry playlist keeping its selection (remapSelection
//     on every re-read after an edit);
//   * the waveform seek mapping (every pointer move while scrubbing).
// Run: node --test tests/js/desktop_perf_guard.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { performance } from 'node:perf_hooks';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const FE = resolve(dirname(fileURLToPath(import.meta.url)), '../../soniqboom/frontend');
const { genreRows } = await import(resolve(FE, 'js/genre_list.js'));
const { remapSelection, remapIndex } = await import(resolve(FE, 'js/playlist/edit_view.js'));
const { pctAtX } = await import(resolve(FE, 'js/player/wave_seek.js'));

function medianMs(fn, runs = 7) {
  fn();                                    // (warm the JIT)
  const t = [];
  for (let i = 0; i < runs; i++) { const t0 = performance.now(); fn(); t.push(performance.now() - t0); }
  return t.sort((a, b) => a - b)[runs >> 1];
}

test('Genres: filtering 30,506 genres stays far under a frame', () => {
  const genres = [];
  for (let i = 0; i < 15000; i++) genres.push({ genre: `Solo ${i}`, count: 1 });
  for (let i = 0; i < 15506; i++) genres.push({ genre: `Group ${i}`, count: 2 + (i % 5) });
  const ms = medianMs(() => genreRows(genres, false));
  const r = genreRows(genres, false);
  assert.equal(r.rows.length, 15506);
  assert.equal(r.single, 15000);
  assert.ok(ms < 20, `genreRows(30,506) took ${ms.toFixed(2)} ms`);
});

test('a 10,000-entry playlist edit keeps its selection without a noticeable pause', () => {
  const old = Array.from({ length: 10000 }, (_, i) => ({ id: `t${i % 7000}`, subsong: i % 13 === 0 ? 1 : undefined }));
  const fresh = old.filter((_, i) => i !== 5000).map((t) => ({ ...t }));
  const sel = new Set(Array.from({ length: 200 }, (_, k) => k * 37));
  const ms = medianMs(() => { remapSelection(old, sel, fresh); remapIndex(old, 9000, fresh); });
  assert.ok(ms < 50, `remap over 10,000 entries took ${ms.toFixed(2)} ms`);
});

test('the waveform seek mapping is constant time', () => {
  const ms = medianMs(() => { for (let i = 0; i < 100000; i++) pctAtX(i % 800, 100, 600); });
  assert.ok(ms < 50, `100,000 mappings took ${ms.toFixed(2)} ms`);
});

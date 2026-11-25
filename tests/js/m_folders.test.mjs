// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The phone Library's Folders (soniqboom/frontend/js/mobile/folders.js):
//   * a folder's and a music folder's names, never a share's user name or
//     password; an archive's inner folder reads "pack.zip › inner";
//   * the music folder a path lies in (the longest root; a prefix sibling
//     "Music FLAC 2" is not inside "Music FLAC");
//   * the line of folders above the open one, and the Play / Shuffle line;
//   * the filter: a word start (also where letters turn into digits), any
//     part of a name in a script without spaces, case and accents ignored;
//   * the open folders are kept as the root's hash and folder names (never a
//     share's address) and come back from them.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  folderLabel, isArchiveFolder, rootKind, rootLabel, rootOf, within, parentsText,
  totalTime, folderScope, folderMatcher, foldText, childPath, savePath, restorePath,
} from '../../soniqboom/frontend/js/mobile/folders.js';

test('folder and music-folder names', () => {
  assert.equal(folderLabel('pack.zip::inner'), 'pack.zip › inner');
  assert.equal(folderLabel('Hunky Dory'), 'Hunky Dory');
  assert.equal(isArchiveFolder('pack.zip::inner'), true);
  assert.equal(isArchiveFolder('Hunky Dory'), false);
  assert.equal(rootKind({ path: '/Volumes/Music', catalogue: '' }), 'drive');
  assert.equal(rootKind({ path: 'ftp://10.0.0.1/Music FLAC' }), 'share');
  assert.equal(rootKind({ path: 'https://x.org/hvsc', catalogue: 'hvsc' }), 'collection');
  assert.equal(rootLabel({ path: 'https://x.org/hvsc', catalogue: 'hvsc', label: 'HVSC' }, {}), 'HVSC');
  assert.equal(rootLabel({ path: 'ftp://h/Music FLAC' }, { 'ftp://h/Music FLAC': 'FLAC' }), 'FLAC');
  assert.equal(rootLabel({ path: 'ftp://h/Music FLAC' }, {}), 'Music FLAC');
  assert.equal(rootLabel({ path: '/Volumes/Music/' }, {}), 'Music');
  // No user name or password ever shows — also one holding "/" or "@".
  for (const p of ['ftp://bob:s3cret@nas/Music', 'ftp://bob:s3/c@ret@nas', 'smb://bob:pw@nas']) {
    const l = rootLabel({ path: p }, {});
    assert.ok(!/bob|s3|cret|pw/.test(l), `${p} → ${l}`);
  }
});

test('the music folder a folder lies in', () => {
  const roots = [{ path: 'ftp://h/Music FLAC' }, { path: 'ftp://h/Music FLAC 2' }, { path: '/M' }, { path: '/M/Sub' }];
  assert.equal(rootOf('ftp://h/Music FLAC/DAVID BOWIE', roots).path, 'ftp://h/Music FLAC');
  assert.equal(rootOf('ftp://h/Music FLAC 2/X', roots).path, 'ftp://h/Music FLAC 2');
  assert.equal(rootOf('/M/Sub/a', roots).path, '/M/Sub');        // (the longest root)
  assert.equal(rootOf('/M', roots).path, '/M');
  assert.equal(rootOf('/Mx/a', roots), null);
  assert.equal(within('/M/a.zip::inner', '/M'), true);
  assert.equal(within('/Mx', '/M'), false);
});

test('the line of folders above the open one, and the Play / Shuffle line', () => {
  assert.equal(parentsText(['FLAC']), 'FLAC');
  assert.equal(parentsText(['FLAC', 'DAVID BOWIE']), 'FLAC › DAVID BOWIE');
  assert.equal(parentsText(['Modland', 'Protracker', 'Purple Motion', 'coop-Jugi']),
               '… › Purple Motion › coop-Jugi');
  assert.equal(parentsText([]), '');
  assert.equal(totalTime(2503), '41:43');
  assert.equal(totalTime(3725), '1:02:05');
  assert.equal(totalTime(0), '');
  // An album folder: its own tracks.
  assert.deepEqual(folderScope({ direct: 11, folders: 0, seconds: 2503 }),
                   { scope: 'direct', play: 'Play', shuffle: 'Shuffle', text: '11 tracks · 41:43' });
  // A folder of folders: everything under it (the count once it is known).
  assert.deepEqual(folderScope({ direct: 0, folders: 26, deep: null }),
                   { scope: 'deep', play: 'Play all', shuffle: 'Shuffle all', text: '26 folders' });
  assert.equal(folderScope({ direct: 0, folders: 26, deep: 288 }).text, '288 tracks in 26 folders');
  assert.equal(folderScope({ direct: 0, folders: 1, deep: 1 }).text, '1 track in 1 folder');
  assert.equal(folderScope({ direct: 1, folders: 2 }).text, '1 track');     // (Play covers its own track only)
  assert.equal(folderScope({ direct: 1, folders: 2 }).scope, 'direct');
  assert.equal(folderScope({ direct: 80833, folders: 0 }).text, '80,833 tracks');
  assert.equal(folderScope({ direct: 0, folders: 0 }).scope, 'none');
  assert.equal(folderScope({ direct: null, folders: 3 }).scope, 'deep');
});

test('the filter: a word start, case and accents ignored', () => {
  const names = ['Ooo Pure', 'Purgatory', 'Purity', 'Purple Motion', 'Spurt', 'Track41', 'CD10 Exciter',
                 'Ägypten', 'Ａｂｃ', 'Jesper Kyd'];
  const pick = (q) => names.filter(folderMatcher(q));
  assert.deepEqual(pick('pur'), ['Ooo Pure', 'Purgatory', 'Purity', 'Purple Motion']);
  assert.deepEqual(pick('PURPLE  mo'), ['Purple Motion']);
  assert.deepEqual(pick('41'), ['Track41']);                 // letters turn into digits
  assert.deepEqual(pick('10'), ['CD10 Exciter']);
  assert.deepEqual(pick('agy'), ['Ägypten']);
  assert.deepEqual(pick('abc'), ['Ａｂｃ']);                  // full-width letters
  assert.deepEqual(pick('kyd'), ['Jesper Kyd']);
  assert.deepEqual(pick('urt'), []);                         // not inside a word
  assert.equal(pick('').length, names.length);
  assert.equal(pick('   ').length, names.length);
  assert.equal(foldText('Ä Ｂ'), 'a b');
});

test('the filter: any part of a name in a script without spaces', () => {
  const names = ['ラブライブ！スーパースター!!', '2021.08.25 - 「鬼滅の刃」オーケストラコンサート', 'BTS', '마마무 Best'];
  const pick = (q) => names.filter(folderMatcher(q));
  assert.deepEqual(pick('スーパー'), ['ラブライブ！スーパースター!!']);
  assert.deepEqual(pick('鬼滅'), ['2021.08.25 - 「鬼滅の刃」オーケストラコンサート']);
  assert.deepEqual(pick('마무'), ['마마무 Best']);
  assert.deepEqual(pick('bts'), ['BTS']);
  // An archive's inner folder is matched as shown.
  assert.equal(folderMatcher('inner')('pack.zip::inner'), true);
});

test('the open folders are kept as a hash and names, and come back from them', () => {
  const roots = [{ path: 'ftp://bob:pw@h/Music FLAC/', hash: 'aa11', name: 'FLAC' }, { path: '/M', hash: 'bb22', name: 'M' }];
  const stack = [{ path: 'ftp://bob:pw@h/Music FLAC/', name: 'FLAC' },
                 { path: 'ftp://bob:pw@h/Music FLAC/DAVID BOWIE', name: 'DAVID BOWIE' },
                 { path: 'ftp://bob:pw@h/Music FLAC/DAVID BOWIE/pack.zip::inner', name: 'pack.zip::inner' }];
  const kept = savePath(stack, roots);
  assert.deepEqual(kept, { root: 'aa11', names: ['DAVID BOWIE', 'pack.zip::inner'] });
  assert.ok(!JSON.stringify(kept).includes('bob') && !JSON.stringify(kept).includes('pw'));   // no address kept
  assert.deepEqual(restorePath(kept, roots), stack);
  assert.equal(childPath('/M/', 'a'), '/M/a');
  assert.equal(childPath('/M', 'a'), '/M/a');
  assert.deepEqual(restorePath({ root: 'aa11', names: [] }, roots), [stack[0]]);
  assert.deepEqual(restorePath({ root: 'gone', names: [] }, roots), []);                   // a removed root
  assert.deepEqual(restorePath({ root: 'bb22', names: ['a/b'] }, roots), []);              // not a name
  assert.deepEqual(restorePath({ root: 'bb22', names: ['..'] }, roots), []);
  assert.deepEqual(restorePath({ root: 'bb22', names: [''] }, roots), []);
  assert.deepEqual(restorePath('nope', roots), []);
  assert.deepEqual(restorePath({ root: 'bb22', names: 'x' }, roots), []);
  assert.equal(savePath([], roots), null);
  assert.equal(savePath([{ path: '/elsewhere', name: 'x' }], roots), null);
});

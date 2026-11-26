// SPDX-FileCopyrightText: 2026 S.F. Cyris
// SPDX-License-Identifier: AGPL-3.0-or-later

// ── CSDb section of a SID's Track Info (Scene tab) ──────────────────────────
//
// What /api/scene/csdb answers (core/csdb.py) as the section's HTML: the
// tune's CSDb entry (name, author, release note), a screenshot of its
// best-rated release, and the releases that use the tune — name (a link to
// csdb.dk), the groups that released it, year, type, party and placing,
// rating.  It reuses the Scene tab's classes (``ti-scene-block`` …), so
// Modern and Retro hi-fi style it like the rest of the tab
// (css/app.css, css/trackinfo-styles.css).  No answer, or one that is not
// ``found``: no section at all.
//
// Pure (no DOM): trackinfo.js puts the string in place, then wires the
// screenshot's load/error (``.ti-csdb-shot``).

export const CSDB_SHOWN = 8;     // releases listed before "N more on CSDb"

const _esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

const _csdbUrl = (u) => (/^https:\/\/csdb\.dk\/[a-z]+\/\?id=\d+$/.test(String(u || '')) ? String(u) : '');
const _shotUrl = (u) => (/^\/api\/scene\/csdb\/shot\/\d+\.png$/.test(String(u || '')) ? String(u) : '');

/** The CSDb SID id the Scene tab's matched Demozoo production links
 *  (``CsdbMusic`` → ``https://csdb.dk/sid/?id=N``), or null. */
export function csdbHint(sceneInfo) {
  const links = sceneInfo && sceneInfo.release && Array.isArray(sceneInfo.release.links)
    ? sceneInfo.release.links : [];
  for (const l of links) {
    const m = /^https?:\/\/csdb\.dk\/sid\/\?id=(\d{1,7})$/.exec(String((l && l.url) || ''));
    if (m) return Number(m[1]);
  }
  return null;
}

function _rating(r) {
  const n = Number(r);
  return Number.isFinite(n) && n > 0 ? String(Math.round(n * 100) / 100) : '';
}

/** One release's line: groups · year · type · placing at party · rating. */
export function releaseMeta(r) {
  const bits = [];
  if (Array.isArray(r.groups) && r.groups.length) {
    const gr = r.group_rating && _rating(r.group_rating.rating);
    bits.push(r.groups.slice(0, 3).join(', ') + (gr ? ` (group ★ ${gr})` : ''));
  }
  if (r.year) bits.push(String(r.year));
  if (r.type) bits.push(r.type);
  const where = r.event || '';
  if (r.place && (r.compo || where)) {
    bits.push(`#${r.place}${r.compo ? ` ${r.compo}` : ''}${where ? ` at ${where}` : ''}`);
  } else if (where) {
    bits.push(`at ${where}`);
  }
  const rt = _rating(r.rating);
  if (rt) bits.push(`★ ${rt}`);
  return bits;
}

/** The section's HTML for a /api/scene/csdb answer; '' when there is none. */
export function csdbSectionHtml(data) {
  if (!data || data.found !== true) return '';
  const sidUrl = _csdbUrl(data.url);
  const rels = Array.isArray(data.releases) ? data.releases.filter((r) => r && r.name) : [];
  const shot = _shotUrl(data.screenshot);
  const shotRel = rels.find((r) => r.id === data.screenshot_release);
  let h = '<div class="ti-scene-block ti-csdb" aria-label="CSDb">';
  h += '<div class="ti-scene-h">CSDb</div>';
  h += '<div class="ti-csdb-top">';
  if (shot) {
    const alt = shotRel ? `Screenshot of ${shotRel.name}` : 'Release screenshot';
    h += `<img class="ti-csdb-shot" src="${_esc(shot)}" alt="${_esc(alt)}" width="320" height="200" decoding="async">`;
  }
  h += '<div class="ti-csdb-sum">';
  if (data.name) h += `<div class="ti-scene-rel-title">${_esc(data.name)}</div>`;
  const meta = [data.author, data.released].filter(Boolean).map(_esc);
  if (meta.length) h += `<div class="ti-scene-meta">${meta.join(' · ')}</div>`;
  const top = _rating(data.top_rating);
  if (top) {
    h += `<div class="ti-csdb-rating" title="The best rating CSDb's users gave a release that uses this tune">`
      + `★ ${top} <span class="ti-scene-disco-m">best release rating</span></div>`;
  }
  if (shotRel) h += `<div class="ti-scene-disco-m ti-csdb-shot-cap">${_esc(shotRel.name)}</div>`;
  h += '</div></div>';
  if (rels.length) {
    h += `<div class="ti-scene-h ti-csdb-sub">Used in ${rels.length === 1 ? '1 release' : `${rels.length} releases`}</div>`;
    h += '<ul class="ti-scene-disco ti-csdb-rels">';
    for (const r of rels.slice(0, CSDB_SHOWN)) {
      const u = _csdbUrl(r.url);
      const name = _esc(r.name);
      const inner = u ? `<a href="${_esc(u)}" target="_blank" rel="noopener">${name}</a>` : name;
      const m = releaseMeta(r).map(_esc).join(' · ');
      h += `<li>${inner}${m ? ` <span class="ti-scene-disco-m">${m}</span>` : ''}</li>`;
    }
    h += '</ul>';
    if (rels.length > CSDB_SHOWN && sidUrl) {
      h += `<a class="ti-scene-more" href="${_esc(sidUrl)}" target="_blank" rel="noopener">`
        + `${rels.length - CSDB_SHOWN} more on CSDb ▸</a>`;
    }
  }
  if (sidUrl) {
    h += `<div class="ti-scene-chips"><a href="${_esc(sidUrl)}" target="_blank" rel="noopener" class="ti-scene-chip">CSDb</a></div>`;
  }
  h += '</div>';
  return h;
}

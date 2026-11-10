# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Demozoo external links as URLs.

The Demozoo export stores a scener's / production's external link as its
type (``link_class``, e.g. ``BandcampArtist``) and a parameter
(``purplemotion``); Demozoo's site builds the URL from them.  ``SITES`` holds
the site name and URL pattern of every link type Demozoo has (November 2026,
149 types: the pattern's ``%s`` is the parameter); the types whose URL is
built from parts of the parameter are ``_BUILT``.  A link type not known here
is left out (``link`` → None) rather than guessed; so are the types Demozoo
itself no longer shows (``ARCHIVED``).
"""
from __future__ import annotations

import re
from urllib.parse import quote, urlsplit

# link_class: (site name, URL pattern)
SITES: dict[str, tuple[str, str]] = {
    'AmigascneFile': ('amigascne.org', 'http://ftp.amigascne.org/pub/amiga%s'),
    'AmpAuthor': ('AMP', 'https://amp.dascene.net/composer/%s/'),
    'AmpGroup': ('AMP', 'https://amp.dascene.net/composers?filter=%s&type=groupid'),
    'AmpModule': ('AMP', 'https://amp.dascene.net/module/%s'),
    'ArtcityArtist': ('ArtCity', 'http://artcity.bitfellas.org/index.php?a=artist&id=%s'),
    'ArtcityImage': ('ArtCity', 'http://artcity.bitfellas.org/index.php?a=show&id=%s'),
    'AsciiarenaArtist': ('AsciiArena', 'https://asciiarena.se/artist/%s'),
    'AsciiarenaCrew': ('AsciiArena', 'https://asciiarena.se/crew/%s'),
    'AsciiarenaRelease': ('AsciiArena', 'https://asciiarena.se/release/%s'),
    'AtariLegendMenuset': ('Atari Legend', 'https://www.atarilegend.com/menusets/%s'),
    'AtarikiEntry': ('Atariki', 'http://atariki.krap.pl/index.php/%s'),
    'AtarimaniaPage': ('Atarimania', 'http://www.atarimania.com/games/%s'),
    'BBSmatesBBS': ('BBSmates', 'http://www.bbsmates.com/viewbbs.aspx?id=%s'),
    'BandcampArtist': ('Bandcamp', 'https://%s.bandcamp.com/'),
    'BaseUrl': ('WWW', '%s'),
    'BitjamAuthor': ('BitJam', 'http://www.bitfellas.org/e107_plugins/radio/radio.php?search&q=%s&type=author&page=1'),
    'BitjamSong': ('BitJam', 'http://www.bitfellas.org/e107_plugins/radio/radio.php?info&id=%s'),
    'BlueskyAccount': ('Bluesky', 'https://bsky.app/profile/%s'),
    'BreaksAmigaParty': ("Break's Amiga Collection", 'http://arabuusimiehet.com/break/amiga/index.php?mode=party&partyid=%s'),
    'C64chGroup': ('C64.CH', 'https://c64.ch/groups/%s'),
    'C64chParty': ('C64.CH', 'https://c64.ch/parties/%s'),
    'C64chProduction': ('C64.CH', 'https://c64.ch/productions/%s'),
    'C64chScener': ('C64.CH', 'https://c64.ch/sceners/%s'),
    'CablesProd': ('Cables', 'https://cables.gl/p/%s'),
    'CablesUser': ('Cables', 'https://cables.gl/user/%s'),
    'CappedVideo': ('Capped.TV', 'http://capped.tv/%s'),
    'CsdbBBS': ('CSDb', 'https://csdb.dk/bbs/?id=%s'),
    'CsdbEvent': ('CSDb', 'https://csdb.dk/event/?id=%s'),
    'CsdbGroup': ('CSDb', 'https://csdb.dk/group/?id=%s'),
    'CsdbMusic': ('CSDb', 'https://csdb.dk/sid/?id=%s'),
    'CsdbRelease': ('CSDb', 'https://csdb.dk/release/?id=%s'),
    'CsdbScener': ('CSDb', 'https://csdb.dk/scener/?id=%s'),
    'DeepSIDProfile': ('DeepSID', 'https://deepsid.chordian.net/?file=/MUSICIANS/%s'),
    'DeepSIDTrack': ('DeepSID', 'https://deepsid.chordian.net/?file=%s'),
    'Defacto2File': ('Defacto2', 'https://defacto2.net/f/%s'),
    'Defacto2Group': ('Defacto2', 'https://defacto2.net/g/%s'),
    'Defacto2Person': ('Defacto2', 'https://defacto2.net/p/%s'),
    'DemopartyNetParty': ('demoparty.net', 'https://www.demoparty.net/%s'),
    'DemopartyNetPartySeries': ('demoparty.net', 'https://www.demoparty.net/%s'),
    'DemopartyNetUser': ('demoparty.net', 'https://www.demoparty.net/users/%s'),
    'DemosceneTvVideo': ('Demoscene.tv', 'http://demoscene.tv/page.php?id=172&vsmaction=view_prod&id_prod=%s'),
    'DeviantartUser': ('deviantART', 'http://%s.deviantart.com'),
    'DhsVideoDbVideo': ('DHS VideoDB', 'http://dhs.nu/video.php?ID=%s'),
    'DiscogsArtist': ('Discogs', 'https://www.discogs.com/artist/%s'),
    'DiscogsLabel': ('Discogs', 'https://www.discogs.com/label/%s'),
    'DosDemosVideo': ('DosDemos.com', 'https://dosdemos.com/demo/%s'),
    'EventsRetrosceneRelease': ('Retroscene events', 'https://events.retroscene.org/%s'),
    'FacebookPage': ('Facebook', 'https://www.facebook.com/%s'),
    'FujiologyFile': ('Fujiology', 'https://fujiology.org%s'),
    'FujiologyFolder': ('Fujiology', 'https://fujiology.org%s'),
    'GameboyDemospottingAuthor': ('Gameboy Demospotting', 'http://gameboy.modermodemet.se/en/author/%s'),
    'GameboyDemospottingDemo': ('Gameboy Demospotting', 'http://gameboy.modermodemet.se/en/demo/%s'),
    'GithubAccount': ('GitHub', 'https://github.com/%s'),
    'GithubRepo': ('GitHub', 'https://github.com/%s'),
    'GooglePlusEvent': ('Google+', 'https://plus.google.com/u/0/events/%s'),
    'GooglePlusPage': ('Google+', 'https://plus.google.com/%s/'),
    'HallOfLightArtist': ('Hall Of Light', 'http://hol.abime.net/hol_search.php?N_ref_artist=%s'),
    'HallOfLightGame': ('Hall Of Light', 'http://hol.abime.net/%s'),
    'HearthisTrack': ('hearthis.at', 'https://hearthis.at/%s'),
    'HearthisUser': ('hearthis.at', 'https://hearthis.at/%s/'),
    'InstagramAccount': ('Instagram', 'https://www.instagram.com/%s'),
    'InternetArchivePage': ('Internet Archive', 'https://archive.org/details/%s'),
    'KestraBitworldAuthor': ('Kestra BitWorld', 'https://janeway.exotica.org.uk/author.php?id=%s'),
    'KestraBitworldParty': ('Kestra BitWorld', 'https://janeway.exotica.org.uk/party.php?id=%s'),
    'KestraBitworldPartySeries': ('Kestra BitWorld', 'https://janeway.exotica.org.uk/series.php?id=%s'),
    'KestraBitworldRelease': ('Kestra BitWorld', 'https://janeway.exotica.org.uk/release.php?id=%s'),
    'LanyrdEvent': ('Lanyrd', 'http://lanyrd.com/%s/'),
    'LinkedInUser': ('LinkedIn', 'https://www.linkedin.com/in/%s'),
    'LivecodeDemozooEvent': ('Livecode', 'https://livecode.demozoo.org/serie/%s.html'),
    'MastodonAccount': ('Mastodon', '%s'),
    'MobygamesDeveloper': ('MobyGames', 'https://www.mobygames.com/developer/sheet/view/developerId,%s/'),
    'ModarchiveMember': ('ModArchive', 'https://modarchive.org/member.php?%s'),
    'ModarchiveModule': ('ModArchive', 'https://modarchive.org/module.php?%s'),
    'ModlandFile': ('Modland', 'https://ftp.modland.com%s'),
    'NectarineArtist': ('Nectarine', 'https://scenestream.net/demovibes/artist/%s/'),
    'NectarineGroup': ('Nectarine', 'https://scenestream.net/demovibes/group/%s/'),
    'NectarineSong': ('Nectarine', 'https://scenestream.net/demovibes/song/%s/'),
    'PaduaOrgFile': ('padua.org', 'ftp://ftp.padua.org/pub/c64%s'),
    'PartyWikiParty': ('PartyWiki', 'https://party.darklite.org/%s/'),
    'Pico8Cart': ('Pico-8', 'https://www.lexaloffle.com/bbs/?tid=%s'),
    'Pico8User': ('Pico-8', 'https://www.lexaloffle.com/bbs/?uid=%s'),
    'PixeljointArtist': ('Pixeljoint', 'http://pixeljoint.com/p/%s.htm'),
    'PixeljointImage': ('Pixeljoint', 'http://pixeljoint.com/pixelart/%s.htm'),
    'Plus4WorldCompo': ('Plus/4 World', 'http://plus4world.powweb.com/compos/%s'),
    'Plus4WorldGroup': ('Plus/4 World', 'http://plus4world.powweb.com/groups/%s'),
    'Plus4WorldMember': ('Plus/4 World', 'http://plus4world.powweb.com/members/%s'),
    'Plus4WorldProduction': ('Plus/4 World', 'http://plus4world.powweb.com/software/%s'),
    'Plus4WorldProfile': ('Plus/4 World', 'http://plus4world.powweb.com/profile/%s'),
    'PolyworkUser': ('Polywork', 'https://www.polywork.com/%s'),
    'PouetBBS': ('Pouët', 'https://www.pouet.net/boards.php?which=%s'),
    'PouetGroup': ('Pouët', 'https://www.pouet.net/groups.php?which=%s'),
    'PouetPartySeries': ('Pouët', 'https://www.pouet.net/party.php?which=%s'),
    'PouetProduction': ('Pouët', 'https://www.pouet.net/prod.php?which=%s'),
    'PushnpopGroup': ("Push'n'Pop", 'http://pushnpop.net/group-%s.html'),
    'PushnpopParty': ("Push'n'Pop", 'http://pushnpop.net/parties-%s.html'),
    'PushnpopProduction': ("Push'n'Pop", 'http://pushnpop.net/prod-%s.html'),
    'PushnpopProfile': ("Push'n'Pop", 'http://pushnpop.net/profile-%s.html'),
    'SceneidAccount': ('Pouët', 'https://www.pouet.net/user.php?who=%s'),
    'ScenesatAct': ('SceneSat', 'https://scenesat.com/act/%s'),
    'ScenesatTrack': ('SceneSat', 'https://scenesat.com/track/%s'),
    'ShadertoyShader': ('Shadertoy', 'https://www.shadertoy.com/view/%s'),
    'ShadertoyUser': ('Shadertoy', 'https://www.shadertoy.com/user/%s'),
    'SixteenColorsArtist': ('16colors', 'https://16colo.rs/artist/%s'),
    'SixteenColorsGroup': ('16colors', 'https://16colo.rs/group/%s'),
    'SixteenColorsPack': ('16colors', 'https://16colo.rs/pack/%s/'),
    'SlengpungParty': ('Slengpung', 'http://www.slengpung.com/?eventid=%s'),
    'SlengpungUser': ('Slengpung', 'http://www.slengpung.com/?userid=%s'),
    'SoundcloudTrack': ('SoundCloud', 'https://soundcloud.com/%s'),
    'SoundcloudUser': ('SoundCloud', 'https://soundcloud.com/%s/'),
    'SpeccyPlAuthor': ('speccy.pl', 'http://speccy.pl/archive/author.php?id=%s'),
    'SpeccyPlProduction': ('speccy.pl', 'http://speccy.pl/archive/prod.php?id=%s'),
    'SpeccyWikiPage': ('SpeccyWiki', 'http://speccy.info/%s'),
    'SpectrumComputingRelease': ('Spectrum Computing', 'https://spectrumcomputing.co.uk/entry/%s'),
    'SpotifyArtist': ('Spotify', 'https://play.spotify.com/artist/%s'),
    'SpotifyTrack': ('Spotify', 'https://play.spotify.com/track/%s'),
    'StonishDisk': ('Stonish', 'http://stonish.net/%s'),
    'TelnetBBSGuideBBS': ('Telnet BBS Guide', 'https://www.telnetbbsguide.com/bbs/%s/'),
    'TelnetLink': ('Telnet', 'telnet://%s'),
    'Tic80Cart': ('TIC-80', 'https://tic80.com/play?cart=%s'),
    'Tic80Dev': ('TIC-80', 'https://tic80.com/dev?id=%s'),
    'TikTokUser': ('TikTok', 'https://www.tiktok.com/@%s'),
    'TwitchChannel': ('Twitch', 'https://twitch.tv/%s'),
    'TwitterAccount': ('Twitter', 'https://twitter.com/%s'),
    'UntergrundFile': ('untergrund.net', 'https://ftp.untergrund.net%s'),
    'VimeoUser': ('Vimeo', 'https://vimeo.com/%s'),
    'VimeoVideo': ('Vimeo', 'https://vimeo.com/%s'),
    'WaybackMachinePage': ('Wayback Machine', 'https://web.archive.org/web/%s'),
    'WikipediaPage': ('Wikipedia', '%s'),
    'YoutubeChannel': ('YouTube', 'https://www.youtube.com/channel/%s'),
    'YoutubeUser': ('YouTube', 'https://www.youtube.com/@%s'),
    'ZXPressIssue': ('ZXPress', 'https://zxpress.ru/issue.php?id=%s'),
    'ZxArtAuthor': ('ZXArt', 'https://zxart.ee/eng/authors/%s/'),
    'ZxArtGroup': ('ZXArt', 'https://zxart.ee/eng/groups/%s/'),
    'ZxArtMusic': ('ZXArt', 'https://zxart.ee/eng/authors/%s/'),
    'ZxArtParty': ('ZXArt', 'https://zxart.ee/eng/parties/%s/'),
    'ZxArtPicture': ('ZXArt', 'https://zxart.ee/eng/authors/%s/'),
    'ZxArtProduction': ('ZXArt', 'https://zxart.ee/eng/authors/%s/'),
    'ZxTunesArtist': ('ZXTunes', 'http://zxtunes.com/author.php?id=%s&ln=eng'),
    'ZxdemoAuthor': ('ZXdemo', 'https://zxdemo.org/author.php?id=%s'),
    'ZxdemoItem': ('ZXdemo', 'https://zxdemo.org/item.php?id=%s'),
    'ZxdemoParty': ('ZXdemo', 'https://zxdemo.org/party.php?id=%s'),
}


def _latin1_path(param: str) -> str:
    """A scene.org path, percent-encoded as Latin-1 like scene.org's own
    links (UTF-8 for a path Latin-1 can't hold)."""
    try:
        return quote(param.encode("iso-8859-1"))
    except UnicodeEncodeError:
        return quote(param.encode("utf-8"))


def _pair(param: str) -> tuple[str, str] | None:
    a, sep, b = param.partition("/")
    return (a, b) if sep and a and b and "/" not in b else None


def _youtube(param: str) -> str:
    vid, sep, t = param.partition("/")
    return f"https://www.youtube.com/watch?v={vid}&t={t}" if sep else \
        f"https://www.youtube.com/watch?v={vid}"


def _github_dir(param: str) -> str | None:
    parts = param.split("/")
    if len(parts) < 3:
        return None
    return f"http://github.com/{parts[0]}/{parts[1]}/tree/{'/'.join(parts[2:])}"


def _bandcamp_track(param: str) -> str | None:
    p = _pair(param)                      # "<artist>/<track>" (the artist: a subdomain)
    return f"https://{p[0]}.bandcamp.com/track/{p[1]}" \
        if p and _LABEL_RE.fullmatch(p[0].lower()) else None


def _dop(param: str) -> str | None:
    p = _pair(param)
    return f"https://diskmag.conspiracy.hu/magazine/{p[0]}/#/edition={p[1]}" if p else None


def _discogs_release(param: str) -> str | None:
    p = _pair(param)                      # "<id>/<slug>"
    return f"http://www.discogs.com/{p[1]}/release/{p[0]}" if p else None


def _pouet_party(param: str) -> str | None:
    p = _pair(param)                      # "<id>/<year>"
    return f"https://www.pouet.net/party.php?which={p[0]}&when={p[1]}" if p else None


# The types Demozoo keeps but doesn't show (dead or archived sites).
ARCHIVED = frozenset({
    "ZxdemoAuthor", "ZxdemoItem", "ZxdemoParty", "PushnpopProduction", "PushnpopParty",
    "PushnpopGroup", "PushnpopProfile", "DemosceneTvVideo", "StonishDisk", "LanyrdEvent",
    "GooglePlusPage", "GooglePlusEvent", "CappedVideo",
})


def sort_key(link_class: str) -> str:
    """The order Demozoo lists links in: the plain web address first, then by
    type."""
    return "0000" if link_class == "BaseUrl" else link_class


# link_class: (site name, builder) — the types whose URL is built from parts
_BUILT = {
    "SceneOrgFile": ("scene.org", lambda p: "https://files.scene.org/view" + _latin1_path(p)),
    "SceneOrgFolder": ("scene.org", lambda p: "https://files.scene.org/browse" + _latin1_path(p)),
    "YoutubeVideo": ("YouTube", _youtube),
    "GithubDirectory": ("GitHub", _github_dir),
    "BandcampTrack": ("Bandcamp", _bandcamp_track),
    "DOPEdition": ("DOP", _dop),
    "DiscogsRelease": ("Discogs", _discogs_release),
    "PouetParty": ("Pouët", _pouet_party),
}


_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")


def _web(url: str) -> bool:
    """An http(s) address with a host (no ``javascript:``, ``data:``, a
    scheme-less ``//host`` or a ``telnet:`` link)."""
    try:
        u = urlsplit(url)
        return u.scheme.lower() in ("http", "https") and bool(u.hostname)
    except ValueError:
        return False


def _on_site(fmt: str, url: str) -> bool:
    """The URL made from pattern ``fmt`` stays on the pattern's site: the
    parameter can't move it to another host ("evil.com/#" as a Bandcamp
    name, "@evil.com" after a fixed host).  A pattern with the parameter in
    the host (``https://%s.bandcamp.com/``) takes one plain name there."""
    try:
        got = urlsplit(url).hostname or ""
        want = urlsplit(fmt.replace("%s", "", 1)).hostname or ""
    except ValueError:
        return False
    if want.startswith("."):                            # (the parameter is a subdomain)
        return got.endswith(want) and bool(_LABEL_RE.fullmatch(got[:-len(want)]))
    return got == want


def link(link_class: str | None, param: str | None) -> dict | None:
    """``{"url", "label", "class"}`` for one Demozoo external link, or None
    for an unknown type, an empty or malformed parameter, a parameter that
    would lead off the type's site, or an address that isn't http(s).
    ``label`` is the site's name — a plain web address (``BaseUrl``) is
    labelled by its host."""
    if not link_class or not param:
        return None
    built = _BUILT.get(link_class)
    if built is not None:
        site, make = built
        try:
            url = make(param)
        except Exception:                               # noqa: BLE001 — a malformed parameter
            url = None
    else:
        entry = SITES.get(link_class)
        if entry is None:
            return None
        site, fmt = entry
        url = fmt % param
        if url and fmt != "%s" and not _on_site(fmt, url):
            return None
    if not url or not _web(url):
        return None
    if link_class == "BaseUrl":
        try:
            host = (urlsplit(url).hostname or "").removeprefix("www.")
        except ValueError:
            host = ""
        site = host or "Website"
    return {"url": url, "label": site, "class": link_class}

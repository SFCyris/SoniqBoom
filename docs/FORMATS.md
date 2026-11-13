# The formats SoniqBoom plays — origins & claims to fame

Every format family SoniqBoom renders, where it came from, and why it
matters.  Retro formats aren't just containers — each one is a piece of
computing history with its own sound, scene, and stories.  The sections at
the end cover how long each kind of tune plays, which files offer a tune
picker, the meters each format gets, and the engines an install has.

The complete list of formats, file names and players: [SUPPORTED-FORMATS.md](SUPPORTED-FORMATS.md).

---

## Commodore 64

| Format | Origin | Claim to fame | Notable |
|---|---|---|---|
| **SID** (`.sid`, `.psid`, `.rsid`) | Commodore 64, 1982 — named for its sound chip, the MOS 6581/8580 *Sound Interface Device* | The most collected chiptune format on Earth: the High Voltage SID Collection (HVSC) curates 50,000+ tunes | Three voices + one filter, yet composers like Rob Hubbard, Martin Galway and Chris Hülsbeck coaxed whole orchestras out of it. The 6581 vs 8580 chip revisions sound audibly different — endless scene debate. RSID variants are recognised by content. Rendered by **sidplayfp**; a tune written for two or three SID chips plays in mono, and Track Info shows the chip model a tune names. |
| **GoatTracker** (`.sng`) | Commodore 64 music tracker by Lasse Öörni (Cadaver); GoatTracker 2 from 2002 | A tracker for writing SID music on a PC, used for many C64 game and demo soundtracks | Converted to a SID tune by GoatTracker's own packer (`gt2reloc`) and played by sidplayfp with the **SID fidelity** settings, in stereo. The song's first subtune plays. GoatTracker 1.x songs play too. Title, author and the copyright text come from the song. The song doesn't store GoatTracker's speed multiplier, so a song written for a multiplied speed plays at the normal speed. A song with no instruments or notes says so instead of playing. An Amiga `.sng` (ZoundMonitor, Richard Joseph) is told apart by its content and plays through UADE. |
| **C64 Sidplayer** (`.mus`, with a `.str` for stereo) | Compute!'s Sidplayer by Craig Chamberlain, Commodore 64, 1980s | Thousands of songs typed in from magazines and books, many with lyrics and credits | Played by sidplayfp with the **SID fidelity** settings. A Stereo Sidplayer song plays its second SID from the `.str` file of the same name beside it. The song's credit lines become the comment. It plays to its end (the library shows its length after the first play). A stereo song inside an archive or on an FTP / WebDAV share plays its first SID only. An Amiga UFO `.mus` is told apart by its content and plays through UADE. |

Under gear → **Renderers → HVSC SID database**, choose **Download from the
internet** (the 6581.nl HVSC mirror, checked weekly) or **Local DOCUMENTS
folder** with HVSC's `DOCUMENTS` folder, and press **Save & reload**. Every SID
HVSC knows then gets its Songlengths length, matched by checksum wherever the
file is, and its STIL commentary. With **Local DOCUMENTS folder** and no folder
set, a scan of an HVSC copy finds it by itself; to turn that off, set `"hvsc_autodetect": false`
in the `renderers` section of SoniqBoom.conf and restart. After updating the
HVSC copy, **Re-extract SID metadata** in the same place (or **Metadata →
HVSC (SID) → Rescan SID lengths**) applies it to the SIDs already in the
library. Under gear → **Renderers → SID fidelity**, pick the chip model
(6581 / 8580, optionally forced over the tune's own choice), filter emulation,
the filter curve and 8580 digiboost.

## Amiga trackers

| Format | Origin | Claim to fame | Notable |
|---|---|---|---|
| **ProTracker MOD** (`.mod`) | Amiga, 1987 (Ultimate Soundtracker → NoiseTracker → ProTracker) | *The* format that invented tracker music: 4 channels of 8-bit samples, patterns, and the entire demoscene workflow | The `M.K.` magic bytes are Mahoney & Kaktus' initials. Karsten Obarski's original editor sold poorly; the pirated clones conquered the world. |
| **ChipTracker** (`.chp`, `.chip`, or `.mod`) and **ProTracker IFF** (`.ptm`) | Amiga | A ProTracker relative with short looped waveforms; ProTracker 3.6's IFF save format | Both play through libopenmpt. A `.ptm` that is a PolyTracker module (PC) plays through libopenmpt too. |
| **OctaMED / MED** (`.med`, `.oct`, `.md0`–`.md3`, `.mmd3`, `.omed`, `.ocss`) | Amiga, 1989, Teijo Kinnunen | Doubled the Amiga's 4 hardware channels to 8 in software | Big in Finland; used for countless Amiga game soundtracks. OctaMED (MMD0–MMD3) modules play through libopenmpt, the older MED 2–4 format through ZXTune, and Modland-style `.mmd0`–`.mmd2` names through UADE. The song name stored in an OctaMED module becomes the track title. |
| **Oktalyzer** (`.okt`) | Amiga, 1988 | Early 8-channel pioneer, before OctaMED made it mainstream | |
| **Face The Music** (`.ftm`; `FTM.title` inside archives) | Amiga | An 8-channel Amiga tracker | Plays through libopenmpt 0.8 or newer, which the Docker image includes; on an install with an older libopenmpt, such files aren't listed. FamiTracker's `.ftm` modules (NES) are told apart by their content — see Console chiptunes. |
| **SoundFX** (`.sfx`, `.sfx13`) | Amiga, late 1980s | An early Amiga sample tracker | `.sfx` plays through libopenmpt, `.sfx13` through UADE. |
| **DigiBooster Pro** (`.dbm`) | Amiga, late 1990s | The Amiga's answer to FastTracker 2 at the platform's twilight | |
| **AHX** (`.ahx`, `.thx`) | Amiga, 1996 (Abyss' Highest eXperience) | Pure synthesis, no samples: a whole tune in a few kilobytes that *sounds* like a C64 on an Amiga | Beloved for 4k/64k demo intros where every byte counts. Played through UADE; the bundled HivelyTracker replayer measures where the song ends, so a tune stops there instead of looping on. Where that replayer can't be built (see *Engines this install has* below), an AHX tune plays to uade's limit of about 8½ minutes. |
| **HivelyTracker** (`.hvl`) | Amiga, 2006 | AHX's community successor — more channels, stereo | SoniqBoom bundles the reference replayer (`hvl2wav`). |

## Amiga exotics (the uade family — ~175 player engines)

These are custom, per-composer or per-game music systems, playable thanks
to **UADE**, which runs the *original Amiga player code* inside a 68k
emulator.  Files are recognised by Amiga prefix naming (`mdat.song`,
`fc13.intro`) as well as by suffix (`song.fc13`), using the player list of
the installed UADE; modules packed with XPK-SQSH or PowerPacker are unpacked
before they play, and one packed with another XPK method, or with damaged
packed data, says so when played.  Companion files a player needs come along
with the module, whether it is on a local disk, a network share, an online
collection or inside an archive: TFMX `smpl.*` sample halves and the other
`SMP.*` sample banks, the replay routine a YM-ST module names, a Sonix
module's `Instruments/` folder, ZoundMonitor's `Samples/` folder, and the
instrument file some Jochen Hippel COSO songs share.  Companion files aren't
listed as tracks of their own.  When an archived Sonix module lacks some
instruments, they play as silence and the track is marked **partial**
(**corrupt** when it has none of them); a Sonix song on disk whose
`Instruments/` folder lacks some is refused when played, naming them.  A
few damaged rips, known by their checksum, are repaired as they play; one
that still plays only in part is marked **partial**, with the reason in
Track Info.  A Synth Dream tune whose sample bank is missing is refused with
that reason and marked **can't play**.
Highlights:

| Format | Origin | Claim to fame | Notable |
|---|---|---|---|
| **TFMX** (`mdat.*` + `smpl.*`, or `.tfx` + `.sam`) | Chris Hülsbeck, 1987 | The custom engine behind *Turrican* — some of the most celebrated game music ever written | Two-file format: music data + samples must travel together. SoniqBoom keeps the pair intact automatically, even inside archives. A song saved as `X.tfx` plays with its samples `X.sam` beside it. |
| **Future Composer** (`.fc`, `.fc13`, `.fc14`, `.smod`) | Amiga, 1988–89 | The gateway editor for hundreds of scene musicians — simpler than trackers, everywhere in cracktros | |
| **SidMon 1/2** (`.sid` *(Amiga!)*, `.sid1`, `.sid2`, `.sd1`, `.sd2`) | Amiga, 1988 | Built to make an Amiga sound like a C64 | The `.sid` name collides with C64 SID files — SoniqBoom tells them apart by content, not name. |
| **David Whittaker** (`.dw`) | The composer's own engine, 1980s | Whittaker scored 100+ games (*Shadow of the Beast*, *Xenon 2* ports…) using his private format | One of several "signature formats" that exist only because one prolific composer used them. |
| **Rob Hubbard** (`.rh`) | The composer's own engine | The C64 legend's Amiga-era system (*Monty on the Run*'s composer) | |
| **Jochen Hippel** (`.hip`, `.hipc`, `.hst`, `.soc`, `.ymst`) | The composer's own engines, Amiga + Atari ST | Hippel converted *Turrican*'s music for the Atari ST and the *Dragonflight* soundtrack | The COSO variant (`.hipc`) is his compressed format; `.hst` is the Atari ST flavour. A `.soc` module plays with UADE's Hippel ST player when it is an Atari ST COSO module, otherwise with the Amiga COSO player; a YM-ST module (`.ymst`) plays with the replay routine its folder holds. A COSO song that keeps its instruments in a file shared with other songs plays with that file; when that file isn't available yet, playback says so. |
| **Delta Music 1/2** (`.dm`, `.dm2`) | Amiga, 1989–90 | Distinctive synth-heavy sound, popular in Danish/German demos | A Delta Music 2 song plays until all four voices have started over; SoniqBoom works that length out from the song itself. |
| **SoundMon / BP SoundMon** (`.bp`) | Brian Postma, Amiga | Synthesis + samples hybrid, a demoscene staple | |
| **Musicline Editor** (`.ml`) | Amiga, 1990s | A tracker with synthesis instruments and up to eight channels | uade plays it. A tune uade renders only as silence — or can't render at all — plays through mlrender (musicline_playback, a C++ port of the Musicline Editor 1.16 replayer): to its end, or until it loops and fades out, or until it falls silent, at most 10 minutes. |
| **JamCracker, Sonic Arranger, Fred, Art of Noise, Ben Daglish, Dave Lowe, …** | Various Amiga eras | Each a small scene or a single composer's toolkit | UADE ships ~175 player engines; SoniqBoom routes every one it detects. |
| **Forgotten Worlds** (`.fw`) | ARC Developments' replayer for the Amiga game | The music of the *Forgotten Worlds* Amiga game | Played with a corrected copy of UADE's Forgotten Worlds player, which SoniqBoom makes from the installed one. |
| **SUNtronic** (`.sun`) | Felix Schmidt's replayer, Amiga | Cracktro and intro music | Modules of the later, position-independent replayer, which UADE's SUN-Tronic player refuses, play with SoniqBoom's own SUNtronic2 player. |
| **ProWizard-packed MODs** (~110 packer variants) | Amiga cracking scene | Crackers "packed" ProTracker modules into bespoke formats to save disk space and obscure ripping | uade unpacks them on the fly — also under the names Channel Player (`.ch1`–`.ch3`, `.chn`), Module Protector (`.mp`), Wanton Packer (`.wnp`), StarTrekker Packer (`.stp`), Fuzzac (`.fuzzac`), Promizer 1.0 (`.pm10`) and Kefrens Sound Machine (`.ksm`, told apart from Ken Silverman's AdLib `.ksm` by its content) give them. Titanics Player (`.tip`), STIM (`.sti`), The Dark Demon (`.tdd`) and Eureka Packer (`.eureka`) modules play through libxmp (`xmprender`). Ben Replay (`.ben`), Binary Packer (`.bnr`), Devils Replay (`.dev`), GnoiPacker (`.gnoi`), Gnu Protected Module (`.gpmo`), GnuPlayer (`.gnu`), HCD Protector (`.hcd`), Newtron Packer (`.nw1`, `.nw2`), Perfect Song (`.pf1`, `.pf2`), Module-Patterncompressor (`.pmd3`), NoiseTracker Compressed (`.ntc`) and The Player 2.2A / 3.0A (`.p22a`, `.p30a`) modules are converted back to ProTracker modules by SoniqBoom and played through `xmprender`. |

## Atari ST

| Format | Origin | Claim to fame | Notable |
|---|---|---|---|
| **SNDH** (`.sndh`) | Atari ST community archive format, 1990s | One file = music data + the original ST replay code + metadata; the SNDH Archive preserves thousands | Driven by the YM2149 square-wave chip — the ST's whole "sound" in three channels. Rendered by **psgplay**, a modern cycle-accurate 68000+YM emulator. |
| **YM** (`.ym`) | Register-dump format by Arnaud Carré (Leonard/Oxygene) | A literal recording of what the YM2149 chip's registers did, frame by frame — perfect playback forever | Usually LHA-compressed inside. SoniqBoom bundles Carré's own **ST-Sound** engine — the format author's reference player. A `.ym` that isn't a YM register dump (another Atari format, or a damaged LHA wrapper) is marked **corrupt** in the library and isn't played. YM4 files (raw or LHA-compressed) are marked the same way — Track Info says *YM4 file — the YM player plays YM2, YM3, YM5, YM6, MIX and YMT files*; YM4 files already in the library get the mark the first time they are played. |
| **SC68** (`.sc68`) | Atari ST/Amiga archival container | Like SNDH but cross-platform, with embedded durations | Rendered by the **sc68** player. |

## Console chiptunes (rips of real game hardware music)

Rendered by **libgme** (Game Music Emu), except the VGM and KSS files noted below.

| Format | Origin | Claim to fame | Notable |
|---|---|---|---|
| **NSF / NSFe** (`.nsf`, `.nsfe`) | NES / Famicom, 1983 hardware | The actual 6502 music code from NES cartridges, replayed through an emulated APU | One file usually holds a game's *entire* soundtrack as subsongs. |
| **SPC** (`.spc`) | Super Nintendo, 1990 | A frozen RAM snapshot of the SNES's Sony-designed audio co-processor | The SPC700 chip was designed by Ken Kutaragi — the man who then built the PlayStation. |
| **GBS** (`.gbs`) | Game Boy, 1989 | Four voices of the most recognisable handheld sound ever made | |
| **VGM / VGZ** (`.vgm`, `.vgz`) | Sega Master System / Genesis + arcade, format from 2005 | Chip-register logging across dozens of sound chips; the vgmrips community archives thousands of sets | Yamaha YM2612 FM = the Genesis "growl". libgme plays the Sega PSG (SN76489) and YM2612 logs; a log that uses any other chip — YM2413, YM2151, the NES APU, the AY-3-8910 and the rest — is rendered by **ZXTune**. |
| **AY** (`.ay`) | ZX Spectrum / Amstrad CPC | The Eastern-European demoscene's chip of choice (AY-3-8910) | |
| **KSS, SAP, GYM, HES** | MSX, Atari 8-bit, Genesis, PC Engine | Each platform's native music-rip format | Packed GYM files (GYMX) are unpacked automatically. KSS rips play every sound chip their music uses — PSG, SCC, MSX-MUSIC and MSX-AUDIO, and the SN76489 for a Sega Master System / Game Gear rip — through kssrender (libkss); where kssrender isn't installed, libgme plays their PSG and SCC voices only. |
| **FamiTracker** (`.ftm`) | NES / Famicom music tracker, from 2005 | The NES chiptune scene's tracker, with the Famicom's expansion chips (VRC6, VRC7, FDS, MMC5, Namco 163, Sunsoft 5B) | Rendered by **Furnace**'s headless player, which imports the module: its first track plays to its end, its loop once more. Title, author and the copyright text come from the module; the library shows the length once the module has been played. A Face The Music `.ftm` (Amiga) is told apart by its content and plays through libopenmpt. |
| **DefleMask** (`.dmf`) | Multi-system chiptune tracker by Delek | Songs for the sound chips of the Sega Genesis, Master System, Game Boy, PC Engine, NES, Commodore 64 and other systems | Rendered by **Furnace**'s headless player, which imports the module: it plays to its end, its loop once more. Title and author come from the module, and the system it was written for is shown as the comment; the library shows the length once the module has been played. An X-Tracker, DSMI Compact or Software Visions `.dmf` is told apart by its content and plays through libopenmpt. |
| **MGSDRV** (`.mgs`) | MSX music driver by Ain., from 1991 | The MML driver many MSX games and scene songs were written for, playing the PSG, MSX-MUSIC (FM) and SCC | Rendered by **kssrender** with the original MGSDRV driver (MGSDRV © Ain./Gigamix). The title comes from the song's text. MuSICA (`.bgm`), MoonBlaster (`.mbm`) and OPLLDRV (`.opx`) songs aren't listed. |

## PSF family (later console rips)

One clever container, many consoles: a **PSF** file holds the game's
original music *code*, executed inside a tiny emulated slice of the
console.  Invented by Neill Corlett in 2003.  `mini` variants share a
common library file per game — SoniqBoom keeps those pairs together, and a
rip whose library file (its `_lib` tag) is missing says which file it needs.
Rendered by **ZXTune** (zxtune123) with the reference emulation cores;
Sega Dreamcast and Naomi rips (`.dsf`, `.minidsf`) play through **dsfrender**
(Highly Theoretical's Dreamcast sound core, with the 8 MB of sound RAM a
Naomi board has) when it is installed; a rip dsfrender refuses or renders
no sound from plays through ZXTune instead, when ZXTune is installed.  A
tagged rip plays for its `length` tag and then fades out over its `fade`
tag.

| Format | Console | Notable |
|---|---|---|
| **PSF / PSF2** (`.psf`, `.minipsf`, `.psf2`, `.minipsf2`) | PlayStation 1 / 2 | The original — *Final Fantasy* and *Castlevania: SotN* rips made it famous. A `.psf` file without the PSF header is an Amiga SoundFactory module and plays through UADE. |
| **USF** (`.usf`, `.miniusf`) | Nintendo 64 | Runs a sliced-down N64 emulation per song |
| **GSF** (`.gsf`, `.minigsf`) | Game Boy Advance | |
| **2SF / NCSF** (`.2sf`, `.mini2sf`, `.ncsf`, `.minincsf`) | Nintendo DS | NCSF is the newer, leaner sequencer-level rip |
| **SSF / DSF** (`.ssf`, `.minissf`, `.dsf`, `.minidsf`) | Sega Saturn / Dreamcast, Naomi arcade | Dreamcast's `.dsf` shares its extension with Sony's DSD audio files — SoniqBoom distinguishes them by content. DSF rips play through dsfrender when it is installed (Naomi sets such as *F355 Challenge*, whose libraries fill more than the Dreamcast's 2 MB of sound RAM, play only there). |

## FM dumps and synth songs (ZXTune, mdxrender, sunvoxrender)

| Format | Origin | Claim to fame | Notable |
|---|---|---|---|
| **S98** (`.s98`) | NEC PC-98 / PC-88, Sharp X68000 and other Japanese computers | A log of what the machine's Yamaha FM and PSG chips (OPN, OPNA, OPM, OPLL, SSG …) were told to play — the usual rip format for PC-98 game music | Rendered by **ZXTune**. Title, artist, game and year come from the file's tags (UTF-8 or Shift-JIS); a file of an older S98 version carries only a title. |
| **MDX** (`.mdx`, with its `.pdx`) | Sharp X68000, from 1988 | The MXDRV driver's song format — X68000 game soundtracks, arcade conversions and a large doujin scene, for the machine's Yamaha YM2151 FM chip and its ADPCM samples | Rendered by **mdxrender**, the original MXDRV driver (portable_mdx) with the X68Sound chip emulation. A song that uses samples names its PDX sample file, which must sit beside it (any letter case); a song whose PDX file is missing says which file it needs. On a network share or in an online collection, only that PDX file is downloaded with the song. The title comes from the song's header. |
| **Farbrausch V2** (`.v2m`) | Windows demoscene, 2000s | The software synthesizer farbrausch wrote for its 64k intros, used in hundreds of demoscene productions | Rendered by **ZXTune**. The file has no tags, so the file name is the title. |
| **Sawteeth** (`.st`) | Software-synthesizer tracker by Jonas Norberg, around 2000 | Songs built entirely from its own synthesized waveforms, filters and envelopes — no samples | Rendered by **sawteethrender**, NostalgicPlayer's Sawteeth player ported to C. The song plays to its end; title and author come from the song, and its length is measured when the file is scanned. A `.st` file that is neither a Sawteeth song nor an Amiga module named by its player's prefix (`jam.title.st`) isn't listed. |
| **SunVox** (`.sunvox`) | Modular synthesizer and tracker by Alexander Zolotov (NightRadio), from 2008 | Synthesizer modules wired together and played from patterns, on desktops and phones | Rendered by **sunvoxrender** with the SunVox Library. The project plays once, to its end; its name becomes the title. Powered by SunVox (modular synth & tracker) Copyright (c) 2008 - 2026, Alexander Zolotov <nightradio@gmail.com>, WarmPlace.ru. |

## PC DOS-era FM synthesis (AdLib / OPL)

The sound of DOS gaming: Yamaha OPL2/OPL3 FM chips, no samples at all.

| Format | Origin | Claim to fame | Notable |
|---|---|---|---|
| **IMF** (`.imf`) | id Software / Apogee | *Wolfenstein 3D*, *Commander Keen*, *Duke Nukem II* | The extension collides with Imago Orpheus tracker modules — content decides. |
| **ROL** (`.rol`) | AdLib Inc.'s own Visual Composer | The "official" AdLib format | Needs AdLib's instrument bank, `standard.bnk` — found automatically (see below). |
| **CMF, D00, RAD, HSC, SCI, LAA, DRO, RIX, A2M, ADL, BAM, KSM, AMD** | Creative Labs, EdLib, Reality, Sierra, LucasArts, DOSBox, AMUSIC… | Every studio had its own FM format | `.dro` is DOSBox's raw OPL log — a perfect recording of whatever the emulated card played (older DRO v1 captures play too). SoniqBoom renders the whole family, id's IMF included, through AdPlug (`adplay`) with the cycle-accurate Nuked OPL3 core. A Westwood `.adl` whose first song is empty starts at its first audible one. |

ROL tunes need AdLib's instrument bank (`standard.bnk`), Sierra `.sci` tunes
their `*patch.003` file and Ken Silverman's `.ksm` tunes `insts.dat`. A bank
beside the tune or in the same archive is used automatically; for a tune
inside an archive or on a network share, the folders up to four levels above
are searched too. Without its bank, a tune says so when played.

## Acorn and Atari trackers (libxmp, libopenmpt)

| Format | Origin | Claim to fame | Notable |
|---|---|---|---|
| **Archimedes Tracker** (`.musx`), **Coconizer** (`.coco`) | Acorn Archimedes, late 1980s | The Archimedes' own tracker scene | Played through libxmp (`xmprender`). |
| **Digital Symphony** (`.dsym`) | Acorn Archimedes, 1990s | An 8-channel Archimedes tracker | Played through libopenmpt. |
| **Digital Tracker** (`.dtm`), **Megatracker** (`.mgt`) | Atari Falcon, 1990s | Multi-channel trackers of the Falcon scene | Digital Tracker through libopenmpt, Megatracker through libxmp (`xmprender`). |

## PC trackers

| Format | Origin | Claim to fame | Notable |
|---|---|---|---|
| **ScreamTracker 3** (`.s3m`) | Future Crew, 1994 | The PC demoscene's breakout format — *Second Reality*'s creators | |
| **FastTracker 2** (`.xm`) | Triton, 1994 | *The* 90s tracker: instruments, envelopes, 32 channels | Half the mod archive is XM. |
| **Impulse Tracker** (`.it`) | Jeffrey Lim, 1995 | The technical peak of DOS trackers — NNAs, filters, stereo samples | Its lineage lives on in OpenMPT/Schism today. |
| **669, MTM, ULT, FAR, STM, GDM, DSM, AMF, WOW** | Various 90s editors | Each a chapter of tracker evolution | Rendered via libopenmpt, the modern reference engine — as are S3M, XM, IT, ProTracker MOD, Oktalyzer and DigiBooster Pro. Imago Orpheus `.imf` modules are told apart from id Software's AdLib IMF by content. An early Digital Sound Interface Kit `.dsm` (the `DSM\x10` variant) plays through `milkyrender` (MilkyPlay, MilkyTracker's playback library) to the song's natural end; without `milkyrender`, playback says no installed player can read it. The later RIFF `.dsm` plays through libopenmpt. |
| **PolyTracker, Digitrakker, Epic MegaGames MASI, Galaxy Music System, Real Tracker, STMIK, Astroidea XMF, X-Tracker, Software Visions** (`.ptm`, `.mdl`, `.psm`, `.j2b`, `.rtm`, `.stx`, `.xmf`, `.dmf`) | DOS editors and game engines, 1990s | *Epic Pinball*, *Jazz Jackrabbit 2* and *Imperium Galactica* music, demoscene trackers | Played through libopenmpt, as are MO3 (`.mo3`, modules with MP3 / Ogg compressed samples), OggMod (`.oxm`), OpenMPT's own MPTM (`.mptm`) and the modules inside Unreal / Unreal Tournament music packages (`.umx`). A file under one of these names that is another format (ZX Spectrum `.psm`, DeFy AdLib Tracker `.dtm`, Extensible Music Format `.xmf`) isn't listed. |
| **FlexTrax, Funktracker, Liquid Tracker** (`.flx`, `.fnk`, `.liq`) | DOS editors, 1990s | Smaller trackers of the PC scene | Played through libxmp (`xmprender`). |

## MIDI & modern audio

| Format | Origin | Notable |
|---|---|---|
| **MIDI** (`.mid`, `.midi`) | 1983 industry standard | Sheet music for synthesizers — SoniqBoom renders through FluidSynth with the active SoundFont (`.sf2` or `.sf3`). The Docker image includes the FluidR3 General MIDI bank; on a native install, upload one or pick one from **Download Known Soundfonts** under gear → **Renderers → Soundfonts** before MIDI plays. |
| **FLAC / ALAC / WavPack** (`.flac`, `.m4a`, `.wv`) | 2001+ | Lossless everyday listening. FLAC plays directly in the browser; ALAC plays directly in Safari and is converted on the fly elsewhere; WavPack is converted on the fly. |
| **WAV / AIFF** (`.wav`, `.aiff`, `.aif`) | 1988–1991 | Uncompressed PCM. WAV plays directly; AIFF is converted on the fly. |
| **Amiga IFF samples** (`.8svx`, `.16sv`) | Electronic Arts' IFF, 1985 | 8-bit (also Fibonacci-delta packed) and 16-bit Amiga samples, mono or stereo; converted on the fly. Their name, author and annotation texts become the title, artist and comment. |
| **MP3 / AAC / Ogg Vorbis / Opus / Musepack** (`.mp3`, `.m4a`, `.aac`, `.ogg`, `.oga`, `.opus`, `.mpc`) | 1993+ | The lossy canon. MP3, AAC, Ogg Vorbis and Opus play directly in the browser (Safari older than 18.4 gets Ogg and Opus converted; an `.oga` file is converted); Musepack is converted on the fly. An `.ogg` file holding Opus is listed as Opus. Dolby Digital and Dolby Digital Plus (AC-3 / E-AC-3) in an `.m4a` file are listed as such and converted on the fly with all their channels (the Dolby Atmos objects an E-AC-3 stream can carry are not played). |
| **MPEG audio layer 1 / 2** (`.mp1`, `.mp2`, `.m2a`) | 1991–1993 | Converted on the fly; listed as MP1 or MP2 by the stream's layer. |
| **Windows Media Audio** (`.wma`) | Microsoft, 1999+ | WMA 1 and 2, WMA Pro (multichannel) and WMA Lossless; converted on the fly and listed as WMA, WMA Pro or WMA Lossless (marked lossless). |
| **Dolby Digital / Digital Plus, DTS, Dolby TrueHD** (`.ac3`, `.eac3`, `.ec3`, `.dts`, `.dtshd`, `.thd`, `.truehd`, `.mlp`) | 1991–2007, cinema and home-theatre surround | Raw surround streams, as ripped from discs. Converted on the fly by ffmpeg; the web player gets all their channels (5.1, 7.1), as WAV — or, for a file too long for a WAV (a 5.1 stream of more than about 80 minutes), as FLAC, and in Safari as a stereo WAV — while an MP3 or a stream under a bitrate cap is stereo. They are listed as AC-3, E-AC-3, DTS, DTS-HD MA / HRA, TrueHD or MLP, with TrueHD, MLP and DTS-HD MA marked lossless. They carry no tags, so the file name is the title. The Dolby Atmos and DTS:X objects such a stream can carry are not played — its 5.1 / 7.1 channels are. On a network share, such a file is read whole when it is scanned. |
| **DSD** (`.dsf`, `.dff`, `.wsd`) | Sony/Philips SACD, 1999 | 1-bit audio at 2.8+ MHz — the audiophile format; converted on the fly by ffmpeg to PCM at up to 96 kHz (this needs an ffmpeg with DSD support — see *Engines this install has*), and labelled DSD64 / DSD128 / DSD256 / DSD512 in the library. |

A file the browser can't play itself starts playing while ffmpeg converts it,
and the result is kept in the conversion cache (gear → **Renderers →
Transcode & render cache**) for the next play. When a conversion is slow, a
**Converting…** notice appears after the delay set in gear → **System →
Settings → "Converting…" notice delay** (gear → **Preferences** for accounts
without admin rights; saved per browser). A file the browser would play
itself but can't decode — an MP3 or FLAC with damaged frames — is retried
once through ffmpeg, and that browser plays it converted from then on. Other
audio files aren't added to the library — for example Monkey's Audio
(`.ape`), and files named `.mp4`, `.m4b`, `.m4r`, `.3gp` or `.caf`, even when
they hold a codec listed above.

## Archives & disk images

With *Scan ZIPs* on, SoniqBoom indexes and plays the tunes inside ZIP
archives (including a ZIP inside a ZIP), Amiga LHA/LZH archives, Commodore
`.d64` / `.d71` / `.d81` disk images (their SID tunes) and Amiga `.adf` disks
(their modules) — no unpacking needed. On network shares (with *Scan ZIPs on
remote shares*), ZIP and LHA/LZH archives are opened. Inside archives,
Amiga prefix-named members are recognised: `MOD.title` plays through
libopenmpt like any ProTracker module, and `mdat.title` and the other UADE
prefixes play through UADE. A member whose name reads both ways — `MOD.Opus`,
`MOD.Sun`, `MOD.cool.It` — is taken by its content: a module when it carries
that module format's signature, or when its bytes aren't the file its suffix
names (a Soundtracker module has no signature); the file the suffix names
otherwise. A tune listed under the other reading before keeps its ratings,
play counts and playlist entries. Outside an archive, UADE prefixes are recognised
too, but a tracker module named `MOD.title` (or `XM.title`, `S3M.title`,
`MED.title` …) is picked up only once it is renamed with a suffix, such as
`title.mod`. A member with no extension at all (`Antom`) is recognised by its
content when it is a module SoniqBoom plays, and listed with that format's
extension (`Antom.ftm`). Companion files (TFMX `smpl.*` halves, AdLib
instrument banks, PSF libraries, MDX `.pdx` sample files) are extracted together with the tune.
SoniqBoom reads LHA archives itself: header levels 0–3 and the methods
`-lh0-`, `-lh1-`, `-lh4-`, `-lh5-`, `-lh6-`, `-lh7-`, `-lz4-`, `-lz5-` and
`-lzs-`. An archive with a member packed with another method (`-lh2-`,
`-lh3-`, `-pm0-` … `-pm2-`, `-lk7-`) is read with the `lha` tool from lhasa,
which the Docker image includes and `install.sh` installs where the system
has it; without it, such an archive isn't read.

## How long a tune plays

Formats that store a program or a register log don't always say how long a
tune lasts. For Amiga, HivelyTracker, SC68, PSF, AdLib and console-chiptune
tunes whose length isn't known up front, the real length appears in the
library once the tune has been rendered.

| Family | Length |
|---|---|
| **SID** | The tune's HVSC Songlengths entry; otherwise gear → **System → Settings → SID duration** (180 s by default; 5–1800 s). Changing it doesn't change SIDs already in the library: they keep the length they were scanned with. |
| **Tracker modules** (libopenmpt) | The length libopenmpt reports when the file is scanned; a module with several subsongs plays them one after another. A one-subsong module whose last section repeats unchanged 8 or more times in a row (a loop count of 63 in a Face The Music module) ends where that section first repeats, with a short fade. |
| **Amiga player formats** (UADE) | Until the player reports the song end, or uade's limit of about 8½ minutes. An AHX tune stops at the song end the HivelyTracker replayer measures, where that replayer is available; a Delta Music 2 song plays until all four voices have started over; a Musicline tune mlrender plays runs to its end, or until it loops and fades out, or until it falls silent, at most 10 minutes. |
| **HivelyTracker** | To the song end, at most 10 minutes. |
| **SNDH** | The length the file states (`FRMS` / `TIME`); 3 minutes when it states none. |
| **YM** | Its frame count — from the header of a YM5 / YM6 file, from the size of a YM2 / YM3 one. MIX and YMT files play without a length shown in lists; YM4 files are marked **corrupt**. |
| **SC68** | The track lengths stored on the disk; 3 minutes for a track without one. |
| **Console chiptunes** (libgme) | A length the file states (an SPC's ID666 tag, NSFe and AY track lengths, an Atari SAP file's TIME lines, or a VGM's intro plus its loop played twice), followed by an 8 s fade — none after a SAP tune whose TIME isn't marked `LOOP`: it ends there. A GYM plays to its own end (one that loops fades out within 8 s after it). A tune that states none plays for the **SID duration** setting, fading out over its last 8 s.A KSS rip kssrender plays ends where it loops, stops or falls silent for 3 s, at most the **SID duration**, with an 8 s fade. |
| **VGM with other chips, MED 2–4** (ZXTune) | Until the tune ends, cut at the **SID duration** setting with an 8 s fade. |
| **PSF family** | The rip's `length` tag, then its `fade` tag (up to 60 s); an untagged rip plays as long as ZXTune renders it — 3 minutes for a Dreamcast rip through dsfrender, its `fade` tag (if it has one) fading out within them. |
| **MDX** (mdxrender) | Until the song has looped twice, then MXDRV's own fade-out (a song that doesn't loop plays to its end), plus 2 s for the last notes — measured when the file is scanned; cut at the **SID duration** setting with an 8 s fade. After a change of that setting, the next play renders the song again and the library shows its new length. |
| **S98, Farbrausch V2** (ZXTune) | The length of the dump or song, read from the file when it is scanned; an S98 dump with a loop point plays through once, without repeating the loop. |
| **GoatTracker** (gt2reloc + sidplayfp) | The **SID duration** setting, its last 8 s faded out — a GoatTracker song loops. After a change of that setting, the next play renders the song again and the library shows its new length; a change of the **SID fidelity** settings renders it again too. |
| **FamiTracker** (Furnace) | The first track to its end, its loop played once more; the library shows the length after the first play. |
| **DefleMask** (Furnace) | To its end, its loop played once more; the library shows the length after the first play. |
| **libxmp modules and converted packed modules** (xmprender) | The module's first sequence to its end, measured when the file is scanned; a module longer than 30 minutes is cut there with an 8 s fade. |
| **C64 Sidplayer** (sidplayfp) | To the song's end (it stops after 3 s of silence), at most 30 minutes; the library shows the length after the first play. |
| **Sawteeth** (sawteethrender) | To the song's end, measured when the file is scanned; a song longer than 30 minutes is cut there with an 8 s fade. |
| **SunVox** (sunvoxrender) | The project's length, measured when the file is scanned; a project longer than 30 minutes is cut there with an 8 s fade. |
| **MGSDRV** (kssrender) | Until the song has looped twice or stopped (or fallen silent for 3 s), then an 8 s fade — measured when the file is scanned; cut at the **SID duration** setting with an 8 s fade. After a change of that setting, the next play renders the song again and the library shows its new length. |
| **AdLib / OPL** | To the song end; lists show 3:00 until the tune has been rendered. |
| **MIDI** | To the end of the song, including the instruments' release. |

gear → **System → Settings → Skip short tunes** judges a tune by these
lengths, without the fade-out; a tune whose length isn't known yet is never
skipped.

## Files with several tunes

When a file holds more than one tune, the tune picker in Track Info lists
them (the first 60 directly, any later one through **Jump to**) for SID,
SNDH and SC68 files, Amiga modules played by UADE that report several
subsongs, AHX and HivelyTracker songs with subsongs, and NSF, NSFe, GBS, AY
and SAP rips. Of a GoatTracker song or a FamiTracker module with several tunes,
only the first plays. Playing the file itself starts at its default
tune: the start song a SID or SNDH file names, otherwise the first tune that
isn't empty. Other formats play as a single track. A rendered tune that
holds nothing audible shows "This tune plays only silence" instead of
playing. In Subsonic apps, album and folder views list a file's other tunes
as songs of their own, titled "Title (Tune n/N)", up to 60 entries per file;
with **Skip short tunes** on, tunes known to be shorter are left out.

## Meters and patterns

Tracker modules played by libopenmpt get a VU meter per channel (up to 32)
and a pattern view in Track Info, from the first play. The packed Amiga modules
SoniqBoom converts and Sawteeth songs get a VU meter per channel too (no
pattern view). Amiga tunes played by UADE, AHX included,
get per-voice meters when they are up to 7 minutes long (gear → **Renderers →
Playback preparation → Per-voice VU meters for Amiga modules**, on by
default), and a C64 SID up to 10 minutes long gets a three-voice meter. Other
formats show a spectrum.

## Engines this install has

gear → **Renderers → Renderer Status** lists each player SoniqBoom uses,
whether this install has it, and its version ("bundled" for the two below). Two engines are
compiled by SoniqBoom itself: the HivelyTracker replayer (`hvl2wav`, which also
measures where an AHX tune ends) and ST-Sound (`ym2wav`, for YM). The Docker
image ships both ready to run; a native install compiles them on first play,
which needs a C compiler (`cc`, `clang` or `gcc`) for HivelyTracker and a C++
compiler (`c++`, `clang++` or `g++`) for ST-Sound. A Docker image built with
`--build-arg SLIM=1` leaves out UADE and sc68, so AHX, the Amiga exotics and
`.sc68` files don't play there; `install.sh --slim` leaves out ZXTune, so the
PSF family (other than the Dreamcast rips dsfrender plays), S98 and
Farbrausch V2 files, MED 2–4 modules and VGM files that use other chips don't
play.

To use a player that isn't on the `PATH`, enter its absolute path under gear
→ **Renderers → Renderer binaries** (`uade123`, `gme`, `adplay`, `psgplay`,
`sc68`, `zxtune123`, `kssrender`, `dsfrender`, `milkyrender`, `mlrender`, `mdxrender`,
`gt2reloc`, `sunvoxrender`, `furnace`, `xmprender`);
**Save paths** applies them at once. The `sidplayfp`, `fluidsynth` and
`openmpt123` paths are set in SoniqBoom.conf (`sidplayfp_path`,
`fluidsynth_path` and `openmpt123_path` in its `renderers` section). When ffmpeg is missing, or the ffmpeg in use lacks the DSD
demuxers (`dsf`, `iff`, `wsd`) or one of the encoders SoniqBoom converts with,
Renderer Status shows a notice offering **Download bundled ffmpeg** (or
**Re-download ffmpeg**, or **Re-download + switch**), which installs a complete
ffmpeg into SoniqBoom's data folder and uses it from then on.

A retro tune is rendered by its player when it's played, and a finished
render is kept in the conversion cache for the next play. SID, MIDI,
tracker modules, AdLib / OPL, SNDH, PSF, S98, Farbrausch V2, MDX, libgme console chiptunes and UADE
tunes, AHX included, start playing in the web player while their render is
still running; in Safari, one whose length isn't known yet starts once its
render has finished. HivelyTracker, YM and SC68 files, and the VGM and
MED 2–4 tunes ZXTune plays, are rendered in full before they start.

---

*Sources for the deeper lore: [HVSC](https://www.hvsc.c64.org/),
[Modland](https://modland.com/), [SNDH Archive](http://sndh.atari.org/),
[Demozoo](https://demozoo.org/), [VGMRips](https://vgmrips.net/),
[UADE](https://zakalwe.fi/uade/).*

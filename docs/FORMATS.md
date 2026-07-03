# The formats SoniqBoom plays — origins & claims to fame

Every format family SoniqBoom renders, where it came from, and why it
matters.  Retro formats aren't just containers — each one is a piece of
computing history with its own sound, scene, and stories.

---

## Commodore 64

| Format | Origin | Claim to fame | Notable |
|---|---|---|---|
| **SID** (`.sid`, `.psid`, `.rsid`) | Commodore 64, 1982 — named for its sound chip, the MOS 6581/8580 *Sound Interface Device* | The most collected chiptune format on Earth: the High Voltage SID Collection (HVSC) curates 50,000+ tunes | Three voices + one filter, yet composers like Rob Hubbard, Martin Galway and Chris Hülsbeck coaxed whole orchestras out of it. The 6581 vs 8580 chip revisions sound audibly different — endless scene debate. SoniqBoom reads HVSC's Songlengths + STIL commentary, and lets you force either chip model. |

## Amiga trackers

| Format | Origin | Claim to fame | Notable |
|---|---|---|---|
| **ProTracker MOD** (`.mod`) | Amiga, 1987 (Ultimate Soundtracker → NoiseTracker → ProTracker) | *The* format that invented tracker music: 4 channels of 8-bit samples, patterns, and the entire demoscene workflow | The `M.K.` magic bytes are Mahoney & Kaktus' initials. Karsten Obarski's original editor sold poorly; the pirated clones conquered the world. |
| **OctaMED / MED** (`.med`, `.oct`) | Amiga, 1989, Teijo Kinnunen | Doubled the Amiga's 4 hardware channels to 8 in software | Big in Finland; used for countless Amiga game soundtracks. |
| **Oktalyzer** (`.okt`) | Amiga, 1988 | Early 8-channel pioneer, before OctaMED made it mainstream | |
| **DigiBooster Pro** (`.dbm`) | Amiga, late 1990s | The Amiga's answer to FastTracker 2 at the platform's twilight | |
| **AHX** (`.ahx`) | Amiga, 1996 (Abyss' Highest eXperience) | Pure synthesis, no samples: a whole tune in a few kilobytes that *sounds* like a C64 on an Amiga | Beloved for 4k/64k demo intros where every byte counts. |
| **HivelyTracker** (`.hvl`) | Amiga, 2006 | AHX's community successor — more channels, stereo | SoniqBoom bundles the reference replayer and compiles it on first use. |

## Amiga exotics (the uade family — ~150 formats)

These are custom, per-composer or per-game music systems, playable thanks
to **UADE**, which runs the *original Amiga player code* inside a 68k
emulator.  Highlights:

| Format | Origin | Claim to fame | Notable |
|---|---|---|---|
| **TFMX** (`mdat.*` + `smpl.*`) | Chris Hülsbeck, 1987 | The custom engine behind *Turrican* — some of the most celebrated game music ever written | Two-file format: music data + samples must travel together. SoniqBoom keeps the pair intact automatically, even inside archives. |
| **Future Composer** (`.fc`, `.fc13`, `.fc14`, `.smod`) | Amiga, 1988–89 | The gateway editor for hundreds of scene musicians — simpler than trackers, everywhere in cracktros | |
| **SidMon 1/2** (`.sid` *(Amiga!)*, `.sid1`, `.sid2`) | Amiga, 1988 | Built to make an Amiga sound like a C64 | The `.sid` name collides with C64 SID files — SoniqBoom tells them apart by content, not name. |
| **David Whittaker** (`.dw`) | The composer's own engine, 1980s | Whittaker scored 100+ games (*Shadow of the Beast*, *Xenon 2* ports…) using his private format | One of several "signature formats" that exist only because one prolific composer used them. |
| **Rob Hubbard** (`.rh`) | The composer's own engine | The C64 legend's Amiga-era system (*Monty on the Run*'s composer) | |
| **Jochen Hippel** (`.hip`, `.hipc`, `.hst`) | The composer's own engines, Amiga + Atari ST | Hippel scored *Turrican*'s ST ports and the *Dragonflight* soundtrack | The `-COSO` variant is his compressed format; `.hst` is the Atari ST flavour. |
| **Delta Music 1/2** (`.dm`, `.dm2`) | Amiga, 1989–90 | Distinctive synth-heavy sound, popular in Danish/German demos | |
| **SoundMon / BP SoundMon** (`.bp`) | Brian Postma, Amiga | Synthesis + samples hybrid, a demoscene staple | |
| **JamCracker, Sonic Arranger, Musicline, Fred, Art of Noise, Ben Daglish, Dave Lowe, …** | Various Amiga eras | Each a small scene or a single composer's toolkit | UADE ships ~175 player engines; SoniqBoom routes every one it detects. |
| **ProWizard-packed MODs** (~110 packer variants) | Amiga cracking scene | Crackers "packed" ProTracker modules into bespoke formats to save disk space and obscure ripping | uade unpacks them on the fly. |

## Atari ST

| Format | Origin | Claim to fame | Notable |
|---|---|---|---|
| **SNDH** (`.sndh`) | Atari ST community archive format, 1990s | One file = music data + the original ST replay code + metadata; the SNDH Archive preserves thousands | Driven by the YM2149 square-wave chip — the ST's whole "sound" in three channels. Rendered by **psgplay**, a modern cycle-accurate 68000+YM emulator. |
| **YM** (`.ym`) | Register-dump format by Arnaud Carré (Leonard/Oxygene) | A literal recording of what the YM2149 chip's registers did, frame by frame — perfect playback forever | Usually LHA-compressed inside. SoniqBoom bundles Carré's own **ST-Sound** engine — the format author's reference player. |
| **SC68** (`.sc68`) | Atari ST/Amiga archival container | Like SNDH but cross-platform, with embedded durations | |

## Console chiptunes (rips of real game hardware music)

| Format | Origin | Claim to fame | Notable |
|---|---|---|---|
| **NSF / NSFe** (`.nsf`) | NES / Famicom, 1983 hardware | The actual 6502 music code from NES cartridges, replayed through an emulated APU | One file usually holds a game's *entire* soundtrack as subsongs. |
| **SPC** (`.spc`) | Super Nintendo, 1990 | A frozen RAM snapshot of the SNES's Sony-designed audio co-processor | The SPC700 chip was designed by Ken Kutaragi — the man who then built the PlayStation. |
| **GBS** (`.gbs`) | Game Boy, 1989 | Four voices of the most recognisable handheld sound ever made | |
| **VGM / VGZ** (`.vgm`) | Sega Master System / Genesis + arcade, format from 2005 | Chip-register logging across dozens of sound chips; the vgmrips community archives thousands of sets | Yamaha YM2612 FM = the Genesis "growl". |
| **AY** (`.ay`) | ZX Spectrum / Amstrad CPC | The Eastern-European demoscene's chip of choice (AY-3-8910) | |
| **KSS, SAP, GYM, HES** | MSX, Atari 8-bit, Genesis, PC Engine | Each platform's native music-rip format | |

## PSF family (later console rips)

One clever container, many consoles: a **PSF** file holds the game's
original music *code*, executed inside a tiny emulated slice of the
console.  Invented by Neill Corlett in 2003.  `mini` variants share a
common library file per game — SoniqBoom keeps those pairs together.

| Format | Console | Notable |
|---|---|---|
| **PSF / PSF2** (`.psf`, `.minipsf`, `.psf2`) | PlayStation 1 / 2 | The original — *Final Fantasy* and *Castlevania: SotN* rips made it famous |
| **USF** (`.usf`, `.miniusf`) | Nintendo 64 | Runs a sliced-down N64 emulation per song |
| **GSF** (`.gsf`, `.minigsf`) | Game Boy Advance | |
| **2SF / NCSF** (`.2sf`, `.ncsf`) | Nintendo DS | NCSF is the newer, leaner sequencer-level rip |
| **SSF / DSF** (`.ssf`, `.dsf`) | Sega Saturn / Dreamcast | Dreamcast's `.dsf` shares its extension with Sony's DSD audio files — SoniqBoom distinguishes them by content |

## PC DOS-era FM synthesis (AdLib / OPL)

The sound of DOS gaming: Yamaha OPL2/OPL3 FM chips, no samples at all.

| Format | Origin | Claim to fame | Notable |
|---|---|---|---|
| **IMF** (`.imf`) | id Software / Apogee | *Wolfenstein 3D*, *Commander Keen*, *Duke Nukem II* | The extension collides with Imago Orpheus tracker modules — content decides. |
| **ROL** (`.rol`) | AdLib Inc.'s own Visual Composer | The "official" AdLib format | Needs its instrument bank (`standard.bnk`) beside it — handled automatically. |
| **CMF, D00, RAD, HSC, SCI, LAA, DRO, RIX, A2M…** | Creative Labs, EdLib, Reality, Sierra, LucasArts, DOSBox… | Every studio had its own FM format | `.dro` is DOSBox's raw OPL log — a perfect recording of whatever the emulated card played. SoniqBoom renders the whole family through AdPlug with the cycle-accurate Nuked OPL3 core. |

## PC trackers

| Format | Origin | Claim to fame | Notable |
|---|---|---|---|
| **ScreamTracker 3** (`.s3m`) | Future Crew, 1994 | The PC demoscene's breakout format — *Second Reality*'s creators | |
| **FastTracker 2** (`.xm`) | Triton, 1994 | *The* 90s tracker: instruments, envelopes, 32 channels | Half the mod archive is XM. |
| **Impulse Tracker** (`.it`) | Jeffrey Lim, 1995 | The technical peak of DOS trackers — NNAs, filters, stereo samples | Its lineage lives on in OpenMPT/Schism today. |
| **669, MTM, ULT, FAR, STM, GDM, DSM, AMF, WOW…** | Various 90s editors | Each a chapter of tracker evolution | Rendered via libopenmpt, the modern reference engine. |

## MIDI & modern audio

| Format | Origin | Notable |
|---|---|---|
| **MIDI** (`.mid`) | 1983 industry standard | Sheet music for synthesizers — SoniqBoom renders through FluidSynth with swappable SoundFonts. |
| **FLAC / ALAC / WavPack / Musepack** | 2001+ | Lossless everyday listening. |
| **MP3 / AAC / Ogg Vorbis / Opus** | 1993+ | The lossy canon. |
| **DSD** (`.dsf`, `.dff`, `.wsd`) | Sony/Philips SACD, 1999 | 1-bit audio at 2.8+ MHz — the audiophile format; transcoded on the fly for browser playback. |

---

*Sources for the deeper lore: [HVSC](https://www.hvsc.c64.org/),
[Modland](https://modland.com/), [SNDH Archive](http://sndh.atari.org/),
[Demozoo](https://demozoo.org/), [VGMRips](https://vgmrips.net/),
[UADE](https://zakalwe.fi/uade/).*

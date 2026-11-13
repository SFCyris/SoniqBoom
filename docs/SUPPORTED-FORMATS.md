# Supported formats and the players that play them

Every file format SoniqBoom lists and plays, the file names it recognises,
and the player that turns it into audio. For the history of the formats see
[FORMATS.md](FORMATS.md); for how long each one plays, its
*How long a tune plays* section.

A file is recognised by its suffix (`song.mod`); a format UADE plays also by
its Amiga prefix name (`mdat.song`), and inside archives a tracker module by
its prefix name too (`mod.song`, `med.song`). Where several formats share a
suffix, the file's content decides (see *Shared suffixes* below). A file under a
supported suffix that turns out to be another format, or one no installed
player reads, is not listed.

## Players

The versions are those the Docker image builds or installs; `install.sh`
builds the same versions of the players it compiles, and takes the others
from Homebrew or the Linux distribution.

| Player | Version (Docker image) | Plays | Docker image |
|---|---|---|---|
| Browser (no conversion) | — | MP3, FLAC, WAV, Ogg Vorbis, Opus; AAC (and ALAC in Safari) after a codec check | — |
| ffmpeg | Debian package (or the bundled static build) | Every other ordinary audio format, surround streams, DSD | Included |
| sidplayfp (libsidplayfp with reSIDfp) | sidplayfp 3.1.0, libsidplayfp 3.1.0, libresidfp 1.2.2 | C64 SID tunes, C64 Sidplayer songs, GoatTracker songs (after `gt2reloc`) | Included |
| `gt2reloc` (GoatTracker's packer) | GoatTracker 2.77 | Converts GoatTracker songs to SID tunes | Included |
| FluidSynth | Debian package, with the FluidR3 General MIDI SoundFont | MIDI | Included |
| libopenmpt / `openmpt123` | 0.8.9 | Tracker modules | Included |
| `xmprender` (libxmp) | xmprender 1.0, libxmp 4.7.4 | The modules only libxmp reads, and packed modules SoniqBoom converts | Included |
| UADE (`uade123`) | 3.05 | AHX and the Amiga custom-player formats | Not in a `SLIM=1` build (heavy source build) |
| `hvl2wav` (HivelyTracker replayer) | bundled with SoniqBoom | HivelyTracker; measures where an AHX tune ends | Included |
| `mlrender` (musicline_playback) | DEViLBOX `57b3e19` | Musicline Editor tunes UADE renders only as silence | Included |
| `milkyrender` (MilkyPlay) | MilkyTracker `e3ceb05` | Early DSIK modules (`DSM\x10`) | Included |
| libgme (Game Music Emu) | 0.6.5 | Console chiptunes | Included |
| `kssrender` (libkss + MGSDRV) | kssrender 1.2, libkss `2ed9588` | MSX / Sega Master System KSS, MSX MGSDRV songs | Included |
| ZXTune (`zxtune123`) | r5112 | PSF family, S98, Farbrausch V2, MED 2–4, VGM with chips libgme lacks (left out by `install.sh --slim`) | Included |
| `dsfrender` (Highly Theoretical + psflib) | Highly Theoretical `2998a4b`, psflib `95509e0` | Dreamcast / Naomi DSF rips | Included |
| `mdxrender` (portable_mdx) | portable_mdx `dc50b06` | Sharp X68000 MDX (+ PDX) | Included |
| `sunvoxrender` (SunVox Library) | 2.1.4d | SunVox projects | Included |
| Furnace (`furnace-headless`) | 0.6.8.3 | FamiTracker and DefleMask modules | Included |
| `sawteethrender` (NostalgicPlayer's Sawteeth player, ported to C) | sawteethrender 1.1, NostalgicPlayer `c6eb0c5` | Sawteeth songs | Included |
| psgplay | `6f1c39f` | Atari ST SNDH | Included |
| `ym2wav` (ST-Sound) | bundled with SoniqBoom | Atari ST YM register dumps | Included |
| sc68 | 2.2.1 | Atari ST / Amiga SC68 disks | Not in a `SLIM=1` build (heavy source build) |
| AdPlug (`adplay`) | Debian package | AdLib / OPL FM formats | Included |

gear → **Renderers → Renderer Status** shows which of these an install has and
the version each reports.

## Ordinary audio

| Format | Suffixes | Player |
|---|---|---|
| MP3 | `.mp3` | Browser |
| MPEG audio layer 1 / 2 | `.mp1`, `.mp2`, `.m2a` | ffmpeg |
| FLAC | `.flac` | Browser |
| WAV | `.wav` | Browser |
| AIFF | `.aiff`, `.aif` | ffmpeg |
| Ogg Vorbis | `.ogg`, `.oga` | Browser for `.ogg` (ffmpeg for Safari before 18.4); ffmpeg for `.oga` |
| Opus | `.opus` (an `.ogg` holding Opus too) | Browser (ffmpeg for Safari before 18.4) |
| AAC / ALAC | `.m4a`, `.aac` | Browser for AAC; ALAC in Safari, ffmpeg elsewhere |
| WavPack | `.wv` | ffmpeg |
| Musepack | `.mpc` | ffmpeg |
| WMA (1, 2, Pro, Lossless) | `.wma` | ffmpeg |
| Amiga IFF samples (8SVX, Fibonacci-packed 8SVX, 16SV) | `.8svx`, `.16sv` | ffmpeg |
| Dolby Digital / Digital Plus | `.ac3`, `.eac3`, `.ec3` (and in `.m4a`) | ffmpeg, all channels |
| DTS / DTS-HD | `.dts`, `.dtshd` | ffmpeg, all channels |
| Dolby TrueHD / MLP | `.thd`, `.truehd`, `.mlp` | ffmpeg, all channels |
| DSD | `.dsf`, `.dff`, `.wsd` | ffmpeg, to PCM up to 96 kHz |

## Commodore 64

| Format | Suffixes | Player |
|---|---|---|
| SID (PSID / RSID) | `.sid`, `.psid`, `.rsid` | sidplayfp |
| C64 Sidplayer | `.mus`, with a `.str` beside a stereo song (a stereo song inside an archive or on an FTP / WebDAV share plays its first SID only) | sidplayfp |
| GoatTracker 1.x / 2.x | `.sng` | `gt2reloc` → sidplayfp |

## MIDI

| Format | Suffixes | Player |
|---|---|---|
| Standard MIDI file | `.mid`, `.midi` | FluidSynth |

## Tracker modules (libopenmpt)

| Format | Suffixes | Player |
|---|---|---|
| ProTracker and its relatives (Soundtracker, NoiseTracker, StarTrekker, FastTracker and Octalyser multichannel MODs …) | `.mod`, `mod.*` | libopenmpt |
| ChipTracker | `.chp`, `.chip` (or `.mod`) | libopenmpt |
| ProTracker IFF / PolyTracker | `.ptm`, `ptm.*` | libopenmpt |
| ScreamTracker 3 / 2 | `.s3m`, `.stm` | libopenmpt |
| STMIK (Scream Tracker Music Interface Kit) | `.stx` | libopenmpt |
| FastTracker 2 | `.xm` | libopenmpt |
| Impulse Tracker | `.it` | libopenmpt |
| OpenMPT MPTM | `.mptm` | libopenmpt |
| OctaMED (MMD0–MMD3) | `.med`, `.oct`, `.md0`, `.md1`, `.md2`, `.md3`, `.mmd3`, `.omed`, `.ocss` | libopenmpt (MED 2–4: ZXTune) |
| Oktalyzer | `.okt` | libopenmpt |
| SoundFX 1 / 2 | `.sfx`, `.sfx2` | libopenmpt |
| DigiBooster Pro | `.dbm` | libopenmpt |
| Face The Music | `.ftm` (with its samples) | libopenmpt 0.8 or newer |
| MultiTracker | `.mtm` | libopenmpt |
| Composer 669 / Unis 669 | `.669` | libopenmpt |
| UltraTracker | `.ult` | libopenmpt |
| Farandole Composer | `.far` | libopenmpt |
| General DigiMusic | `.gdm` | libopenmpt |
| Imago Orpheus | `.imf` | libopenmpt |
| Grave Composer (Mod's Grave) | `.wow` | libopenmpt |
| ASYLUM / DSMI | `.amf` | libopenmpt |
| Digital Sound Interface Kit (RIFF) | `.dsm` | libopenmpt (the early `DSM\x10` form: `milkyrender`) |
| Digital Symphony | `.dsym` | libopenmpt |
| Digital Tracker | `.dtm` | libopenmpt |
| X-Tracker / DSMI Compact / Software Visions | `.dmf` | libopenmpt |
| Digitrakker | `.mdl` | libopenmpt |
| Epic MegaGames MASI | `.psm` | libopenmpt |
| Galaxy Music System | `.j2b` | libopenmpt |
| Real Tracker | `.rtm` | libopenmpt |
| Astroidea XMF | `.xmf` | libopenmpt |
| MO3 | `.mo3` | libopenmpt |
| OggMod | `.oxm` | libopenmpt |
| Unreal / Unreal Tournament music packages | `.umx` | libopenmpt |

`.stx`, `.mptm`, `.sfx2`, `.dsym`, `.dtm`, `.dmf`, `.mdl`, `.psm`, `.j2b`, `.rtm`,
`.xmf`, `.mo3`, `.oxm`, `.umx` and Face The Music's `.ftm` are listed when the
installed libopenmpt names them among the formats it reads.

## Modules libxmp plays (`xmprender`)

| Format | Suffixes | Player |
|---|---|---|
| Archimedes Tracker | `.musx` | `xmprender` |
| Coconizer | `.coco` | `xmprender` |
| FlexTrax | `.flx` | `xmprender` |
| Funktracker | `.fnk` | `xmprender` |
| Liquid Tracker | `.liq` | `xmprender` |
| Megatracker | `.mgt` | `xmprender` |
| Titanics Player | `.tip` | `xmprender` |
| STIM (Slam Tilt) | `.sti` | `xmprender` |
| The Dark Demon | `.tdd` | `xmprender` |
| Eureka Packer | `.eureka` | `xmprender` |

## Packed modules SoniqBoom converts (then `xmprender`)

SoniqBoom turns these back into ProTracker modules, which `xmprender` plays.

| Format | Suffixes |
|---|---|
| Ben Replay | `.ben` |
| Binary Packer | `.bnr` |
| Devils Replay | `.dev` |
| GnoiPacker | `.gnoi` |
| Gnu Protected Module | `.gpmo` |
| GnuPlayer | `.gnu` |
| HCD Protector | `.hcd` |
| Newtron Packer 1.0 / 2.0 | `.nw1`, `.nw2` |
| Perfect Song 1 / 2 | `.pf1`, `.pf2` |
| Module-Patterncompressor | `.pmd3` |
| NoiseTracker Compressed | `.ntc` |
| The Player 2.2A / 3.0A | `.p22a`, `.p30a` |

## Other Amiga engines

| Format | Suffixes | Player |
|---|---|---|
| AHX | `.ahx`, `.thx` | UADE (the song end from `hvl2wav`) |
| HivelyTracker | `.hvl` | `hvl2wav` |
| Musicline Editor | `.ml` | UADE; `mlrender` for a tune UADE renders only as silence |
| TFMX | `mdat.*` + `smpl.*`, or `.tfx` + `.sam` | UADE |

## Amiga custom players (UADE)

Every player of the installed UADE's `eagleplayer.conf` (this list: UADE
3.05). A name in the middle column is recognised both as a suffix
(`song.fc13`) and as an Amiga prefix (`fc13.song`); one in the right column
only as a prefix — its suffix belongs to another format or is a common
non-music suffix. Names after "also" are other suffixes the player's
modules are saved under; they are staged under the player's own name for
UADE; one marked "(suffix only)" has its prefix form played by another
engine. ProWizard's packers are the PTK-Prowiz row. An `mdat.` module the
TFMX player refuses plays with the TFMX ST player.

<!-- uade-table:begin (scripts/supported_formats_doc.py) -->
| Player | Suffix (`song.<x>`) and prefix (`<x>.song`) names | Prefix name only (`<x>.song`) |
|---|---|---|
| Action Amics | `ast` | — |
| ADPCM mono | `adpcm` | — |
| AHX | `thx` | `ahx` |
| Alcatraz Packer | `alp` | — |
| AM Composer | `amc` | — |
| Amiga Custom | `cus`, `cust`, `custom` | — |
| AMOS | `abk` | — |
| Anders 0land | `hot` | — |
| Andrew Parton | `bye` | — |
| APro Sys | `aps` | — |
| Art And Magic | `aam` | — |
| Art Of Noise 4 V | `aon`, `aon4` | — |
| Art Of Noise 8 V | `aon8` | — |
| Ashley Hogg | `ash` | — |
| Audio Sculpture | `adsc`, `mod_adsc4` | — |
| Beathoven Synthesizer | `bss` | — |
| Ben Daglish | `bd` | — |
| Ben Daglish SID | `bds` | — |
| Blade Packer | `uds` | — |
| ChipTracker | `kris` | — |
| Cinemaware | `cin` | — |
| Core Design | `core` | — |
| Custom Made | `cm`, `rk`, `rkb` | — |
| Darius Zendeh | `dz`, `mkiio` | — |
| Dave Lowe | `dl` | — |
| Dave Lowe Deli | `dl_deli` | — |
| Dave Lowe New | `dln` | — |
| David Hanney | `dh` | — |
| David Whittaker | `dw`, `dwold` | — |
| Delta Music | `dlm1`, `dm`, `dm1` | — |
| Delta Music 2 | `dlm2`, `dm2` | — |
| Desire | `dsr` | — |
| DIGI Booster | `digi` (suffix only) | `db` |
| Digital Sonix Chrome | `dsc` | — |
| Digital Sound Studio | `dss` | — |
| Dirk Bialluch | `tpu` | — |
| Dynamic Synthesizer | `dns` | — |
| Ear Ache | `ea`, `mg` | — |
| EMS | `ems` | — |
| EMS 6 | `emsv6` | — |
| Fashion Tracker | — | `ex` |
| Forgotten Worlds Game | `fw` | — |
| Fred Editor | `fred`; also `frd` | — |
| Fred Gray | `gray` | — |
| Future Composer | `fc13`, `fc3`, `smod` | — |
| Future Composer | `fc`, `fc14`, `fc4` | — |
| Future Composer | `bfc`, `bsi`, `fc-bsi` | — |
| Future Composer | `fp` | — |
| Glue Mon | `glue` | `gm` |
| GMC | `gmc` | — |
| Howie Davies | `hd` | — |
| Images Music System | `ims` | — |
| In Stereo | also `is10` | `is` |
| In Stereo2.0 | `is20` | — |
| Infogrames | `dum` | — |
| Jam Cracker | `jam`, `jc` | — |
| Janko Mrsic Flogel | `jmf` | — |
| Janne Salmijarvi Optimizer | — | `js` |
| Jason Brooke | `jb`, `jcb`, `jcbo` | — |
| Jason Page | `jp`, `jpn`, `jpnd` | — |
| Jeroen Tel | `jt`, `mon_old` | — |
| Jesper Olsen | `jo` | — |
| Jochen Hippel | `hip`, `mcmd`, `sog` | — |
| Jochen Hippel 7V | `hip7`, `s7g` | — |
| Jochen Hippel CoSo | `hipc`, `soc` | — |
| Jochen Hippel ST | `hst` | — |
| Kim Christensen | `kim` | — |
| Kris Hatlelid | `kh` | — |
| Laxity | `powt`, `pt` | — |
| Leggless Music Editor | `lme` | — |
| Magnetic Fields Packer | `mfp` | — |
| Major Tom | `arp`, `hn`, `mtp2`, `thn` | — |
| Maniacs Of Noise | `mon` | — |
| Mark Cooksey | `mc`, `mcr` | — |
| Mark Cooksey Old | `mco` | — |
| Mark II | `mk2`, `mkii` | — |
| Martin Walker | `avp`, `mw` | — |
| Maximum Effect | `max` | — |
| MCMD | `mcmd_org` | — |
| MED | `mmd0`, `mmd1`, `mmd2` | — |
| Medley | `mso` | — |
| MIDI Loriciel | — | `midi` |
| Mike Davies | — | `md` |
| MMDC | `mmdc` | — |
| Mosh Packer | `mosh` | — |
| Mugician | `dmu`, `mug` | — |
| Mugician II | `dmu2`, `mug2` | — |
| Multi Media Sound | `mms`, `sfx20` | — |
| Music Assembler | — | `ma` |
| Music Maker 4 V | `mm4`, `sdata` | — |
| Music Maker 8 V | `mm8` | — |
| Musicline Editor | `ml` | — |
| Nick Pelling Packer | `npp` | — |
| Novo Trade Packer | `ntp` | — |
| NTSP system | `two` | — |
| Octa MED | `octamed` | — |
| Oktalyzer | `okta` | — |
| on Escapee | `one` | — |
| Paul Robotham | `dat` | — |
| Paul Shields | — | `ps` |
| Paul Summers | `snk` | — |
| Paul Tonge | `pat` | — |
| Peter Verswyvelen | `pvp` | — |
| Pierre Adane | `pap` | — |
| Pokeynoise | `pn` | — |
| PreTracker | `prt` | — |
| Professional Sound Artists | `psa` | — |
| ProTracker (packed) | `40a`, `40b`, `41a`, `50a`, `60a`, `61a`, `ac1`, `ac1d`, `aval`, `chan`, `cp`, `cplx`, `crb`, `eu`, `fc-m`, `fcm`, `ft`, `fuz`, `fuzz`, `gv`, `hmc`, `hrt`, `hrt!`, `ice`, `it1`, `kef`, `kef7`, `krs`, `lax`, `mexxmp`, `mod15`, `mod15_mst`, `mod_comp`, `mod_doc`, `mod_flt4`, `mod_ntk`, `mod_ntk1`, `mod_ntk2`, `mod_ntkamp`, `mpro`, `noisepacker2`, `noisepacker3`, `np`, `np1`, `np2`, `np3`, `nr`, `nru`, `ntpk`, `p10`, `p21`, `p30`, `p40a`, `p40b`, `p41a`, `p4x`, `p50a`, `p5a`, `p5x`, `p60`, `p60a`, `p61`, `p61a`, `p6x`, `pha`, `pin`, `pm`, `pm0`, `pm01`, `pm1`, `pm10c`, `pm18a`, `pm2`, `pm20`, `pm4`, `pm40`, `pmz`, `polk`, `pp10`, `pp20`, `pp21`, `pp30`, `ppk`, `pr1`, `pr2`, `prom`, `pru`, `pru1`, `pru2`, `prun`, `prun1`, `prun2`, `pwr`, `pyg`, `pygm`, `pygmy`, `skt`, `skyt`, `snt`, `snt!`, `st2`, `st26`, `st30`, `star`, `stpk`, `tp`, `tp1`, `tp2`, `tp3`, `un2`, `unic`, `unic2`, `wn`, `xan`, `xann`, `zen`; also `ch1`, `ch2`, `ch3`, `chn`, `fuzzac`, `kms`, `mp`, `pm10`, `stp`, `wnp` | `di`, `ksm` |
| Protracker4 | `mod3` | — |
| Puma Tracker | `puma` | — |
| Quadra Composer | `emod`, `qc` | — |
| Quartet | `qpa` | — |
| Quartet PSG | `sqt` | — |
| Quartet ST | `qts` | — |
| Richard Joseph | `rj` | — |
| Richard Joseph Player | `rjp` | — |
| Riff Raff | `riff` | — |
| Rob Hubbard | `rh` | — |
| Rob Hubbard ST | `rho` | — |
| SCUMM | `scumm` | — |
| Sean Connolly | `s-c`, `scn` | — |
| Sean Conran | `scr` | — |
| SidMon | `sid1`, `smn`; also `sd1` | `sid` |
| SidMon II | `sid2`; also `sd2` | — |
| Sierra AGI | `agi` | — |
| Silmarils | `mok` | — |
| Sonic Arranger | `lion`, `sa`, `sa-p`, `sonic` | — |
| Sonic Arranger pc all | `sa_old` | — |
| Sonix Music Driver | `smus`, `snx`, `tiny` | — |
| Sound Control | `sc`, `sct` | — |
| Sound Factory | — | `psf` |
| Sound FX | `sfx13` | `sfx` |
| Sound Images | `tw` | — |
| Sound Master | `sm`, `sm1`, `sm2`, `sm3`, `smpro` | — |
| Sound Player | `sjs` | — |
| Sound Programming Language | `spl` | — |
| SoundMon | `bp`, `sndmon`; also `bp2` | — |
| SoundMon | `bp3` | — |
| Soundtracker IV | `mod15_st-iv` | — |
| Special FX | `jd` | — |
| Special FX ST | `doda` | — |
| Speedy A1 System | `sas` | — |
| Speedy System | — | `ss` |
| Steve Barrett | `sb` | — |
| Steve Turner | `jpo`, `jpold` | — |
| SUN Tronic | `sun` | — |
| Syn Tracker | `synmod` | `st` |
| Synth | `syn` | — |
| Synth Dream | `sdr` | — |
| Synth Pack | `osp` | — |
| TCB Tracker | `tcb` | — |
| TFMX | `mdat`, `tfmxpro`; also `tfx` | — |
| TFMX | `tfhdpro`, `tfmx` | — |
| TFMX 7V | `tfmx7v` | — |
| TFMX 7V | `tfhd7v` | — |
| TFMX ST | `mdst` | — |
| The Musical Enlightenment | `tme` | — |
| Thomas Hermann | `thm` | — |
| Tim Follin | `tf` | — |
| Time Tracker | `tmk` | — |
| Titanics Packer | `tits` | — |
| Tomy Tracker | `sg` | — |
| Tronic | `dp`, `trc`, `tro`, `tronic` | — |
| UFO | `mus`, `ufo` | — |
| Ultimate Soundtracker | `mod15_ust` | — |
| Voodoo Supreme Synthesizer | `vss` | — |
| Wally Beben | `wb` | — |
| YM 2149 | `ymst` | `ym` |
| Zound Monitor | `sng` | — |
<!-- uade-table:end -->

## Atari ST

| Format | Suffixes | Player |
|---|---|---|
| SNDH | `.sndh` | psgplay |
| YM register dumps | `.ym` | `ym2wav` (ST-Sound) |
| SC68 | `.sc68` | sc68 |

## Console chiptunes

| Format | Suffixes | Player |
|---|---|---|
| NES / Famicom | `.nsf`, `.nsfe` | libgme |
| Super Nintendo | `.spc` | libgme |
| Game Boy | `.gbs` | libgme |
| Sega Master System / Mega Drive / arcade chip logs | `.vgm`, `.vgz` | libgme; ZXTune for a VGM using a chip libgme lacks |
| ZX Spectrum / Amstrad CPC | `.ay` | libgme |
| MSX / Sega Master System | `.kss` | `kssrender` (libgme without it) |
| Atari 8-bit (POKEY) | `.sap` | libgme |
| Mega Drive register dumps | `.gym` (packed GYMX too) | libgme |
| PC Engine | `.hes` | libgme |
| MSX MGSDRV | `.mgs` | `kssrender` |
| FamiTracker | `.ftm` | Furnace |
| DefleMask | `.dmf` | Furnace |

## PSF family (console rips)

| Format | Suffixes | Player |
|---|---|---|
| PlayStation | `.psf`, `.minipsf` (+ `.psflib`) | ZXTune |
| PlayStation 2 | `.psf2`, `.minipsf2` | ZXTune |
| Nintendo 64 | `.usf`, `.miniusf` (+ `.usflib`) | ZXTune |
| Game Boy Advance | `.gsf`, `.minigsf` (+ `.gsflib`) | ZXTune |
| Nintendo DS | `.2sf`, `.mini2sf`, `.ncsf`, `.minincsf` (+ their libraries) | ZXTune |
| Sega Saturn | `.ssf`, `.minissf` | ZXTune |
| Sega Dreamcast / Naomi | `.dsf`, `.minidsf` (+ `.dsflib`) | `dsfrender`; ZXTune for a rip `dsfrender` refuses |

## FM dumps and synthesizer songs

| Format | Suffixes | Player |
|---|---|---|
| S98 (PC-98, X68000 … FM logs) | `.s98` | ZXTune |
| Farbrausch V2 | `.v2m` | ZXTune |
| Sharp X68000 MDX | `.mdx` (+ the `.pdx` it names) | `mdxrender` |
| SunVox | `.sunvox` | `sunvoxrender` |
| Sawteeth | `.st` (`SWTD` binary or `SWTT` text) | `sawteethrender` |

## AdLib / OPL FM

| Format | Suffixes | Player |
|---|---|---|
| id Software / Apogee IMF | `.imf` | AdPlug |
| AdLib Visual Composer | `.rol` (+ `standard.bnk`) | AdPlug |
| Creative Music File | `.cmf` | AdPlug |
| EdLib | `.d00` | AdPlug |
| Reality AdLib Tracker | `.rad` | AdPlug |
| LucasArts | `.laa` | AdPlug |
| Sierra | `.sci` (+ its patch file) | AdPlug |
| DOSBox OPL capture | `.dro` | AdPlug |
| HSC AdLib Composer | `.hsc` | AdPlug |
| RIX | `.rix` | AdPlug |
| AdLib Tracker 2 | `.a2m` | AdPlug |
| Westwood ADL | `.adl` | AdPlug |
| Bob's AdLib Music | `.bam` | AdPlug |
| Ken Silverman's music | `.ksm` (+ `insts.dat`) | AdPlug |
| AMUSIC AdLib Tracker | `.amd` | AdPlug |

## Shared suffixes

| Suffix | Formats, told apart by content |
|---|---|
| `.sid` | C64 SID (PSID / RSID header): sidplayfp · Amiga SidMon: UADE |
| `.psf` | PlayStation rip (`PSF` header): ZXTune · Amiga SoundFactory: UADE |
| `.dsf` | Sony DSD Stream File: ffmpeg · Dreamcast rip: `dsfrender` |
| `.imf` | Imago Orpheus: libopenmpt · id Software IMF: AdPlug |
| `.ftm` | FamiTracker: Furnace · Face The Music: libopenmpt |
| `.sng` | GoatTracker: `gt2reloc` → sidplayfp · ZoundMonitor / Richard Joseph: UADE |
| `.mus` | C64 Sidplayer: sidplayfp · Amiga UFO: UADE |
| `.st` | Sawteeth: `sawteethrender` · an Amiga module under a UADE prefix name (`jam.title.st`): UADE; any other `.st` is not listed |
| `.ksm` | Ken Silverman's AdLib music: AdPlug · Kefrens Sound Machine: UADE |
| `.fred` | Fred Editor modules: UADE · songs saved in the Fred Editor's own format (`Fred Editor ` header): not listed |
| `.dsm` | early DSIK (`DSM\x10`): `milkyrender` · RIFF DSIK: libopenmpt |
| `.ptm` | ProTracker IFF and PolyTracker: both libopenmpt |
| `.dmf` | DefleMask: Furnace · X-Tracker, DSMI Compact and Software Visions: libopenmpt |
| `.dtm`, `.psm`, `.xmf` | Digital Tracker, Epic MegaGames MASI, Astroidea XMF: libopenmpt · DeFy AdLib Tracker, ZX Spectrum Pro Sound Maker, Extensible Music Format: not played |
| `.med` | OctaMED (MMD0–MMD3): libopenmpt · MED 2–4: ZXTune |

## Archives and disk images

These hold tunes; the tunes inside play through the players above.

| Container | Suffixes | Read by |
|---|---|---|
| ZIP (a ZIP inside a ZIP too) | `.zip` | SoniqBoom |
| LHA / LZH | `.lha`, `.lzh` | SoniqBoom; `lha` (lhasa) for the methods it doesn't decode |
| Commodore disk images (their SID tunes) | `.d64`, `.d71`, `.d81` | SoniqBoom |
| Amiga disk images (their modules) | `.adf` | SoniqBoom |
| XPK SQSH, PowerPacker packed modules | (any module suffix) | SoniqBoom, before the module's player |

## Builds without some players

| Build | Left out | Formats that don't play |
|---|---|---|
| Docker `--build-arg SLIM=1` | UADE, sc68 | AHX, the Amiga custom players, `.sc68` |
| `install.sh --slim` | ZXTune | PSF family (except Dreamcast rips `dsfrender` plays), S98, Farbrausch V2, MED 2–4, VGM using chips libgme lacks |

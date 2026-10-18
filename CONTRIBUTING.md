# Contributing to SoniqBoom

Thanks for wanting to get involved. SoniqBoom is a self-hosted music server with
a particular obsession: it plays the formats other servers gave up on — SID,
tracker modules, Amiga and Atari chiptunes, console rips, AdLib, MIDI, DSD —
alongside every mainstream lossless and lossy codec.

**How contribution works here.** SoniqBoom is a single-maintainer project, and
the code is written and maintained by me. I'm **not taking code contributions
(pull requests)** — but the door is very much open: the most
valuable way to shape SoniqBoom is through the GitHub channels below, and they
genuinely do. Bug reports, format requests, and feature
ideas are all warmly welcome and all read.

## Ways to take part

- **Report a bug.** Open an [Issue](https://github.com/SFCyris/SoniqBoom/issues)
  with the SoniqBoom version (gear → **System** → **About** for admins, or
  **Settings** on the phone layout), how you run it (Docker / bare-metal, OS,
  CPU arch), and the exact steps. The matching lines from the log help too
  (admins: gear → **Log**; the file is `log/soniqboom.log` in SoniqBoom's data
  folder, `/data/log/soniqboom.log` in Docker). For a format that won't play,
  include the file's extension, the renderer and version that
  gear → **Renderers → Renderer Status** shows for it (admins), and, if you
  can, a short sample.
- **Request a format or feature.** Start a thread in
  [Discussions → Ideas](https://github.com/SFCyris/SoniqBoom/discussions/categories/ideas).
  That's where directions get debated and requests are upvoted, so the
  most-wanted ones rise to the top — it's the single biggest influence on what
  I work on. For a retro format, a reference recording helps me check the
  behaviour.
- **Ask, share, compare notes.**
  [Discussions](https://github.com/SFCyris/SoniqBoom/discussions) is the place
  for setups, tips, and "is this a bug or just me?".
- **Flag a docs gap.** If the in-app manual, `README.md`, or `DEPLOY.md` is
  missing or wrong, open an issue and I'll fix it.

What I work on is driven by what's upvoted, what's broken, and what I have
time for. No dated promises — but everything above lands in front of me and
shapes the project.

## Project status

SoniqBoom is actively developed and cuts tagged releases — each `v*` tag builds
the source archive and the multi-arch Docker image. It's a small,
single-maintainer project: its direction is shaped by the channels above and the
code is built and maintained by me.

## Licence & forking

SoniqBoom is **AGPL-3.0-or-later** — see [`LICENSE`](LICENSE). You're free to
run, study, and self-host it, and to fork and modify your own copy under those
terms. The upstream codebase here is maintained by me, but tinkering on your own
fork is very much in the spirit of the project.

## Security

Please **do not** open a public issue for a security problem. Report it
privately via GitHub's **Security → Report a vulnerability** on this repository.

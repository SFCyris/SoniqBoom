# Contributing to SoniqBoom

Thanks for wanting to help. SoniqBoom is a self-hosted music server with a
particular obsession: it plays the formats other servers gave up on — SID,
tracker modules, Amiga and Atari chiptunes, console rips, AdLib, MIDI, DSD —
alongside every mainstream lossless and lossy codec. Contributions that make
that work better, on more hardware, for more people, are very welcome.

## Project status & roadmap

SoniqBoom is actively developed and cuts tagged releases — each `v*` tag builds
the source archive and the multi-arch Docker image. It's a small project, so the
honest picture:

- **Feature ideas and the roadmap live in
  [Discussions → Ideas](https://github.com/SFCyris/SoniqBoom/discussions/categories/ideas)** —
  that's where directions are debated and requests are upvoted, so the
  most-wanted ones rise to the top. Start one there rather than guessing what's
  planned.
- **Bugs** go in [Issues](https://github.com/SFCyris/SoniqBoom/issues).
- What gets built next is driven by what's upvoted, what's broken, and what a
  contributor is willing to build. No dated promises.

## Ways to help

- **Report a bug.** Open an issue with the SoniqBoom version (`Settings → About`
  or the footer), how you run it (Docker / bare-metal, OS, CPU arch), and the
  exact steps. For a format that won't play, attach the file's extension and, if
  you can, a short sample.
- **Request a format or feature.** Say which player/format and, for retro
  formats, a reference recording so behaviour can be checked.
- **Send a pull request.** Small, focused PRs are easiest to review. If it's a
  large change, open an issue first so we can agree on the approach before you
  write it.
- **Improve the docs.** The in-app manual (`docs/manual/`), `README.md`, and
  `DEPLOY.md` all take fixes.

## Development setup

SoniqBoom is a **Python 3.11+ / FastAPI (asyncio)** backend with a **vanilla-JS
single-page app** — there is **no frontend build step**, so you edit
`soniqboom/frontend/**` and reload.

```bash
git clone https://github.com/SFCyris/SoniqBoom.git
cd SoniqBoom
bash install.sh          # installs Python, ffmpeg, and the retro-format players
bash run.sh --port 8080  # start the dev server
# stop / restart:  bash shutdown.sh  ·  bash restart.sh
```

- Python changes require a server restart; frontend files are served fresh from
  disk on reload.
- Renderers are external binaries the server shells out to (see
  `docs/manual/` and the format table in `README.md`). The Docker image builds
  them from source in a multi-stage build; on bare metal `install.sh` fetches or
  compiles them.

## Tests

```bash
.venv/bin/python -m pytest tests/          # the suite lives in tests/
```

Please add or update a test with any behavioural change. For a renderer change,
include (or point at) a small sample file that exercises it.

## Coding style

- **Backend:** match the surrounding code — type hints on new functions, small
  focused modules under `soniqboom/`. Keep the audio path non-blocking (use
  `asyncio.to_thread` for CPU/IO-bound work). Don't add a dependency without a
  clear reason; the runtime stays lean on purpose.
- **Frontend:** vanilla JS, no framework, no bundler. Match the existing module
  style in `soniqboom/frontend/js/`.
- **Comments** explain *why*, not *what*. State constraints the code can't.

## Pull-request checklist

- [ ] Focused scope; the diff does one thing.
- [ ] Tests pass and cover the change.
- [ ] No new hard dependency without discussion.
- [ ] Docs/manual updated if user-facing behaviour changed.
- [ ] You have the right to submit the code under the project licence.

## Licence

SoniqBoom is **AGPL-3.0-or-later**. By contributing you agree your contribution
is licensed under the same terms. See [`LICENSE`](LICENSE).

## Security

Please **do not** open a public issue for a security problem — see
[`SECURITY.md`](SECURITY.md) for how to report it privately.

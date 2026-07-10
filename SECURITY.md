# Security policy

## Reporting a vulnerability

Please report security issues **privately**, not in a public issue or pull
request.

Use GitHub's private vulnerability reporting on this repository:
**Security → Report a vulnerability**
(<https://github.com/SFCyris/SoniqBoom/security/advisories/new>).

Include, as best you can:

- what the issue is and the impact (what an attacker could do),
- the SoniqBoom version and how you run it (Docker / bare-metal, OS, arch),
- steps or a proof-of-concept to reproduce.

You'll get an acknowledgement, and a fix or mitigation plan once the report is
confirmed. Please give a reasonable window to release a fix before any public
disclosure.

## Supported versions

SoniqBoom is a single-maintainer project. Security fixes land on the **latest
release**; there is no long-term back-porting to older versions. Run a current
version (see the [releases page](https://github.com/SFCyris/SoniqBoom/releases))
to stay covered.

## Scope notes for self-hosters

SoniqBoom is designed to be run on your own hardware/LAN. A few things that are
your responsibility to configure, not bugs:

- **Exposure to the internet.** If you publish the server beyond your LAN, put it
  behind HTTPS and authentication you trust — see `deploy/docker-compose.https.yml`
  and `DEPLOY.md`. The app authenticates users but is not hardened for hostile
  public exposure by default.
- **Library file access.** The server reads the music folders and remote sources
  (SMB/FTP/WebDAV) you give it; scope those credentials to read-only where you
  can.
- **The `/data` volume** holds your users, sessions, and config — back it up and
  don't share it.

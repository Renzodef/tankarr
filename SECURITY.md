# Security policy

## Supported versions

Security fixes are merged into `main` and shipped in the next stable release.

| Version | Supported |
| --- | --- |
| Latest stable release (`latest`) | Yes |
| `main` (unreleased changes) | Yes |
| Older releases | No: please update |

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Report it
privately through GitHub:
[Security → Report a vulnerability](https://github.com/Renzodef/tankarr/security/advisories/new).

Include what an attacker can do, the steps to reproduce it, the version or image
tag you tested and, if you have one, a suggested fix. You will receive an
acknowledgement within a week. Once a fix is released, the advisory is published
with credit to you unless you prefer to stay anonymous.

Out of scope: problems in the third-party services Tankarr connects to
(Suwayomi and its extensions, Prowlarr, qBittorrent, SABnzbd, Komga, Kavita,
Stump), which should be reported to those projects, and attacks that require
an attacker who already controls the host or the Tankarr configuration folder.

## Running Tankarr safely

- **Keep authentication on.** Tankarr refuses to run without a login: set
  `TANKARR_AUTH_USERNAME` and `TANKARR_AUTH_PASSWORD`, or use the one it creates
  and prints to the log on first start, then change it under Settings → Security.
- **Do not expose port 8787 to the internet.** Use it on your local network or
  a VPN, or put it behind a reverse proxy with HTTPS.
- **Protect the configuration folder** (`/config`). It holds the database and,
  in `metadata.env`, the passwords and API keys of your integrations. Backup
  bundles contain the same secrets unencrypted: store them on trusted storage.
- **Treat the API key like the password.** `api-key` in `/config`, shown under
  Settings → Security, gives other applications the same access as the login.
  Regenerate it there if it leaks.
- **Update regularly**, and follow the release notes. The System page says when
  a newer release exists.
- Tankarr downloads and opens archives from the internet. It runs as an
  unprivileged user, extracts archives in subprocesses with memory, size and
  time limits, and never follows symbolic links from an archive, but keep the
  container isolated from data it does not need.

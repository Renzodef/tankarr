---
description: Install Tankarr, the self-hosted manga and comics manager, with Docker Compose or docker run on Linux, a NAS or a Raspberry Pi, or run it from source.
---

# Installation

Tankarr runs as a single Docker container. Images are published on the GitHub
Container Registry for `linux/amd64` and `linux/arm64`, so the same
instructions work on a Linux server, a NAS, a Raspberry Pi 4 or 5 and Docker
Desktop.

## Requirements

- Docker Engine 24 or newer with the Compose plugin, or any Docker-compatible
  runtime (Podman, Unraid, Synology Container Manager, TrueNAS apps).
- About 2 GB of disk space for the image.
- Memory: around 400 MB for Tankarr alone, about 1.5 GB when the managed
  Suwayomi engine is enabled (it runs a Java server inside the container).
- A folder for the library (your CBZ files) and a folder for Tankarr's own
  data. Both should be on local disks or a reliable NAS mount.

## Folders and ports

| Container path | Purpose | Required |
| --- | --- | --- |
| `/config` | Database, settings, secrets, metadata cache, managed Suwayomi runtime, backups and the recycle bin | Yes |
| `/library` | The manga and comics library Tankarr writes: one folder per series, CBZ files, covers | Yes |
| `/import` | Read-only drop folder for files you already own (**Library Import**) | No |
| `/downloads` | Read-only view of the folder where qBittorrent saves Tankarr's torrents | Only with qBittorrent |
| `/usenet` | Read-only view of the folder where SABnzbd completes Tankarr's downloads | Only with SABnzbd |

| Port | Purpose |
| --- | --- |
| `8787` | Web interface and API |
| `4567` | Web interface of the managed Suwayomi server. Publish it only if you want to open Suwayomi itself; it uses the Tankarr login. |

Tankarr writes the library as
`{Series} ({Authors})/{Series} - v{Volume} c{Chapter} [{Language}].cbz`, with a
`ComicInfo.xml` inside every archive and `cover.jpg` in every series folder.
Point your reader (Komga, Kavita, Stump...) at the same folder.

## Docker Compose (recommended)

Create a folder for Tankarr with this `docker-compose.yml`:

```yaml
services:
  tankarr:
    image: ghcr.io/renzodef/tankarr:latest
    container_name: tankarr
    environment:
      TZ: Etc/UTC
      # Optional: choose the login yourself (see Authentication below).
      # TANKARR_AUTH_USERNAME: ${TANKARR_AUTH_USERNAME}
      # TANKARR_AUTH_PASSWORD: ${TANKARR_AUTH_PASSWORD}
    volumes:
      - ./config:/config
      - /path/to/comics:/library
      - /path/to/import:/import:ro
      # Only if you use a download client; see "Download clients" below.
      # - /path/to/downloads/tankarr:/downloads:ro
      # - /path/to/usenet/complete:/usenet:ro
    ports:
      - "8787:8787"
    restart: unless-stopped
```

Start it and read the login that Tankarr creates on its first start:

```sh
docker compose up -d
docker compose logs tankarr | grep "created one"
```

Open `http://<your-server>:8787`, sign in, and change the password under
**Settings → Security**. Continue with [Getting started](getting-started.md).

The repository also contains a complete [`docker-compose.yml`](https://github.com/Renzodef/tankarr/blob/main/docker-compose.yml)
and [`.env.sample`](https://github.com/Renzodef/tankarr/blob/main/.env.sample)
that expose every common option as a variable. That file publishes the web
port on `127.0.0.1` only; set `TANKARR_BIND_ADDRESS=0.0.0.0` in `.env` to reach
Tankarr from other devices on your network.

## docker run

```sh
docker run -d --name tankarr \
  -e TZ=Etc/UTC \
  -v /srv/tankarr/config:/config \
  -v /path/to/comics:/library \
  -v /path/to/import:/import:ro \
  -p 8787:8787 \
  --restart unless-stopped \
  ghcr.io/renzodef/tankarr:latest
```

## Authentication

Tankarr never serves an installation without a login. On the first start
without credentials it creates the user `admin` with a random password, prints
both once to the log and keeps them in `/config/generated-login.json`
(readable only by the container user). To choose the login yourself, set both
`TANKARR_AUTH_USERNAME` and `TANKARR_AUTH_PASSWORD`, or save a login under
**Settings → Security**: either replaces the generated one. Setting only one of
the two variables stops Tankarr at startup on purpose. A login saved on the
Settings page takes precedence over the environment.

`TANKARR_AUTH_REQUIRED=false` allows an instance without credentials. It exists
for development machines that nothing else can reach; never use it elsewhere.

Failed sign-ins are logged with the client address and slowed down after five
attempts. Behind a reverse proxy, tell the server which proxy to trust so it
sees the real client address, for example
`FORWARDED_ALLOW_IPS=172.18.0.2` (the proxy's address, see the
[uvicorn documentation](https://www.uvicorn.org/settings/#http)).

The default method, `forms`, shows a login page with a signed, HTTP-only
session cookie and "Remember me", like the other *arr applications.
`TANKARR_AUTH_METHOD=basic` uses the browser's credential prompt instead. API
clients can always use HTTP Basic authentication. The method, user name and
password can be changed later from **Settings → Security**.

## File permissions

The container runs as user and group `1000:1000`, never as root. Tankarr must
be able to write to `/config` and `/library`, and to read `/import`,
`/downloads` and `/usenet`.

- If your folders belong to UID/GID 1000, nothing needs to be done.
- Otherwise run the container as the owner of your media, for example
  `user: "1001:100"` in Compose or `--user 1001:100` with `docker run`, and
  make sure that user can write the host folders:

```sh
sudo chown -R 1001:100 /srv/tankarr/config /path/to/comics
```

## Download clients

Tankarr hands torrent and Usenet releases to your existing qBittorrent and
SABnzbd and imports the finished files itself. Each client writes into its own
path; Tankarr reads the same files through its own read-only mount, the same
idea as the remote path mappings of Sonarr and Radarr:

| Client | The client writes to (its own path) | Tankarr reads from (container path) |
| --- | --- | --- |
| qBittorrent | `TANKARR_QBITTORRENT_SAVE_PATH`, for example `/data/downloads/tankarr` | `/downloads` (`TANKARR_TORRENT_DOWNLOAD_DIR`) |
| SABnzbd | `TANKARR_SABNZBD_COMPLETE_PATH`, for example `/data/downloads/usenet` | `/usenet` (`TANKARR_USENET_DOWNLOAD_DIR`) |

Mount the host folder behind the client's path into Tankarr at the container
path, and set the environment variable of that row:

```yaml
    environment:
      TANKARR_TORRENT_DOWNLOAD_DIR: /downloads
      TANKARR_USENET_DOWNLOAD_DIR: /usenet
    volumes:
      - /srv/data/downloads/tankarr:/downloads:ro
      - /srv/data/downloads/usenet:/usenet:ro
```

The addresses, credentials, categories and client paths are then entered in
**Settings → Indexers & torrents**; see [Integrations](integrations.md).

## Running next to Sonarr, Radarr and Prowlarr

If your *arr applications share a Docker network, add Tankarr to the same
network and refer to the other services by their service names, for example
`http://prowlarr:9696`, `http://qbittorrent:8080` and `http://sabnzbd:8080`.
Tankarr needs no inbound connection from them.

## Reverse proxy

Any reverse proxy works; forward the `Host` header and allow request bodies of
a few megabytes for cover uploads. On its own host name, a minimal Caddy
example:

```caddyfile
tankarr.example.com {
    reverse_proxy tankarr:8787
}
```

To publish Tankarr under a sub-path of a shared host name, like the other *arr
applications, set `TANKARR_URL_BASE=/tankarr` and restart. The interface then
answers at `https://home.example.com/tankarr/` and the API at
`/tankarr/api/...`; the proxy forwards the path unchanged, never stripping the
prefix:

```caddyfile
home.example.com {
    reverse_proxy /tankarr* tankarr:8787
}
```

```nginx
location /tankarr/ {
    proxy_pass http://tankarr:8787;
    proxy_set_header Host $host;
}
```

Outside the base only the health checks (`/api/health`, `/api/ready`) answer,
so the image's own health check keeps working without knowing it.

Do not expose port 8787 directly to the internet. Keep Tankarr on your local
network or VPN, or put it behind HTTPS with authentication enabled.

## Raspberry Pi and other small hosts

The `arm64` image runs on a 64-bit Raspberry Pi OS or any other 64-bit arm
distribution (32-bit systems are not supported). On a Raspberry Pi:

- give the container at least 1.5 GB of memory when the managed Suwayomi engine
  is enabled (`mem_limit: 1536m` in Compose), and keep swap enabled;
- keep `/config` on a disk rather than on the SD card: the database is written
  often;
- `TANKARR_DOWNLOAD_PIPELINE_MAX=0` (the default) lets Tankarr size its
  download concurrency from the live CPU, memory and temperature of the host.

## Building the image yourself

```sh
git clone https://github.com/Renzodef/tankarr.git
cd tankarr
docker build -t tankarr:local .
```

Then use `image: tankarr:local` in your Compose file. Multi-architecture builds
work with `docker buildx build --platform linux/amd64,linux/arm64 .`.

## Running from source

For development, or on a host without Docker. You need Python 3.12 or newer,
Node.js 22, and for full functionality Java 25 (managed Suwayomi), Tesseract
OCR, `bsdtar` (libarchive) or `unar` and Poppler.

```sh
git clone https://github.com/Renzodef/tankarr.git
cd tankarr
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
npm ci --prefix frontend
npm run build --prefix frontend
TANKARR_DATA_DIR=./data TANKARR_LIBRARY_DIR=/path/to/comics .venv/bin/tankarr
```

Tankarr listens on <http://localhost:8787>. The interactive API documentation
is served at `/docs` after you sign in.

## Updating

Pull the new image and recreate the container:

```sh
docker compose pull tankarr
docker compose up -d tankarr
```

Read [Upgrading](upgrading.md) for release channels, backups and rolling back.

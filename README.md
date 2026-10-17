# files

A tiny static file host for this machine's HDD drop box, plus a public,
no-auth **photo/file album system** at `/albums` (any file type).

Drop any file into **`/mnt/archive-new/files/`** and it is immediately reachable
at:

```
https://agentmaxxer.tail6ca935.ts.net/files/<filename>
```

(and, once the Vercel rewrite below is deployed, at
`https://files.maxwe.me/<filename>`).

> [!WARNING]
> **Anything placed in the served directory becomes public. Anything uploaded
> to an album is public too.**
> The directory is exposed on the internet through Tailscale Funnel. Album
> creation and uploads require **no login**. Do not put secrets, private keys,
> `.env` files, personal documents, or anything else you would not post
> publicly. Dotfiles (names starting with `.`) are hidden from listings and
> refused by the server, but that is *not* a confidentiality guarantee — treat
> the whole directory as public.

## What it is

* Pure **Starlette + uvicorn**, no FastAPI, no nginx/caddy.
* Binds **`127.0.0.1:8760` only**. Public access exists *solely* via Tailscale
  Funnel at `/files`.
* Serves one directory tree (`FILES_DIR`, default `/mnt/archive-new/files`).
* Correct `Content-Type` (from extension), `HEAD` support, and full **HTTP Range**
  support (single and multipart) via Starlette's `FileResponse`.
* Files are served **inline** (`Content-Disposition: inline`) so PDFs and images
  render in the browser instead of downloading; `X-Content-Type-Options: nosniff`.
* Minimal, styled directory index of non-hidden entries.
* Path-traversal protection: `..` segments and dotfiles are rejected per path
  component, and a realpath containment check stops symlinks from escaping the
  root.
* Honors `FILES_ROOT_PATH` (`/files`) so generated links are correct behind the
  Funnel path; a small `PrefixMiddleware` also accepts the prefixed form for
  direct access.
* Gracefully degrades when the archive HDD is unmounted (the `fstab` entry is
  `nofail`): the index explains the host is unavailable rather than crashing.

## Public albums (`/albums`)

Public shared albums, functionally like Google Photos shared albums: anyone can
create an album, anyone can upload **any file type**, anyone can view. There is
**no auth** and **no private album** — the UI says so on every page. Images,
video/audio, PDFs, Office documents, text/code and archives get in-browser
previews; unknown binaries download.

The GUI follows the Google Photos layout: a persistent left navigation rail on
desktop (`Photos`, `Albums`, `New album`, `Upload`), a top search app bar, a
date-grouped photo/video feed, album cards, Material-style dialogs, a
full-screen lightbox with prev/next, and a mobile bottom bar (`Photos`,
`Albums`, `Upload`, `New`). On phones the rail becomes a slide-out drawer; a
**New album** dialog is reachable from every page, and uploads can target any
album from the global **Upload** dialog. Album pages add an **Add photos**
button, camera capture, and drag-and-drop over the grid. Long-press a tile on
mobile (or use the hover circle on desktop) to enter Google Photos-style
selection mode, then **Hide** the selected items; files always stay on disk.
Search is available on both Photos (`?q=` by filename/album) and Albums
(`?q=` by title/description) views. The UI auto-detects iPhone/iPad and Android
(server-side User-Agent plus client fallback) and uses `Choose Photos & Files`
plus a camera capture input on iOS.

On iPhone/iPad the lightbox exposes **Save to Photos** (and the “save / download”
link routes images/videos through the same flow). This uses the Web Share API:
iOS opens the share sheet where **Save Image** / **Save Video** writes to the
photo library. A web page cannot silently write to Photos — that share-sheet
confirmation is the supported iOS path. Long-pressing the image and choosing
“Save to Photos” also works. Other file types still download to Files/Downloads.

```
https://agentmaxxer.tail6ca935.ts.net/files/albums
https://files.maxwe.me/albums          (existing Vercel rewrite; no change needed)
```

### Routes

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/albums` | Google Photos-style feed of photos/videos across all albums |
| `GET` | `/albums/albums` | album card grid (`/albums/photos` is an alias for the feed) |
| `GET` | `/albums/{album_id}` | album item grid, add photos, drag-drop, lightbox/player |
| `GET` | `/albums/asset/{asset_id}` | original (inline for safe media, attachment otherwise; HTTP Range) |
| `GET` | `/albums/thumb/{asset_id}` | 512 px JPEG thumbnail (images, docs, text, icons) |
| `GET` | `/albums/preview/{asset_id}` | raster/video/text preview (see below) |
| `POST` | `/albums` | create album (form; JSON also accepted) |
| `POST` | `/albums/{album_id}/upload` | multipart upload, no-JS fallback → 303 |
| `GET` | `/albums/api/albums` | list albums (JSON) |
| `POST` | `/albums/api/albums` | create album (JSON) |
| `GET` | `/albums/api/albums/{album_id}` | album + visible assets (JSON) |
| `POST` | `/albums/api/albums/{album_id}/upload` | multipart upload, field `files` (JSON) |
| `POST` | `/albums/api/albums/{album_id}/delete` | shallow-delete album |
| `POST` | `/albums/api/assets/{asset_id}/delete` | shallow-delete asset |
| `GET` | `/albums/api/stats` | counts, cache usage, configured limits |

`DELETE` is also accepted on the two `/delete` API routes.

### Storage layout

```
/mnt/archive-new/files/albums/<album_id>/<asset_id>.<ext>   canonical (HDD)
~/Projects/files/data/cache/<album_id>/<asset_id>.<ext>     SSD cache (LRU)
~/Projects/files/data/thumbs/<asset_id>.jpg                 SSD thumbnails (all types)
~/Projects/files/data/previews/<asset_id>.*                 raster/video/text preview cache
~/Projects/files/data/tmp/                                  multipart spool + *.part
~/Projects/files/data/albums.sqlite3                        metadata (WAL)
~/Projects/files/data/notifications.log                     email fallback log
```

* Uploads are streamed into the SSD cache, inserted into SQLite, and answered
  immediately. A background worker copies each file to the HDD, verifies its
  SHA-256, then sets `on_hdd=1`.
* **Any file type is accepted.** Pillow-verified images (JPEG/PNG/WebP/GIF/BMP/
  ICO/AVIF and HEIC/HEIF via `pillow-heif`) are served inline with a JPEG
  thumbnail. Common video/audio containers (`.mp4`, `.mov`, `.webm`, `.mp3`,
  `.m4a`, …) are served inline for the lightbox. Everything else (PDF, ZIP,
  HTML, SVG, text, unknown binaries) is served as
  `application/octet-stream` + `Content-Disposition: attachment` with
  `X-Content-Type-Options: nosniff`, so uploads can never execute as HTML/JS on
  this origin. The original file stays on disk either way.
* Assets are served from the SSD copy while it exists, then from the HDD.
* The cache is LRU-bounded (`ALBUMS_CACHE_BUDGET_BYTES`, 6 GiB by default and
  additionally capped at half of free space). A cache file is **only** evicted
  after `on_hdd=1` and after re-checking that the HDD file exists with the same
  size. If the SSD is full, the upload is written directly to the HDD instead.
* A 30-second maintenance loop retries pending copies (including after a
  restart), evicts cache/thumbnails, and removes stale `*.part` staging files.
* The static host **refuses** `/albums/...` raw filesystem paths and hides the
  `albums/` directory from its root listing, so `/albums/asset/<id>` is the only
  way to fetch an album file. This keeps shallow deletion meaningful.

### Previews

Previews are generated lazily and cached on the SSD (2 GiB LRU preview budget;
thumbnails are a separate 512 MiB budget). The original file is **never**
modified; previews are disposable derivatives.

* **Images**: JPEG/MPO (iPhone HDR)/PNG/WebP/GIF/BMP/ICO/AVIF are served inline
  directly. HEIC/
  HEIF (`pillow-heif`), TIFF, JPEG XL (`pillow-jxl-plugin`), PSD and camera RAW
  (`rawpy`: DNG/CR2/CR3/NEF/ARW/RAF/ORF/RW2/…) get a server-rendered JPEG
  preview, so Chrome/Safari/Android all show something even when the browser
  cannot decode the original. SVG is rasterised with CairoSVG and the original
  is always an attachment (never inline markup). Every image gets a JPEG
  thumbnail.
* **Video**: browser-safe H.264/VP8/VP9/AV1 in MP4/M4V/WebM plays directly with
  Range support. HEVC/H.265, MOV, MKV, AVI, MPEG-4 and other containers are
  transcoded to a 720p H.264/AAC MP4 preview (cached, Range-capable) on first
  view. Poster frames are extracted for thumbnails.
* **Documents**: PDFs and Office/ODF documents (DOC/DOCX/ODT/RTF, XLS/XLSX/
  ODS, PPT/PPTX/ODP, EPUB) are converted to PDF with headless LibreOffice when
  needed, then the **first page** is rendered to JPEG for grid + lightbox with a
  “Download original” link. Text is extracted with `pdftotext` when useful.
* **Text-like files**: TXT/MD/RST/CSV/TSV/JSON/XML/YAML/TOML/code/HTML/CSS/JS/
  shell/SQL/… show the first 256 KiB as escaped monospace text. JSON is
  pretty-printed when valid. HTML/SVG are **not** rendered as markup.
* **Archives**: ZIP/TAR/TGZ listing (first 200 entries with sizes). GZ/BZ2/XZ
  show the compressed name/size.
* **Audio**: MP3/M4A/AAC/WAV/OGG/OPUS/FLAC play inline in the lightbox.
* **Anything else**: generated extension icon, size/type panel and a download
  button.

External converters (`ffmpeg`/`ffprobe`, `pdftoppm`/`pdftotext`, LibreOffice)
run under **bubblewrap** when available: no network namespace, read-only
`/usr`/`/lib`/`/etc`, only the one source file exposed, a writable temp work
dir, and CPU/memory/file-size timeouts. Pillow/CairoSVG/rawpy run in-process.

SQLite tables: `albums`, `assets` (paths, size, MIME, sha256, dimensions,
`on_hdd`, `deleted`, `notified`), `upload_events` (persisted rate-limit bytes),
`notifications` (delivery attempts), `meta`.

### Shallow deletion only

Deleting an asset or album sets `deleted=1` (and `deleted_at`) in SQLite.
Deleted rows disappear from the UI/API (404). **No code path unlinks a canonical
file on the HDD or in the cache.** The only unlinks are staging `*.part` files,
SSD cache copies already verified on the HDD, and regenerable thumbnails.

### Limits, validation and abuse controls

* Global sliding window: **10 GiB / 5 minutes** across all uploads
  (`ALBUMS_RATE_LIMIT_BYTES`, `ALBUMS_RATE_WINDOW_SECONDS`). Events live in
  SQLite, so a restart does not reset the window. Rejections are `429` with
  `Retry-After`.
* Per-IP window: **2 GiB / 5 minutes** (`ALBUMS_PER_IP_RATE_BYTES`). The client
  IP is the first `X-Forwarded-For` hop when present (best-effort; headers are
  spoofable and this is an abuse brake, not authentication).
* Per file: **1 GiB** (`ALBUMS_MAX_FILE_BYTES`); per request: **50 files**
  (`ALBUMS_MAX_FILES`) and **2 GiB** body (`ALBUMS_MAX_REQUEST_BYTES`). These
  are sized so short phone videos work; the global/per-IP windows still apply.
* **All file types are accepted.** Image/video/audio previews are described
  above; PDFs/Office/text/archives get previews; everything else is an opaque
  download. Active content (HTML/SVG/scripts) is never served inline from this
  origin. A text file renamed `.jpg` becomes a generic attachment, not an inline
  image.
* Uploaded filenames are display-only: directory components and control
  characters are stripped, and paths are built only from UUID album/asset ids.
  Album titles/descriptions and filenames are HTML-escaped server-side; the UI
  uses `textContent` and sets a restrictive CSP.
* Path traversal is blocked by id validation, realpath containment checks, and
  the static host's existing per-component checks.
* `X-Forwarded-For` is only an additional brake; the global window is the hard
  cap.

Other tunables: `ALBUMS_DATA_DIR`, `ALBUMS_HDD_DIR`, `ALBUMS_HDD_MOUNT`,
`ALBUMS_CACHE_DIR`, `ALBUMS_DB_PATH`, `ALBUMS_THUMB_DIR`, `ALBUMS_TMP_DIR`,
`ALBUMS_CACHE_BUDGET_BYTES`, `ALBUMS_THUMB_BUDGET_BYTES`,
`ALBUMS_MIN_FREE_BYTES` (refuse uploads below this much free HDD, 5 GiB),
`ALBUMS_CREATE_MAX_BODY_BYTES`, `ALBUMS_THUMB_MAX_PX`, `ALBUMS_NOTIFY_ENABLED`,
`ALBUMS_PREVIEW_DIR`, `ALBUMS_PREVIEW_BUDGET_BYTES`,
`ALBUMS_PREVIEW_IMAGE_MAX_PX`, `ALBUMS_PREVIEW_VIDEO_HEIGHT`,
`ALBUMS_PREVIEW_VIDEO_TIMEOUT_SECONDS`, `ALBUMS_PREVIEW_DOC_TIMEOUT_SECONDS`,
`ALBUMS_PREVIEW_TEXT_BYTES`, `ALBUMS_PREVIEW_SANDBOX`.

On a large video (>50 MB) the share file is fetched on demand; if iOS drops the
user-activation window during the download, the button changes to **Tap Save
again** and the second tap opens the share sheet immediately with the cached file.

### Email notifications

When enabled, every successful upload enqueues a best-effort notification to
`maxwe@cmu.edu` (`ALBUMS_NOTIFY_TO`). Delivery runs on a dedicated background
worker and never delays the upload response.

**Current state: email is disabled** (`ALBUMS_NOTIFY_ENABLED=0`), so uploads
send nothing and write nothing to `notifications.log`; `assets.notified` stays
`0`. Set `ALBUMS_NOTIFY_ENABLED=1` to turn it back on. With the flag enabled and
no SMTP, notifications are appended as JSON lines to
`~/Projects/files/data/notifications.log`, the `notifications` row is `logged`,
and `assets.notified` stays `0` (local fallback).

To enable email, put these in **`~/Projects/files/.env`** (run.sh sources it;
the file is gitignored), ensure `ALBUMS_NOTIFY_ENABLED=1`, and restart
`files.service`:

```bash
ALBUMS_NOTIFY_ENABLED=1
ALBUMS_SMTP_HOST=smtp.example.com
ALBUMS_SMTP_PORT=587
ALBUMS_SMTP_USER=your-smtp-user
ALBUMS_SMTP_PASS=your-smtp-password
ALBUMS_SMTP_FROM=files@maxwe.me
ALBUMS_SMTP_STARTTLS=1     # 587/STARTTLS; set ALBUMS_SMTP_SSL=1 for implicit TLS (465)
ALBUMS_NOTIFY_TO=maxwe@cmu.edu
```

If SMTP is unset, an optional Resend-compatible HTTP fallback is used when a
key is present:

```bash
ALBUMS_RESEND_API_KEY=re_...
# optional: ALBUMS_EMAIL_API_URL=https://api.resend.com/emails
```

Delivery precedence: SMTP → HTTP API → local log. SMTP/HTTP failures are also
logged locally and leave `notified=0`; successful sends set `notified=1`.

## Layout

```
app.py                       ASGI app (Starlette): static host + album routes
albums.py                    album routes, lifespan, background workers
albums_store.py              SQLite schema/CRUD, rate window, copy/evict, thumbs
albums_storage.py            streaming ingest + image validation
albums_preview.py            thumbnails, image/document/video/text previews
albums_notify.py             SMTP/HTTP/local notification delivery
albums_ui.py                 server-rendered HTML/CSS/JS
albums_config.py             ALBUMS_* environment configuration
requirements.txt             starlette + uvicorn + python-multipart + Pillow + HEIC/JXL/RAW/Cairo
data/                        gitignored DB, cache, thumbs, tmp, notification log
scripts/run.sh               exec .venv/bin/uvicorn app:asgi_app
scripts/install-service.sh   write + enable + start /etc/systemd/system/files.service
deploy/vercel.json           rewrite for files.maxwe.me (owner deploys on Vercel)
```

## Install

```bash
cd ~/Projects/files
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # starlette, uvicorn, multipart, Pillow, HEIC/JXL/RAW, Cairo
scripts/install-service.sh          # needs passwordless sudo
systemctl is-active files.service
```

Preview converters (system packages):

```bash
sudo apt-get install -y ffmpeg poppler-utils bubblewrap \
  libreoffice-writer libreoffice-calc libreoffice-impress
```

The unit runs as `max`, `Restart=on-failure`, `WantedBy=multi-user.target`, and
sets `FILES_DIR=/mnt/archive-new/files`, `FILES_ROOT_PATH=/files`,
`FILES_HOST=127.0.0.1`, `FILES_PORT=8760`.

```bash
# (re)create the HDD drop box (owned by max)
sudo install -d -o max -g max /mnt/archive-new/files
```

## Tailscale Funnel

Funnel terminates TLS on the `*.ts.net` name and forwards to loopback. Mount the
service at `/files` (preserving the existing `/`, `/music`, `/notepad` mounts):

```bash
sudo tailscale funnel --bg --yes --https=443 --set-path=/files http://127.0.0.1:8760
tailscale funnel status
```

Expected:

```
https://agentmaxxer.tail6ca935.ts.net (Funnel on)
|-- /        proxy http://127.0.0.1:8720
|-- /music   proxy http://127.0.0.1:8740
|-- /notepad proxy http://127.0.0.1:8750
|-- /files   proxy http://127.0.0.1:8760
```

Tailscale **strips the `/files` prefix** before forwarding, so the app receives
`/` for an external `/files` request. `FILES_ROOT_PATH=/files` only affects the
links the app generates (so a browser clicking a listing entry hits the right
public URL).

## Custom domain — Vercel rewrite (owner action)

`files.maxwe.me` is served by a Vercel project whose only job is to proxy to the
Funnel URL. `deploy/vercel.json`:

```json
{
  "rewrites": [
    { "source": "/(.*)", "destination": "https://agentmaxxer.tail6ca935.ts.net/files/$1" }
  ]
}
```

Steps for the owner: create/point the Vercel project, add `files.maxwe.me` as a
domain, deploy the `deploy/vercel.json` above. This mirrors `music.maxwe.me` and
`notepad.maxwe.me`.

> **Confirmed 2026-10-17:** `https://files.maxwe.me/albums` and
> `https://files.maxwe.me/albums/api/...` work through this rewrite. **No Vercel
> change is needed for the album feature.**

### Range caveat (important for large media)

The **Funnel URL itself supports HTTP Range** — `curl -r 0-99` returns `206`.
Vercel's rewrite proxy **may not forward the `Range` header**, in which case
large media served through `files.maxwe.me` might not seek/stream properly and
could be read in full. For large audio/video, prefer the direct Funnel URL:

```
https://agentmaxxer.tail6ca935.ts.net/files/<filename>
```

## Verification

```bash
# put a test file in the drop box
echo 'hello files' > /mnt/archive-new/files/_probe.txt

# local: headers (should be 200, content-type text/plain)
curl -sI http://127.0.0.1:8760/_probe.txt
# local: Range (should print 5 for bytes 0-4)
curl -s -r 0-4 http://127.0.0.1:8760/_probe.txt | wc -c
# public via Funnel (should be 200)
curl -s -o /dev/null -w '%{http_code}\n' https://agentmaxxer.tail6ca935.ts.net/files/_probe.txt
tailscale funnel status

rm /mnt/archive-new/files/_probe.txt
```

### Album verification

```bash
# create an album (JSON)
ALBUM=$(curl -s -X POST http://127.0.0.1:8760/albums/api/albums \
  -H 'Content-Type: application/json' \
  -d '{"title":"test album"}' | jq -r .album.id)

# upload two images; response is JSON, file is already in the SSD cache
curl -s -X POST "http://127.0.0.1:8760/albums/api/albums/$ALBUM/upload" \
  -F "files=@a.jpg;type=image/jpeg" -F "files=@b.jpg;type=image/jpeg" | jq .

# wait for the background copy, then check both copies + DB
find /mnt/archive-new/files/albums/$ALBUM -type f
find ~/Projects/files/data/cache/$ALBUM -type f
sqlite3 ~/Projects/files/data/albums.sqlite3 \
  "select original_name,on_hdd,deleted from assets where album_id='$ALBUM'"

# Range + thumbnail
curl -s -r 0-99 -o /dev/null -w '%{http_code}\n' \
  "http://127.0.0.1:8760/albums/asset/<asset_id>"          # 206
curl -s -o /dev/null -w '%{http_code}\n' \
  "http://127.0.0.1:8760/albums/thumb/<asset_id>"          # 200 image/jpeg

# shallow delete: 404 afterwards, but the file is still on disk
curl -s -X POST "http://127.0.0.1:8760/albums/api/assets/<asset_id>/delete"
test -f /mnt/archive-new/files/albums/$ALBUM/<asset_id>.jpg && echo "file kept"

# public
curl -s -o /dev/null -w '%{http_code}\n' \
  https://agentmaxxer.tail6ca935.ts.net/files/albums       # 200
```

## Related

Nightly maintenance (`~/Projects/tools/nightly/`) restarts `files.service`
together with `board`/`agent-admin`/`moosic` so newly deployed code goes live;
`files.service` is already in the restart allowlist. Machine records:
`~/INSTALLED.md`, `~/REPRODUCE.md`.

# files

A tiny read-only static file host for this machine's HDD drop box.

Drop any file into **`/mnt/archive-new/files/`** and it is immediately reachable
at:

```
https://agentmaxxer.tail6ca935.ts.net/files/<filename>
```

(and, once the Vercel rewrite below is deployed, at
`https://files.maxwe.me/<filename>`).

> [!WARNING]
> **Anything placed in the served directory becomes public.**
> The directory is exposed on the internet through Tailscale Funnel. Do not put
> secrets, private keys, `.env` files, personal documents, or anything else you
> would not post publicly. Dotfiles (names starting with `.`) are hidden from
> listings and refused by the server, but that is *not* a confidentiality
> guarantee — treat the whole directory as public.

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

## Layout

```
app.py                       ASGI app (Starlette)
requirements.txt             starlette + uvicorn
scripts/run.sh               exec .venv/bin/uvicorn app:asgi_app
scripts/install-service.sh   write + enable + start /etc/systemd/system/files.service
deploy/vercel.json           rewrite for files.maxwe.me (owner deploys on Vercel)
```

## Install

```bash
cd ~/Projects/files
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
scripts/install-service.sh          # needs passwordless sudo
systemctl is-active files.service
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

## Related

Nightly maintenance (`~/Projects/tools/nightly/`) restarts `files.service`
together with `board`/`agent-admin`/`moosic` so newly deployed code goes live.
Machine records: `~/INSTALLED.md`, `~/REPRODUCE.md`.

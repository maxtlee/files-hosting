"""files — tiny static file host + public photo albums.

Serves a single directory tree (default /mnt/archive-new/files) over loopback,
and a public no-auth album system under /albums (see albums.py).
Public access is handled by Tailscale Funnel at /files; this app binds
127.0.0.1 only and never exposes anything on its own.

Design notes
------------
* Pure Starlette (no FastAPI): Starlette Routes auto-add HEAD to GET routes and
  its FileResponse implements HTTP Range (single + multipart), ETag/Last-Modified
  and conditional requests out of the box.
* Path traversal is blocked twice: per-component rejection of ``..``/dotfiles and
  a realpath containment check (so symlinks cannot escape the root either).
* Files are served inline (never as an attachment) with a guessed content type.
* ``FILES_ROOT_PATH`` (e.g. ``/files``) prefixes generated links so the same app
  works both directly and behind the Funnel path. Funnel strips the prefix before
  forwarding, but the PrefixMiddleware also accepts the prefixed form for direct
  access.
"""

from __future__ import annotations

import asyncio
import html
import json
import mimetypes
import os
import tempfile
import urllib.parse
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route

import albums_config as cfg
import publish as publish_mod
from albums import lifespan as albums_lifespan
from albums import routes as albums_routes

# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #

ROOT = Path(os.environ.get("FILES_DIR", "/mnt/archive-new/files")).resolve()
PREFIX = os.environ.get("FILES_ROOT_PATH", "").rstrip("/")
# Cache-Control for served files (seconds). 0/empty disables the header.
try:
    FILE_MAX_AGE = int(os.environ.get("FILES_CACHE_SECONDS", "300"))
except ValueError:
    FILE_MAX_AGE = 300

# Folder archive downloads (never buffered): caps are checked from metadata
# before the stream/temp file starts, so oversized folders fail fast.
try:
    ARCHIVE_MAX_ENTRIES = int(os.environ.get("FILES_ARCHIVE_MAX_ENTRIES", "100000"))
except ValueError:
    ARCHIVE_MAX_ENTRIES = 100000
try:
    ARCHIVE_MAX_BYTES = int(
        os.environ.get("FILES_ARCHIVE_MAX_BYTES", str(20 * 1024**3))
    )
except ValueError:
    ARCHIVE_MAX_BYTES = 20 * 1024**3
try:
    ARCHIVE_MAX_CONCURRENT = max(
        1, int(os.environ.get("FILES_ARCHIVE_MAX_CONCURRENT", "2"))
    )
except ValueError:
    ARCHIVE_MAX_CONCURRENT = 2

_archive_sem = asyncio.Semaphore(ARCHIVE_MAX_CONCURRENT)

# Try to make the served dir if missing (it lives on a `nofail` HDD, so this can
# legitimately fail when the disk is unmounted — we degrade gracefully below).
try:
    ROOT.mkdir(parents=True, exist_ok=True)
except OSError as exc:  # pragma: no cover - depends on host state
    print(f"[files] cannot create {ROOT}: {exc}", flush=True)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _quote_rel(rel: str) -> str:
    """URL-quote a root-relative path, keeping ``/`` separators."""
    parts = [urllib.parse.quote(p) for p in rel.strip("/").split("/") if p]
    return "/".join(parts)


def public_url(rel: str = "", is_dir: bool = False) -> str:
    """Absolute URL path for a root-relative entry, honoring PREFIX."""
    rel = _quote_rel(rel)
    path = f"{PREFIX}/{rel}" if rel else f"{PREFIX}/"
    if is_dir and not path.endswith("/"):
        path += "/"
    return path or "/"


def safe_resolve(rel: str) -> Optional[Path]:
    """Resolve ``rel`` under ROOT or return None if it tries to escape.

    ``rel`` is the already percent-decoded request path relative to the root.
    Rejects NUL bytes, ``..`` segments and any dotfile component; then verifies
    the realpath stays inside ROOT (catches symlink escapes).
    """
    if "\x00" in rel:
        return None
    for part in rel.split("/"):
        if part in ("", "."):
            continue
        if part == ".." or part.startswith("."):
            return None
    try:
        candidate = (ROOT / rel).resolve()
    except (OSError, RuntimeError):
        return None
    if candidate != ROOT and ROOT not in candidate.parents:
        return None
    return candidate


def _human_size(n: int) -> str:
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < step or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= step
    return f"{n:.1f} TB"


def _resp(request: Request, body: str, status: int = 200,
          content_type: str = "text/plain; charset=utf-8") -> Response:
    """Response helper that honors HEAD (headers only, no body)."""
    raw = body.encode("utf-8")
    headers = {
        "content-type": content_type,
        "content-length": str(len(raw)),
        "cache-control": "no-cache",
    }
    if request.method == "HEAD":
        return Response(status_code=status, headers=headers)
    return Response(content=raw, status_code=status, headers=headers)


def _error(request: Request, status: int, message: str) -> Response:
    return _resp(request, f"{status} {message}\n", status=status)


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# machine-readable listings (curl / scripts)
# --------------------------------------------------------------------------- #

def _listing_json(
    request: Request, target: Path, rel: str, include_hidden: bool
) -> Response:
    """JSON directory listing: ``{path, entries:[{name,type,size,mtime,url}]}``."""
    rel = rel.strip("/")
    if target.is_file():
        try:
            st = target.stat()
        except OSError as exc:
            return _error(request, 403, f"cannot stat: {exc}")
        entries = [{
            "name": target.name,
            "type": "file",
            "size": st.st_size,
            "mtime": int(st.st_mtime),
            "url": public_url(rel),
        }]
        data = {"path": rel, "entries": entries}
        return _resp(
            request,
            json.dumps(data, ensure_ascii=False) + "\n",
            content_type="application/json; charset=utf-8",
        )
    if not target.is_dir():
        return _error(request, 404, "Not Found")

    try:
        raw = list(os.scandir(target))
    except OSError as exc:
        return _error(request, 403, f"cannot read directory: {exc}")

    entries = []
    for e in raw:
        if e.name.startswith(".publish-"):
            continue
        if not include_hidden and e.name.startswith("."):
            continue
        # Match the HTML index: the album tree is only reachable via /albums.
        if not rel and e.name == "albums":
            continue
        try:
            is_dir = e.is_dir()
            st = e.stat()
        except OSError:
            continue
        child_rel = f"{rel}/{e.name}" if rel else e.name
        entries.append({
            "name": e.name,
            "type": "dir" if is_dir else "file",
            "size": 0 if is_dir else st.st_size,
            "mtime": int(st.st_mtime),
            "url": public_url(child_rel, is_dir),
        })
    entries.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
    data = {"path": rel, "entries": entries}
    return _resp(
        request,
        json.dumps(data, ensure_ascii=False) + "\n",
        content_type="application/json; charset=utf-8",
    )


# --------------------------------------------------------------------------- #
# folder archives (tar.gz via `tar` subprocess; zip via a bounded temp file)
# --------------------------------------------------------------------------- #

def _collect_archive(
    target: Path, include_hidden: bool
) -> tuple[list[tuple[str, Path, bool, int]], int]:
    """List regular files/dirs for an archive, refusing symlinks.

    Returns ``(entries, total_bytes)`` where each entry is
    ``(arcname, path, is_dir, size)``. Hidden files/dirs are skipped unless
    ``include_hidden``; symlinks are always skipped (never followed).
    """
    base = target.name
    entries: list[tuple[str, Path, bool, int]] = [(base, target, True, 0)]
    total = 0
    for dirpath, dirnames, filenames in os.walk(target, followlinks=False):
        current = Path(dirpath)
        try:
            parent_rel = current.relative_to(target).as_posix()
        except ValueError:
            parent_rel = ""
        keep = []
        for name in sorted(dirnames):
            path = current / name
            if not include_hidden and name.startswith("."):
                continue
            try:
                if path.is_symlink():
                    continue
            except OSError:
                continue
            keep.append(name)
            arc = f"{base}/{name}" if parent_rel in ("", ".") else (
                f"{base}/{parent_rel}/{name}"
            )
            entries.append((arc, path, True, 0))
        dirnames[:] = keep

        for name in sorted(filenames):
            if not include_hidden and name.startswith("."):
                continue
            path = current / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                st = path.stat()
            except OSError:
                continue
            total += st.st_size
            arc = f"{base}/{name}" if parent_rel in ("", ".") else (
                f"{base}/{parent_rel}/{name}"
            )
            entries.append((arc, path, False, st.st_size))
            if len(entries) > ARCHIVE_MAX_ENTRIES:
                raise ValueError(
                    f"folder has too many entries (max {ARCHIVE_MAX_ENTRIES})"
                )
            if total > ARCHIVE_MAX_BYTES:
                raise ValueError(
                    f"folder is too large to archive "
                    f"(max {ARCHIVE_MAX_BYTES} bytes)"
                )

        if len(entries) > ARCHIVE_MAX_ENTRIES:
            raise ValueError(
                f"folder has too many entries (max {ARCHIVE_MAX_ENTRIES})"
            )
        if total > ARCHIVE_MAX_BYTES:
            raise ValueError(
                f"folder is too large to archive (max {ARCHIVE_MAX_BYTES} bytes)"
            )
    if len(entries) > ARCHIVE_MAX_ENTRIES:
        raise ValueError(f"folder has too many entries (max {ARCHIVE_MAX_ENTRIES})")
    if total > ARCHIVE_MAX_BYTES:
        raise ValueError(
            f"folder is too large to archive (max {ARCHIVE_MAX_BYTES} bytes)"
        )
    return entries, total


def _archive_headers(name: str, ext: str) -> dict:
    filename = f"{name}.{ext}"
    ascii_name = (
        filename.encode("ascii", "replace")
        .decode("ascii")
        .replace('"', "_")
        .replace("\r", "_")
        .replace("\n", "_")
    )
    encoded = urllib.parse.quote(filename, safe="")
    return {
        "content-disposition": (
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{encoded}"
        ),
        "x-content-type-options": "nosniff",
        "cache-control": "no-store",
    }


def _write_tar_manifest(entries: list[tuple[str, Path, bool, int]]) -> Path:
    cfg.TMP_DIR.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="archive-", suffix=".list.part", dir=cfg.TMP_DIR)
    with os.fdopen(fd, "wb") as fh:
        for arcname, _path, _is_dir, _size in entries:
            fh.write(os.fsencode(arcname))
            fh.write(b"\0")
    return Path(name)


async def _tar_stream(
    target: Path, entries: list[tuple[str, Path, bool, int]], manifest: Path
):
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "tar",
            "-czf", "-",
            "--directory", str(target.parent),
            "--null",
            "--no-recursion",
            "--ignore-failed-read",
            "--files-from", str(manifest),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        while True:
            chunk = await proc.stdout.read(64 * 1024)
            if not chunk:
                break
            yield chunk
        rc = await proc.wait()
        if rc != 0:
            print(f"[files] tar exited {rc} for {target}", flush=True)
    finally:
        try:
            manifest.unlink()
        except OSError:
            pass
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
        _archive_sem.release()


def _build_zip(
    entries: list[tuple[str, Path, bool, int]], tmp_path: Path
) -> None:
    with zipfile.ZipFile(
        tmp_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
    ) as zf:
        for arcname, path, is_dir, _size in entries:
            if is_dir:
                zf.writestr(arcname.rstrip("/") + "/", b"")
            else:
                zf.write(path, arcname)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


async def _serve_archive(
    request: Request, target: Path, rel: str, kind: str
) -> Response:
    if not rel.strip("/"):
        return _error(request, 403, "refusing to archive the root directory")
    if not target.is_dir():
        return _error(request, 404, "Not Found")

    # Never expose dotfiles in downloads, even via ?hidden=1. Direct dotfile
    # requests are refused by the static handler, so archives must match.
    try:
        entries, total = await asyncio.to_thread(
            _collect_archive, target, False
        )
    except ValueError as exc:
        return _error(request, 413, str(exc))

    name = target.name
    if kind == "zip":
        if request.method == "HEAD":
            return Response(
                media_type="application/zip",
                headers=_archive_headers(name, "zip"),
            )
        try:
            await asyncio.wait_for(_archive_sem.acquire(), timeout=0.05)
        except asyncio.TimeoutError:
            return _error(request, 429, "too many concurrent archive builds")
        tmp_path: Optional[Path] = None
        try:
            archive_dir = cfg.TMP_DIR / "archives"
            archive_dir.mkdir(parents=True, exist_ok=True)
            try:
                free = os.statvfs(archive_dir).f_bavail * os.statvfs(archive_dir).f_frsize
            except OSError:
                free = 0
            if free and total > free * 0.8:
                return _error(request, 507, "not enough temporary space for zip")
            fd, raw = tempfile.mkstemp(
                prefix="download-", suffix=".zip.part", dir=archive_dir
            )
            os.close(fd)
            tmp_path = Path(raw)
            await asyncio.to_thread(_build_zip, entries, tmp_path)
        except Exception as exc:
            print(f"[files] zip build failed for {target}: {exc}", flush=True)
            if tmp_path is not None:
                _unlink(tmp_path)
            return _error(request, 500, "could not build zip archive")
        finally:
            _archive_sem.release()
        return FileResponse(
            tmp_path,
            media_type="application/zip",
            headers=_archive_headers(name, "zip"),
            background=BackgroundTask(_unlink, tmp_path),
        )

    # tar.gz streams straight from a `tar` subprocess.
    if request.method == "HEAD":
        return Response(
            media_type="application/gzip",
            headers=_archive_headers(name, "tar.gz"),
        )
    try:
        await asyncio.wait_for(_archive_sem.acquire(), timeout=0.05)
    except asyncio.TimeoutError:
        return _error(request, 429, "too many concurrent archive builds")
    try:
        manifest = await asyncio.to_thread(_write_tar_manifest, entries)
    except Exception as exc:
        _archive_sem.release()
        print(f"[files] tar manifest failed for {target}: {exc}", flush=True)
        return _error(request, 500, "could not prepare tar archive")

    return StreamingResponse(
        _tar_stream(target, entries, manifest),
        media_type="application/gzip",
        headers=_archive_headers(name, "tar.gz"),
    )


# --------------------------------------------------------------------------- #
# directory listing
# --------------------------------------------------------------------------- #

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ color-scheme: light dark; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; padding: 1.5rem; font: 15px/1.5 system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }}
h1 {{ font-size: 1.15rem; margin: 0 0 .2rem; }}
p.sub {{ margin: 0 0 1rem; opacity: .65; font-size: .85rem; }}
table {{ border-collapse: collapse; width: 100%; max-width: 60rem; }}
th, td {{ text-align: left; padding: .35rem .6rem; border-bottom: 1px solid rgba(127,127,127,.25); }}
th {{ font-weight: 600; opacity: .65; font-size: .8rem; text-transform: uppercase; letter-spacing: .04em; }}
td.size, th.size {{ text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }}
td.mtime {{ white-space: nowrap; opacity: .7; font-size: .85rem; }}
a {{ text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.dir a {{ font-weight: 600; }}
.empty {{ opacity: .6; font-style: italic; padding: 1rem .6rem; }}
.warn {{ padding: 1rem; border: 1px solid rgba(200,120,0,.5); background: rgba(200,120,0,.1); border-radius: 6px; max-width: 60rem; }}
a.albums {{ display: inline-block; margin: 0 0 1rem; padding: .55rem .85rem; border: 1px solid rgba(127,127,127,.35); border-radius: 7px; font-weight: 600; }}
code {{ background: rgba(127,127,127,.15); padding: .1rem .3rem; border-radius: 4px; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def _listing(request: Request, target: Path, rel: str) -> Response:
    if not target.is_dir():
        return _error(request, 404, "Not Found")

    title = "/" + rel.strip("/") if rel.strip("/") else "/"
    rows = []
    try:
        entries = [
            e for e in os.scandir(target)
            if not e.name.startswith(".")
            # The album tree is served by /albums (with DB-backed visibility);
            # hide the raw directory from the static index so hidden/deleted
            # items cannot be discovered by browsing.
            and not (not rel.strip("/") and e.name == "albums")
        ]
    except OSError as exc:
        return _resp(request, f"403 cannot read directory: {exc}\n", status=403)

    def sort_key(e):
        try:
            is_dir = e.is_dir()
        except OSError:
            is_dir = False
        return (not is_dir, e.name.lower())

    entries.sort(key=sort_key)

    if rel.strip("/"):
        parent = rel.strip("/").rsplit("/", 1)[0] if "/" in rel.strip("/") else ""
        rows.append(
            f'<tr class="dir"><td><a href="{html.escape(public_url(parent, True))}">../</a></td>'
            f'<td class="size">—</td><td class="mtime"></td></tr>'
        )

    for e in entries:
        try:
            is_dir = e.is_dir()
            st = e.stat()
        except OSError:
            continue
        child_rel = f"{rel.strip('/')}/{e.name}" if rel.strip("/") else e.name
        link = html.escape(public_url(child_rel, is_dir))
        name = html.escape(e.name) + ("/" if is_dir else "")
        size = "—" if is_dir else _human_size(st.st_size)
        mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
        cls = ' class="dir"' if is_dir else ""
        rows.append(
            f'<tr{cls}><td><a href="{link}">{name}</a></td>'
            f'<td class="size">{size}</td><td class="mtime">{mtime}</td></tr>'
        )

    if rows:
        table = (
            "<table><thead><tr><th>Name</th><th class='size'>Size</th>"
            "<th>Modified</th></tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )
    else:
        table = '<div class="empty">Empty directory.</div>'

    albums_href = f"{PREFIX}/albums" if PREFIX else "/albums"
    publish_href = f"{PREFIX}/publish" if PREFIX else "/publish"
    body = (
        f"<h1>{html.escape(title)}</h1>"
        f'<a class="albums" href="{html.escape(albums_href)}">'
        "Public albums — create one or upload any file type →</a> "
        f'<a class="albums" href="{html.escape(publish_href)}">'
        "Publish a folder (tar.gz/zip) →</a>"
        f'<p class="sub">Drop files here to publish them at '
        f'<code>{html.escape(public_url(rel.strip("/"), True))}</code>. '
        "Machine-readable listing: add <code>?format=json</code>; "
        "whole-folder download: add <code>?archive=tar.gz</code> or "
        "<code>?archive=zip</code>.</p>"
        f"{table}"
    )
    return _resp(request, _PAGE.format(title=html.escape(title), body=body),
                 content_type="text/html; charset=utf-8")


# --------------------------------------------------------------------------- #
# request handlers
# --------------------------------------------------------------------------- #

async def handle(request: Request) -> Response:
    # ASGI `path` (and therefore the route param) is already percent-decoded by
    # the server — don't decode again or filenames containing a literal '%' break.
    rel = (request.path_params.get("path", "") or "").lstrip("/")

    # `/<dir>/.index.json` is an alias for `?format=json`; strip the marker
    # before path validation (it is a dotfile component the resolver refuses).
    index_json = rel == ".index.json" or rel.endswith("/.index.json")
    if rel == ".index.json":
        rel = ""
    elif index_json:
        rel = rel[: -len("/.index.json")]

    # An empty path is the root directory, which is always a directory request.
    dir_requested = rel == "" or rel.endswith("/")

    # The canonical album tree lives under FILES_DIR/albums, but raw files must
    # only be served by /albums/asset/<id> (which honors shallow deletion).
    # Without this guard a known path would bypass the database entirely.
    if rel == "albums" or rel.startswith("albums/"):
        return _error(request, 404, "Not Found")

    if not ROOT.is_dir():
        return _resp(
            request,
            _PAGE.format(
                title="files",
                body='<div class="warn"><b>File host unavailable.</b> The archive '
                     f'disk looks unmounted, so <code>{html.escape(str(ROOT))}</code> '
                     "does not exist. Nothing is being served.</div>",
            ),
            content_type="text/html; charset=utf-8",
        )

    target = safe_resolve(rel)
    if target is None:
        return _error(request, 403, "Forbidden")

    fmt = (request.query_params.get("format") or "").strip().lower()
    if fmt == "json" or index_json:
        return _listing_json(
            request,
            target,
            rel,
            include_hidden=_truthy(request.query_params.get("hidden")),
        )

    archive = (request.query_params.get("archive") or "").strip().lower()
    if archive:
        if archive in ("tgz", "tar.gz", "targz"):
            kind = "tar.gz"
        elif archive == "zip":
            kind = "zip"
        else:
            return _error(request, 400, "unsupported archive (use tar.gz or zip)")
        if target.is_file():
            return _error(request, 400, "cannot archive a file")
        return await _serve_archive(request, target, rel, kind)

    if target.is_file():
        if dir_requested:
            return RedirectResponse(public_url(rel.strip("/")), status_code=307)
        headers = {
            "x-content-type-options": "nosniff",
        }
        # Never let a shared proxy/CDN (Vercel) cache file bodies. Vercel keys
        # its edge cache on the URL and ignores the Range header, so a cached
        # 206 partial poisons later full downloads and seeks (truncated files,
        # unplayable video). no-store keeps every request Range-correct.
        headers["cache-control"] = "no-store"
        media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return FileResponse(
            target,
            media_type=media,
            filename=target.name,
            content_disposition_type="inline",
            headers=headers,
        )

    if target.is_dir():
        if not dir_requested:
            return RedirectResponse(public_url(rel.strip("/"), True), status_code=307)
        return _listing(request, target, rel)

    return _error(request, 404, "Not Found")


app = Starlette(
    routes=albums_routes() + publish_mod.routes() + [
        Route("/", handle),
        Route("/{path:path}", handle),
    ],
    lifespan=albums_lifespan,
)


# --------------------------------------------------------------------------- #
# optional path-prefix stripping (mirrors Moosic's PrefixMiddleware)
# --------------------------------------------------------------------------- #
class PrefixMiddleware:
    def __init__(self, asgi_app, prefix: str):
        self.app, self.prefix = asgi_app, prefix

    async def __call__(self, scope, receive, send):
        if self.prefix and scope["type"] in ("http", "websocket"):
            path = scope.get("path", "/")
            if path == self.prefix:
                await RedirectResponse(url=self.prefix + "/", status_code=307)(
                    scope, receive, send
                )
                return
            if path.startswith(self.prefix + "/"):
                scope = dict(scope)
                scope["path"] = path[len(self.prefix):]
                scope["root_path"] = self.prefix
        await self.app(scope, receive, send)


asgi_app = PrefixMiddleware(app, PREFIX)

"""Publish whole folders into the public static tree.

``POST /publish`` is token-protected (``FILES_PUBLISH_TOKEN``). It accepts a
single ``.zip`` / ``.tar`` / ``.tar.gz`` archive plus a target ``folder`` name,
extracts it into an SSD staging directory with strict zip/tar-slip checks, then
copies the verified tree to a hidden directory on the served HDD and atomically
renames it into place. A multi-file mode (``files`` + ``paths``) is also
accepted for small trees that do not need an archive.

The route is registered before the static catch-all so ``/publish`` never
collides with a folder of the same name.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import shutil
import stat
import tarfile
import urllib.parse
import uuid
import zipfile
from pathlib import Path
from typing import Optional

from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

import albums_config as acfg
import albums_store as store_mod

store = store_mod.store

ROOT = Path(os.environ.get("FILES_DIR", "/mnt/archive-new/files")).expanduser().resolve()
PREFIX = os.environ.get("FILES_ROOT_PATH", "").rstrip("/")
TOKEN = os.environ.get("FILES_PUBLISH_TOKEN", "").strip()

TMP_DIR = Path(
    os.environ.get("FILES_PUBLISH_TMP_DIR", str(Path(acfg.TMP_DIR) / "publish"))
).expanduser()
MAX_REQUEST_BYTES = int(
    os.environ.get("FILES_PUBLISH_MAX_REQUEST_BYTES", str(2 * 1024**3))
)
MAX_ENTRIES = int(os.environ.get("FILES_PUBLISH_MAX_ENTRIES", "50000"))
MAX_FILES = int(os.environ.get("FILES_PUBLISH_MAX_FILES", "5000"))
MAX_TOTAL_BYTES = int(
    os.environ.get("FILES_PUBLISH_MAX_TOTAL_BYTES", str(20 * 1024**3))
)
MIN_FREE_BYTES = int(
    os.environ.get("FILES_PUBLISH_MIN_FREE_BYTES", str(2 * 1024**3))
)
MAX_NAME = 120
_RESERVED = {"albums", "publish"}

_HTML_HEADERS = {
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "content-security-policy": (
        "default-src 'none'; style-src 'unsafe-inline'; "
        "script-src 'unsafe-inline'; connect-src 'self'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
}


class PublishError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


class _BodyTooLarge(Exception):
    pass


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _json(data: dict, status: int = 200, headers: Optional[dict] = None) -> JSONResponse:
    hdrs = {"cache-control": "no-store", "x-content-type-options": "nosniff"}
    if headers:
        hdrs.update(headers)
    return JSONResponse(data, status_code=status, headers=hdrs)


def _error(message: str, status: int, headers: Optional[dict] = None) -> JSONResponse:
    return _json({"ok": False, "error": message}, status, headers)


def _public_url(folder: str) -> str:
    rel = "/".join(
        urllib.parse.quote(p) for p in folder.split("/") if p
    )
    return f"{PREFIX}/{rel}/" if PREFIX else f"/{rel}/"


def _client_ip(request: Request) -> str:
    for header in ("x-forwarded-for", "x-real-ip"):
        raw = request.headers.get(header)
        if raw:
            first = raw.split(",")[0].strip()
            if first:
                return first[:64]
    return request.client.host if request.client else "unknown"


def _authorized(request: Request) -> Optional[Response]:
    """Return an error response unless the request carries the publish token."""
    if not TOKEN:
        return _error(
            "publishing is disabled: FILES_PUBLISH_TOKEN is not set", 403
        )
    supplied = ""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    if not supplied:
        supplied = (request.query_params.get("token") or "").strip()
    if not supplied:
        return _error("missing publish token", 401, {"www-authenticate": "Bearer"})
    if not hmac.compare_digest(supplied, TOKEN):
        return _error("invalid publish token", 403)
    return None


def _limit_body(request: Request, cap: int) -> dict:
    """Backstop for a missing/lying Content-Length during multipart parsing."""
    original = request._receive
    counter = {"bytes": 0}

    async def limited():
        message = await original()
        if message.get("type") == "http.request":
            counter["bytes"] += len(message.get("body", b"") or b"")
            if counter["bytes"] > cap:
                raise _BodyTooLarge()
        return message

    request._receive = limited
    return counter


def _valid_folder(name: str) -> bool:
    if not name or len(name) > MAX_NAME:
        return False
    if name in (".", "..") or name.startswith("."):
        return False
    if name.lower() in _RESERVED:
        return False
    if "/" in name or "\\" in name or "\x00" in name:
        return False
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        return False
    return True


def _member_rel(name: str) -> Optional[str]:
    """Sanitize an archive member / relative upload path.

    Raises ``PublishError`` for absolute paths, ``..`` traversal, drive letters,
    backslashes, NULs and overlong components. Returns ``None`` for hidden
    entries (skipped by default).
    """
    name = name or ""
    if "\x00" in name:
        raise PublishError("archive entry contains a NUL byte")
    if name.startswith("/") or name.startswith("\\"):
        raise PublishError("archive contains an absolute path")
    if re.match(r"^[A-Za-z]:", name):
        raise PublishError("archive contains a drive-letter path")
    if "\\" in name:
        raise PublishError("archive contains a backslash path")
    parts: list[str] = []
    for part in name.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise PublishError("archive contains a '..' path")
        if len(part) > 255:
            raise PublishError("archive contains an overlong path component")
        parts.append(part)
    if not parts:
        return None
    if any(part.startswith(".") for part in parts):
        return None
    return "/".join(parts)


def _safe_child(base: Path, rel: str) -> Path:
    target = (base / rel).resolve()
    if target != base and base not in target.parents:
        raise PublishError("archive path escapes the staging directory")
    return target


# --------------------------------------------------------------------------- #
# safe extraction
# --------------------------------------------------------------------------- #

def _extract_zip(archive: Path, dest: Path) -> tuple[int, int, int]:
    files = entries = total = 0
    try:
        with zipfile.ZipFile(archive) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ENTRIES:
                raise PublishError(
                    f"archive has too many entries (max {MAX_ENTRIES})", 413
                )
            for info in infos:
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode) or stat.S_ISSOCK(mode) or stat.S_ISFIFO(mode):
                    raise PublishError("archive contains a symlink or special file")
                if stat.S_ISBLK(mode) or stat.S_ISCHR(mode):
                    raise PublishError("archive contains a device file")
                rel = _member_rel(info.filename)
                if rel is None:
                    continue
                target = _safe_child(dest, rel)
                entries += 1
                if entries > MAX_ENTRIES:
                    raise PublishError(
                        f"archive has too many entries (max {MAX_ENTRIES})", 413
                    )
                if info.is_dir() or info.filename.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    raise PublishError("archive contains duplicate paths")
                written = 0
                try:
                    with zf.open(info) as src, open(target, "xb") as dst:
                        while True:
                            chunk = src.read(1024 * 1024)
                            if not chunk:
                                break
                            written += len(chunk)
                            total += len(chunk)
                            if total > MAX_TOTAL_BYTES:
                                raise PublishError(
                                    "archive expands beyond the size limit", 413
                                )
                            dst.write(chunk)
                except PublishError:
                    raise
                except Exception as exc:
                    raise PublishError(f"cannot extract zip entry: {exc}") from exc
                files += 1
    except PublishError:
        raise
    except zipfile.BadZipFile as exc:
        raise PublishError("invalid zip archive") from exc
    except OSError as exc:
        raise PublishError(f"cannot extract zip archive: {exc}") from exc
    return files, entries, total


def _extract_tar(archive: Path, dest: Path) -> tuple[int, int, int]:
    files = entries = total = 0
    try:
        with tarfile.open(archive, mode="r:*") as tf:
            seen = 0
            for member in tf:
                seen += 1
                if seen > MAX_ENTRIES:
                    raise PublishError(
                        f"archive has too many entries (max {MAX_ENTRIES})", 413
                    )
                if (
                    member.issym()
                    or member.islnk()
                    or member.isdev()
                    or member.isfifo()
                ):
                    raise PublishError(
                        "archive contains a symlink, hardlink or special file"
                    )
                if not (member.isfile() or member.isdir()):
                    raise PublishError("archive contains an unsupported entry type")
                rel = _member_rel(member.name)
                if rel is None:
                    continue
                target = _safe_child(dest, rel)
                entries += 1
                if entries > MAX_ENTRIES:
                    raise PublishError(
                        f"archive has too many entries (max {MAX_ENTRIES})", 413
                    )
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    raise PublishError("archive contains duplicate paths")
                src = tf.extractfile(member)
                if src is None:
                    raise PublishError("cannot read tar entry")
                written = 0
                with src, open(target, "xb") as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        total += len(chunk)
                        if total > MAX_TOTAL_BYTES:
                            raise PublishError(
                                "archive expands beyond the size limit", 413
                            )
                        dst.write(chunk)
                if written != member.size:
                    raise PublishError("tar entry size changed during extraction")
                files += 1
    except PublishError:
        raise
    except (tarfile.TarError, OSError) as exc:
        raise PublishError(f"invalid tar archive: {exc}") from exc
    return files, entries, total


def _archive_kind(filename: str, path: Path) -> Optional[str]:
    lower = (filename or "").lower()
    if lower.endswith((".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
        return "tar"
    if lower.endswith(".tar"):
        return "tar"
    if lower.endswith(".zip"):
        return "zip"
    try:
        with open(path, "rb") as fh:
            head = fh.read(4)
            if head.startswith(b"PK"):
                return "zip"
            if head.startswith(b"\x1f\x8b"):
                return "tar"
            fh.seek(257)
            if fh.read(5) == b"ustar":
                return "tar"
    except OSError:
        return None
    return None


def _save_upload(upload: UploadFile, dest: Path, cap: int) -> int:
    upload.file.seek(0)
    total = 0
    with open(dest, "wb") as out:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                raise PublishError("archive is too large", 413)
            out.write(chunk)
        out.flush()
        os.fsync(out.fileno())
    if total == 0:
        raise PublishError("empty archive")
    return total


def _select_payload(stage: Path) -> Path:
    """Strip a single top-level directory produced by ``tar/zip -r folder``."""
    try:
        entries = [e for e in stage.iterdir() if e.name not in (".", "..")]
    except OSError:
        return stage
    if len(entries) == 1:
        only = entries[0]
        try:
            if only.is_dir() and not only.is_symlink():
                return only
        except OSError:
            pass
    return stage


def _tree_stats(root: Path) -> tuple[int, int, int]:
    files = entries = total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        kept = []
        for name in dirnames:
            path = Path(dirpath) / name
            if path.is_symlink():
                raise PublishError("staged tree contains a symlink")
            kept.append(name)
        dirnames[:] = kept
        entries += len(kept)
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                raise PublishError("staged tree contains a symlink")
            try:
                st = path.stat()
            except OSError as exc:
                raise PublishError(f"cannot stat staged file: {exc}") from exc
            if not stat.S_ISREG(st.st_mode):
                raise PublishError("staged tree contains a special file")
            files += 1
            entries += 1
            total += st.st_size
            if files > MAX_FILES:
                raise PublishError(f"too many files (max {MAX_FILES})", 413)
            if total > MAX_TOTAL_BYTES:
                raise PublishError("tree is larger than the size limit", 413)
    return files, entries, total


def _copy_tree(src: Path, dst: Path) -> None:
    """Copy a verified tree to the HDD without ever following symlinks."""
    dst.mkdir(parents=True, exist_ok=False)
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        rel_dir = Path(dirpath).relative_to(src)
        target_dir = dst / rel_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        kept = []
        for name in dirnames:
            path = Path(dirpath) / name
            if path.is_symlink():
                raise PublishError("staged tree contains a symlink")
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            source = Path(dirpath) / name
            if source.is_symlink():
                raise PublishError("staged tree contains a symlink")
            shutil.copy2(source, target_dir / name)


def _remove_path(path: Path) -> None:
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def _place_payload(payload: Path, folder: str, overwrite: bool) -> tuple[int, int]:
    final = ROOT / folder
    if final.exists() or final.is_symlink():
        if not overwrite:
            raise PublishError(
                f"folder '{folder}' already exists (pass overwrite=1 to replace)",
                409,
            )

    files, _entries, total = _tree_stats(payload)
    try:
        free = shutil.disk_usage(ROOT).free
    except OSError:
        free = 0
    if free < total + MIN_FREE_BYTES:
        raise PublishError("storage full", 507)

    token = uuid.uuid4().hex
    hdd_stage = ROOT / f".publish-tmp-{token}"
    backup: Optional[Path] = None
    try:
        _copy_tree(payload, hdd_stage)
        hdd_files, _he, hdd_total = _tree_stats(hdd_stage)
        if (hdd_files, hdd_total) != (files, total):
            raise PublishError("storage verification failed", 500)

        if final.exists() or final.is_symlink():
            backup = ROOT / f".publish-old-{token}"
            os.replace(final, backup)
            try:
                os.replace(hdd_stage, final)
            except Exception:
                os.replace(backup, final)
                backup = None
                raise
            _remove_path(backup)
            backup = None
        else:
            os.replace(hdd_stage, final)
    except Exception:
        _remove_path(hdd_stage)
        if backup is not None and not final.exists():
            try:
                os.replace(backup, final)
                backup = None
            except OSError:
                pass
        raise
    finally:
        if backup is not None:
            _remove_path(backup)
    return files, total


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

async def publish_api(request: Request) -> Response:
    denied = _authorized(request)
    if denied is not None:
        return denied

    raw_length = request.headers.get("content-length")
    content_length: Optional[int] = None
    if raw_length:
        try:
            content_length = int(raw_length)
        except ValueError:
            content_length = None
    if content_length is not None and content_length > MAX_REQUEST_BYTES:
        return _error("request too large", 413)

    if not ROOT.is_dir():
        return _error("storage unavailable (HDD not mounted)", 503)

    reserved = content_length if content_length is not None else MAX_REQUEST_BYTES
    event_id: Optional[int] = None
    try:
        ok, retry_after, event_id, _ = store.rate_reserve(
            _client_ip(request), reserved, "publish"
        )
    except Exception:
        ok, retry_after, event_id = True, 0, None
    if not ok:
        return _error(
            "publish rate limit exceeded (global/per-IP 5-minute window)",
            429,
            {"retry-after": str(retry_after)},
        )

    stage = TMP_DIR / uuid.uuid4().hex
    actual = 0
    body_counter = {"bytes": 0}
    ok_done = False
    try:
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        body_counter = _limit_body(request, MAX_REQUEST_BYTES)
        try:
            form = await request.form(
                max_files=MAX_FILES + 2,
                max_fields=32,
                max_part_size=256 * 1024,
            )
        except MultiPartException as exc:
            return _error(f"invalid multipart upload: {exc}", 400)
        except _BodyTooLarge:
            return _error("request body too large", 413)
        except Exception as exc:
            return _error(f"cannot parse upload: {exc}", 400)

        folder = str(form.get("folder") or "").strip()
        if not _valid_folder(folder):
            return _error(
                "invalid folder name (single path component, no leading dot, "
                f"max {MAX_NAME} chars)",
                400,
            )
        overwrite = str(form.get("overwrite") or "").lower() in (
            "1", "true", "yes", "on",
        )

        archive = form.get("archive")
        if not isinstance(archive, UploadFile):
            archive = None

        stage.mkdir(parents=True, exist_ok=False)
        payload = stage / "payload"
        payload.mkdir()
        entries = files = extracted = 0

        if archive is not None:
            archive_path = stage / "upload"
            actual = await asyncio.to_thread(
                _save_upload, archive, archive_path, MAX_REQUEST_BYTES
            )
            kind = _archive_kind(archive.filename or "", archive_path)
            if kind is None:
                raise PublishError(
                    "unsupported archive type (use .zip, .tar or .tar.gz)", 400
                )
            extractor = _extract_zip if kind == "zip" else _extract_tar
            files, entries, extracted = await asyncio.to_thread(
                extractor, archive_path, payload
            )
            payload = _select_payload(payload)
            if entries == 0:
                raise PublishError("archive contains no usable entries", 400)
        else:
            uploads = [
                value for _key, value in form.multi_items()
                if isinstance(value, UploadFile) and value is not archive
            ]
            if not uploads:
                raise PublishError("no archive or files in request", 400)
            if len(uploads) > MAX_FILES:
                raise PublishError(f"too many files (max {MAX_FILES})", 413)

            raw_paths = form.getlist("paths")
            paths: list[str] = []
            if len(raw_paths) == 1 and raw_paths[0].strip().startswith("["):
                try:
                    parsed = json.loads(raw_paths[0])
                except ValueError:
                    parsed = None
                if isinstance(parsed, list):
                    paths = [str(p) for p in parsed]
            if not paths:
                paths = [str(p) for p in raw_paths]
            if len(paths) != len(uploads):
                paths = [u.filename or "" for u in uploads]

            for upload, raw_rel in zip(uploads, paths):
                rel = _member_rel(raw_rel or upload.filename or "")
                if rel is None:
                    continue
                dest = _safe_child(payload, rel)
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists() or dest.is_symlink():
                    raise PublishError("duplicate upload path")
                actual += await asyncio.to_thread(
                    _save_upload, upload, dest, MAX_TOTAL_BYTES
                )
                files += 1
                entries += 1
                if files > MAX_FILES:
                    raise PublishError(f"too many files (max {MAX_FILES})", 413)

        files, entries, total = await asyncio.to_thread(_tree_stats, payload)
        if entries == 0:
            raise PublishError("nothing to publish", 400)

        files, total = await asyncio.to_thread(
            _place_payload, payload, folder, overwrite
        )
        ok_done = True
        return _json(
            {
                "ok": True,
                "url": _public_url(folder),
                "folder": folder,
                "files": files,
                "bytes": total,
            },
            status=201,
        )
    except PublishError as exc:
        return _error(exc.message, exc.status)
    except Exception as exc:
        print(f"[publish] error: {exc}", flush=True)
        return _error("publish failed", 500)
    finally:
        _remove_path(stage)
        try:
            store.rate_finalize(
                event_id,
                max(actual, int(body_counter.get("bytes", 0))),
                "done" if ok_done else "cancelled",
            )
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# web UI
# --------------------------------------------------------------------------- #

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Publish folder</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ margin: 0; padding: 1.5rem; font: 15px/1.5 system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }}
main {{ max-width: 42rem; }}
h1 {{ font-size: 1.2rem; margin: 0 0 .3rem; }}
p.sub {{ margin: 0 0 1.2rem; opacity: .7; font-size: .9rem; }}
label {{ display: block; margin: .8rem 0 .25rem; font-weight: 600; font-size: .85rem; }}
input {{ width: 100%; box-sizing: border-box; padding: .55rem .6rem; border: 1px solid rgba(127,127,127,.4); border-radius: 7px; background: transparent; color: inherit; }}
button {{ margin-top: 1rem; padding: .55rem 1rem; border-radius: 7px; border: 1px solid rgba(127,127,127,.4); font-weight: 600; cursor: pointer; }}
pre {{ margin-top: 1rem; padding: .8rem; border-radius: 7px; background: rgba(127,127,127,.12); white-space: pre-wrap; word-break: break-word; min-height: 1.5rem; }}
.warn {{ padding: .8rem; border: 1px solid rgba(200,120,0,.5); background: rgba(200,120,0,.1); border-radius: 7px; }}
code {{ background: rgba(127,127,127,.15); padding: .1rem .3rem; border-radius: 4px; }}
a {{ color: inherit; }}
</style>
</head>
<body>
<main>
<h1>Publish folder</h1>
<p class="sub">Upload a <code>.tar.gz</code>, <code>.tar</code> or <code>.zip</code> and
extract it into the public tree. API clients can use
<code>Authorization: Bearer $FILES_PUBLISH_TOKEN</code>.</p>
{notice}
<form id="publish-form">
  <label for="folder">Target folder name</label>
  <input id="folder" name="folder" required maxlength="{max_name}" placeholder="myfolder">
  <label for="archive">Archive</label>
  <input id="archive" name="archive" type="file" required
         accept=".zip,.tar,.tar.gz,.tgz,.tar.bz2,.tar.xz">
  <label for="token">Publish token</label>
  <input id="token" name="token" type="password" required autocomplete="current-password">
  <label><input name="overwrite" type="checkbox" value="1" style="width:auto"> Replace an existing folder</label>
  <button type="submit">Publish</button>
</form>
<pre id="result"></pre>
</main>
<script>
(function () {{
  var form = document.getElementById('publish-form');
  var out = document.getElementById('result');
  form.addEventListener('submit', async function (event) {{
    event.preventDefault();
    out.textContent = 'Publishing…';
    var data = new FormData(form);
    var token = data.get('token') || '';
    data.delete('token');
    try {{
      var response = await fetch(window.location.pathname, {{
        method: 'POST',
        headers: {{ 'Authorization': 'Bearer ' + token }},
        body: data
      }});
      var text = await response.text();
      var payload;
      try {{ payload = JSON.parse(text); }} catch (err) {{ payload = {{ error: text }}; }}
      out.textContent = JSON.stringify(payload, null, 2) + '\\nHTTP ' + response.status;
    }} catch (err) {{
      out.textContent = 'Request failed: ' + err;
    }}
  }});
}})();
</script>
</body>
</html>
"""


async def publish_page(request: Request) -> Response:
    if not TOKEN:
        notice = (
            '<div class="warn"><b>Publishing is disabled.</b> Set '
            '<code>FILES_PUBLISH_TOKEN</code> in the service environment '
            '(<code>.env</code>) and restart <code>files.service</code>.</div>'
        )
    else:
        notice = (
            '<p class="sub">Already have a token? Use the CLI: '
            '<code>scripts/publish.sh ./myfolder myfolder</code>.</p>'
        )
    body = _PAGE.format(notice=notice, max_name=MAX_NAME)
    headers = dict(_HTML_HEADERS)
    if request.method == "HEAD":
        return Response(status_code=200, headers=headers)
    return HTMLResponse(body, headers=headers)


# --------------------------------------------------------------------------- #
# routes (registered before the static catch-all)
# --------------------------------------------------------------------------- #

def routes() -> list[Route]:
    return [
        Route("/publish", publish_page, methods=["GET", "HEAD"]),
        Route("/publish", publish_api, methods=["POST"]),
        Route("/publish/", publish_page, methods=["GET", "HEAD"]),
        Route("/publish/", publish_api, methods=["POST"]),
    ]

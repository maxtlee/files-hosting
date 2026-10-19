"""Public photo albums — routes, background workers, lifespan.

Mounted into the existing ``files`` Starlette app. Public access is the same as
the static host: loopback only, exposed by Tailscale Funnel at ``/files``.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException
from starlette.requests import ClientDisconnect, Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Route

import albums_config as cfg
import albums_notify as notify
import albums_preview as preview
import albums_storage as storage
import albums_store as store_mod
import albums_ui as ui

store = store_mod.store

_ID_RE = re.compile(r"^[0-9a-f]{32}$")

_HTML_HEADERS = {
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "content-security-policy": (
        "default-src 'none'; img-src 'self' data:; media-src 'self'; "
        "style-src 'unsafe-inline'; "
        "script-src 'unsafe-inline'; connect-src 'self'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
}


# --------------------------------------------------------------------------- #
# background workers
# --------------------------------------------------------------------------- #

_copy_queue: "asyncio.Queue[str]" = asyncio.Queue()
_notify_queue: "asyncio.Queue[tuple[dict, list[dict]]]" = asyncio.Queue()
_enqueued_copies: set[str] = set()


def _enqueue_copy(asset_id: str) -> None:
    if asset_id in _enqueued_copies:
        return
    _enqueued_copies.add(asset_id)
    _copy_queue.put_nowait(asset_id)


async def _copy_worker() -> None:
    while True:
        asset_id = await _copy_queue.get()
        try:
            ok = await asyncio.to_thread(store.copy_asset_to_hdd, asset_id)
            if not ok:
                print(f"[albums] HDD copy pending/failed for {asset_id}", flush=True)
        except Exception as exc:  # never kill the worker
            print(f"[albums] copy worker error {asset_id}: {exc}", flush=True)
        finally:
            _enqueued_copies.discard(asset_id)
            _copy_queue.task_done()


async def _notify_worker() -> None:
    while True:
        album, assets = await _notify_queue.get()
        try:
            result = await asyncio.to_thread(notify.notify_batch, album, assets)
            if result.get("status") not in ("sent", "logged"):
                print(f"[albums] notification: {result}", flush=True)
        except Exception as exc:  # never kill the worker
            print(f"[albums] notify worker error: {exc}", flush=True)
        finally:
            _notify_queue.task_done()


async def _maintenance() -> None:
    last_hdd_cleanup = 0.0
    while True:
        try:
            # Recover uploads that were accepted but not yet copied (e.g. after
            # a restart). Includes assets hidden while the copy was pending:
            # the canonical file must still reach the HDD.
            for asset_id in await asyncio.to_thread(store.pending_copy_ids):
                _enqueue_copy(asset_id)
            await asyncio.to_thread(store.evict_cache)
            await asyncio.to_thread(store.evict_thumbs)
            await asyncio.to_thread(preview.evict_previews)
            await asyncio.to_thread(preview.cleanup_preview_temps)
            for upload_id in await asyncio.to_thread(
                store.cleanup_upload_sessions, cfg.CHUNK_TTL_SECONDS
            ):
                _chunk_cleanup(upload_id)
            # Orphan chunk dirs (e.g. a failed init) are removed after the TTL.
            try:
                cutoff = time.time() - cfg.CHUNK_TTL_SECONDS
                for entry in os.scandir(cfg.CHUNK_DIR):
                    if not entry.is_dir():
                        continue
                    try:
                        if entry.stat().st_mtime < cutoff and not store.get_upload_session(entry.name):
                            shutil.rmtree(entry.path, ignore_errors=True)
                    except OSError:
                        pass
            except OSError:
                pass
            now = time.monotonic()
            include_hdd = now - last_hdd_cleanup > 3600
            await asyncio.to_thread(
                store.cleanup_temps, 3600, include_hdd
            )
            if include_hdd:
                last_hdd_cleanup = now
        except Exception as exc:
            print(f"[albums] maintenance error: {exc}", flush=True)
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(_app):
    cfg.init_dirs()
    store.connect()
    try:
        fixed = await asyncio.to_thread(store.reclassify_legacy_images)
        if fixed:
            print(f"[albums] reclassified {fixed} legacy image asset(s)", flush=True)
    except Exception as exc:
        print(f"[albums] legacy reclassify failed: {exc}", flush=True)
    tasks = [
        asyncio.create_task(_copy_worker(), name="albums-copy"),
        asyncio.create_task(_maintenance(), name="albums-maintenance"),
    ]
    if cfg.NOTIFY_ENABLED:
        tasks.append(asyncio.create_task(_notify_worker(), name="albums-notify"))
    print(
        f"[albums] data={cfg.DATA_DIR} hdd={cfg.HDD_DIR} "
        f"cache={cfg.CACHE_DIR} cache_budget={cfg.effective_cache_budget()} "
        f"previews={cfg.PREVIEW_DIR}",
        flush=True,
    )
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        store.close()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _html(body: str, status: int = 200, *, head: bool = False) -> Response:
    if head:
        return Response(status_code=status, headers=_HTML_HEADERS)
    return HTMLResponse(body, status_code=status, headers=_HTML_HEADERS)


def _json(data: Any, status: int = 200, headers: Optional[dict] = None) -> JSONResponse:
    hdrs = {"cache-control": "no-store", "x-content-type-options": "nosniff"}
    if headers:
        hdrs.update(headers)
    return JSONResponse(data, status_code=status, headers=hdrs)


def _api_error(message: str, status: int, headers: Optional[dict] = None) -> JSONResponse:
    return _json({"ok": False, "error": message}, status, headers)


def _valid_id(value: str) -> bool:
    return bool(_ID_RE.match(value or ""))


def _client_ip(request: Request) -> str:
    # Behind Tailscale Funnel / Vercel the TCP peer is loopback, so prefer the
    # forwarded chain. Best-effort only (headers are spoofable by design here).
    for header in ("x-forwarded-for", "x-real-ip"):
        raw = request.headers.get(header)
        if raw:
            first = raw.split(",")[0].strip()
            if first:
                return first[:64]
    return request.client.host if request.client else "unknown"


def _device(request: Request) -> dict:
    return ui.device_from_user_agent(request.headers.get("user-agent", ""))


def _wants_json(request: Request) -> bool:
    if request.url.path.startswith("/albums/api/"):
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


def _safe_file(path_value: Optional[str], base: Path) -> Optional[Path]:
    """Return a resolved regular file only if it is contained by ``base``."""
    if not path_value:
        return None
    try:
        base_r = base.resolve()
        candidate = Path(path_value).resolve()
    except (OSError, RuntimeError):
        return None
    if candidate != base_r and base_r not in candidate.parents:
        return None
    try:
        return candidate if candidate.is_file() else None
    except OSError:
        return None


def _asset_public(asset: dict) -> dict:
    base = cfg.ALBUMS_BASE_PATH
    kind = store_mod.asset_kind(asset)
    pkind = preview.preview_kind(asset)
    return {
        "id": asset["id"],
        "album_id": asset["album_id"],
        "name": asset["original_name"],
        "mime": asset["mime"],
        "kind": kind,
        "preview_kind": pkind,
        "size": asset["size"],
        "width": asset.get("width"),
        "height": asset.get("height"),
        "created_at": asset["created_at"],
        "on_hdd": bool(asset.get("on_hdd")),
        "url": f"{base}/asset/{asset['id']}",
        "thumb_url": f"{base}/thumb/{asset['id']}",
        "preview_url": f"{base}/preview/{asset['id']}",
    }


def _album_public(album: dict) -> dict:
    base = cfg.ALBUMS_BASE_PATH
    return {
        "id": album["id"],
        "title": album["title"],
        "description": album.get("description") or "",
        "created_at": album["created_at"],
        "asset_count": album.get("asset_count"),
        "cover_url": (
            f"{base}/thumb/{album['cover_id']}" if album.get("cover_id") else None
        ),
        "url": f"{base}/{album['id']}",
    }


def _album_or_404(album_id: str) -> Optional[dict]:
    if not _valid_id(album_id):
        return None
    return store.get_album(album_id)


class _BodyTooLarge(Exception):
    pass


def _limit_body(request: Request, cap: int) -> dict:
    """Backstop for missing/lying Content-Length during multipart parsing.

    Returns a mutable ``{"bytes": n}`` counter so the caller can charge the
    rate window for bytes actually received (including multipart overhead).
    """
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


# --------------------------------------------------------------------------- #
# pages
# --------------------------------------------------------------------------- #

async def photos_page(request: Request) -> Response:
    query = (request.query_params.get("q") or "").strip()[:100]
    albums = store.list_albums()
    media = store.list_recent_media(query=query)
    return _html(
        ui.render_photos_page(
            media, albums, device=_device(request), query=query
        ),
        head=request.method == "HEAD",
    )


async def albums_page(request: Request) -> Response:
    query = (request.query_params.get("q") or "").strip()[:100]
    albums = store.list_albums()
    if query:
        ql = query.lower()
        albums = [
            a for a in albums
            if ql in (a.get("title") or "").lower()
            or ql in (a.get("description") or "").lower()
        ]
    return _html(
        ui.render_albums_page(albums, device=_device(request), query=query),
        head=request.method == "HEAD",
    )


async def album_page(request: Request) -> Response:
    albums = store.list_albums()
    album = _album_or_404(request.path_params.get("album_id", ""))
    if not album:
        return _html(
            ui.render_not_found(device=_device(request), albums=albums),
            status=404,
            head=request.method == "HEAD",
        )
    assets = store.list_assets(album["id"])
    body = ui.render_album_page(
        album,
        assets,
        max_file=cfg.MAX_FILE_BYTES,
        max_files=cfg.MAX_FILES,
        max_request=cfg.MAX_REQUEST_BYTES,
        device=_device(request),
        albums=albums,
    )
    return _html(body, head=request.method == "HEAD")


# --------------------------------------------------------------------------- #
# albums API
# --------------------------------------------------------------------------- #

async def create_album(request: Request) -> Response:
    wants_json = _wants_json(request)
    content_type = request.headers.get("content-type", "").lower()
    raw_length = request.headers.get("content-length")
    if raw_length:
        try:
            if int(raw_length) > cfg.CREATE_MAX_BODY:
                return _api_error("request body too large", 413) if wants_json else _html(
                    ui.render_not_found("Request is too large."), status=413
                )
        except ValueError:
            pass
    _limit_body(request, cfg.CREATE_MAX_BODY)
    try:
        if "application/json" in content_type:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
        else:
            form = await request.form(max_fields=10, max_part_size=8192)
            payload = {
                "title": form.get("title", ""),
                "description": form.get("description", ""),
            }
    except Exception as exc:
        message = f"invalid request: {exc}"
        return _api_error(message, 400) if wants_json else _html(
            ui.render_not_found(message), status=400
        )

    title = str(payload.get("title") or "").strip()[: cfg.MAX_TITLE]
    description = str(payload.get("description") or "").strip()[
        : cfg.MAX_DESCRIPTION
    ]
    if not title:
        title = "Untitled album"

    album = store.create_album(title, description)
    if wants_json:
        return _json({"ok": True, "album": _album_public(album)}, status=201)
    return RedirectResponse(f"{cfg.ALBUMS_BASE_PATH}/{album['id']}", status_code=303)


async def list_albums_api(request: Request) -> Response:
    albums = store.list_albums()
    return _json({"ok": True, "albums": [_album_public(a) for a in albums]})


async def album_detail_api(request: Request) -> Response:
    album = _album_or_404(request.path_params.get("album_id", ""))
    if not album:
        return _api_error("album not found", 404)
    assets = store.list_assets(album["id"])
    data = _album_public(album)
    data["assets"] = [_asset_public(a) for a in assets]
    return _json({"ok": True, "album": data})


# --------------------------------------------------------------------------- #
# upload
# --------------------------------------------------------------------------- #

async def _handle_upload(request: Request, album_id: str) -> Response:
    wants_json = _wants_json(request)
    album = _album_or_404(album_id)
    if not album:
        return _api_error("album not found", 404) if wants_json else _html(
            ui.render_not_found(), status=404
        )

    content_length: Optional[int] = None
    raw_length = request.headers.get("content-length")
    if raw_length:
        try:
            content_length = int(raw_length)
        except ValueError:
            content_length = None
    if content_length is not None and content_length > cfg.MAX_REQUEST_BYTES:
        return _api_error(
            f"request too large (max {storage.human_limit(cfg.MAX_REQUEST_BYTES)})",
            413,
        ) if wants_json else _html(
            ui.render_not_found("Upload is too large."), status=413
        )

    if not cfg.HDD_DIR.is_dir() or not cfg.hdd_mounted():
        return _api_error("storage unavailable (HDD not mounted)", 503) if wants_json else _html(
            ui.render_not_found("Storage is unavailable."), status=503
        )
    try:
        free_hdd = shutil.disk_usage(cfg.HDD_DIR).free
    except OSError:
        free_hdd = 0
    if free_hdd < cfg.MIN_FREE_BYTES:
        return _api_error("storage full", 507) if wants_json else _html(
            ui.render_not_found("Storage is full."), status=507
        )

    reserved = content_length if content_length is not None else cfg.MAX_REQUEST_BYTES
    ok, retry_after, event_id, _remaining = store.rate_reserve(
        _client_ip(request), reserved, album_id
    )
    if not ok:
        headers = {"retry-after": str(retry_after)}
        if wants_json:
            return _api_error(
                "upload rate limit exceeded (global/per-IP 5-minute window)",
                429,
                headers,
            )
        body = (
            "<h1>Rate limit</h1><p>Too many uploads right now. "
            f"Try again in {retry_after} seconds.</p>"
        )
        return HTMLResponse(body, status_code=429, headers={**_HTML_HEADERS, **headers})

    successful: list[dict] = []
    errors: list[str] = []
    error_status = 400
    actual_bytes = 0
    body_counter = {"bytes": 0}
    try:
        # Backstop for a missing or lying Content-Length.
        body_counter = _limit_body(request, cfg.MAX_REQUEST_BYTES)
        try:
            form = await request.form(
                max_files=cfg.MAX_FILES,
                max_fields=50,
                max_part_size=64 * 1024,
            )
        except ClientDisconnect:
            print("[albums] client disconnected during multipart upload", flush=True)
            return _api_error(
                "client disconnected during upload; please retry", 400
            ) if wants_json else _html(
                ui.render_not_found("Upload connection lost. Please retry."),
                status=400,
            )
        except MultiPartException as exc:
            return _api_error(f"invalid multipart upload: {exc}", 400) if wants_json else _html(
                ui.render_not_found("Invalid upload."), status=400
            )
        except _BodyTooLarge:
            return _api_error("request body too large", 413) if wants_json else _html(
                ui.render_not_found("Upload is too large."), status=413
            )

        files = [
            value for _key, value in form.multi_items()
            if isinstance(value, UploadFile)
        ]
        if not files:
            return _api_error("no files in request", 400) if wants_json else _html(
                ui.render_not_found("No files selected."), status=400
            )
        if len(files) > cfg.MAX_FILES:
            return _api_error(
                f"too many files (max {cfg.MAX_FILES})", 400
            ) if wants_json else _html(
                ui.render_not_found("Too many files."), status=400
            )

        for upload in files:
            actual_bytes += int(upload.size or 0)
            if upload.size is not None and upload.size > cfg.MAX_FILE_BYTES:
                errors.append(
                    f"{storage.sanitize_name(upload.filename)}: larger than "
                    f"{storage.human_limit(cfg.MAX_FILE_BYTES)}"
                )
                error_status = 413
                continue
            try:
                row = await asyncio.to_thread(
                    storage.ingest_upload,
                    upload.file,
                    album_id=album["id"],
                    original_name=upload.filename,
                )
            except storage.UploadError as exc:
                errors.append(f"{storage.sanitize_name(upload.filename)}: {exc.message}")
                if exc.status in (413, 415):
                    error_status = exc.status
                continue
            except Exception as exc:
                print(f"[albums] ingest error: {exc}", flush=True)
                errors.append(
                    f"{storage.sanitize_name(upload.filename)}: upload failed"
                )
                continue
            successful.append(row)

        if successful:
            for row in successful:
                if not row.get("on_hdd"):
                    _enqueue_copy(row["id"])
            if cfg.NOTIFY_ENABLED:
                # Fire-and-forget; the notifier has its own worker.
                _notify_queue.put_nowait((album, successful))

        payload = {
            "ok": bool(successful),
            "album_id": album["id"],
            "assets": [_asset_public(a) for a in successful],
            "errors": errors,
        }
        if not successful:
            payload["error"] = "no files were accepted"
            return _api_error(
                "; ".join(errors) or "no files were accepted", error_status
            ) if wants_json else _html(
                ui.render_not_found("; ".join(errors) or "No files accepted."),
                status=error_status,
            )
        if wants_json:
            return _json(payload, status=201)
        return RedirectResponse(
            f"{cfg.ALBUMS_BASE_PATH}/{album['id']}?uploaded={len(successful)}",
            status_code=303,
        )
    finally:
        store.rate_finalize(
            event_id,
            max(actual_bytes, int(body_counter.get("bytes", 0))),
            "done" if successful else "cancelled",
        )


async def upload_api(request: Request) -> Response:
    return await _handle_upload(request, request.path_params.get("album_id", ""))


async def upload_form(request: Request) -> Response:
    return await _handle_upload(request, request.path_params.get("album_id", ""))


# --------------------------------------------------------------------------- #
# resumable chunked uploads (used by the browser UI)
# --------------------------------------------------------------------------- #

def _chunk_dir(upload_id: str):
    return cfg.CHUNK_DIR / upload_id


def _chunk_cleanup(upload_id: str) -> None:
    shutil.rmtree(_chunk_dir(upload_id), ignore_errors=True)


async def chunk_init_api(request: Request) -> Response:
    album_id = request.path_params.get("album_id", "")
    album = _album_or_404(album_id)
    if not album:
        return _api_error("album not found", 404)
    if not cfg.HDD_DIR.is_dir() or not cfg.hdd_mounted():
        return _api_error("storage unavailable (HDD not mounted)", 503)
    try:
        free_hdd = shutil.disk_usage(cfg.HDD_DIR).free
    except OSError:
        free_hdd = 0
    if free_hdd < cfg.MIN_FREE_BYTES:
        return _api_error("storage full", 507)
    raw_length = request.headers.get("content-length")
    if raw_length and int(raw_length) > 64 * 1024:
        return _api_error("request body too large", 413)
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("JSON object required")
        size = int(payload.get("size") or 0)
    except Exception:
        return _api_error("invalid upload metadata", 400)
    if size <= 0:
        return _api_error("empty file", 400)
    if size > cfg.MAX_FILE_BYTES:
        return _api_error(
            f"file too large (max {storage.human_limit(cfg.MAX_FILE_BYTES)})", 413
        )
    name = storage.sanitize_name(payload.get("name"))
    session = store.create_upload_session(album_id, name, size)
    return _json(
        {
            "ok": True,
            "upload_id": session["id"],
            "chunk_bytes": cfg.CHUNK_BYTES,
            "max_chunk_bytes": cfg.MAX_CHUNK_BYTES,
            "size": size,
        },
        status=201,
    )


async def chunk_put_api(request: Request) -> Response:
    upload_id = request.path_params.get("upload_id", "")
    if not _valid_id(upload_id):
        return _api_error("upload session not found", 404)
    try:
        index = int(request.path_params.get("index", "-1"))
    except ValueError:
        return _api_error("invalid chunk index", 400)
    if index < 0:
        return _api_error("invalid chunk index", 400)
    session = store.get_upload_session(upload_id)
    if not session:
        return _api_error("upload session not found", 404)
    if session["completed"]:
        return _api_error("upload already completed", 409)
    if index > session["next_index"]:
        return _api_error("chunk out of order", 409)

    raw_length = request.headers.get("content-length")
    try:
        content_length = int(raw_length) if raw_length else None
    except ValueError:
        content_length = None
    if content_length is not None and content_length > cfg.MAX_CHUNK_BYTES:
        return _api_error("chunk too large", 413)

    expected = content_length if content_length is not None else cfg.MAX_CHUNK_BYTES
    ok, retry_after, event_id, _remaining = store.rate_reserve(
        _client_ip(request), expected, session["album_id"]
    )
    if not ok:
        return _api_error(
            "upload rate limit exceeded (global/per-IP 5-minute window)",
            429,
            {"retry-after": str(retry_after)},
        )

    chunk_dir = _chunk_dir(upload_id)
    chunk_dir.mkdir(parents=True, exist_ok=True)
    part = chunk_dir / f"{index}.part"
    final = chunk_dir / f"{index}.chunk"
    total = 0
    error_status = 400
    error_message = "upload failed"
    try:
        with open(part, "wb") as fh:
            async for data in request.stream():
                total += len(data)
                if total > cfg.MAX_CHUNK_BYTES:
                    error_status, error_message = 413, "chunk too large"
                    raise _BodyTooLarge()
                if session["received"] + total > session["size"]:
                    error_status, error_message = 413, "upload exceeds declared size"
                    raise _BodyTooLarge()
                fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if total == 0:
            error_status, error_message = 400, "empty chunk"
            raise ValueError("empty chunk")
        os.replace(part, final)
        recorded, reason, updated = store.record_chunk(upload_id, index, total)
        if not recorded:
            try:
                final.unlink()
            except OSError:
                pass
            return _api_error(reason, 409 if "order" in reason else 400)
        return _json(
            {
                "ok": True,
                "upload_id": upload_id,
                "received": updated["received"] if updated else 0,
                "next_index": updated["next_index"] if updated else index + 1,
                "size": session["size"],
            }
        )
    except ClientDisconnect:
        error_status, error_message = 400, "connection lost"
        print(f"[albums] client disconnected during chunk {upload_id}/{index}", flush=True)
        return _api_error("connection lost; retry this chunk", 400)
    except _BodyTooLarge:
        return _api_error(error_message, error_status)
    except OSError as exc:
        print(f"[albums] chunk write failed {upload_id}/{index}: {exc}", flush=True)
        return _api_error("storage unavailable", 507)
    except ValueError as exc:
        return _api_error(str(exc), error_status)
    finally:
        try:
            if part.exists():
                part.unlink()
        except OSError:
            pass
        store.rate_finalize(event_id, total, "done" if final.exists() else "cancelled")


async def chunk_complete_api(request: Request) -> Response:
    upload_id = request.path_params.get("upload_id", "")
    if not _valid_id(upload_id):
        return _api_error("upload session not found", 404)
    session = store.get_upload_session(upload_id)
    if not session:
        return _api_error("upload session not found", 404)
    if session["completed"] and session.get("asset_id"):
        row = store.get_asset_any(session["asset_id"])
        if row:
            return _json({"ok": True, "asset": _asset_public(row)})
        return _api_error("uploaded asset was removed", 410)
    if session["received"] != session["size"] or session["next_index"] == 0:
        return _api_error(
            f"incomplete upload ({session['received']} of {session['size']} bytes)",
            400,
        )

    chunk_dir = _chunk_dir(upload_id)
    assembled = chunk_dir / "assembled.part"
    total = 0
    try:
        with open(assembled, "wb") as out:
            for i in range(session["next_index"]):
                chunk = chunk_dir / f"{i}.chunk"
                if not chunk.is_file():
                    return _api_error(f"missing chunk {i}", 400)
                with open(chunk, "rb") as src:
                    while True:
                        data = src.read(1024 * 1024)
                        if not data:
                            break
                        out.write(data)
                        total += len(data)
        if total != session["size"]:
            return _api_error(
                f"size mismatch (expected {session['size']}, got {total})", 400
            )
        with open(assembled, "rb") as fh:
            row = await asyncio.to_thread(
                storage.ingest_upload,
                fh,
                album_id=session["album_id"],
                original_name=session["original_name"],
            )
    except storage.UploadError as exc:
        return _api_error(exc.message, exc.status)
    except Exception as exc:
        print(f"[albums] chunk complete failed {upload_id}: {exc}", flush=True)
        return _api_error("upload assembly failed", 500)

    store.complete_upload_session(upload_id, row["id"])
    _chunk_cleanup(upload_id)
    if not row.get("on_hdd"):
        _enqueue_copy(row["id"])
    if cfg.NOTIFY_ENABLED:
        album = store.get_album(session["album_id"])
        if album:
            _notify_queue.put_nowait((album, [row]))
    return _json({"ok": True, "asset": _asset_public(row)}, status=201)


# --------------------------------------------------------------------------- #
# delete (shallow only)
# --------------------------------------------------------------------------- #

async def delete_asset_api(request: Request) -> Response:
    asset_id = request.path_params.get("asset_id", "")
    if not _valid_id(asset_id):
        return _api_error("asset not found", 404)
    # Shallow: DB flag only. No file is ever unlinked here.
    if not store.mark_asset_deleted(asset_id):
        return _api_error("asset not found", 404)
    return _json({"ok": True, "deleted": asset_id, "files_kept": True})


async def delete_album_api(request: Request) -> Response:
    album_id = request.path_params.get("album_id", "")
    if not _valid_id(album_id):
        return _api_error("album not found", 404)
    if not store.mark_album_deleted(album_id):
        return _api_error("album not found", 404)
    return _json({"ok": True, "deleted": album_id, "files_kept": True})


async def delete_album_form(request: Request) -> Response:
    album_id = request.path_params.get("album_id", "")
    if _valid_id(album_id):
        store.mark_album_deleted(album_id)
    return RedirectResponse(cfg.ALBUMS_BASE_PATH, status_code=303)


# --------------------------------------------------------------------------- #
# media
# --------------------------------------------------------------------------- #

def _asset_file(row: dict) -> Optional[Path]:
    # Prefer the SSD copy while it exists; fall back to the canonical HDD file.
    cache = _safe_file(row.get("cache_path"), cfg.CACHE_DIR)
    if cache:
        return cache
    return _safe_file(row.get("hdd_path"), cfg.HDD_DIR)


async def serve_asset(request: Request) -> Response:
    asset_id = request.path_params.get("asset_id", "")
    if not _valid_id(asset_id):
        return _html(ui.render_not_found(), status=404)
    row = store.get_asset(asset_id)
    if not row:
        return _html(ui.render_not_found(), status=404)
    path = _asset_file(row)
    if path is None:
        return _html(ui.render_not_found("The photo file is unavailable."), status=404)

    await asyncio.to_thread(store.touch_asset, asset_id)
    inline = store_mod.asset_is_inline(row)
    served_mime = row["mime"] if inline else "application/octet-stream"
    # Images are safe to edge-cache; video/audio/PDF/opaque downloads are
    # Range-capable and must not be cached by a CDN that ignores Range (Vercel
    # would pin a 206 partial to the URL and break later downloads/seeks).
    cache = "public, max-age=86400" if served_mime.startswith("image/") else "no-store"
    headers = {
        "x-content-type-options": "nosniff",
        "cache-control": cache,
        "content-security-policy": "default-src 'none'",
    }
    return FileResponse(
        path,
        # Generic files are forced to a download; HTML/SVG/anything active is
        # never served inline from this public origin.
        media_type=served_mime,
        filename=row["original_name"],
        content_disposition_type="inline" if inline else "attachment",
        headers=headers,
    )


_thumb_locks: dict[str, asyncio.Lock] = {}
_thumb_lock_guard = asyncio.Lock()
# Bound concurrent Pillow decodes (public endpoints; avoids RAM/CPU pile-ups).
_thumb_semaphore = asyncio.Semaphore(2)


async def serve_thumb(request: Request) -> Response:
    asset_id = request.path_params.get("asset_id", "")
    if not _valid_id(asset_id):
        return _html(ui.render_not_found(), status=404)
    row = store.get_asset(asset_id)
    if not row:
        return _html(ui.render_not_found(), status=404)
    thumb = cfg.THUMB_DIR / f"{asset_id}.jpg"
    if not thumb.is_file():
        async with _thumb_lock_guard:
            lock = _thumb_locks.setdefault(asset_id, asyncio.Lock())
        async with lock:
            async with _thumb_semaphore:
                thumb = await asyncio.to_thread(
                    preview.ensure_thumbnail, row
                ) or thumb
    if not thumb.is_file():
        return _html(ui.render_not_found("No thumbnail available."), status=404)

    await asyncio.to_thread(store.touch_asset, asset_id)
    return FileResponse(
        thumb,
        media_type="image/jpeg",
        headers={
            "cache-control": "public, max-age=31536000, immutable",
            "x-content-type-options": "nosniff",
        },
    )


# --------------------------------------------------------------------------- #
# previews (image raster, video transcode, document render, text)
# --------------------------------------------------------------------------- #

_preview_locks: dict[str, asyncio.Lock] = {}
_preview_lock_guard = asyncio.Lock()
_preview_semaphore = asyncio.Semaphore(2)
_video_semaphore = asyncio.Semaphore(1)


async def serve_preview(request: Request) -> Response:
    asset_id = request.path_params.get("asset_id", "")
    if not _valid_id(asset_id):
        return _html(ui.render_not_found(), status=404)
    row = store.get_asset(asset_id)
    if not row:
        return _html(ui.render_not_found(), status=404)

    kind = preview.preview_kind(row)
    base = cfg.ALBUMS_BASE_PATH
    asset_url = f"{base}/asset/{asset_id}"
    common = {"x-content-type-options": "nosniff"}

    if kind in ("image", "video", "audio"):
        needs_raster = kind == "image" and store_mod.asset_needs_raster_preview(row)
        if kind == "audio":
            return RedirectResponse(asset_url, status_code=302)
        if kind == "video":
            async with _video_semaphore:
                out = await asyncio.to_thread(preview.ensure_video_preview, row)
            if out is None:
                return RedirectResponse(asset_url, status_code=302)
            await asyncio.to_thread(store.touch_asset, asset_id)
            return FileResponse(
                out,
                media_type="video/mp4",
                headers={
                    **common,
                    # Range-capable video: don't let Vercel cache a 206 partial.
                    "cache-control": "no-store",
                    "content-disposition": "inline",
                },
            )
        if not needs_raster:
            return RedirectResponse(asset_url, status_code=302)
        thumb = cfg.PREVIEW_DIR / f"{asset_id}.jpg"
        if not thumb.is_file():
            async with _preview_lock_guard:
                lock = _preview_locks.setdefault(asset_id, asyncio.Lock())
            async with lock:
                async with _preview_semaphore:
                    thumb = await asyncio.to_thread(
                        preview.ensure_preview_image, row
                    ) or thumb
        if not thumb.is_file():
            return _html(ui.render_not_found("Preview unavailable."), status=404)
        await asyncio.to_thread(store.touch_asset, asset_id)
        return FileResponse(
            thumb,
            media_type="image/jpeg",
            headers={
                **common,
                "cache-control": "public, max-age=86400",
                "content-disposition": "inline",
            },
        )

    if kind in ("pdf", "office"):
        out = cfg.PREVIEW_DIR / f"{asset_id}.jpg"
        if not out.is_file():
            async with _preview_lock_guard:
                lock = _preview_locks.setdefault(asset_id, asyncio.Lock())
            async with lock:
                async with _preview_semaphore:
                    out = await asyncio.to_thread(
                        preview.ensure_preview_image, row
                    ) or out
        if not out.is_file():
            return _html(ui.render_not_found("Preview unavailable."), status=404)
        await asyncio.to_thread(store.touch_asset, asset_id)
        return FileResponse(
            out,
            media_type="image/jpeg",
            headers={
                **common,
                "cache-control": "public, max-age=86400",
                "content-disposition": "inline",
                "x-preview-kind": kind,
            },
        )

    if kind in ("text", "archive"):
        text, truncated = await asyncio.to_thread(preview.text_preview, row)
        if not text:
            return PlainTextResponse(
                "No preview available. Download the original file.",
                status_code=200,
                headers={
                    **common,
                    "cache-control": "no-store",
                    "x-preview-empty": "1",
                },
            )
        return PlainTextResponse(
            text,
            media_type="text/plain; charset=utf-8",
            headers={
                **common,
                "cache-control": "public, max-age=300",
                "x-preview-truncated": "1" if truncated else "0",
            },
        )

    return _html(ui.render_not_found("No preview for this file type."), status=404)


# --------------------------------------------------------------------------- #
# ops/status
# --------------------------------------------------------------------------- #

async def stats_api(_request: Request) -> Response:
    data = store.stats()
    data.update(
        {
            "ok": True,
            "preview_bytes": preview.preview_bytes(),
            "preview_budget_bytes": cfg.effective_preview_budget(),
            "rate_window_seconds": cfg.RATE_WINDOW_SECONDS,
            "rate_limit_bytes": cfg.RATE_LIMIT_BYTES,
            "per_ip_rate_bytes": cfg.PER_IP_RATE_BYTES,
            "max_file_bytes": cfg.MAX_FILE_BYTES,
            "max_files": cfg.MAX_FILES,
            "max_request_bytes": cfg.MAX_REQUEST_BYTES,
        }
    )
    return _json(data)


# --------------------------------------------------------------------------- #
# routes (registered before the static catch-all)
# --------------------------------------------------------------------------- #

def routes() -> list[Route]:
    return [
        Route("/albums", photos_page, methods=["GET", "HEAD"]),
        Route("/albums/", photos_page, methods=["GET", "HEAD"]),
        Route("/albums/albums", albums_page, methods=["GET", "HEAD"]),
        Route("/albums/albums/", albums_page, methods=["GET", "HEAD"]),
        Route("/albums/photos", photos_page, methods=["GET", "HEAD"]),
        Route("/albums", create_album, methods=["POST"]),
        Route("/albums/", create_album, methods=["POST"]),

        Route("/albums/api/albums", list_albums_api, methods=["GET"]),
        Route("/albums/api/albums", create_album, methods=["POST"]),
        Route("/albums/api/albums/{album_id}", album_detail_api, methods=["GET"]),
        Route(
            "/albums/api/albums/{album_id}/upload",
            upload_api,
            methods=["POST"],
        ),
        Route(
            "/albums/api/albums/{album_id}/delete",
            delete_album_api,
            methods=["POST", "DELETE"],
        ),
        Route(
            "/albums/api/assets/{asset_id}/delete",
            delete_asset_api,
            methods=["POST", "DELETE"],
        ),
        Route(
            "/albums/api/albums/{album_id}/chunk/init",
            chunk_init_api,
            methods=["POST"],
        ),
        Route(
            "/albums/api/chunks/{upload_id}/complete",
            chunk_complete_api,
            methods=["POST"],
        ),
        Route(
            "/albums/api/chunks/{upload_id}/{index}",
            chunk_put_api,
            methods=["POST", "PUT"],
        ),
        Route("/albums/api/stats", stats_api, methods=["GET"]),

        Route("/albums/asset/{asset_id}", serve_asset, methods=["GET", "HEAD"]),
        Route("/albums/thumb/{asset_id}", serve_thumb, methods=["GET", "HEAD"]),
        Route("/albums/preview/{asset_id}", serve_preview, methods=["GET", "HEAD"]),

        Route("/albums/{album_id}/upload", upload_form, methods=["POST"]),
        Route("/albums/{album_id}/delete", delete_album_form, methods=["POST"]),
        Route("/albums/{album_id}", album_page, methods=["GET", "HEAD"]),
    ]

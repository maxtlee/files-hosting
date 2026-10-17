"""Streaming upload ingestion: SSD staging -> validation -> metadata."""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import BinaryIO, Optional

import albums_config as cfg
import albums_store as store_mod


class UploadError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def sanitize_name(name: Optional[str]) -> str:
    """Display-only filename: strip directories/control chars, cap length.

    The value is never used to build a filesystem path.
    """
    name = (name or "").replace("\x00", "")
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch >= " " and ch != "\x7f").strip()
    return (name or "photo")[:200]


def human_limit(n: int) -> str:
    if n >= 1024**3:
        return f"{n / (1024**3):.1f} GB"
    if n >= 1024**2:
        return f"{n // (1024**2)} MB"
    return f"{max(1, n // 1024)} KB"


def _open_staging(asset_id: str) -> tuple[BinaryIO, Path, bool]:
    """Return (file, path, direct_hdd). Falls back to HDD when the SSD is full."""
    cfg.TMP_DIR.mkdir(parents=True, exist_ok=True)
    part = cfg.TMP_DIR / f"{asset_id}.part"
    try:
        return open(part, "wb"), part, False
    except OSError:
        # SSD unavailable/full: stage directly on the HDD instead of failing.
        cfg.HDD_DIR.mkdir(parents=True, exist_ok=True)
        part = cfg.HDD_DIR / f".{asset_id}.part"
        return open(part, "wb"), part, True


def _copy_to_hdd(part: Path, hdd_path: Path) -> None:
    hdd_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = hdd_path.parent / f".{hdd_path.name}.copy.part"
    with open(part, "rb") as src, open(tmp, "wb") as dst:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)
        dst.flush()
        os.fsync(dst.fileno())
    os.replace(tmp, hdd_path)


def ingest_upload(
    src: BinaryIO,
    *,
    album_id: str,
    original_name: Optional[str],
) -> dict:
    """Stream one uploaded file into the cache (or HDD) and return its DB row.

    Raises ``UploadError`` for validation/limit problems. The caller always owns
    the ``finally`` cleanup of any staging file on failure.
    """
    album = store_mod.store.get_album(album_id)
    if not album:
        raise UploadError("album not found", 404)

    asset_id = uuid.uuid4().hex
    display_name = sanitize_name(original_name)
    size = 0
    digest = hashlib.sha256()

    fh, part, direct_hdd = _open_staging(asset_id)
    try:
        src.seek(0)
        with fh:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > cfg.MAX_FILE_BYTES:
                    raise UploadError(
                        f"file too large (max {human_limit(cfg.MAX_FILE_BYTES)})",
                        413,
                    )
                digest.update(chunk)
                fh.write(chunk)
            fh.flush()
            os.fsync(fh.fileno())
        if size == 0:
            raise UploadError("empty file", 400)

        media = store_mod.probe_media(part, display_name)
        ext = media["ext"]
        mime = media["mime"]
        width = media["width"]
        height = media["height"]

        hdd_path = cfg.HDD_DIR / album_id / f"{asset_id}.{ext}"
        cache_path: Optional[Path] = cfg.CACHE_DIR / album_id / f"{asset_id}.{ext}"
        on_hdd = direct_hdd

        if direct_hdd:
            hdd_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(part, hdd_path)
            on_hdd = True
            cache_path = None
        else:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(part, cache_path)
            except OSError as exc:
                # Cache write failed (e.g. ENOSPC): fall back to the HDD. The
                # upload must never be lost just because the cache is full.
                print(f"[albums] cache commit failed ({exc}); using HDD", flush=True)
                try:
                    _copy_to_hdd(part, hdd_path)
                except OSError as hdd_exc:
                    raise UploadError("storage unavailable", 507) from hdd_exc
                on_hdd = True
                cache_path = None
                try:
                    part.unlink()
                except OSError:
                    pass

        if on_hdd:
            try:
                if hdd_path.stat().st_size != size:
                    raise UploadError("storage verification failed", 500)
            except OSError as exc:
                raise UploadError("storage unavailable", 507) from exc

        row = store_mod.store.insert_asset(
            asset_id=asset_id,
            album_id=album_id,
            original_name=display_name,
            ext=ext,
            mime=mime,
            size=size,
            sha256=digest.hexdigest(),
            width=width,
            height=height,
            cache_path=str(cache_path) if cache_path else None,
            hdd_path=str(hdd_path),
            on_hdd=on_hdd,
        )
        return row
    except Exception:
        # Only ever removes the *staging* file. Committed assets are untouched.
        try:
            if part.exists() and not part.name.endswith(".copy.part"):
                part.unlink()
        except OSError:
            pass
        raise

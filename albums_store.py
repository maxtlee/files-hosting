"""SQLite metadata + filesystem lifecycle for the album system.

Storage model
-------------
* Canonical asset: ``HDD_DIR/<album_id>/<asset_id>.<ext>``.
* Upload cache:    ``CACHE_DIR/<album_id>/<asset_id>.<ext>`` (SSD).
* Thumbnails:      ``THUMB_DIR/<asset_id>.jpg`` (SSD, regenerable).
* Metadata:        ``DB_PATH`` (SSD, gitignored ``data/``).

Shallow deletion only ever flips ``deleted=1`` in SQLite. No code path in this
module unlinks a canonical asset — not on the HDD and not in the cache. The only
``unlink`` calls are for *staging* files (``*.part``), the **cache** copy of an
asset that is already verified on the HDD (LRU eviction), and regenerable
thumbnails.

Uploads are streamed into the SSD cache, inserted into SQLite, and answered
immediately. A background worker copies them to the HDD, verifies SHA-256, then
marks ``on_hdd=1``. Cache eviction only considers rows with ``on_hdd=1`` and
re-checks the HDD file exists with the right size first.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from PIL import Image, ImageOps

try:  # optional iPhone HEIC/HEIF support
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - optional dependency
    pillow_heif = None

try:  # optional JPEG XL support
    import pillow_jxl  # noqa: F401  (registers the Pillow plugin)
except Exception:  # pragma: no cover - optional dependency
    pillow_jxl = None

try:  # optional camera RAW support (DNG/CR2/NEF/ARW/...)
    import rawpy
except Exception:  # pragma: no cover - optional dependency
    rawpy = None

import albums_config as cfg

# Refuse absurdly large decompression bombs while still allowing normal photos.
Image.MAX_IMAGE_PIXELS = 120_000_000

# Browser-renderable image formats we can safely serve inline. Pillow verifies
# the bytes; anything else is accepted as a generic file and served as an
# attachment (never inline HTML/SVG from this origin).
SAFE_IMAGE_FORMATS = {
    "JPEG": ("jpg", "image/jpeg"),
    "MPO": ("jpg", "image/jpeg"),   # iPhone HDR/MPO is JPEG-compatible
    "PNG": ("png", "image/png"),
    "WEBP": ("webp", "image/webp"),
    "GIF": ("gif", "image/gif"),
    "BMP": ("bmp", "image/bmp"),
    "ICO": ("ico", "image/x-icon"),
    "AVIF": ("avif", "image/avif"),
    "HEIF": ("heic", "image/heic"),
    "HEIC": ("heic", "image/heic"),
    "TIFF": ("tiff", "image/tiff"),
    "JXL": ("jxl", "image/jxl"),
    "PSD": ("psd", "image/vnd.adobe.photoshop"),
}
SAFE_IMAGE_MIME = {v[1] for v in SAFE_IMAGE_FORMATS.values()}
SAFE_IMAGE_MIME.update({"image/svg+xml", "image/x-raw"})
RAW_EXT = {
    "dng", "cr2", "cr3", "nef", "arw", "raf", "orf", "rw2", "pef", "srw",
    "x3f", "raw",
}
# Browser-renderable without a raster derivative. Everything else gets a
# server-generated JPEG preview so Chrome/Android and Safari all work.
NEEDS_RASTER_IMAGE_MIME = {
    "image/heic",
    "image/tiff",
    "image/jxl",
    "image/vnd.adobe.photoshop",
    "image/svg+xml",
    "image/x-raw",
}
DIRECT_IMAGE_MIME = SAFE_IMAGE_MIME - NEEDS_RASTER_IMAGE_MIME

# Media that is safe to serve inline by container type. A malicious HTML file
# named .mp4 is still served as video/mp4 + nosniff, which browsers do not
# execute as markup.
VIDEO_MIME_BY_EXT = {
    "mp4": "video/mp4",
    "m4v": "video/x-m4v",
    "mov": "video/quicktime",
    "webm": "video/webm",
    "ogv": "video/ogg",
    "mkv": "video/x-matroska",
    "avi": "video/x-msvideo",
}
AUDIO_MIME_BY_EXT = {
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "wav": "audio/wav",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
    "opus": "audio/ogg",
    "flac": "audio/flac",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS albums (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    deleted     INTEGER NOT NULL DEFAULT 0,
    deleted_at  REAL
);

CREATE TABLE IF NOT EXISTS assets (
    id            TEXT PRIMARY KEY,
    album_id      TEXT NOT NULL,
    original_name TEXT NOT NULL,
    ext           TEXT NOT NULL,
    mime          TEXT NOT NULL,
    size          INTEGER NOT NULL,
    sha256        TEXT NOT NULL,
    width         INTEGER,
    height        INTEGER,
    cache_path    TEXT,
    hdd_path      TEXT NOT NULL,
    on_hdd        INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    last_access   REAL NOT NULL,
    deleted       INTEGER NOT NULL DEFAULT 0,
    deleted_at    REAL,
    notified      INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (album_id) REFERENCES albums(id)
);

CREATE INDEX IF NOT EXISTS idx_assets_album ON assets(album_id, deleted, created_at);
CREATE INDEX IF NOT EXISTS idx_assets_hdd ON assets(on_hdd, deleted);
CREATE INDEX IF NOT EXISTS idx_assets_access ON assets(last_access);

CREATE TABLE IF NOT EXISTS upload_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    bytes    INTEGER NOT NULL,
    ip       TEXT NOT NULL,
    album_id TEXT,
    asset_id TEXT,
    state    TEXT NOT NULL DEFAULT 'reserved'
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON upload_events(ts);
CREATE INDEX IF NOT EXISTS idx_events_ip_ts ON upload_events(ip, ts);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    album_id   TEXT,
    asset_ids  TEXT,
    channel    TEXT,
    status     TEXT,
    detail     TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _dict(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
    return dict(row) if row is not None else None


def _within(path: Path, base: Path) -> bool:
    try:
        return path == base or base in path.parents
    except OSError:
        return False


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _safe_ext(original_name: str) -> str:
    suffix = Path(original_name or "").suffix.lower().lstrip(".")
    cleaned = "".join(ch for ch in suffix if ch.isalnum())
    return cleaned[:12] or "bin"


def probe_media(path: Path, original_name: str) -> dict:
    """Classify an accepted upload.

    Images are verified with Pillow and served inline with their canonical MIME.
    Everything else is accepted as a generic file: the extension decides
    video/audio (safe containers) and all remaining files are served as
    ``application/octet-stream`` + ``Content-Disposition: attachment``.
    """
    ext = _safe_ext(original_name)
    if ext in RAW_EXT and rawpy is not None:
        try:
            with rawpy.imread(str(path)) as raw:
                width, height = raw.sizes.width, raw.sizes.height
            return {
                "kind": "image",
                "ext": ext,
                "mime": "image/x-raw",
                "width": width,
                "height": height,
            }
        except Exception:
            pass
    if ext == "svg":
        try:
            head = path.open("rb").read(8192).lower()
        except OSError:
            head = b""
        if b"<svg" in head:
            return {
                "kind": "image",
                "ext": "svg",
                "mime": "image/svg+xml",
                "width": None,
                "height": None,
            }
    try:
        with Image.open(path) as im:
            fmt = (im.format or "").upper()
            width, height = im.size
            im.verify()
        if fmt in SAFE_IMAGE_FORMATS:
            ext, mime = SAFE_IMAGE_FORMATS[fmt]
            return {
                "kind": "image",
                "ext": ext,
                "mime": mime,
                "width": width,
                "height": height,
            }
    except Exception:
        # Not a decodable image (or a format we do not render): generic file.
        pass

    ext = _safe_ext(original_name)
    if ext in VIDEO_MIME_BY_EXT:
        return {
            "kind": "video",
            "ext": ext,
            "mime": VIDEO_MIME_BY_EXT[ext],
            "width": None,
            "height": None,
        }
    if ext in AUDIO_MIME_BY_EXT:
        return {
            "kind": "audio",
            "ext": ext,
            "mime": AUDIO_MIME_BY_EXT[ext],
            "width": None,
            "height": None,
        }
    return {
        "kind": "file",
        "ext": ext,
        "mime": "application/octet-stream",
        "width": None,
        "height": None,
    }


def asset_kind(asset: dict) -> str:
    """Return ``image`` / ``video`` / ``audio`` / ``file`` for a DB row."""
    mime = (asset.get("mime") or "").lower()
    ext = (asset.get("ext") or "").lower()
    if mime in SAFE_IMAGE_MIME:
        return "image"
    if ext in VIDEO_MIME_BY_EXT and mime == VIDEO_MIME_BY_EXT[ext]:
        return "video"
    if ext in AUDIO_MIME_BY_EXT and mime == AUDIO_MIME_BY_EXT[ext]:
        return "audio"
    return "file"


def asset_is_inline(asset: dict) -> bool:
    kind = asset_kind(asset)
    if kind in ("video", "audio"):
        return True
    if kind == "image":
        return (asset.get("mime") or "").lower() in DIRECT_IMAGE_MIME
    return False


def asset_needs_raster_preview(asset: dict) -> bool:
    return asset_kind(asset) == "image" and (
        asset.get("mime") or ""
    ).lower() in NEEDS_RASTER_IMAGE_MIME


class Store:
    def __init__(self, db_path: Path = cfg.DB_PATH):
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------------ DB
    def connect(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(
                    self.db_path, check_same_thread=False, isolation_level=None
                )
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.executescript(SCHEMA)
                self._conn = conn
            return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.connect().execute(sql, tuple(params))

    def _one(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
        with self._lock:
            return _dict(self._exec(sql, params).fetchone())

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._exec(sql, params).fetchall()]

    # --------------------------------------------------------------- albums
    def create_album(self, title: str, description: str = "") -> dict:
        import uuid

        album_id = uuid.uuid4().hex
        now = time.time()
        with self._lock:
            self._exec(
                "INSERT INTO albums (id, title, description, created_at) VALUES (?,?,?,?)",
                (album_id, title, description, now),
            )
        return {
            "id": album_id,
            "title": title,
            "description": description,
            "created_at": now,
            "deleted": 0,
        }

    def get_album(self, album_id: str) -> Optional[dict]:
        return self._one(
            "SELECT * FROM albums WHERE id=? AND deleted=0", (album_id,)
        )

    def list_albums(self) -> list[dict]:
        return self._all(
            """
            SELECT a.*,
                   (SELECT COUNT(*) FROM assets x
                     WHERE x.album_id=a.id AND x.deleted=0) AS asset_count,
                   (SELECT x.id FROM assets x
                     WHERE x.album_id=a.id AND x.deleted=0
                     ORDER BY (x.mime LIKE 'image/%') DESC,
                              x.created_at DESC LIMIT 1) AS cover_id
              FROM albums a
             WHERE a.deleted=0
             ORDER BY a.created_at DESC
            """
        )

    def mark_album_deleted(self, album_id: str) -> bool:
        now = time.time()
        with self._lock:
            cur = self._exec(
                "UPDATE albums SET deleted=1, deleted_at=? WHERE id=? AND deleted=0",
                (now, album_id),
            )
            if cur.rowcount == 0:
                return False
            # Shallow-hide every asset too. Files are never touched.
            self._exec(
                "UPDATE assets SET deleted=1, deleted_at=? "
                "WHERE album_id=? AND deleted=0",
                (now, album_id),
            )
            return True

    # --------------------------------------------------------------- assets
    def insert_asset(
        self,
        *,
        asset_id: str,
        album_id: str,
        original_name: str,
        ext: str,
        mime: str,
        size: int,
        sha256: str,
        width: int,
        height: int,
        cache_path: Optional[str],
        hdd_path: str,
        on_hdd: bool,
    ) -> dict:
        now = time.time()
        with self._lock:
            self._exec(
                """
                INSERT INTO assets
                    (id, album_id, original_name, ext, mime, size, sha256,
                     width, height, cache_path, hdd_path, on_hdd,
                     created_at, last_access)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    asset_id, album_id, original_name, ext, mime, size, sha256,
                    width, height, cache_path, hdd_path, 1 if on_hdd else 0,
                    now, now,
                ),
            )
        return self.get_asset(asset_id) or {}

    def reclassify_legacy_images(self, limit: int = 500) -> int:
        """Re-probe legacy ``application/octet-stream`` image uploads.

        Before MPO/JXL/RAW support existed, an iPhone MPO/JPEG or other valid
        image could be stored as an opaque file. This only updates metadata
        (mime/dimensions); paths and bytes are untouched.
        """
        image_exts = (
            "jpg", "jpeg", "png", "webp", "gif", "bmp", "ico", "avif",
            "heic", "heif", "tiff", "tif", "jxl", "psd", "svg", "dng",
            "cr2", "cr3", "nef", "arw", "raf", "orf", "rw2", "pef", "srw",
            "x3f", "raw",
        )
        placeholders = ",".join("?" * len(image_exts))
        rows = self._all(
            f"SELECT id, original_name, ext, cache_path, hdd_path "
            f"FROM assets WHERE mime='application/octet-stream' "
            f"AND lower(ext) IN ({placeholders}) LIMIT ?",
            (*image_exts, limit),
        )
        fixed = 0
        for row in rows:
            path = None
            for raw, base in (
                (row.get("cache_path"), cfg.CACHE_DIR),
                (row.get("hdd_path"), cfg.HDD_DIR),
            ):
                if raw:
                    candidate = Path(raw)
                    if _within(candidate, base) and candidate.is_file():
                        path = candidate
                        break
            if path is None:
                continue
            try:
                info = probe_media(path, row["original_name"])
            except Exception:
                continue
            if info.get("kind") != "image":
                continue
            with self._lock:
                self._exec(
                    "UPDATE assets SET mime=?, width=?, height=? WHERE id=?",
                    (info["mime"], info.get("width"), info.get("height"), row["id"]),
                )
            # Drop any icon-card thumb/preview generated while it was opaque;
            # they regenerate from the real image on the next request.
            for stale in (
                cfg.THUMB_DIR / f"{row['id']}.jpg",
                cfg.PREVIEW_DIR / f"{row['id']}.jpg",
            ):
                try:
                    stale.unlink()
                except OSError:
                    pass
            fixed += 1
        return fixed

    def get_asset(self, asset_id: str) -> Optional[dict]:
        return self._one(
            """
            SELECT x.*, a.title AS album_title
              FROM assets x JOIN albums a ON a.id=x.album_id
             WHERE x.id=? AND x.deleted=0 AND a.deleted=0
            """,
            (asset_id,),
        )

    def get_asset_any(self, asset_id: str) -> Optional[dict]:
        return self._one("SELECT * FROM assets WHERE id=?", (asset_id,))

    def list_recent_media(
        self, limit: int = 500, offset: int = 0, query: str = ""
    ) -> list[dict]:
        """Photos/videos across all visible albums, newest first."""
        sql = """
            SELECT x.*, a.title AS album_title
              FROM assets x JOIN albums a ON a.id=x.album_id
             WHERE x.deleted=0 AND a.deleted=0
               AND (x.mime LIKE 'image/%' OR x.mime LIKE 'video/%')
        """
        params: list = []
        if query:
            sql += " AND (x.original_name LIKE ? OR a.title LIKE ?)"
            like = f"%{query}%"
            params += [like, like]
        sql += " ORDER BY x.created_at DESC LIMIT ? OFFSET ?"
        params += [max(1, limit), max(0, offset)]
        return self._all(sql, params)

    def list_assets(self, album_id: str) -> list[dict]:
        return self._all(
            """
            SELECT x.* FROM assets x JOIN albums a ON a.id=x.album_id
             WHERE x.album_id=? AND x.deleted=0 AND a.deleted=0
             ORDER BY x.created_at DESC
            """,
            (album_id,),
        )

    def mark_asset_deleted(self, asset_id: str) -> bool:
        with self._lock:
            cur = self._exec(
                "UPDATE assets SET deleted=1, deleted_at=? "
                "WHERE id=? AND deleted=0",
                (time.time(), asset_id),
            )
            return cur.rowcount > 0

    def touch_asset(self, asset_id: str) -> None:
        try:
            with self._lock:
                self._exec(
                    "UPDATE assets SET last_access=? WHERE id=?",
                    (time.time(), asset_id),
                )
        except sqlite3.Error:
            pass

    def set_on_hdd(self, asset_id: str) -> None:
        with self._lock:
            self._exec("UPDATE assets SET on_hdd=1 WHERE id=?", (asset_id,))

    def set_notified(self, asset_ids: Iterable[str]) -> None:
        ids = list(asset_ids)
        if not ids:
            return
        with self._lock:
            self._exec(
                "UPDATE assets SET notified=1 WHERE id IN (%s)"
                % ",".join("?" * len(ids)),
                ids,
            )

    def pending_copy_ids(self) -> list[str]:
        return [
            r["id"]
            for r in self._all(
                "SELECT id FROM assets WHERE on_hdd=0 ORDER BY created_at ASC"
            )
        ]

    def cache_candidates(self) -> list[dict]:
        return self._all(
            """
            SELECT id, cache_path, hdd_path, size, last_access
              FROM assets
             WHERE on_hdd=1 AND cache_path IS NOT NULL AND cache_path!=''
             ORDER BY last_access ASC
            """
        )

    # ----------------------------------------------------------- rate limit
    def _window_totals(self, now: float) -> tuple[int, dict[str, int]]:
        cutoff = now - cfg.RATE_WINDOW_SECONDS
        rows = self._all(
            "SELECT ip, SUM(bytes) AS b FROM upload_events WHERE ts>? GROUP BY ip",
            (cutoff,),
        )
        total = 0
        per_ip: dict[str, int] = {}
        for r in rows:
            b = int(r["b"] or 0)
            total += b
            per_ip[r["ip"]] = b
        return total, per_ip

    def _retry_after(
        self,
        now: float,
        expected: int,
        *,
        ip: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> int:
        cutoff = now - cfg.RATE_WINDOW_SECONDS
        limit = cfg.RATE_LIMIT_BYTES if limit is None else limit
        if ip is None:
            rows = self._all(
                "SELECT ts, bytes FROM upload_events WHERE ts>? ORDER BY ts ASC",
                (cutoff,),
            )
        else:
            rows = self._all(
                "SELECT ts, bytes FROM upload_events "
                "WHERE ts>? AND ip=? ORDER BY ts ASC",
                (cutoff, ip),
            )
        total = sum(int(r["bytes"]) for r in rows)
        need = total + expected - limit
        wait = 1.0
        for r in rows:
            if need <= 0:
                break
            need -= int(r["bytes"])
            wait = max(wait, float(r["ts"]) + cfg.RATE_WINDOW_SECONDS - now)
        return max(1, int(wait) + 1)

    def rate_reserve(
        self, ip: str, expected: int, album_id: str = ""
    ) -> tuple[bool, int, Optional[int], int]:
        """Atomically check the sliding window and reserve ``expected`` bytes.

        Returns ``(ok, retry_after, event_id, remaining_global)``.
        """
        now = time.time()
        expected = max(0, int(expected))
        with self._lock:
            # Opportunistic cleanup; keeps the table from growing forever.
            self._exec(
                "DELETE FROM upload_events WHERE ts < ?",
                (now - max(cfg.RATE_WINDOW_SECONDS * 4, 3600),),
            )
            total, per_ip = self._window_totals(now)
            ip_total = per_ip.get(ip, 0)
            global_blocked = total + expected > cfg.RATE_LIMIT_BYTES
            ip_blocked = ip_total + expected > cfg.PER_IP_RATE_BYTES
            if global_blocked or ip_blocked:
                waits = [1]
                if global_blocked:
                    waits.append(self._retry_after(now, expected))
                if ip_blocked:
                    waits.append(
                        self._retry_after(
                            now, expected, ip=ip, limit=cfg.PER_IP_RATE_BYTES
                        )
                    )
                return False, max(waits), None, max(
                    0, cfg.RATE_LIMIT_BYTES - total
                )
            cur = self._exec(
                "INSERT INTO upload_events (ts, bytes, ip, album_id, state) "
                "VALUES (?,?,?,?, 'reserved')",
                (now, expected, ip, album_id),
            )
            return True, 0, int(cur.lastrowid), max(
                0, cfg.RATE_LIMIT_BYTES - total - expected
            )

    def rate_finalize(
        self, event_id: Optional[int], actual_bytes: int, state: str = "done"
    ) -> None:
        if event_id is None:
            return
        with self._lock:
            self._exec(
                "UPDATE upload_events SET bytes=?, state=? WHERE id=?",
                (max(0, int(actual_bytes)), state, event_id),
            )

    def rate_cancel(self, event_id: Optional[int]) -> None:
        if event_id is None:
            return
        with self._lock:
            self._exec("DELETE FROM upload_events WHERE id=?", (event_id,))

    # -------------------------------------------------------- notifications
    def add_notification(
        self,
        album_id: str,
        asset_ids: list[str],
        channel: str,
        status: str,
        detail: str = "",
    ) -> None:
        with self._lock:
            self._exec(
                "INSERT INTO notifications "
                "(created_at, album_id, asset_ids, channel, status, detail) "
                "VALUES (?,?,?,?,?,?)",
                (
                    time.time(), album_id, ",".join(asset_ids),
                    channel, status, detail[:2000],
                ),
            )

    # -------------------------------------------------------------- storage
    def copy_asset_to_hdd(self, asset_id: str) -> bool:
        """Copy one cache file to the HDD and verify it. Idempotent."""
        row = self.get_asset_any(asset_id)
        if not row:
            return False
        if not cfg.hdd_mounted():
            return False
        hdd_path = Path(row["hdd_path"])
        cache_path = Path(row["cache_path"]) if row["cache_path"] else None
        if not _within(hdd_path, cfg.HDD_DIR):
            print(f"[albums] refusing suspicious hdd path {hdd_path}", flush=True)
            return False
        if row["on_hdd"] and hdd_path.is_file():
            return True

        # Crash after os.replace but before the DB update: verify the HDD copy.
        if (cache_path is None or not cache_path.is_file()) and hdd_path.is_file():
            if hdd_path.stat().st_size == row["size"] and sha256_file(hdd_path) == row["sha256"]:
                self.set_on_hdd(asset_id)
                return True
            print(f"[albums] HDD copy failed verification: {hdd_path}", flush=True)
            return False

        if cache_path is None or not cache_path.is_file():
            print(f"[albums] cache file missing for {asset_id}", flush=True)
            return False
        if not _within(cache_path, cfg.CACHE_DIR):
            print(f"[albums] refusing suspicious cache path {cache_path}", flush=True)
            return False

        try:
            hdd_path.parent.mkdir(parents=True, exist_ok=True)
            part = hdd_path.parent / f".{hdd_path.name}.part"
            h = hashlib.sha256()
            with open(cache_path, "rb") as src, open(part, "wb") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    h.update(chunk)
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            if h.hexdigest() != row["sha256"] or part.stat().st_size != row["size"]:
                try:
                    part.unlink()
                except OSError:
                    pass
                print(f"[albums] copy hash/size mismatch for {asset_id}", flush=True)
                return False
            os.replace(part, hdd_path)
            _fsync_dir(hdd_path.parent)
        except OSError as exc:
            print(f"[albums] HDD copy failed for {asset_id}: {exc}", flush=True)
            try:
                part.unlink()  # staging only
            except (OSError, UnboundLocalError):
                pass
            return False

        self.set_on_hdd(asset_id)
        return True

    def cache_bytes(self) -> int:
        total = 0
        try:
            for root, _dirs, files in os.walk(cfg.CACHE_DIR):
                for name in files:
                    if name.endswith(".part"):
                        continue
                    try:
                        total += (Path(root) / name).stat().st_size
                    except OSError:
                        pass
        except OSError:
            pass
        return total

    def evict_cache(self, budget: Optional[int] = None) -> int:
        """Drop SSD copies that are already verified on the HDD (LRU).

        NEVER unlinks an asset without a present, size-matching HDD copy.
        Returns bytes freed.
        """
        budget = cfg.effective_cache_budget() if budget is None else budget
        total = self.cache_bytes()
        if total <= budget:
            return 0
        freed = 0
        for row in self.cache_candidates():
            if total - freed <= budget:
                break
            cache = Path(row["cache_path"])
            hdd = Path(row["hdd_path"])
            if not _within(cache, cfg.CACHE_DIR) or not _within(hdd, cfg.HDD_DIR):
                continue
            try:
                if not cache.is_file() or not hdd.is_file():
                    continue
                if hdd.stat().st_size != row["size"] or cache.stat().st_size != row["size"]:
                    continue
                # ``on_hdd=1`` is only set after a SHA-256-verified copy.
                size = cache.stat().st_size
                cache.unlink()  # SSD copy only; canonical HDD file untouched
                freed += size
            except OSError:
                continue
        if total - freed > budget:
            # Still over budget: reclaim old orphan files from failed inserts.
            # These were never acknowledged as successful uploads.
            freed += self.sweep_orphan_cache()
        return freed

    def sweep_orphan_cache(self, older_than: float = 3600) -> int:
        """Delete old cache files with no DB row (failed-insert leftovers)."""
        referenced = {
            str(Path(r["cache_path"]).resolve())
            for r in self._all(
                "SELECT cache_path FROM assets WHERE cache_path IS NOT NULL AND cache_path!=''"
            )
        }
        cutoff = time.time() - older_than
        freed = 0
        for root, _dirs, files in os.walk(cfg.CACHE_DIR):
            for name in files:
                if name.endswith(".part"):
                    continue
                path = Path(root) / name
                try:
                    resolved = str(path.resolve())
                    if resolved in referenced:
                        continue
                    st = path.stat()
                    if st.st_mtime >= cutoff:
                        continue
                    path.unlink()
                    freed += st.st_size
                except OSError:
                    continue
        return freed

    def evict_thumbs(self, budget: Optional[int] = None) -> int:
        """Thumbnails are regenerable; keep their SSD footprint bounded too."""
        budget = cfg.THUMB_BUDGET_BYTES if budget is None else budget
        entries: list[tuple[float, int, Path]] = []
        total = 0
        try:
            for entry in os.scandir(cfg.THUMB_DIR):
                if not entry.is_file():
                    continue
                st = entry.stat()
                entries.append((st.st_mtime, st.st_size, Path(entry.path)))
                total += st.st_size
        except OSError:
            return 0
        if total <= budget:
            return 0
        entries.sort()
        freed = 0
        for _mtime, size, path in entries:
            if total - freed <= budget:
                break
            try:
                path.unlink()
                freed += size
            except OSError:
                pass
        return freed

    def cleanup_temps(self, older_than: float = 3600, include_hdd: bool = True) -> int:
        """Remove stale ``*.part`` staging files (never committed assets)."""
        cutoff = time.time() - older_than
        removed = 0
        bases = (cfg.TMP_DIR, cfg.CACHE_DIR)
        if include_hdd:
            bases = bases + (cfg.HDD_DIR,)
        for base in bases:
            if not base.exists():
                continue
            for root, _dirs, files in os.walk(base):
                for name in files:
                    if not name.endswith(".part"):
                        continue
                    path = Path(root) / name
                    try:
                        if path.stat().st_mtime < cutoff:
                            path.unlink()
                            removed += 1
                    except OSError:
                        pass
        return removed

    def stats(self) -> dict:
        counts = self._one(
            """
            SELECT
              (SELECT COUNT(*) FROM albums WHERE deleted=0) AS albums,
              (SELECT COUNT(*) FROM assets WHERE deleted=0) AS assets,
              (SELECT COUNT(*) FROM assets WHERE deleted=1) AS deleted_assets,
              (SELECT COUNT(*) FROM assets WHERE on_hdd=0) AS pending_hdd,
              (SELECT COUNT(*) FROM assets WHERE deleted=0 AND notified=0) AS unnotified,
              (SELECT COALESCE(SUM(size),0) FROM assets WHERE deleted=0) AS asset_bytes
            """
        ) or {}
        counts.update(
            {
                "cache_bytes": self.cache_bytes(),
                "cache_budget_bytes": cfg.effective_cache_budget(),
                "hdd_dir_available": cfg.HDD_DIR.is_dir() and cfg.hdd_mounted(),
            }
        )
        return counts


store = Store()


def ensure_thumbnail(asset: dict) -> Optional[Path]:
    """Return a JPEG thumbnail path for an asset, generating it if needed."""
    if asset_kind(asset) != "image":
        return None
    asset_id = asset["id"]
    out = cfg.THUMB_DIR / f"{asset_id}.jpg"
    if out.is_file():
        return out
    src: Optional[Path] = None
    if asset.get("cache_path"):
        candidate = Path(asset["cache_path"])
        if _within(candidate, cfg.CACHE_DIR) and candidate.is_file():
            src = candidate
    if src is None:
        candidate = Path(asset["hdd_path"])
        if _within(candidate, cfg.HDD_DIR) and candidate.is_file():
            src = candidate
    if src is None:
        return None

    cfg.THUMB_DIR.mkdir(parents=True, exist_ok=True)
    part = cfg.THUMB_DIR / f".{asset_id}.part"
    try:
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            im = im.convert("RGB") if im.mode not in ("RGB", "L") else im
            im.thumbnail((cfg.THUMB_MAX_PX, cfg.THUMB_MAX_PX), Image.LANCZOS)
            im.save(part, "JPEG", quality=82, optimize=True)
        os.replace(part, out)
        return out
    except Exception as exc:
        print(f"[albums] thumbnail failed for {asset_id}: {exc}", flush=True)
        try:
            part.unlink()
        except OSError:
            pass
        return None

"""Configuration for the public photo-album extension.

Everything is environment-driven so the systemd unit (or ``.env``) is the only
place that needs to change. Defaults are safe for this box:

* canonical assets on the HDD (``/mnt/archive-new/files/albums``)
* an SSD staging/cache directory under the repo's gitignored ``data/``
* a 6 GiB LRU cache budget (the root SSD has ~21 GiB free)
* a 10 GiB / 5 minute global upload limit, plus a 2 GiB / 5 minute per-IP cap
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# ``FILES_ROOT_PATH`` is what the static host uses for generated links. The
# album UI must generate the same public prefix (``/files`` in production,
# empty for direct localhost access).
PREFIX = os.environ.get("FILES_ROOT_PATH", "").rstrip("/")
ALBUMS_BASE_PATH = f"{PREFIX}/albums"

PUBLIC_BASE_URL = os.environ.get(
    "ALBUMS_PUBLIC_BASE_URL",
    "https://agentmaxxer.tail6ca935.ts.net/files",
).rstrip("/")


def _int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


DATA_DIR = Path(
    os.environ.get("ALBUMS_DATA_DIR", str(BASE_DIR / "data"))
).expanduser().resolve()
DB_PATH = Path(
    os.environ.get("ALBUMS_DB_PATH", str(DATA_DIR / "albums.sqlite3"))
).expanduser()

_FILES_DIR = Path(os.environ.get("FILES_DIR", "/mnt/archive-new/files"))
HDD_DIR = Path(
    os.environ.get("ALBUMS_HDD_DIR", str(_FILES_DIR / "albums"))
).expanduser()
HDD_MOUNT = Path(
    os.environ.get("ALBUMS_HDD_MOUNT", "/mnt/archive-new")
).expanduser()
CACHE_DIR = Path(
    os.environ.get("ALBUMS_CACHE_DIR", str(DATA_DIR / "cache"))
).expanduser()
TMP_DIR = Path(
    os.environ.get("ALBUMS_TMP_DIR", str(DATA_DIR / "tmp"))
).expanduser()
CHUNK_DIR = Path(
    os.environ.get("ALBUMS_CHUNK_DIR", str(DATA_DIR / "chunks"))
).expanduser()
THUMB_DIR = Path(
    os.environ.get("ALBUMS_THUMB_DIR", str(DATA_DIR / "thumbs"))
).expanduser()
PREVIEW_DIR = Path(
    os.environ.get("ALBUMS_PREVIEW_DIR", str(DATA_DIR / "previews"))
).expanduser()
NOTIFY_LOG = Path(
    os.environ.get("ALBUMS_NOTIFY_LOG", str(DATA_DIR / "notifications.log"))
).expanduser()

# 6 GiB default. ``effective_cache_budget()`` additionally caps this at half of
# the free space on the cache filesystem so the SSD can never fill up.
CACHE_BUDGET_BYTES = _int("ALBUMS_CACHE_BUDGET_BYTES", 6 * 1024**3, 64 * 1024**2)
THUMB_BUDGET_BYTES = _int("ALBUMS_THUMB_BUDGET_BYTES", 512 * 1024**2, 16 * 1024**2)
PREVIEW_BUDGET_BYTES = _int("ALBUMS_PREVIEW_BUDGET_BYTES", 2 * 1024**3, 64 * 1024**2)
PREVIEW_IMAGE_MAX_PX = _int("ALBUMS_PREVIEW_IMAGE_MAX_PX", 2048, 256)
PREVIEW_VIDEO_HEIGHT = _int("ALBUMS_PREVIEW_VIDEO_HEIGHT", 720, 144)
PREVIEW_VIDEO_TIMEOUT = _int("ALBUMS_PREVIEW_VIDEO_TIMEOUT_SECONDS", 300, 10)
PREVIEW_DOC_TIMEOUT = _int("ALBUMS_PREVIEW_DOC_TIMEOUT_SECONDS", 120, 5)
PREVIEW_TEXT_BYTES = _int("ALBUMS_PREVIEW_TEXT_BYTES", 256 * 1024, 1024)
PREVIEW_VIDEO_MAX_BYTES = _int(
    "ALBUMS_PREVIEW_VIDEO_MAX_BYTES", 1536 * 1024**2, 16 * 1024**2
)
PREVIEW_SANDBOX = _bool("ALBUMS_PREVIEW_SANDBOX", True)

MAX_FILE_BYTES = _int("ALBUMS_MAX_FILE_BYTES", 1024 * 1024**2, 1)
MAX_FILES = _int("ALBUMS_MAX_FILES", 50, 1)
# Per-request body cap. Keeps Starlette's multipart spool bounded; also the
# amount reserved against the global window when Content-Length is absent.
MAX_REQUEST_BYTES = _int("ALBUMS_MAX_REQUEST_BYTES", 2 * 1024**3, 1)
CHUNK_BYTES = _int("ALBUMS_CHUNK_BYTES", 4 * 1024**2, 256 * 1024)
MAX_CHUNK_BYTES = _int("ALBUMS_MAX_CHUNK_BYTES", 8 * 1024**2, CHUNK_BYTES)
CHUNK_TTL_SECONDS = _int("ALBUMS_CHUNK_TTL_SECONDS", 24 * 3600, 3600)
# Refuse uploads when the canonical HDD is nearly full (abuse brake + ops guard).
MIN_FREE_BYTES = _int("ALBUMS_MIN_FREE_BYTES", 5 * 1024**3, 0)
CREATE_MAX_BODY = _int("ALBUMS_CREATE_MAX_BODY_BYTES", 64 * 1024, 1024)

RATE_WINDOW_SECONDS = _int("ALBUMS_RATE_WINDOW_SECONDS", 300, 1)
RATE_LIMIT_BYTES = _int("ALBUMS_RATE_LIMIT_BYTES", 10 * 1024**3, 1)
PER_IP_RATE_BYTES = _int("ALBUMS_PER_IP_RATE_BYTES", 2 * 1024**3, 1)

MAX_TITLE = 200
MAX_DESCRIPTION = 2000
THUMB_MAX_PX = _int("ALBUMS_THUMB_MAX_PX", 512, 64)

# Email is intentionally disabled for now. Set ALBUMS_NOTIFY_ENABLED=1 (and
# SMTP/HTTP settings below) when it is wanted again.
NOTIFY_ENABLED = _bool("ALBUMS_NOTIFY_ENABLED", False)
NOTIFY_TO = os.environ.get("ALBUMS_NOTIFY_TO", "maxwe@cmu.edu")
NOTIFY_FROM = os.environ.get(
    "ALBUMS_SMTP_FROM",
    os.environ.get("ALBUMS_SMTP_USER", "files-albums@maxwe.me"),
)

SMTP_HOST = os.environ.get("ALBUMS_SMTP_HOST", "").strip()
SMTP_PORT = _int("ALBUMS_SMTP_PORT", 587, 1)
SMTP_USER = os.environ.get("ALBUMS_SMTP_USER", "").strip()
SMTP_PASS = os.environ.get("ALBUMS_SMTP_PASS", "")
SMTP_SSL = _bool("ALBUMS_SMTP_SSL", False)          # implicit TLS (port 465)
SMTP_STARTTLS = _bool("ALBUMS_SMTP_STARTTLS", True)  # STARTTLS (port 587)
SMTP_TIMEOUT = _int("ALBUMS_SMTP_TIMEOUT_SECONDS", 10, 1)

# Optional HTTP email fallback (Resend-compatible JSON API). Used only when
# SMTP is *not* configured and a key is present.
RESEND_API_KEY = os.environ.get("ALBUMS_RESEND_API_KEY", "").strip()
EMAIL_API_URL = os.environ.get(
    "ALBUMS_EMAIL_API_URL", "https://api.resend.com/emails"
).strip()


def init_dirs() -> None:
    """Create local dirs. The HDD dir may legitimately fail (unmounted disk)."""
    for path in (DATA_DIR, TMP_DIR, CHUNK_DIR, THUMB_DIR, PREVIEW_DIR, CACHE_DIR):
        path.mkdir(parents=True, exist_ok=True)
    try:
        HDD_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover - host state
        print(f"[albums] cannot create HDD dir {HDD_DIR}: {exc}", flush=True)


def hdd_mounted() -> bool:
    """False when the configured HDD mountpoint is absent/unmounted.

    Only enforced when ``HDD_DIR`` actually lives under ``HDD_MOUNT`` (tests and
    alternate layouts are unaffected). Prevents silently writing album files to
    a root-filesystem directory while the archive disk is missing.
    """
    try:
        if HDD_DIR == HDD_MOUNT or HDD_MOUNT in HDD_DIR.parents:
            return os.path.ismount(str(HDD_MOUNT))
    except OSError:
        return False
    return True


def effective_preview_budget() -> int:
    try:
        free = shutil.disk_usage(PREVIEW_DIR).free
    except OSError:
        return PREVIEW_BUDGET_BYTES
    if free <= 0:
        return 0
    return max(0, min(PREVIEW_BUDGET_BYTES, int(free * 0.5)))


def effective_cache_budget() -> int:
    """Configured budget, capped at half the free space of the cache filesystem."""
    try:
        free = shutil.disk_usage(CACHE_DIR).free
    except OSError:
        return CACHE_BUDGET_BYTES
    if free <= 0:
        return 0
    # Half of free space, never more than the configured budget. No artificial
    # floor: if the SSD is nearly full we evict down instead of pinning 64 MiB.
    return max(0, min(CACHE_BUDGET_BYTES, int(free * 0.5)))

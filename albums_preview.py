"""Preview generation for images, video, documents, text and archives.

Everything here is best-effort and cached under ``data/previews`` /
``data/thumbs``. External converters (ffmpeg, pdftoppm, pdftotext,
LibreOffice, CairoSVG) are run in a bubblewrap sandbox where available:
network unshared, only ``/usr``/``/lib``/``/etc`` plus the one source file and
writable work dir mounted. CPU/memory/file-size limits are applied.
"""

from __future__ import annotations

import io
import json
import os
import re
import resource
import shutil
import subprocess
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont, ImageOps

import albums_config as cfg
import albums_store as store

try:
    import cairosvg
except Exception:  # pragma: no cover - optional
    cairosvg = None

try:
    import rawpy
except Exception:  # pragma: no cover - optional
    rawpy = None

TEXT_EXT = {
    "txt", "text", "md", "markdown", "rst", "log", "csv", "tsv", "json",
    "jsonl", "xml", "yaml", "yml", "toml", "ini", "cfg", "conf", "env",
    "py", "js", "mjs", "cjs", "ts", "tsx", "jsx", "css", "scss", "html",
    "htm", "sh", "bash", "zsh", "fish", "c", "h", "cc", "cpp", "hpp",
    "java", "kt", "go", "rs", "rb", "php", "sql", "lua", "pl", "r",
    "swift", "dart", "vue", "svelte", "tex", "bib", "vcf", "ics", "srt",
    "vtt",
}
OFFICE_EXT = {
    "doc", "docx", "odt", "rtf", "xls", "xlsx", "ods", "ppt", "pptx",
    "odp", "epub", "pages", "key", "numbers",
}
ARCHIVE_EXT = {"zip", "tar", "tgz", "gz", "bz2", "xz", "7z"}
PDF_EXT = {"pdf"}

MONO_FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")
BOLD_FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
SOFFICE = "/usr/lib/libreoffice/program/soffice"
LO_PROFILE = "lo-profile"


# --------------------------------------------------------------------------- #
# source paths
# --------------------------------------------------------------------------- #

def _within(path: Path, base: Path) -> bool:
    try:
        base_r = base.resolve()
        p = path.resolve()
    except (OSError, RuntimeError):
        return False
    return p == base_r or base_r in p.parents


def source_path(asset: dict) -> Optional[Path]:
    for key, base in (("cache_path", cfg.CACHE_DIR), ("hdd_path", cfg.HDD_DIR)):
        raw = asset.get(key)
        if not raw:
            continue
        p = Path(raw)
        if _within(p, base) and p.is_file():
            return p.resolve()
    return None


def preview_kind(asset: dict) -> str:
    kind = store.asset_kind(asset)
    if kind in ("image", "video", "audio"):
        return kind
    ext = (asset.get("ext") or "").lower()
    if ext in PDF_EXT:
        return "pdf"
    if ext in OFFICE_EXT:
        return "office"
    if ext in TEXT_EXT:
        return "text"
    if ext in ARCHIVE_EXT:
        return "archive"
    return "file"


# --------------------------------------------------------------------------- #
# sandboxed subprocess helpers
# --------------------------------------------------------------------------- #

def _limits(timeout: int):
    def apply() -> None:
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout + 10))
            resource.setrlimit(resource.RLIMIT_AS, (4 * 1024**3, 4 * 1024**3))
            resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024**3, 2 * 1024**3))
            resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
        except (OSError, ValueError):
            pass

    return apply


def _bwrap_available() -> bool:
    return bool(cfg.PREVIEW_SANDBOX and shutil.which("bwrap"))


def _run(
    cmd: list[str],
    *,
    work: Path,
    timeout: int,
    source: Optional[Path] = None,
) -> tuple[int, bytes, bytes]:
    """Run a converter, sandboxed when possible. ``cmd`` may use /src and /work."""
    work.mkdir(parents=True, exist_ok=True)
    sandbox_cmd: Optional[list[str]] = None
    direct_cmd: list[str]
    if _bwrap_available():
        sandbox_cmd = [
            "bwrap", "--die-with-parent", "--unshare-all", "--new-session",
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/lib", "/lib",
            "--ro-bind", "/lib64", "/lib64",
            "--ro-bind", "/etc", "/etc",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
            "--bind", str(work), "/work", "--chdir", "/work",
            "--setenv", "HOME", "/work", "--setenv", "TMPDIR", "/tmp",
            "--setenv", "PATH", "/usr/bin:/bin",
        ]
        if source is not None:
            # Expose only the one source file, not its whole album directory.
            sandbox_cmd += [
                "--dir", "/src",
                "--ro-bind", str(source), "/src/" + source.name,
            ]
        sandbox_cmd += cmd
    # Commands are assembled with /src/<name>, /work/<name>, or file:///work/...
    direct_cmd: list[str] = []
    for part in cmd:
        if part.startswith("/src/"):
            direct_cmd.append(str(source.parent / part[5:]) if source else part)
        elif part == "/src":
            direct_cmd.append(str(source.parent) if source else part)
        elif part.startswith("file:///work/"):
            direct_cmd.append("file://" + str(work) + part[len("file:///work"):])
        elif part.startswith("/work/"):
            direct_cmd.append(str(work / part[6:]))
        elif part == "/work":
            direct_cmd.append(str(work))
        else:
            direct_cmd.append(part)

    def invoke(argv: list[str]) -> tuple[int, bytes, bytes]:
        try:
            proc = subprocess.run(
                argv, cwd=str(work), timeout=timeout, capture_output=True,
                preexec_fn=_limits(timeout),
            )
            return proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            return 124, exc.stdout or b"", b"timeout"
        except OSError as exc:
            return 127, b"", str(exc).encode()

    if sandbox_cmd:
        rc, out, err = invoke(sandbox_cmd)
        # If bubblewrap itself failed (not the converter), retry directly.
        if rc != 0 and (b"bwrap:" in err or b"execvp" in err):
            return invoke(direct_cmd)
        return rc, out, err
    return invoke(direct_cmd)


def _cleanup_work(work: Path) -> None:
    try:
        shutil.rmtree(work, ignore_errors=True)
    except OSError:
        pass


def _workdir(asset_id: str) -> Path:
    base = cfg.PREVIEW_DIR / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{asset_id[:12]}-", dir=base))


# --------------------------------------------------------------------------- #
# raster helpers
# --------------------------------------------------------------------------- #

def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = BOLD_FONT if bold else MONO_FONT
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        return ImageFont.load_default()


def _to_rgb(im: Image.Image) -> Image.Image:
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, "white")
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    return im.convert("RGB")


def _save_jpeg(im: Image.Image, out: Path, quality: int = 84) -> bool:
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        part = out.parent / f".{out.name}.part"
        im.convert("RGB").save(part, "JPEG", quality=quality, optimize=True)
        os.replace(part, out)
        return True
    except Exception:
        try:
            part.unlink()
        except (OSError, UnboundLocalError):
            pass
        return False


def _render_svg(source: Path, out: Path, max_px: int) -> bool:
    if cairosvg is None:
        return False
    try:
        if source.stat().st_size > 5 * 1024**2:
            return False
        data = source.read_bytes()
        png = cairosvg.svg2png(bytestring=data, output_width=max_px, unsafe=False)
        with Image.open(io.BytesIO(png)) as im:
            im.thumbnail((max_px, max_px), Image.LANCZOS)
            return _save_jpeg(_to_rgb(im), out)
    except Exception:
        return False


def _render_raw(source: Path, out: Path, max_px: int) -> bool:
    if rawpy is None:
        return False
    try:
        if source.stat().st_size > 300 * 1024**2:
            return False
        with rawpy.imread(str(source)) as raw:
            rgb = raw.postprocess(use_camera_wb=True, half_size=True)
        im = Image.fromarray(rgb)
        im.thumbnail((max_px, max_px), Image.LANCZOS)
        return _save_jpeg(im, out)
    except Exception:
        return False


def _render_image(source: Path, out: Path, max_px: int) -> bool:
    ext = source.suffix.lower().lstrip(".")
    if ext == "svg":
        return _render_svg(source, out, max_px)
    if ext in store.RAW_EXT:
        return _render_raw(source, out, max_px)
    try:
        with Image.open(source) as im:
            im.thumbnail((max_px, max_px), Image.LANCZOS)
            return _save_jpeg(_to_rgb(im), out)
    except Exception:
        return False


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not raw:
            lines.append("")
            continue
        current = ""
        for word in raw.split(" "):
            candidate = word if not current else current + " " + word
            if draw.textlength(candidate, font=font) <= max_width or not current:
                current = candidate
            else:
                lines.append(current)
                current = word
            if len(lines) > 40:
                break
        lines.append(current)
        if len(lines) > 40:
            break
    return lines[:40]


def _text_card(text: str, ext: str, name: str, out: Path, size: int = 512) -> bool:
    im = Image.new("RGB", (size, size), (15, 23, 42))
    draw = ImageDraw.Draw(im)
    draw.rectangle((0, 0, size, 8), fill=(37, 99, 235))
    font = _font(18)
    small = _font(13)
    ext_font = _font(15, bold=True)
    ext_label = (ext or "FILE").upper()
    try:
        tw = draw.textlength(ext_label, font=ext_font)
        draw.rounded_rectangle(
            (size - tw - 26, 20, size - 12, 46), radius=6, fill=(30, 41, 59)
        )
        draw.text((size - tw - 19, 25), ext_label, font=ext_font, fill=(147, 197, 253))
    except Exception:
        pass
    y = 64
    lines = _wrap(draw, text[:4000], font, size - 48)
    for line in lines:
        if y > size - 54:
            break
        draw.text((24, y), line, font=font, fill=(226, 232, 240))
        y += 24
    draw.rectangle((0, size - 34, size, size), fill=(15, 23, 42))
    draw.text((24, size - 26), name[:60], font=small, fill=(148, 163, 184))
    return _save_jpeg(im, out)


def _icon_card(ext: str, label: str, name: str, out: Path, size: int = 512) -> bool:
    im = Image.new("RGB", (size, size), (24, 33, 47))
    draw = ImageDraw.Draw(im)
    draw.rectangle((0, 0, size, 8), fill=(37, 99, 235))
    ext_font = _font(74, bold=True)
    small = _font(14)
    label_font = _font(13, bold=True)
    ext_label = (ext or "FILE").upper()[:6]
    try:
        tw = draw.textlength(ext_label, font=ext_font)
        draw.text(((size - tw) / 2, size / 2 - 78), ext_label, font=ext_font,
                  fill=(226, 232, 240))
        lw = draw.textlength(label, font=label_font)
        draw.text(((size - lw) / 2, size / 2 + 12), label.upper(), font=label_font,
                  fill=(147, 197, 253))
        nw = draw.textlength(name[:44], font=small)
        draw.text(((size - nw) / 2, size - 44), name[:44], font=small,
                  fill=(148, 163, 184))
    except Exception:
        pass
    return _save_jpeg(im, out)


# --------------------------------------------------------------------------- #
# text / archive extraction
# --------------------------------------------------------------------------- #

def _read_text(source: Path, limit: int) -> tuple[str, bool]:
    try:
        with open(source, "rb") as fh:
            raw = fh.read(limit + 1)
    except OSError:
        return "", False
    truncated = len(raw) > limit
    raw = raw[:limit]
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16", errors="replace")
    elif raw.startswith(b"\xef\xbb\xbf"):
        text = raw.decode("utf-8-sig", errors="replace")
    else:
        text = raw.decode("utf-8", errors="replace")
    return text.replace("\x00", ""), truncated


def _archive_listing(source: Path) -> str:
    ext = source.suffix.lower().lstrip(".")
    out: list[str] = []
    try:
        if ext == "zip":
            with zipfile.ZipFile(source) as zf:
                for info in zf.infolist()[:200]:
                    out.append(f"{info.file_size:>12,}  {info.filename}")
                if len(zf.infolist()) > 200:
                    out.append("... (truncated)")
        elif ext in ("tar", "tgz"):
            with tarfile.open(source, "r:*") as tf:
                members = tf.getmembers()[:200]
                for m in members:
                    out.append(f"{m.size:>12,}  {m.name}")
                if len(tf.getmembers()) > 200:
                    out.append("... (truncated)")
        elif ext in ("gz", "bz2", "xz"):
            try:
                with tarfile.open(source, "r:*") as tf:
                    members = tf.getmembers()[:200]
                    for m in members:
                        out.append(f"{m.size:>12,}  {m.name}")
                    if len(tf.getmembers()) > 200:
                        out.append("... (truncated)")
            except Exception:
                out.append(
                    f"Compressed file: {source.name} "
                    f"({source.stat().st_size:,} bytes)"
                )
                out.append("Download the original to decompress it.")
        else:
            out.append(f"Archive: {source.name}")
    except Exception as exc:
        out.append(f"Could not list archive: {exc}")
    return "\n".join(out)


def _pdf_text(source: Path, asset_id: str) -> tuple[str, bool]:
    work = _workdir(asset_id)
    try:
        rc, out, err = _run(
            ["pdftotext", "-f", "1", "-l", "10", "-layout", "/src/" + source.name, "-"],
            work=work, timeout=cfg.PREVIEW_DOC_TIMEOUT, source=source,
        )
        text = out.decode("utf-8", errors="replace")
        return text[: cfg.PREVIEW_TEXT_BYTES], len(out) > cfg.PREVIEW_TEXT_BYTES
    finally:
        _cleanup_work(work)


def _failure_marker(asset_id: str, kind: str) -> Path:
    cfg.PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    return cfg.PREVIEW_DIR / f".{asset_id}.{kind}.failed"


def _recent_failure(asset_id: str, kind: str, ttl: float = 3600) -> bool:
    marker = _failure_marker(asset_id, kind)
    try:
        return marker.is_file() and time.time() - marker.stat().st_mtime < ttl
    except OSError:
        return False


def _mark_failure(asset_id: str, kind: str) -> None:
    try:
        _failure_marker(asset_id, kind).write_text(str(time.time()), "utf-8")
    except OSError:
        pass


def _clear_failure(asset_id: str, kind: str) -> None:
    try:
        _failure_marker(asset_id, kind).unlink()
    except OSError:
        pass


def _office_pdf(source: Path, asset_id: str) -> Optional[Path]:
    cached = cfg.PREVIEW_DIR / f"{asset_id}.pdf"
    if cached.is_file():
        return cached
    if _recent_failure(asset_id, "office"):
        return None
    work = _workdir(asset_id)
    try:
        cmd = [
            "sh", SOFFICE, "--headless", "--norestore", "--nolockcheck",
            "--nodefault", "--nofirststartwizard",
            f"-env:UserInstallation=file:///work/{LO_PROFILE}",
            "--convert-to", "pdf", "--outdir", "/work", "/src/" + source.name,
        ]
        rc, out, err = _run(
            cmd, work=work, timeout=cfg.PREVIEW_DOC_TIMEOUT, source=source
        )
        produced = work / f"{source.stem}.pdf"
        if not produced.is_file():
            # LibreOffice names the output after the input basename; our files
            # are already <asset_id>.<ext>.
            candidates = list(work.glob("*.pdf"))
            produced = candidates[0] if candidates else produced
        if produced.is_file():
            cfg.PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
            part = cfg.PREVIEW_DIR / f".{asset_id}.pdf.part"
            shutil.copyfile(produced, part)
            os.replace(part, cached)
            _clear_failure(asset_id, "office")
            return cached
        print(f"[albums] office conversion failed {asset_id}: {err[-300:]!r}", flush=True)
        _mark_failure(asset_id, "office")
        return None
    finally:
        _cleanup_work(work)


def _office_text(source: Path, asset_id: str) -> tuple[str, bool]:
    pdf = _office_pdf(source, asset_id)
    if not pdf:
        return "", False
    return _pdf_text(pdf, asset_id + "-txt")


# --------------------------------------------------------------------------- #
# document raster + video poster/transcode
# --------------------------------------------------------------------------- #

def _pdf_first_page(pdf: Path, asset_id: str, out: Path, max_px: int) -> bool:
    work = _workdir(asset_id)
    try:
        rc, out_b, err = _run(
            [
                "pdftoppm", "-jpeg", "-f", "1", "-singlefile",
                "-scale-to", str(max_px), "/src/" + pdf.name, "/work/page",
            ],
            work=work, timeout=cfg.PREVIEW_DOC_TIMEOUT, source=pdf,
        )
        produced = work / "page.jpg"
        if produced.is_file():
            out.parent.mkdir(parents=True, exist_ok=True)
            part = out.parent / f".{out.name}.part"
            shutil.copyfile(produced, part)
            os.replace(part, out)
            return True
        print(f"[albums] pdftoppm failed {asset_id}: {err[-200:]!r}", flush=True)
        return False
    finally:
        _cleanup_work(work)


def _document_image(asset: dict, out: Path, max_px: int) -> bool:
    source = source_path(asset)
    if source is None:
        return False
    ext = (asset.get("ext") or "").lower()
    if ext == "pdf":
        return _pdf_first_page(source, asset["id"], out, max_px)
    pdf = _office_pdf(source, asset["id"])
    if not pdf:
        return False
    return _pdf_first_page(pdf, asset["id"], out, max_px)


def _video_info(source: Path, asset_id: str) -> dict:
    work = _workdir(asset_id)
    try:
        rc, out, err = _run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=format_name,duration",
                "-show_entries", "stream=codec_type,codec_name,profile,pix_fmt",
                "-of", "json", "/src/" + source.name,
            ],
            work=work, timeout=60, source=source,
        )
        if rc != 0:
            return {}
        return json.loads(out.decode("utf-8", errors="replace") or "{}")
    except Exception:
        return {}
    finally:
        _cleanup_work(work)


def video_is_browser_safe(info: dict, ext: str = "") -> bool:
    # ffprobe reports Matroska and WebM as "matroska,webm"; trust the on-disk
    # extension for container policy. MOV is Safari-only in practice, so it is
    # transcoded for cross-browser previews too.
    ext = ext.lower().lstrip(".")
    streams = info.get("streams", [])
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    vcodecs = {s.get("codec_name") for s in video_streams}
    acodecs = {s.get("codec_name") for s in streams if s.get("codec_type") == "audio"}
    if not vcodecs:
        return False
    profiles = " ".join(str(s.get("profile") or "") for s in video_streams).lower()
    pix_fmts = " ".join(str(s.get("pix_fmt") or "") for s in video_streams).lower()
    if any(x in profiles for x in ("10", "4:2:2", "4:4:4")):
        return False
    if any(x in pix_fmts for x in ("10", "12", "422", "444")):
        return False
    safe_container = ext in {"mp4", "m4v", "webm"}  # OGV is transcoded
    safe_video = vcodecs <= {"h264", "vp8", "vp9", "av1", "theora"}
    safe_audio = acodecs <= {"aac", "mp3", "opus", "vorbis"} if acodecs else True
    return safe_container and safe_video and safe_audio


def _video_poster(source: Path, asset_id: str, out: Path, max_px: int) -> bool:
    for seek in ("1", "0"):
        work = _workdir(asset_id)
        try:
            rc, _o, _e = _run(
                [
                    "ffmpeg", "-y", "-loglevel", "error", "-ss", seek,
                    "-i", "/src/" + source.name, "-frames:v", "1",
                    "-vf", f"scale=-2:{max_px}", "-q:v", "3", "/work/poster.jpg",
                ],
                work=work, timeout=60, source=source,
            )
            produced = work / "poster.jpg"
            if produced.is_file():
                out.parent.mkdir(parents=True, exist_ok=True)
                part = out.parent / f".{out.name}.part"
                shutil.copyfile(produced, part)
                os.replace(part, out)
                return True
        finally:
            _cleanup_work(work)
    return False


def ensure_video_preview(asset: dict) -> Optional[Path]:
    """Return a browser-playable H.264 MP4, or None when the original is safe."""
    source = source_path(asset)
    if source is None:
        return None
    asset_id = asset["id"]
    info = _video_info(source, asset_id)
    if video_is_browser_safe(info, asset.get("ext") or source.suffix):
        return None
    out = cfg.PREVIEW_DIR / f"{asset_id}.mp4"
    if out.is_file():
        return out
    if _recent_failure(asset_id, "video"):
        return None
    work = _workdir(asset_id)
    try:
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", "/src/" + source.name,
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-vf", f"scale=-2:{cfg.PREVIEW_VIDEO_HEIGHT}",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", "/work/preview.mp4",
        ]
        rc, _out, err = _run(
            cmd, work=work, timeout=cfg.PREVIEW_VIDEO_TIMEOUT, source=source
        )
        produced = work / "preview.mp4"
        if not produced.is_file():
            print(f"[albums] video transcode failed {asset_id}: {err[-300:]!r}", flush=True)
            _mark_failure(asset_id, "video")
            return None
        if produced.stat().st_size > cfg.PREVIEW_VIDEO_MAX_BYTES:
            print(f"[albums] video preview too large for {asset_id}", flush=True)
            _mark_failure(asset_id, "video")
            return None
        cfg.PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        part = cfg.PREVIEW_DIR / f".{asset_id}.mp4.part"
        shutil.copyfile(produced, part)
        os.replace(part, out)
        _clear_failure(asset_id, "video")
        return out
    finally:
        _cleanup_work(work)


# --------------------------------------------------------------------------- #
# public entry points
# --------------------------------------------------------------------------- #

def text_preview(asset: dict) -> tuple[str, bool]:
    source = source_path(asset)
    if source is None:
        return "", False
    kind = preview_kind(asset)
    if kind == "text":
        text, truncated = _read_text(source, cfg.PREVIEW_TEXT_BYTES)
        if (asset.get("ext") or "").lower() == "json" and not truncated:
            try:
                text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
            except Exception:
                pass
        return text, truncated
    if kind == "pdf":
        return _pdf_text(source, asset["id"])
    if kind == "office":
        return _office_text(source, asset["id"])
    if kind == "archive":
        return _archive_listing(source), False
    return "", False


def ensure_preview_image(asset: dict, max_px: Optional[int] = None) -> Optional[Path]:
    """Large raster preview for images / PDFs / Office docs (null when N/A)."""
    max_px = cfg.PREVIEW_IMAGE_MAX_PX if max_px is None else max_px
    out = cfg.PREVIEW_DIR / f"{asset['id']}.jpg"
    if out.is_file():
        return out
    kind = preview_kind(asset)
    source = source_path(asset)
    if source is None:
        return None
    if kind == "image":
        return out if _render_image(source, out, max_px) else None
    if kind in ("pdf", "office"):
        return out if _document_image(asset, out, max_px) else None
    return None


def ensure_thumbnail(asset: dict) -> Optional[Path]:
    """Thumbnail for every asset type; falls back to a generated icon card."""
    out = cfg.THUMB_DIR / f"{asset['id']}.jpg"
    if out.is_file():
        return out
    kind = preview_kind(asset)
    source = source_path(asset)
    if source is None:
        return None
    ext = (asset.get("ext") or "file").upper()
    name = asset.get("original_name") or "file"
    if kind == "image":
        return out if _render_image(source, out, cfg.THUMB_MAX_PX) else _icon_card(
            ext, "image", name, out
        )
    if kind == "video":
        if _video_poster(source, asset["id"], out, cfg.THUMB_MAX_PX):
            return out
        return _icon_card(ext, "video", name, out)
    if kind in ("pdf", "office"):
        if _document_image(asset, out, cfg.THUMB_MAX_PX):
            return out
        return _icon_card(ext, kind, name, out)
    if kind in ("text", "archive"):
        text, _trunc = text_preview(asset)
        if not text:
            text = name
        return out if _text_card(text, ext, name, out) else _icon_card(
            ext, kind, name, out
        )
    if kind == "audio":
        return out if _icon_card(ext, "audio", name, out) else None
    return out if _icon_card(ext, "file", name, out) else None


def evict_previews(budget: Optional[int] = None) -> int:
    """Previews are regenerable; keep their SSD footprint bounded (LRU)."""
    budget = cfg.effective_preview_budget() if budget is None else budget
    entries: list[tuple[float, int, Path]] = []
    total = 0
    try:
        for root, dirs, files in os.walk(cfg.PREVIEW_DIR):
            if Path(root) == cfg.PREVIEW_DIR and "tmp" in dirs:
                dirs.remove("tmp")
            for name in files:
                if name.endswith(".part"):
                    continue
                path = Path(root) / name
                try:
                    st = path.stat()
                except OSError:
                    continue
                entries.append((st.st_mtime, st.st_size, path))
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


def preview_bytes() -> int:
    total = 0
    try:
        for root, dirs, files in os.walk(cfg.PREVIEW_DIR):
            if Path(root) == cfg.PREVIEW_DIR and "tmp" in dirs:
                dirs.remove("tmp")
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


def cleanup_preview_temps(older_than: float = 3600) -> int:
    cutoff = time.time() - older_than
    removed = 0
    # Stale failure markers (24h TTL).
    try:
        for entry in os.scandir(cfg.PREVIEW_DIR):
            if entry.is_file() and entry.name.startswith(".") and entry.name.endswith(".failed"):
                if entry.stat().st_mtime < time.time() - 86400:
                    os.unlink(entry.path)
                    removed += 1
    except OSError:
        pass
    base = cfg.PREVIEW_DIR / "tmp"
    if not base.is_dir():
        return removed
    for entry in os.scandir(base):
        try:
            if entry.stat().st_mtime < cutoff:
                if entry.is_dir():
                    shutil.rmtree(entry.path, ignore_errors=True)
                else:
                    os.unlink(entry.path)
                removed += 1
        except OSError:
            pass
    return removed

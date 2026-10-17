"""Best-effort, asynchronous upload notifications.

Precedence when delivering an email:

1. SMTP if ``ALBUMS_SMTP_HOST`` is set (implicit TLS if ``ALBUMS_SMTP_SSL=1``,
   otherwise STARTTLS when ``ALBUMS_SMTP_STARTTLS=1``).
2. Resend-compatible HTTP API if ``ALBUMS_RESEND_API_KEY`` is set.
3. Local fallback: append a JSON line to ``data/notifications.log`` and leave
   ``assets.notified=0`` (as required when no mail transport is configured).

Delivery happens on a dedicated background worker/thread, never in the upload
response path.
"""

from __future__ import annotations

import html
import json
import smtplib
import ssl
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
import albums_config as cfg
import albums_store as store_mod


def asset_url(asset_id: str) -> str:
    return f"{cfg.PUBLIC_BASE_URL}/albums/asset/{asset_id}"


def album_url(album_id: str) -> str:
    return f"{cfg.PUBLIC_BASE_URL}/albums/{album_id}"


def _build(album: dict, assets: list[dict]) -> tuple[str, str, str]:
    n = len(assets)
    title = album.get("title") or "Untitled album"
    subject = (
        f"[files albums] {n} new photo{'s' if n != 1 else ''} in "
        f"\u201c{title}\u201d"
    )
    lines = [
        f"New upload in album \"{title}\".",
        "",
        f"Album: {album_url(album['id'])}",
        "",
    ]
    for a in assets:
        lines.append(
            f"- {a['original_name']}  ({a['mime']}, "
            f"{a['size'] / 1024 / 1024:.2f} MB)\n  {asset_url(a['id'])}"
        )
    lines += [
        "",
        "This is an automated notification from files.maxwe.me/albums.",
        "Shallow deletes only hide rows; files remain on disk.",
    ]
    text = "\n".join(lines)
    rows = "".join(
        "<li><a href='{u}'>{n}</a> <small>({m}, {s:.2f} MB)</small></li>".format(
            u=html.escape(asset_url(a["id"]), quote=True),
            n=html.escape(a["original_name"]),
            m=html.escape(a["mime"]),
            s=a["size"] / 1024 / 1024,
        )
        for a in assets
    )
    html_body = (
        "<p>New upload in album "
        f"<a href='{html.escape(album_url(album['id']), quote=True)}'>"
        f"{html.escape(title)}</a>.</p><ul>{rows}</ul>"
        "<p style='color:#666'>Automated notification from "
        "files.maxwe.me/albums.</p>"
    )
    return subject, text, html_body


def _append_log(subject: str, text: str, detail: str = "") -> None:
    cfg.NOTIFY_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": time.time(),
        "to": cfg.NOTIFY_TO,
        "subject": subject,
        "text": text,
        "detail": detail,
    }
    with open(cfg.NOTIFY_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _deliver(subject: str, text: str, html_body: str) -> tuple[str, str, str]:
    """Return ``(channel, status, detail)``. Never raises."""
    if cfg.SMTP_HOST:
        try:
            _smtp_send(subject, text, html_body)
            return "smtp", "sent", cfg.SMTP_HOST
        except Exception as exc:
            detail = f"SMTP delivery failed: {exc}"
            try:
                _append_log(subject, text, detail)
            except OSError:
                pass
            return "smtp", "error", detail

    if cfg.RESEND_API_KEY:
        try:
            _http_send(subject, text, html_body)
            return "http", "sent", cfg.EMAIL_API_URL
        except Exception as exc:
            detail = f"HTTP email failed: {exc}"
            try:
                _append_log(subject, text, detail)
            except OSError:
                pass
            return "http", "error", detail

    # No transport configured: required local fallback, notified stays false.
    try:
        _append_log(subject, text, "no SMTP/HTTP transport configured")
        return "log", "logged", str(cfg.NOTIFY_LOG)
    except OSError as exc:
        return "log", "error", f"cannot write notification log: {exc}"


def _smtp_send(subject: str, text: str, html_body: str) -> None:
    msg = EmailMessage()
    msg["From"] = cfg.NOTIFY_FROM
    msg["To"] = cfg.NOTIFY_TO
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")

    if cfg.SMTP_SSL:
        with smtplib.SMTP_SSL(
            cfg.SMTP_HOST, cfg.SMTP_PORT, timeout=cfg.SMTP_TIMEOUT,
            context=ssl.create_default_context(),
        ) as smtp:
            if cfg.SMTP_USER:
                smtp.login(cfg.SMTP_USER, cfg.SMTP_PASS)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(
            cfg.SMTP_HOST, cfg.SMTP_PORT, timeout=cfg.SMTP_TIMEOUT
        ) as smtp:
            smtp.ehlo()
            if cfg.SMTP_STARTTLS:
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            if cfg.SMTP_USER:
                smtp.login(cfg.SMTP_USER, cfg.SMTP_PASS)
            smtp.send_message(msg)


def _http_send(subject: str, text: str, html_body: str) -> None:
    payload = json.dumps(
        {
            "from": cfg.NOTIFY_FROM,
            "to": [cfg.NOTIFY_TO],
            "subject": subject,
            "text": text,
            "html": html_body,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        cfg.EMAIL_API_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {cfg.RESEND_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "files-albums/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=cfg.SMTP_TIMEOUT) as resp:
        if not 200 <= resp.status < 300:
            raise RuntimeError(f"HTTP {resp.status}")


def notify_batch(album: dict, assets: list[dict]) -> dict:
    """Deliver one notification for a batch of uploaded assets."""
    if not assets:
        return {"channel": "none", "status": "skipped"}
    subject, text, html_body = _build(album, assets)
    channel, status, detail = _deliver(subject, text, html_body)
    ids = [a["id"] for a in assets]
    try:
        store_mod.store.add_notification(
            album["id"], ids, channel, status, detail
        )
    except Exception:
        pass
    if status == "sent":
        try:
            store_mod.store.set_notified(ids)
        except Exception:
            pass
    return {"channel": channel, "status": status, "detail": detail}

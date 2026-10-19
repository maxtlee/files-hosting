"""Server-rendered GUI for the public album library.

The layout intentionally follows the Google Photos pattern: persistent left
navigation on desktop, a top search app bar, a date-grouped photo grid, album
cards, full-screen lightbox, Material-style dialogs, and a mobile bottom bar.
It is an independent implementation (no Google assets or branding).
"""

from __future__ import annotations

import html
from datetime import datetime, timedelta

import albums_config as cfg
import albums_preview as preview_mod
import albums_store as store_mod

BASE = cfg.ALBUMS_BASE_PATH
ALBUMS_BASE = BASE + "/albums"


def device_from_user_agent(user_agent: str) -> dict:
    ua = (user_agent or "").lower()
    ios = any(token in ua for token in ("iphone", "ipad", "ipod"))
    android = "android" in ua
    mobile = ios or android or "mobile" in ua
    return {"ios": ios, "android": android, "mobile": mobile}


def _device_classes(device: dict) -> str:
    classes = ["device-mobile" if device.get("mobile") else "device-desktop"]
    if device.get("ios"):
        classes.append("device-ios")
    if device.get("android"):
        classes.append("device-android")
    return " ".join(classes)


_ICONS = {
    "photos": "M21 19V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2ZM8.5 11.5l2.5 3 3.5-4.5 4.5 6H5l3.5-4.5Z",
    "albums": "M4 4h6v6H4V4Zm10 0h6v6h-6V4ZM4 14h6v6H4v-6Zm10 0h6v6h-6v-6Z",
    "add": "M11 5h2v6h6v2h-6v6h-2v-6H5v-2h6V5Z",
    "upload": "M12 3 7 8h3v7h4V8h3l-5-5ZM5 19h14v2H5v-2Z",
    "menu": "M3 6h18v2H3V6Zm0 5h18v2H3v-2Zm0 5h18v2H3v-2Z",
    "search": "M15.5 14h-.79l-.28-.27A6.47 6.47 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5Zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14Z",
    "close": "M18.3 5.71 12 12l6.3 6.29-1.41 1.42L12 14.83l-6.29 6.29-1.42-1.42L10.59 12 4.29 5.71 5.71 4.29 12 10.59l6.29-6.3 1.42 1.42Z",
    "back": "M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2Z",
    "left": "M15.41 7.41 14 6l-6 6 6 6 1.41-1.41L10.83 12l4.58-4.59Z",
    "right": "M8.59 16.59 10 18l6-6-6-6-1.41 1.41L13.17 12l-4.58 4.59Z",
    "download": "M19 9h-4V3H9v6H5l7 7 7-7ZM5 18v2h14v-2H5Z",
    "more": "M12 8a2 2 0 1 0 0-4 2 2 0 0 0 0 4Zm0 6a2 2 0 1 0 0-4 2 2 0 0 0 0 4Zm0 6a2 2 0 1 0 0-4 2 2 0 0 0 0 4Z",
    "check": "M9 16.17 4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41L9 16.17Z",
    "play": "M8 5v14l11-7L8 5Z",
    "folder": "M10 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-8l-2-2Z",
    "camera": "M12 15.2a3.2 3.2 0 1 0 0-6.4 3.2 3.2 0 0 0 0 6.4ZM9 3 7.17 5H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-3.17L15 3H9Z",
}


def _icon(name: str, cls: str = "") -> str:
    path = _ICONS.get(name, "")
    return (
        f'<svg class="ic {cls}" viewBox="0 0 24 24" aria-hidden="true" '
        f'focusable="false"><path d="{path}"/></svg>'
    )


_CSS = r"""
:root {
  color-scheme: light dark;
  --bg: #ffffff; --fg: #202124; --muted: #5f6368; --line: #dadce0;
  --surface: #f1f3f4; --surface-2: #f8f9fa; --hover: #f1f3f4;
  --blue: #1a73e8; --blue-soft: #e8f0fe; --card: #ffffff;
  --shadow: 0 1px 2px rgba(60,64,67,.3), 0 1px 3px 1px rgba(60,64,67,.15);
  --sidebar: 240px; --appbar: 64px;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #202124; --fg: #e8eaed; --muted: #9aa0a6; --line: #3c4043;
    --surface: #2d2e30; --surface-2: #292a2d; --hover: #303134;
    --blue: #8ab4f8; --blue-soft: #1f3047; --card: #202124;
    --shadow: 0 1px 2px rgba(0,0,0,.5), 0 1px 3px 1px rgba(0,0,0,.35);
  }
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html, body { margin: 0; }
body {
  background: var(--bg); color: var(--fg);
  font: 14px/1.45 "Google Sans", Roboto, system-ui, -apple-system, "Segoe UI", sans-serif;
  -webkit-text-size-adjust: 100%;
}
a { color: inherit; }
button { font: inherit; }
.ic { width: 24px; height: 24px; fill: currentColor; display: block; flex: none; }
.ic.sm { width: 18px; height: 18px; }
.app { min-height: 100vh; }
.brand-mark {
  width: 26px; height: 26px; display: grid; grid-template-columns: 1fr 1fr;
  gap: 2px; flex: none; transform: rotate(0deg);
}
.brand-mark i { display: block; border-radius: 50% 50% 50% 6px; }
.brand-mark i:nth-child(1) { background: #4285f4; }
.brand-mark i:nth-child(2) { background: #ea4335; transform: rotate(90deg); }
.brand-mark i:nth-child(3) { background: #34a853; transform: rotate(270deg); }
.brand-mark i:nth-child(4) { background: #fbbc04; transform: rotate(180deg); }
.sidebar {
  position: fixed; inset: 0 auto 0 0; width: var(--sidebar); z-index: 40;
  display: flex; flex-direction: column; padding: 8px 12px;
  background: var(--bg); border-right: 1px solid var(--line);
}
.brand {
  display: flex; align-items: center; gap: 12px; text-decoration: none;
  padding: 14px 12px 24px; font-size: 20px; font-weight: 500; color: var(--fg);
}
.nav { display: grid; gap: 2px; }
.nav-link {
  display: flex; align-items: center; gap: 16px; height: 44px; padding: 0 16px;
  border: 0; background: transparent; color: var(--fg); text-decoration: none;
  border-radius: 0 999px 999px 0; font-weight: 500; font-size: 14px;
  cursor: pointer; text-align: left; white-space: nowrap;
}
.nav-link:hover { background: var(--hover); }
.nav-link.active { background: var(--blue-soft); color: var(--blue); }
.nav-sep { height: 1px; background: var(--line); margin: 12px 8px; }
.sidebar-spacer { flex: 1; }
.sidebar-foot {
  border-top: 1px solid var(--line); padding: 12px 12px 16px;
  color: var(--muted); font-size: 12px; display: grid; gap: 8px;
}
.sidebar-foot a { text-decoration: none; }
.sidebar-foot a:hover { text-decoration: underline; }
.main { margin-left: var(--sidebar); min-height: 100vh; }
.appbar {
  position: sticky; top: 0; z-index: 30; display: flex; align-items: center;
  gap: 16px; height: var(--appbar); padding: 8px 24px;
  background: var(--bg);
}
.appbar-brand { display: none; align-items: center; gap: 10px; text-decoration: none; font-size: 18px; font-weight: 500; }
.appbar-title { display: none; font-size: 20px; font-weight: 500; }
.search {
  flex: 1; max-width: 720px; height: 48px; display: flex; align-items: center;
  gap: 10px; padding: 0 16px; background: var(--surface);
  border-radius: 999px; color: var(--muted);
}
.search input {
  flex: 1; min-width: 0; border: 0; outline: 0; background: transparent;
  color: var(--fg); font: inherit; font-size: 16px;
}
.search input::placeholder { color: var(--muted); }
.appbar-actions { margin-left: auto; display: flex; align-items: center; gap: 8px; }
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  height: 40px; padding: 0 20px; border-radius: 999px; border: 1px solid var(--line);
  background: var(--bg); color: var(--fg); text-decoration: none; font-weight: 500;
  font-size: 14px; cursor: pointer; white-space: nowrap;
}
.btn:hover { background: var(--hover); }
.btn.primary { background: var(--blue); color: #fff; border-color: transparent; }
.btn.primary:hover { filter: brightness(1.05); }
.btn.ghost { border-color: transparent; background: transparent; }
.btn.ghost:hover { background: var(--hover); }
.iconbtn {
  width: 40px; height: 40px; border: 0; border-radius: 50%; background: transparent;
  color: var(--fg); display: grid; place-items: center; cursor: pointer; text-decoration: none;
}
.iconbtn:hover { background: var(--hover); }
.avatar {
  width: 32px; height: 32px; border-radius: 50%; background: #5f6368;
  color: #fff; display: grid; place-items: center; font-weight: 600; font-size: 14px;
  margin-left: 4px;
}
.content { padding: 8px 24px 96px; max-width: 1600px; margin: 0 auto; }
.page-head {
  display: flex; align-items: flex-end; justify-content: space-between;
  gap: 16px; margin: 16px 0 18px; flex-wrap: wrap;
}
h1 { font-size: 26px; line-height: 1.2; font-weight: 400; margin: 0 0 4px; letter-spacing: -.01em; }
.sub { color: var(--muted); font-size: 13px; margin: 0; }
.day { margin-top: 24px; }
.day h2 {
  font-size: 14px; font-weight: 500; color: var(--fg); margin: 0 0 10px;
  position: sticky; top: var(--appbar); z-index: 5; background: var(--bg);
  padding: 6px 0; display: inline-block;
}
.grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 4px;
}
.tile {
  position: relative; aspect-ratio: 1; border-radius: 8px; overflow: hidden;
  background: var(--surface); cursor: pointer; isolation: isolate;
}
.tile img { width: 100%; height: 100%; object-fit: cover; display: block; }
.tile:hover img { filter: brightness(.94); }
.tile .select {
  position: absolute; top: 8px; left: 8px; width: 24px; height: 24px; padding: 0;
  border-radius: 50%; border: 2px solid #fff; background: rgba(0,0,0,.18);
  color: #fff; display: grid; place-items: center; z-index: 3; cursor: pointer;
  opacity: 0; transition: opacity .12s, background .12s; box-shadow: 0 0 0 1px rgba(0,0,0,.18);
  pointer-events: none;
}
.tile:hover .select, .tile.selected .select, body.selection-mode .tile .select {
  opacity: 1; pointer-events: auto;
}
.tile .select .ic { width: 16px; height: 16px; opacity: 0; }
.tile.selected .select { background: var(--blue); border-color: var(--blue); }
.tile.selected .select .ic { opacity: 1; }
.tile.selected::after {
  content: ""; position: absolute; inset: 0; border: 3px solid var(--blue);
  border-radius: 8px; pointer-events: none; z-index: 2;
}
.tile.selected img { filter: brightness(.78); }
.tile .play {
  position: absolute; left: 50%; top: 50%; width: 44px; height: 44px;
  transform: translate(-50%, -50%); border-radius: 50%;
  background: rgba(0,0,0,.45); color: #fff; display: grid; place-items: center;
  pointer-events: none; z-index: 1;
}
.tile .play .ic { width: 22px; height: 22px; margin-left: 2px; }
.albums-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 22px;
}
.album-card { text-decoration: none; color: inherit; display: block; border-radius: 12px; }
.album-cover {
  position: relative; aspect-ratio: 4 / 3; border-radius: 12px; overflow: hidden;
  background: linear-gradient(135deg, var(--blue-soft), var(--surface));
  display: grid; place-items: center;
}
.album-cover img { width: 100%; height: 100%; object-fit: cover; display: block; }
.album-cover .ph { color: var(--muted); }
.album-cover .ph .ic { width: 42px; height: 42px; }
.album-card:hover .album-cover { filter: brightness(.95); }
.album-meta { padding: 10px 2px 0; }
.album-title { font-size: 14px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.album-count { font-size: 12px; color: var(--muted); margin-top: 2px; }
.album-head {
  display: flex; gap: 14px; align-items: flex-start; flex-wrap: wrap;
  margin: 6px 0 20px;
}
.album-head .grow { flex: 1; min-width: 220px; }
.album-head h1 { font-size: 28px; }
.album-desc { color: var(--muted); margin: 2px 0 0; white-space: pre-wrap; }
.album-actions { display: flex; gap: 6px; align-items: center; }
.progress { height: 4px; border-radius: 4px; background: var(--surface); overflow: hidden; margin: 10px 0; display: none; }
.progress > i { display: block; height: 100%; width: 0; background: var(--blue); transition: width .12s; }
.upload-status { min-height: 18px; font-size: 12px; color: var(--muted); }
.upload-status.err { color: #d93025; }
.empty { text-align: center; padding: 72px 20px; color: var(--muted); }
.empty .ic { width: 64px; height: 64px; margin: 0 auto 16px; opacity: .55; }
.empty h2 { font-size: 18px; font-weight: 500; color: var(--fg); margin: 0 0 6px; }
.empty p { margin: 0 auto 18px; max-width: 44ch; }
.empty-actions { display: flex; gap: 10px; justify-content: center; flex-wrap: wrap; }
.chip { display: inline-flex; align-items: center; gap: 8px; background: var(--surface); border-radius: 999px; padding: 6px 12px; font-size: 12px; color: var(--muted); }
.mobile-only { display: none; }
.bottom-nav { display: none; }
.selection-bar {
  position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%);
  z-index: 70; display: flex; align-items: center; gap: 8px;
  background: #202124; color: #fff; border-radius: 999px; padding: 6px 6px 6px 20px;
  box-shadow: 0 6px 24px rgba(0,0,0,.35); white-space: nowrap;
}
.selection-bar button {
  border: 0; background: transparent; color: #fff; font-weight: 500;
  padding: 9px 14px; border-radius: 999px; cursor: pointer;
}
.selection-bar button:hover { background: rgba(255,255,255,.14); }
.selection-bar .primary { color: #8ab4f8; }
.toast {
  position: fixed; left: 50%; bottom: 90px; transform: translateX(-50%);
  background: #323232; color: #fff; padding: 12px 18px; border-radius: 8px;
  z-index: 90; box-shadow: 0 6px 20px rgba(0,0,0,.3); font-size: 13px;
}
dialog {
  border: 0; padding: 0; border-radius: 28px; background: var(--card);
  color: var(--fg); max-width: min(92vw, 520px); box-shadow: var(--shadow);
}
dialog::backdrop { background: rgba(0,0,0,.5); }
.dlg { padding: 24px; }
.dlg h2 { margin: 0 0 8px; font-size: 22px; font-weight: 400; }
.dlg p { margin: 0 0 12px; color: var(--muted); font-size: 14px; }
.field { display: grid; gap: 6px; margin: 16px 0; font-size: 12px; color: var(--muted); font-weight: 500; }
input[type=text], textarea, select {
  width: 100%; border: 1px solid var(--line); border-radius: 8px; padding: 12px;
  background: var(--bg); color: var(--fg); font: inherit; font-size: 15px; outline: none;
}
input[type=text]:focus, textarea:focus, select:focus { border-color: var(--blue); }
textarea { resize: vertical; min-height: 72px; }
.dlg-actions { display: flex; justify-content: flex-end; gap: 4px; margin-top: 24px; }
.textbtn {
  border: 0; background: transparent; color: var(--blue); font-weight: 500;
  padding: 10px 16px; border-radius: 999px; cursor: pointer;
}
.textbtn:hover { background: var(--blue-soft); }
.upload-area {
  border: 2px dashed var(--line); border-radius: 16px; padding: 24px;
  text-align: center; background: var(--surface-2); margin-top: 8px;
}
.upload-area.drag { border-color: var(--blue); background: var(--blue-soft); }
.upload-area p { margin: 0 0 10px; color: var(--muted); }
.hint { color: var(--muted); font-size: 12px; margin-top: 10px; }
dialog.lightbox {
  width: 100vw; height: 100vh; max-width: none; max-height: none; margin: 0;
  border-radius: 0; background: #000; color: #fff; overflow: hidden;
}
dialog.lightbox::backdrop { background: #000; }
.lb-top {
  position: absolute; inset: 0 0 auto 0; z-index: 4; display: flex;
  align-items: center; gap: 12px; padding: 10px 16px;
  background: linear-gradient(rgba(0,0,0,.7), transparent);
}
.lb-name { flex: 1; min-width: 0; font-size: 14px; color: #e8eaed; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.lb-icon {
  width: 42px; height: 42px; border: 0; border-radius: 50%; background: transparent;
  color: #fff; display: grid; place-items: center; cursor: pointer; text-decoration: none;
}
.lb-icon:hover { background: rgba(255,255,255,.14); }
.lb-pill {
  border: 1px solid rgba(255,255,255,.3); background: rgba(255,255,255,.12);
  color: #fff; border-radius: 999px; height: 38px; padding: 0 16px; cursor: pointer;
  font-weight: 500; white-space: nowrap;
}
.lb-stage { height: 100%; display: grid; place-items: center; padding: 66px 72px 72px; }
.lb-stage img, .lb-stage video {
  max-width: 100%; max-height: 100%; object-fit: contain; display: block;
}
.lb-stage audio { width: min(80vw, 560px); }
.lb-doc { text-align: center; color: #ddd; max-width: 100%; max-height: 100%; }
.lb-doc img { max-width: min(92vw, 1100px); max-height: 78vh; object-fit: contain; background: #fff; border-radius: 4px; }
.lb-doc p { color: #aaa; font-size: 12px; margin: 8px 0 0; }
.lb-doc a { color: #8ab4f8; }
.lb-text {
  display: none; margin: 0; width: min(92vw, 1000px); max-height: 78vh; overflow: auto;
  padding: 18px 20px; background: #17181b; color: #e8eaed; border-radius: 8px;
  font: 12px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  white-space: pre-wrap; overflow-wrap: anywhere; text-align: left;
}
.lb-file { padding: 30px; text-align: center; color: #ddd; }
.lb-file .ext { font-size: 42px; font-weight: 700; display: block; margin-bottom: 8px; }
.lb-file a { color: #8ab4f8; }
.lb-nav {
  position: absolute; top: 50%; transform: translateY(-50%); z-index: 4;
  width: 48px; height: 48px; border: 0; border-radius: 50%;
  background: rgba(255,255,255,.12); color: #fff; display: grid; place-items: center;
  cursor: pointer;
}
.lb-nav:hover { background: rgba(255,255,255,.22); }
.lb-prev { left: 14px; }
.lb-next { right: 14px; }
.drop-overlay {
  position: fixed; inset: 0; z-index: 100; display: none; place-items: center;
  background: rgba(26,115,232,.12); border: 4px dashed var(--blue);
  color: var(--blue); font-size: 20px; font-weight: 500;
}
body.dragging .drop-overlay { display: grid; }
@media (max-width: 900px) {
  .sidebar { transform: translateX(-100%); transition: transform .18s ease; box-shadow: var(--shadow); width: min(80vw, 300px); }
  body.nav-open .sidebar { transform: none; }
  .scrim { display: none; }
  body.nav-open .scrim { display: block; position: fixed; inset: 0; z-index: 35; background: rgba(0,0,0,.4); }
  .main { margin-left: 0; }
  .appbar { flex-wrap: wrap; height: auto; padding: 8px 12px 10px; gap: 8px; }
  .appbar-brand { display: flex; }
  .appbar-title { display: none; }
  .appbar .search { order: 3; flex-basis: 100%; max-width: none; height: 44px; }
  .appbar-actions .new-album-btn, .appbar-actions .upload-btn { display: none; }
  .menu-btn { display: grid; }
  .content { padding: 4px 10px 104px; }
  h1 { font-size: 22px; }
  .grid { grid-template-columns: repeat(auto-fill, minmax(104px, 1fr)); gap: 2px; }
  .tile { border-radius: 4px; }
  .tile .select { width: 22px; height: 22px; top: 6px; left: 6px; }
  .albums-grid { grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 14px; }
  .album-cover { border-radius: 8px; }
  .day h2 { top: 112px; }
  body[data-view="photos"] .page-head h1 { display: none; }
  .mobile-only { display: inline-flex; }
  .bottom-nav {
    display: flex; position: fixed; left: 0; right: 0; bottom: 0; z-index: 50;
    height: calc(58px + env(safe-area-inset-bottom));
    padding-bottom: env(safe-area-inset-bottom);
    background: var(--bg); border-top: 1px solid var(--line);
  }
  .bottom-nav a, .bottom-nav button {
    flex: 1; border: 0; background: transparent; color: var(--muted);
    text-decoration: none; display: flex; flex-direction: column; align-items: center;
    justify-content: center; gap: 2px; font-size: 11px; font-weight: 500; cursor: pointer;
  }
  .bottom-nav .active { color: var(--blue); }
  .selection-bar { bottom: calc(70px + env(safe-area-inset-bottom)); }
  .lb-stage { padding: 58px 8px 60px; }
  .lb-nav { width: 42px; height: 42px; }
  .lb-prev { left: 4px; }
  .lb-next { right: 4px; }
  .lb-pill { font-size: 12px; padding: 0 12px; }
  .lb-name { font-size: 12px; }
}
@media (min-width: 901px) {
  .menu-btn, .scrim { display: none !important; }
}
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
"""

_DEVICE_JS = r"""
(function () {
  "use strict";
  var body = document.body;
  var ua = navigator.userAgent || "";
  var isIOS = /iPad|iPhone|iPod/.test(ua) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  var isAndroid = /Android/i.test(ua);
  if (isIOS) { body.classList.add("device-mobile", "device-ios"); }
  else if (isAndroid) { body.classList.add("device-mobile", "device-android"); }
  else if (/Mobi/i.test(ua)) { body.classList.add("device-mobile"); }
  if (!isIOS && !isAndroid && !/Mobi/i.test(ua)) {
    body.classList.remove("device-mobile");
    body.classList.add("device-desktop");
  }
  if (location.hash === "#new") {
    setTimeout(function () {
      var btn = document.querySelector("[data-new-album]");
      if (btn) btn.click();
    }, 120);
  }
})();
"""

_UPLOAD_JS = r"""
(function () {
  "use strict";
  var BASE = document.body.dataset.base || "/albums";

  function message(v) {
    if (v === null || v === undefined) return "";
    if (typeof v === "string") return v;
    if (typeof v === "number" || typeof v === "boolean") return String(v);
    if (Array.isArray(v)) {
      return v.map(message).filter(Boolean).join("; ");
    }
    if (typeof v === "object") {
      var keys = ["message", "error", "detail", "reason", "description"];
      for (var i = 0; i < keys.length; i++) {
        if (v[keys[i]] !== undefined) {
          var m = message(v[keys[i]]);
          if (m) return m;
        }
      }
      try { return JSON.stringify(v); } catch (e) { return ""; }
    }
    return String(v);
  }
  function errorFrom(status, text, statusText) {
    var data = null;
    try { data = JSON.parse(text); } catch (e) {}
    if (data) {
      var m = message(data.error) || message(data.detail) ||
              message(data.message) || message(data.errors);
      if (m) return m.length > 500 ? m.slice(0, 500) + "…" : m;
    }
    if (status === 0) {
      return "Connection lost during upload. Check your network and tap Upload again.";
    }
    if (status === 413) return "This file is too large for the upload limit.";
    if (status === 429) return "Upload rate limit reached. Try again shortly.";
    if (text && text.length < 300 && text.indexOf("<") !== 0) return text;
    return "Upload failed (HTTP " + status + (statusText ? " " + statusText : "") + ").";
  }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function putChunk(uploadId, index, blob, onProgress) {
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open("POST", BASE + "/api/chunks/" + encodeURIComponent(uploadId) + "/" + index);
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.setRequestHeader("Accept", "application/json");
      xhr.timeout = 180000;
      xhr.upload.onprogress = function (e) {
        if (e.lengthComputable && onProgress) onProgress(e.loaded);
      };
      xhr.onload = function () {
        var data = null;
        try { data = JSON.parse(xhr.responseText); } catch (e) {}
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(data || {});
        } else {
          var err = new Error(errorFrom(xhr.status, xhr.responseText, xhr.statusText));
          err.status = xhr.status;
          reject(err);
        }
      };
      xhr.onerror = function () {
        var err = new Error(errorFrom(0, "", ""));
        err.status = 0;
        reject(err);
      };
      xhr.ontimeout = xhr.onerror;
      xhr.send(blob);
    });
  }
  async function uploadOne(file, albumId, onProgress) {
    var init = await fetch(BASE + "/api/albums/" + encodeURIComponent(albumId) + "/chunk/init", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify({ name: file.name, size: file.size, mime: file.type || "application/octet-stream" })
    });
    var initData = null;
    try { initData = await init.json(); } catch (e) {}
    if (!init.ok) throw new Error(errorFrom(init.status, JSON.stringify(initData || {}), init.statusText));
    var uploadId = initData.upload_id;
    var chunkBytes = initData.chunk_bytes || 4194304;
    var sent = 0, index = 0;
    while (sent < file.size) {
      var end = Math.min(sent + chunkBytes, file.size);
      var blob = file.slice(sent, end);
      var attempt = 0;
      for (;;) {
        try {
          await putChunk(uploadId, index, blob, function (loaded) {
            if (onProgress) onProgress(sent + loaded, file.size);
          });
          break;
        } catch (err) {
          attempt++;
          var retryable = !err.status || err.status >= 500 || err.status === 0;
          if (!retryable || attempt >= 4) throw err;
          await sleep(attempt * 1000);
        }
      }
      sent = end;
      index++;
      if (onProgress) onProgress(sent, file.size);
    }
    var done = await fetch(BASE + "/api/chunks/" + encodeURIComponent(uploadId) + "/complete", {
      method: "POST",
      headers: { "Accept": "application/json" }
    });
    var doneData = null;
    try { doneData = await done.json(); } catch (e) {}
    if (!done.ok) throw new Error(errorFrom(done.status, JSON.stringify(doneData || {}), done.statusText));
    return doneData.asset;
  }
  async function uploadFiles(files, albumId, opts) {
    opts = opts || {};
    var total = 0, done = 0, assets = [];
    for (var i = 0; i < files.length; i++) total += files[i].size;
    for (var j = 0; j < files.length; j++) {
      if (opts.onFile) opts.onFile(j + 1, files.length, files[j].name);
      var asset = await uploadOne(files[j], albumId, function (loaded) {
        if (opts.onProgress) opts.onProgress(done + loaded, total);
      });
      done += files[j].size;
      if (opts.onProgress) opts.onProgress(done, total);
      assets.push(asset);
    }
    return assets;
  }
  window.AlbumsUpload = { uploadFiles: uploadFiles, errorFrom: errorFrom, message: message };
})();
"""

_LAYOUT_JS = r"""
(function () {
  "use strict";
  var body = document.body;
  var BASE = body.dataset.base || "/albums";
  var menu = document.getElementById("menu-btn");
  var scrim = document.getElementById("scrim");
  function closeNav() { body.classList.remove("nav-open"); }
  if (menu) menu.addEventListener("click", function () { body.classList.toggle("nav-open"); });
  if (scrim) scrim.addEventListener("click", closeNav);
  document.querySelectorAll(".sidebar a, .sidebar .nav-link").forEach(function (el) {
    el.addEventListener("click", closeNav);
  });

  function openDialog(id) {
    document.querySelectorAll("dialog[open]").forEach(function (d) { d.close(); });
    var d = document.getElementById(id);
    if (d) d.showModal();
  }
  document.querySelectorAll("[data-new-album]").forEach(function (el) {
    el.addEventListener("click", function (e) {
      e.preventDefault();
      closeNav();
      openDialog("new-album-dialog");
    });
  });
  document.querySelectorAll("[data-close]").forEach(function (el) {
    el.addEventListener("click", function () {
      var d = el.closest("dialog");
      if (d) d.close();
    });
  });

  // ---- global upload dialog --------------------------------------------
  var upDialog = document.getElementById("global-upload-dialog");
  var upSelect = document.getElementById("global-upload-album");
  var upFiles = document.getElementById("global-upload-files");
  var upPick = document.getElementById("global-upload-pick");
  var upGo = document.getElementById("global-upload-go");
  var upStatus = document.getElementById("global-upload-status");
  var upBar = document.getElementById("global-upload-progress");
  var upFill = upBar ? upBar.querySelector("i") : null;
  var upArea = document.getElementById("global-upload-area");
  var pending = null;

  function setUploadStatus(msg, cls) {
    if (!upStatus) return;
    upStatus.textContent = msg || "";
    upStatus.className = "upload-status" + (cls ? " " + cls : "");
  }
  function openUpload() {
    closeNav();
    if (!upDialog) return;
    var def = body.dataset.defaultAlbum || "";
    if (upSelect && def) upSelect.value = def;
    setUploadStatus("");
    if (upBar) upBar.style.display = "none";
    upDialog.showModal();
  }
  document.querySelectorAll("[data-open-upload]").forEach(function (el) {
    el.addEventListener("click", function (e) { e.preventDefault(); openUpload(); });
  });
  if (upPick && upFiles) upPick.addEventListener("click", function () { upFiles.click(); });
  if (upFiles) upFiles.addEventListener("change", function () {
    pending = upFiles.files;
    setUploadStatus(pending && pending.length ? pending.length + " file(s) selected" : "");
  });
  if (upArea) {
    ["dragenter", "dragover"].forEach(function (ev) {
      upArea.addEventListener(ev, function (e) { e.preventDefault(); upArea.classList.add("drag"); });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      upArea.addEventListener(ev, function (e) { e.preventDefault(); upArea.classList.remove("drag"); });
    });
    upArea.addEventListener("drop", function (e) {
      if (e.dataTransfer && e.dataTransfer.files) {
        pending = e.dataTransfer.files;
        setUploadStatus(pending.length + " file(s) selected");
      }
    });
  }
  if (upGo) upGo.addEventListener("click", function () {
    var album = upSelect ? upSelect.value : "";
    if (!album) { setUploadStatus("Create an album first.", "err"); return; }
    if (!pending || !pending.length) { setUploadStatus("Choose files first.", "err"); return; }
    var files = Array.prototype.slice.call(pending);
    upGo.disabled = true;
    if (upBar) { upBar.style.display = "block"; upFill.style.width = "0%"; }
    setUploadStatus("Preparing upload…");
    AlbumsUpload.uploadFiles(files, album, {
      onFile: function (i, n, name) {
        setUploadStatus("Uploading file " + i + " of " + n + " — " + name + "…");
      },
      onProgress: function (done, total) {
        if (upFill && total) upFill.style.width = Math.min(100, Math.round(done / total * 100)) + "%";
      }
    }).then(function () {
      setUploadStatus("Uploaded. Opening album…", "ok");
      location.href = BASE + "/" + encodeURIComponent(album);
    }).catch(function (err) {
      upGo.disabled = false;
      if (upBar) upBar.style.display = "none";
      setUploadStatus(err && err.message ? err.message : "Upload failed.", "err");
    });
  });
  if (upDialog) upDialog.addEventListener("close", function () {
    pending = null;
    if (upFiles) upFiles.value = "";
    setUploadStatus("");
  });
})();
"""

_GRID_JS = r"""
(function () {
  "use strict";
  var body = document.body;
  var BASE = body.dataset.base || "/albums";
  var gridRoots = document.querySelectorAll("[data-grid]");
  if (!gridRoots.length) return;
  var selected = new Set();
  var bar = document.getElementById("selection-bar");
  var countEl = document.getElementById("selection-count");
  var toastEl = document.getElementById("toast");
  var isIOS = body.classList.contains("device-ios");
  var isMobile = body.classList.contains("device-mobile");
  var shareSupported = !!(navigator.share && navigator.canShare);
  var shareCache = {};
  var current = -1;
  var textToken = 0;
  var currentFull = "", currentName = "", currentMime = "";
  var currentShare = "", currentShareName = "", currentShareMime = "";

  function tiles() {
    return Array.prototype.slice.call(document.querySelectorAll("[data-grid] .tile"));
  }
  function toast(msg) {
    if (!toastEl) return;
    toastEl.textContent = msg;
    toastEl.hidden = false;
    clearTimeout(toastEl._t);
    toastEl._t = setTimeout(function () { toastEl.hidden = true; }, 2800);
  }
  function updateSelection() {
    var n = selected.size;
    document.querySelectorAll("[data-grid] .tile").forEach(function (t) {
      t.classList.toggle("selected", selected.has(t.dataset.id));
    });
    if (bar) bar.hidden = n === 0;
    if (countEl) countEl.textContent = n + " selected";
    body.classList.toggle("selection-mode", n > 0);
  }
  document.addEventListener("click", function (e) {
    var sel = e.target.closest("[data-select-asset]");
    if (!sel) return;
    e.preventDefault();
    e.stopPropagation();
    var id = sel.getAttribute("data-select-asset");
    if (selected.has(id)) selected.delete(id); else selected.add(id);
    updateSelection();
  });
  var clear = document.getElementById("selection-clear");
  if (clear) clear.addEventListener("click", function () { selected.clear(); updateSelection(); });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && selected.size) { selected.clear(); updateSelection(); }
  });
  // Mobile long-press selection (Google Photos-style). Desktop uses the
  // hover circle.
  var pressTimer = null, suppressClick = false;
  function cancelPress() {
    if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
  }
  document.addEventListener("pointerdown", function (e) {
    var tile = e.target.closest("[data-grid] .tile");
    if (!tile || e.target.closest("[data-select-asset]")) return;
    if (e.pointerType === "mouse") return;
    cancelPress();
    pressTimer = setTimeout(function () {
      pressTimer = null;
      suppressClick = true;
      var id = tile.dataset.id;
      if (selected.has(id)) selected.delete(id); else selected.add(id);
      updateSelection();
      if (navigator.vibrate) navigator.vibrate(12);
      setTimeout(function () { suppressClick = false; }, 1200);
    }, 450);
  }, { passive: true });
  ["pointerup", "pointermove", "pointercancel"].forEach(function (ev) {
    document.addEventListener(ev, cancelPress, { passive: true });
  });

  // ---- dialogs / hide ---------------------------------------------------
  var confirmDlg = document.getElementById("confirm-dialog");
  var confirmTitle = document.getElementById("confirm-title");
  var confirmText = document.getElementById("confirm-text");
  var confirmYes = document.getElementById("confirm-yes");
  var confirmNo = document.getElementById("confirm-no");
  var pendingAction = null;
  function ask(title, text, action) {
    if (!confirmDlg) { if (window.confirm(text)) action(); return; }
    confirmTitle.textContent = title;
    confirmText.textContent = text;
    pendingAction = action;
    confirmDlg.showModal();
  }
  if (confirmYes) confirmYes.addEventListener("click", function () {
    var fn = pendingAction; pendingAction = null;
    if (confirmDlg) confirmDlg.close();
    if (fn) fn();
  });
  if (confirmNo) confirmNo.addEventListener("click", function () {
    pendingAction = null;
    if (confirmDlg) confirmDlg.close();
  });
  function postJSON(url) {
    return fetch(url, { method: "POST", headers: { "Accept": "application/json" } });
  }
  var hideBtn = document.getElementById("selection-hide");
  if (hideBtn) hideBtn.addEventListener("click", function () {
    var ids = Array.from(selected);
    if (!ids.length) return;
    ask("Hide " + ids.length + " item(s)?",
        "They disappear from the site, but every file stays on disk and is never deleted.",
        function () {
          Promise.all(ids.map(function (id) {
            return postJSON(BASE + "/api/assets/" + encodeURIComponent(id) + "/delete")
              .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); });
          })).then(function () {
            selected.clear(); updateSelection(); location.reload();
          }).catch(function (err) { toast("Could not hide: " + err.message); });
        });
  });
  var albumHide = document.querySelector("[data-delete-album]");
  if (albumHide) albumHide.addEventListener("click", function () {
    var album = body.dataset.album || "";
    if (!album) return;
    ask("Hide this album?",
        "The album and its items disappear from the site. Nothing is deleted from disk.",
        function () {
          postJSON(BASE + "/api/albums/" + encodeURIComponent(album) + "/delete")
            .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); location.href = BASE + "/albums"; })
            .catch(function (err) { toast("Could not hide album: " + err.message); });
        });
  });

  // ---- lightbox ---------------------------------------------------------
  var lb = document.getElementById("lightbox");
  var lbImg = document.getElementById("lb-img");
  var lbVideo = document.getElementById("lb-video");
  var lbAudio = document.getElementById("lb-audio");
  var lbDoc = document.getElementById("lb-doc");
  var lbDocImg = document.getElementById("lb-doc-img");
  var lbDocDownload = document.getElementById("lb-doc-download");
  var lbText = document.getElementById("lb-text");
  var lbFile = document.getElementById("lb-file");
  var lbFileExt = document.getElementById("lb-file-ext");
  var lbFileName = document.getElementById("lb-file-name");
  var lbDownload = document.getElementById("lb-download");
  var lbName = document.getElementById("lb-name");
  var lbOpen = document.getElementById("lb-open");
  var lbSave = document.getElementById("lb-save");
  var lbPrev = document.getElementById("lb-prev");
  var lbNext = document.getElementById("lb-next");

  function stopMedia() {
    textToken += 1;
    [lbVideo, lbAudio].forEach(function (el) {
      if (!el) return;
      try { el.pause(); } catch (e) {}
      el.removeAttribute("src");
      try { el.load(); } catch (e) {}
      el.style.display = "none";
    });
    if (lbImg) { lbImg.removeAttribute("src"); lbImg.style.display = "none"; }
    if (lbDocImg) lbDocImg.removeAttribute("src");
    if (lbDoc) lbDoc.style.display = "none";
    if (lbText) { lbText.textContent = ""; lbText.style.display = "none"; }
    if (lbFile) lbFile.style.display = "none";
  }
  function showFilePanel(name, ext, full) {
    if (!lbFile) return;
    lbFile.style.display = "block";
    if (lbFileExt) lbFileExt.textContent = (ext || "FILE").toUpperCase();
    if (lbFileName) lbFileName.textContent = name;
    if (lbDownload) lbDownload.href = full;
  }
  function showTextPreview(tile, url, name) {
    if (!lbText) { showFilePanel(name, tile.dataset.ext, tile.dataset.full); return; }
    var token = ++textToken;
    lbText.textContent = "Loading preview…";
    lbText.style.display = "block";
    fetch(url, { headers: { "Accept": "text/plain" } }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.text();
    }).then(function (text) {
      if (token !== textToken) return;
      if (!text || text.indexOf("No preview available.") === 0) throw new Error("empty");
      lbText.textContent = text;
    }).catch(function () {
      if (token !== textToken) return;
      lbText.style.display = "none";
      showFilePanel(name, tile.dataset.ext, tile.dataset.full);
    });
  }
  function originalFile(url, name, mime) {
    if (!shareCache[url]) {
      shareCache[url] = fetch(url).then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.blob();
      }).then(function (blob) {
        var type = mime || blob.type || "application/octet-stream";
        try { return new File([blob], name || "file", { type: type }); }
        catch (e) { blob.name = name || "file"; return blob; }
      }).catch(function (err) { delete shareCache[url]; throw err; });
    }
    return shareCache[url];
  }
  function resetSaveLabel() {
    if (!lbSave) return;
    lbSave.textContent = isIOS ? "Save to Photos" : "Save / share";
    lbSave.disabled = false;
  }
  function saveCurrent() {
    if (!currentShare) return;
    var url = currentShare, name = currentShareName, mime = currentShareMime;
    if (lbSave) { lbSave.disabled = true; lbSave.textContent = "Preparing…"; }
    var retry = false;
    originalFile(url, name, mime).then(function (file) {
      if (navigator.canShare && navigator.share && navigator.canShare({ files: [file] })) {
        return navigator.share({ files: [file], title: name });
      }
      throw new Error("share-unsupported");
    }).catch(function (err) {
      if (err && err.name === "AbortError") return;
      if (err && err.name === "NotAllowedError") { retry = true; return; }
      window.location.href = currentFull || url;
    }).finally(function () {
      if (retry && lbSave) {
        lbSave.disabled = false;
        lbSave.textContent = "Tap Save again";
        setTimeout(resetSaveLabel, 4000);
      } else { resetSaveLabel(); }
    });
  }
  if (lbSave) {
    lbSave.textContent = isIOS ? "Save to Photos" : "Save / share";
    lbSave.hidden = !isMobile || !shareSupported;
    lbSave.addEventListener("click", saveCurrent);
  }
  if (lbOpen) {
    if (isMobile) lbOpen.title = "Save or download";
    lbOpen.addEventListener("click", function (e) {
      if (isMobile && (currentKindIs("image") || currentKindIs("video")) &&
          navigator.share && navigator.canShare) {
        e.preventDefault();
        saveCurrent();
      }
    });
  }
  var currentKind = "";
  function currentKindIs(k) { return currentKind === k; }

  function show(i) {
    var t = tiles();
    if (!t.length) return;
    current = (i + t.length) % t.length;
    var tile = t[current];
    var kind = tile.dataset.previewKind || tile.dataset.kind || "image";
    var full = tile.dataset.full;
    var previewUrl = tile.dataset.preview || full;
    var thumb = tile.dataset.thumb;
    var name = tile.dataset.name || "File";
    var mime = tile.dataset.mime || "";
    currentKind = kind;
    currentFull = full;
    currentName = name;
    currentMime = mime;
    currentShare = full;
    currentShareName = name;
    currentShareMime = mime;
    if (kind === "video") {
      currentShare = previewUrl;
      currentShareName = name.replace(/\.[^.]+$/, "") + ".mp4";
      currentShareMime = "video/mp4";
    } else if (kind === "image") {
      var iosUrl = tile.dataset.shareIos || "";
      var safeUrl = tile.dataset.shareSafe || full;
      currentShare = isIOS && iosUrl ? iosUrl : safeUrl;
      if (currentShare !== full) {
        currentShareName = name.replace(/\.[^.]+$/, "") + ".jpg";
        currentShareMime = "image/jpeg";
      }
    }
    stopMedia();
    if (lbSave) {
      lbSave.hidden = !isMobile || !shareSupported ||
                      (kind !== "image" && kind !== "video");
      lbSave.disabled = false;
      lbSave.textContent = isIOS ? "Save to Photos" : "Save / share";
    }
    var size = parseInt(tile.dataset.size || "0", 10);
    if (isMobile && (kind === "image" || kind === "video") &&
        (!size || size <= 50 * 1024 * 1024)) {
      originalFile(currentShare, currentShareName, currentShareMime).catch(function () {});
    }
    if (kind === "image" && lbImg) {
      lbImg.onerror = function () { if (thumb && lbImg.src !== thumb) lbImg.src = thumb; };
      lbImg.src = previewUrl; lbImg.alt = name; lbImg.style.display = "block";
    } else if (kind === "video" && lbVideo) {
      lbVideo.src = previewUrl;
      if (thumb) lbVideo.poster = thumb;
      lbVideo.style.display = "block";
      try { lbVideo.load(); } catch (e) {}
    } else if (kind === "audio" && lbAudio) {
      lbAudio.src = previewUrl; lbAudio.style.display = "block";
    } else if ((kind === "pdf" || kind === "office") && lbDoc) {
      lbDoc.style.display = "block";
      if (lbDocImg) {
        lbDocImg.onerror = function () {
          lbDoc.style.display = "none";
          showFilePanel(name, tile.dataset.ext, full);
        };
        lbDocImg.src = previewUrl; lbDocImg.alt = name;
      }
      if (lbDocDownload) lbDocDownload.href = full;
    } else if (kind === "text" || kind === "archive") {
      showTextPreview(tile, previewUrl, name);
    } else {
      showFilePanel(name, tile.dataset.ext, full);
    }
    if (lbName) lbName.textContent = (current + 1) + " / " + t.length + " — " + name;
    if (lbOpen) lbOpen.href = full;
    if (lb && !lb.open) lb.showModal();
  }
  document.querySelectorAll("[data-grid]").forEach(function (root) {
    root.addEventListener("click", function (e) {
      if (e.target.closest("[data-select-asset]")) return;
      var tile = e.target.closest(".tile");
      if (!tile) return;
      if (suppressClick) { suppressClick = false; return; }
      if (body.classList.contains("selection-mode")) {
        var id = tile.dataset.id;
        if (selected.has(id)) selected.delete(id); else selected.add(id);
        updateSelection();
        return;
      }
      show(tiles().indexOf(tile));
    });
  });
  if (lbPrev) lbPrev.addEventListener("click", function () { show(current - 1); });
  if (lbNext) lbNext.addEventListener("click", function () { show(current + 1); });
  if (lb) {
    lb.addEventListener("keydown", function (e) {
      if (e.key === "ArrowLeft") { e.preventDefault(); show(current - 1); }
      if (e.key === "ArrowRight") { e.preventDefault(); show(current + 1); }
    });
    lb.addEventListener("close", function () { stopMedia(); shareCache = {}; });
  }
  var lbClose = document.getElementById("lb-close");
  if (lbClose) lbClose.addEventListener("click", function () { lb.close(); });
})();
"""

_ALBUM_UPLOAD_JS = r"""
(function () {
  "use strict";
  var body = document.body;
  var BASE = body.dataset.base || "/albums";
  var ALBUM = body.dataset.album || "";
  if (!ALBUM) return;
  var MAX_FILE = parseInt(body.dataset.maxFile || "0", 10);
  var MAX_FILES = parseInt(body.dataset.maxFiles || "50", 10);
  var MAX_REQUEST = parseInt(body.dataset.maxRequest || "0", 10);
  var fileInput = document.getElementById("file-input");
  var cameraInput = document.getElementById("camera-input");
  var bar = document.getElementById("upload-bar");
  var fill = bar ? bar.querySelector("i") : null;
  var statusEl = document.getElementById("upload-status");
  var dragging = 0;

  function say(msg, cls) {
    if (!statusEl) return;
    statusEl.textContent = msg || "";
    statusEl.className = "upload-status" + (cls ? " " + cls : "");
  }
  function mb(n) {
    if (n >= 1073741824) return (n / 1073741824).toFixed(2) + " GB";
    return (n / 1048576).toFixed(1) + " MB";
  }
  function upload(files) {
    if (!files || !files.length) return;
    if (files.length > MAX_FILES) { say("Too many files (max " + MAX_FILES + ").", "err"); return; }
    var list = [], total = 0;
    for (var i = 0; i < files.length; i++) {
      var f = files[i];
      if (MAX_FILE && f.size > MAX_FILE) { say("Skipped " + f.name + ": over " + mb(MAX_FILE), "err"); continue; }
      total += f.size; list.push(f);
    }
    if (!list.length) return;
    if (MAX_REQUEST && total > MAX_REQUEST) { say("Selection is " + mb(total) + "; max " + mb(MAX_REQUEST), "err"); return; }
    if (!window.AlbumsUpload) { say("Upload helper unavailable. Reload the page.", "err"); return; }
    if (bar) { bar.style.display = "block"; fill.style.width = "0%"; }
    say("Preparing upload…");
    AlbumsUpload.uploadFiles(list, ALBUM, {
      onFile: function (i, n, name) { say("Uploading file " + i + " of " + n + " — " + name + "…"); },
      onProgress: function (done, totalBytes) {
        if (fill && totalBytes) fill.style.width = Math.min(100, Math.round(done / totalBytes * 100)) + "%";
      }
    }).then(function () {
      if (bar) bar.style.display = "none";
      say("Uploaded. Loading…", "ok");
      setTimeout(function () { location.reload(); }, 300);
    }).catch(function (err) {
      if (bar) bar.style.display = "none";
      say(err && err.message ? err.message : "Upload failed.", "err");
    });
  }
  document.querySelectorAll("[data-upload-trigger]").forEach(function (el) {
    el.addEventListener("click", function () { if (fileInput) fileInput.click(); });
  });
  document.querySelectorAll("[data-camera-trigger]").forEach(function (el) {
    el.addEventListener("click", function () { if (cameraInput) cameraInput.click(); });
  });
  if (fileInput) fileInput.addEventListener("change", function () { upload(fileInput.files); fileInput.value = ""; });
  if (cameraInput) cameraInput.addEventListener("change", function () { upload(cameraInput.files); cameraInput.value = ""; });
  ["dragenter", "dragover"].forEach(function (ev) {
    document.addEventListener(ev, function (e) {
      if (!e.dataTransfer || Array.prototype.indexOf.call(e.dataTransfer.types, "Files") < 0) return;
      e.preventDefault(); dragging++; body.classList.add("dragging");
    });
  });
  ["dragleave", "drop"].forEach(function (ev) {
    document.addEventListener(ev, function (e) {
      if (!e.dataTransfer) return;
      e.preventDefault();
      dragging = Math.max(0, dragging - 1);
      if (ev === "drop" || dragging === 0) body.classList.remove("dragging");
      if (ev === "drop" && e.dataTransfer.files) upload(e.dataTransfer.files);
    });
  });
})();
"""


def _fmt_size(n: int) -> str:
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < step or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= step
    return f"{n:.1f} TB"


def _date_label(ts: float) -> str:
    dt = datetime.fromtimestamp(ts)
    today = datetime.now().date()
    d = dt.date()
    if d == today:
        return "Today"
    if d == today - timedelta(days=1):
        return "Yesterday"
    return dt.strftime("%b %d, %Y").replace(" 0", " ")


def _groups(items: list[dict]) -> list[tuple[str, list[dict]]]:
    out: list[tuple[str, list[dict]]] = []
    current = None
    for item in items:
        label = _date_label(item["created_at"])
        if current is None or current[0] != label:
            current = (label, [])
            out.append(current)
        current[1].append(item)
    return out


def _tile(a: dict) -> str:
    aid = a["id"]
    name = a.get("original_name") or "file"
    kind = preview_mod.preview_kind(a)
    full = f"{BASE}/asset/{aid}"
    thumb = f"{BASE}/thumb/{aid}"
    preview = f"{BASE}/preview/{aid}"
    mime = a.get("mime") or "application/octet-stream"
    ext = (a.get("ext") or "file").upper()[:6]
    direct_image = mime in store_mod.DIRECT_IMAGE_MIME
    if kind == "video":
        share_safe = share_ios = preview
    elif kind == "image":
        share_safe = full if direct_image else preview
        share_ios = full if mime == "image/heic" else share_safe
    else:
        share_safe = share_ios = full
    play = (
        f'<span class="play">{_icon("play")}</span>'
        if kind == "video" else ""
    )
    return (
        f'<div class="tile" data-id="{html.escape(aid)}" '
        f'data-kind="{html.escape(kind)}" data-preview-kind="{html.escape(kind)}" '
        f'data-full="{html.escape(full)}" data-preview="{html.escape(preview)}" '
        f'data-name="{html.escape(name)}" data-mime="{html.escape(mime)}" '
        f'data-size="{int(a.get("size") or 0)}" data-ext="{html.escape(ext)}" '
        f'data-thumb="{html.escape(thumb)}" '
        f'data-share-safe="{html.escape(share_safe)}" '
        f'data-share-ios="{html.escape(share_ios)}">'
        f'<img src="{html.escape(thumb)}" alt="{html.escape(name)}" loading="lazy" decoding="async">'
        + play
        + f'<button class="select" type="button" data-select-asset="{html.escape(aid)}" '
          f'aria-label="Select {html.escape(name)}">{_icon("check")}</button>'
        + "</div>"
    )


def _grid_sections(items: list[dict]) -> str:
    groups = _groups(items)
    if not groups:
        return ""
    out = []
    for label, group in groups:
        tiles = "".join(_tile(a) for a in group)
        out.append(
            f'<section class="day"><h2>{html.escape(label)}</h2>'
            f'<div class="grid" data-grid>{tiles}</div></section>'
        )
    return "".join(out)


def _brand() -> str:
    return (
        '<span class="brand-mark" aria-hidden="true">'
        "<i></i><i></i><i></i><i></i></span>"
    )


def _nav_link(href: str, icon: str, label: str, active: bool) -> str:
    cls = "nav-link active" if active else "nav-link"
    return f'<a class="{cls}" href="{html.escape(href)}">{_icon(icon)}<span>{html.escape(label)}</span></a>'


def _sidebar(view: str, albums: list[dict]) -> str:
    total = sum(int(a.get("asset_count") or 0) for a in albums)
    active_albums = view in ("albums", "album")
    return (
        '<aside class="sidebar" id="sidebar">'
        f'<a class="brand" href="{html.escape(BASE)}">{_brand()}<span>Photos</span></a>'
        '<nav class="nav">'
        + _nav_link(BASE, "photos", "Photos", view == "photos")
        + _nav_link(ALBUMS_BASE, "albums", "Albums", active_albums)
        + "</nav>"
        '<div class="nav-sep"></div>'
        '<nav class="nav">'
        '<button class="nav-link" type="button" data-new-album>'
        + _icon("add") + "<span>New album</span></button>"
        '<button class="nav-link" type="button" data-open-upload>'
        + _icon("upload") + "<span>Upload</span></button>"
        "</nav>"
        '<div class="sidebar-spacer"></div>'
        '<div class="sidebar-foot">'
        f'<a href="{html.escape(cfg.PREFIX + "/")}">File host</a>'
        f"<span>{total} item{'s' if total != 1 else ''} in {len(albums)} album"
        f"{'s' if len(albums) != 1 else ''}</span>"
        "</div></aside>"
    )


def _appbar(view: str, query: str, title: str) -> str:
    action = ALBUMS_BASE if view == "albums" else BASE
    return (
        '<header class="appbar">'
        '<button class="iconbtn menu-btn" id="menu-btn" type="button" aria-label="Menu">'
        + _icon("menu") + "</button>"
        f'<a class="appbar-brand" href="{html.escape(BASE)}">{_brand()}<span>Photos</span></a>'
        f'<span class="appbar-title">{html.escape(title)}</span>'
        f'<form class="search" method="get" action="{html.escape(action)}">'
        + _icon("search")
        + f'<input type="search" name="q" value="{html.escape(query)}" '
          'placeholder="Search your photos" aria-label="Search">'
        "</form>"
        '<div class="appbar-actions">'
        '<button class="btn primary new-album-btn" type="button" data-new-album>New album</button>'
        '<button class="iconbtn upload-btn" type="button" data-open-upload aria-label="Upload">'
        + _icon("upload") + "</button>"
        '<span class="avatar" title="Public albums">M</span>'
        "</div></header>"
    )


def _bottom_nav(view: str) -> str:
    a_photos = "active" if view == "photos" else ""
    a_albums = "active" if view in ("albums", "album") else ""
    return (
        '<nav class="bottom-nav" aria-label="Main navigation">'
        f'<a class="{a_photos}" href="{html.escape(BASE)}">{_icon("photos")}<span>Photos</span></a>'
        f'<a class="{a_albums}" href="{html.escape(ALBUMS_BASE)}">{_icon("albums")}<span>Albums</span></a>'
        '<button type="button" data-open-upload>' + _icon("upload") + "<span>Upload</span></button>"
        '<button type="button" data-new-album>' + _icon("add") + "<span>New</span></button>"
        "</nav>"
    )


def _dialogs(albums: list[dict], current_album_id: str = "") -> str:
    opts = []
    for a in albums:
        selected = " selected" if a["id"] == current_album_id else ""
        opts.append(
            f'<option value="{html.escape(a["id"])}"{selected}>'
            f'{html.escape(a["title"])} ({int(a.get("asset_count") or 0)})</option>'
        )
    if not opts:
        opts.append('<option value="">No albums yet</option>')
    no_albums = "" if albums else (
        '<p class="hint">No albums yet. <button class="textbtn" type="button" '
        'data-new-album>Create an album</button> first.</p>'
    )
    return (
        '<dialog id="new-album-dialog" class="dialog">'
        f'<form class="dlg" method="post" action="{html.escape(BASE)}">'
        "<h2>New album</h2>"
        '<label class="field">Title<input name="title" type="text" maxlength="200" required '
        'placeholder="Weekend in Yosemite"></label>'
        '<label class="field">Description (optional)<textarea name="description" '
        'maxlength="2000" placeholder="A few words about this album"></textarea></label>'
        '<div class="dlg-actions"><button class="textbtn" type="button" data-close>Cancel</button>'
        '<button class="btn primary" type="submit">Create</button></div></form></dialog>'
        '<dialog id="global-upload-dialog" class="dialog"><div class="dlg">'
        "<h2>Upload</h2>"
        '<label class="field">Album<select id="global-upload-album">'
        + "".join(opts) + "</select></label>"
        '<div class="upload-area" id="global-upload-area">'
        "<p>Drop any files here, or</p>"
        '<button class="btn" id="global-upload-pick" type="button">Choose files</button>'
        '<input id="global-upload-files" type="file" multiple hidden>'
        + no_albums +
        '<p class="hint">Any file type · images, video, documents and text get previews.</p>'
        "</div>"
        '<div class="progress" id="global-upload-progress"><i></i></div>'
        '<div class="upload-status" id="global-upload-status"></div>'
        '<div class="dlg-actions"><button class="textbtn" type="button" data-close>Cancel</button>'
        '<button class="btn primary" id="global-upload-go" type="button">Upload</button></div>'
        "</div></dialog>"
        '<dialog id="confirm-dialog" class="dialog"><div class="dlg">'
        '<h3 id="confirm-title" style="margin:0 0 8px;font-size:20px;font-weight:400">Are you sure?</h3>'
        '<p id="confirm-text"></p>'
        '<div class="dlg-actions"><button class="textbtn" type="button" id="confirm-no">Cancel</button>'
        '<button class="btn primary" type="button" id="confirm-yes">Hide</button></div>'
        "</div></dialog>"
    )


def _lightbox() -> str:
    return (
        '<dialog id="lightbox" class="lightbox">'
        '<div class="lb-top">'
        '<button class="lb-icon" type="button" id="lb-close" aria-label="Close">'
        + _icon("back") + "</button>"
        '<span class="lb-name" id="lb-name"></span>'
        '<button class="lb-pill" type="button" id="lb-save" hidden>Save to Photos</button>'
        '<a class="lb-icon" id="lb-open" href="#" target="_blank" rel="noopener" '
        'aria-label="Download" download>' + _icon("download") + "</a>"
        "</div>"
        '<div class="lb-stage">'
        '<img id="lb-img" alt="" style="display:none">'
        '<video id="lb-video" controls playsinline style="display:none"></video>'
        '<audio id="lb-audio" controls style="display:none"></audio>'
        '<div class="lb-doc" id="lb-doc" style="display:none">'
        '<img id="lb-doc-img" alt="">'
        '<p>First-page preview · <a id="lb-doc-download" href="#" download>Download original</a></p>'
        "</div>"
        '<pre class="lb-text" id="lb-text"></pre>'
        '<div class="lb-file" id="lb-file" style="display:none">'
        '<span class="ext" id="lb-file-ext">FILE</span>'
        '<p id="lb-file-name"></p>'
        '<a id="lb-download" href="#" download>Download file</a></div>'
        "</div>"
        '<button class="lb-nav lb-prev" type="button" id="lb-prev" aria-label="Previous">'
        + _icon("left") + "</button>"
        '<button class="lb-nav lb-next" type="button" id="lb-next" aria-label="Next">'
        + _icon("right") + "</button>"
        "</dialog>"
    )


def _shell(
    *,
    view: str,
    title: str,
    content: str,
    device: dict,
    albums: list[dict],
    query: str = "",
    current_album: dict | None = None,
    with_grid: bool = False,
    album_mode: bool = False,
    max_file: int = 0,
    max_files: int = 0,
    max_request: int = 0,
) -> str:
    current_id = current_album["id"] if current_album else ""
    album_attr = f' data-album="{html.escape(current_id)}"' if current_id else ""
    default_album = current_id or (albums[0]["id"] if albums else "")
    limits = ""
    if album_mode:
        limits = (
            f' data-max-file="{int(max_file)}" data-max-files="{int(max_files)}"'
            f' data-max-request="{int(max_request)}"'
        )
    body_attrs = (
        f'class="{_device_classes(device)}" data-base="{html.escape(BASE)}" '
        f'data-view="{html.escape(view)}"{album_attr} '
        f'data-default-album="{html.escape(default_album)}"{limits}'
    )
    scripts = (
        "<script>" + _DEVICE_JS + "</script>"
        + "<script>" + _UPLOAD_JS + "</script>"
        + "<script>" + _LAYOUT_JS + "</script>"
    )
    if with_grid:
        scripts += "<script>" + _GRID_JS + "</script>"
    if album_mode:
        scripts += "<script>" + _ALBUM_UPLOAD_JS + "</script>"
    return (
        '<!doctype html><html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
        f"<title>{html.escape(title)}</title>"
        "<style>" + _CSS + "</style></head><body " + body_attrs + ">"
        '<div class="app">'
        + _sidebar(view, albums)
        + '<div class="scrim" id="scrim"></div>'
        + '<div class="main">'
        + _appbar(view, query, "Photos" if view == "photos" else "Albums")
        + '<main class="content">' + content + "</main></div></div>"
        + _bottom_nav(view)
        + _dialogs(albums, current_id)
        + ('<div id="selection-bar" class="selection-bar" hidden>'
           '<span id="selection-count">0 selected</span>'
           '<button type="button" id="selection-hide" class="primary">Hide</button>'
           '<button type="button" id="selection-clear">Clear</button></div>'
           if with_grid else "")
        + '<div id="toast" class="toast" hidden></div>'
        + ('<div class="drop-overlay">Drop to upload</div>' if album_mode else "")
        + (_lightbox() if with_grid else "")
        + scripts
        + "</body></html>"
    )


def _media_label(media: list[dict]) -> str:
    photos = sum(1 for a in media if (a.get("mime") or "").startswith("image/"))
    videos = sum(1 for a in media if (a.get("mime") or "").startswith("video/"))
    parts = []
    if photos:
        parts.append(f"{photos} photo" + ("s" if photos != 1 else ""))
    if videos:
        parts.append(f"{videos} video" + ("s" if videos != 1 else ""))
    return " and ".join(parts) if parts else "0 items"


def render_photos_page(
    media: list[dict], albums: list[dict], *, device: dict, query: str = ""
) -> str:
    title = "Photos"
    head = (
        '<div class="page-head"><div>'
        f"<h1>{'Search results' if query else title}</h1>"
        f'<p class="sub">{_media_label(media)}'
        + (f' for “{html.escape(query)}”' if query else "")
        + "</p></div></div>"
    )
    if media:
        content = head + _grid_sections(media)
    else:
        content = head + (
            '<div class="empty">' + _icon("photos")
            + "<h2>" + ("No matches" if query else "No photos or videos yet") + "</h2>"
            + "<p>" + ("Try a different search." if query else
                        "Upload photos or videos into an album and they will appear here.") + "</p>"
            + '<div class="empty-actions">'
            + '<button class="btn primary" type="button" data-open-upload>Upload</button>'
            + '<button class="btn" type="button" data-new-album>New album</button>'
            + "</div></div>"
        )
    return _shell(
        view="photos", title="Photos · files", content=content, device=device,
        albums=albums, query=query, with_grid=True,
    )


def render_albums_page(
    albums: list[dict], *, device: dict, query: str = ""
) -> str:
    cards = []
    for a in albums:
        count = int(a.get("asset_count") or 0)
        cover = a.get("cover_id")
        if cover:
            cover_html = (
                f'<img src="{html.escape(BASE + "/thumb/" + cover)}" alt="" '
                'loading="lazy" decoding="async">'
            )
        else:
            cover_html = f'<span class="ph">{_icon("folder")}</span>'
        cards.append(
            f'<a class="album-card" href="{html.escape(BASE + "/" + a["id"])}">'
            f'<div class="album-cover">{cover_html}</div>'
            '<div class="album-meta">'
            f'<div class="album-title">{html.escape(a["title"])}</div>'
            f'<div class="album-count">{count} item{"s" if count != 1 else ""}</div>'
            "</div></a>"
        )
    head = (
        '<div class="page-head"><div>'
        f"<h1>{'Search results' if query else 'Albums'}</h1>"
        f'<p class="sub">{len(albums)} album{"s" if len(albums) != 1 else ""}'
        + (f' for “{html.escape(query)}”' if query else "")
        + "</p></div></div>"
    )
    if cards:
        content = head + '<div class="albums-grid">' + "".join(cards) + "</div>"
    else:
        content = head + (
            '<div class="empty">' + _icon("albums")
            + "<h2>" + ("No matches" if query else "No albums yet") + "</h2>"
            + "<p>" + ("Try a different search." if query else
                        "Create an album to start uploading photos, videos and files.") + "</p>"
            + '<div class="empty-actions"><button class="btn primary" type="button" '
              'data-new-album>Create album</button></div></div>'
        )
    return _shell(
        view="albums", title="Albums · files", content=content, device=device,
        albums=albums, query=query,
    )


def render_album_page(
    album: dict,
    assets: list[dict],
    *,
    max_file: int,
    max_files: int,
    max_request: int,
    device: dict,
    albums: list[dict],
) -> str:
    count = len(assets)
    desc = album.get("description") or ""
    head = (
        '<div class="album-head">'
        f'<a class="iconbtn" href="{html.escape(ALBUMS_BASE)}" aria-label="Back to albums">'
        + _icon("back") + "</a>"
        '<div class="grow">'
        f'<h1>{html.escape(album["title"])}</h1>'
        + (f'<p class="album-desc">{html.escape(desc)}</p>' if desc else "")
        + f'<p class="sub">{count} item{"s" if count != 1 else ""}</p>'
        "</div>"
        '<div class="album-actions">'
        '<button class="btn primary" type="button" data-upload-trigger>Add photos</button>'
        '<button class="btn ghost mobile-only" type="button" data-camera-trigger>Camera</button>'
        '<button class="iconbtn" type="button" data-delete-album aria-label="More / hide album">'
        + _icon("more") + "</button>"
        "</div></div>"
        '<input id="file-input" type="file" multiple hidden>'
        '<input id="camera-input" type="file" accept="image/*,video/*" capture="environment" hidden>'
        '<div class="progress" id="upload-bar"><i></i></div>'
        '<div class="upload-status" id="upload-status"></div>'
    )
    if assets:
        content = head + _grid_sections(assets)
    else:
        content = head + (
            '<div class="empty">' + _icon("photos")
            + "<h2>No items yet</h2>"
            + "<p>Add photos, videos or any file type. Images and videos get previews; "
              "documents get first-page previews.</p>"
            + '<div class="empty-actions"><button class="btn primary" type="button" '
              'data-upload-trigger>Add photos</button></div></div>'
        )
    return _shell(
        view="album", title=f"{album['title']} · files", content=content,
        device=device, albums=albums, current_album=album, with_grid=True,
        album_mode=True, max_file=max_file, max_files=max_files,
        max_request=max_request,
    )


def render_not_found(
    message: str = "This album or file does not exist (or was hidden).",
    device: dict | None = None,
    albums: list[dict] | None = None,
) -> str:
    device = device or {"ios": False, "android": False, "mobile": False}
    albums = albums or []
    content = (
        '<div class="empty">' + _icon("photos")
        + "<h2>Not found</h2>"
        + f"<p>{html.escape(message)}</p>"
        + f'<div class="empty-actions"><a class="btn" href="{html.escape(BASE)}">'
          "Back to photos</a></div></div>"
    )
    return _shell(
        view="photos", title="Not found · files", content=content, device=device,
        albums=albums,
    )

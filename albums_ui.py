"""Server-rendered HTML/CSS/JS for the public album UI.

All user-controlled values are HTML-escaped server-side (``html.escape``) or
inserted with ``textContent`` in JavaScript. No user value is ever concatenated
into ``innerHTML``.

The UI is device-aware: the server inspects the User-Agent and classes the body
``device-mobile`` / ``device-ios`` / ``device-android``, which reveals a fixed
bottom action bar and camera capture button on phones.
"""

from __future__ import annotations

import html

import albums_config as cfg
import albums_preview as preview_mod
import albums_store as store_mod

BASE = cfg.ALBUMS_BASE_PATH


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


_CSS = r"""
:root {
  color-scheme: light dark;
  --bg: #f7f7f5; --fg: #16181c; --muted: #6b7280; --card: #ffffff;
  --line: rgba(15,23,42,.10); --accent: #2563eb; --accent-fg: #fff;
  --danger: #b91c1c; --soft: rgba(37,99,235,.08); --shadow: 0 1px 2px rgba(0,0,0,.06);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0b0d11; --fg: #e8eaed; --muted: #9aa3af; --card: #14171d;
    --line: rgba(255,255,255,.10); --accent: #6ea0ff; --accent-fg: #08101f;
    --danger: #ff7b7b; --soft: rgba(110,160,255,.12); --shadow: 0 1px 2px rgba(0,0,0,.4);
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; }
body {
  background: var(--bg); color: var(--fg);
  font: 15px/1.5 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  -webkit-text-size-adjust: 100%;
}
a { color: inherit; }
.wrap { max-width: 1120px; margin: 0 auto; padding: 18px 18px 64px; }
body.device-mobile .wrap { padding-bottom: 108px; }
header.top { display: flex; align-items: center; gap: 10px; padding: 6px 0 18px; flex-wrap: wrap; }
.brand { font-weight: 700; letter-spacing: -.02em; text-decoration: none; font-size: 1.05rem; }
.brand span { color: var(--muted); font-weight: 500; }
.spacer { flex: 1; }
.header-actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.btn {
  appearance: none; border: 1px solid var(--line); background: var(--card);
  color: var(--fg); border-radius: 10px; padding: 9px 14px; font: inherit;
  font-weight: 600; cursor: pointer; text-decoration: none; display: inline-flex;
  align-items: center; justify-content: center; gap: 8px; min-height: 42px;
  box-shadow: var(--shadow);
}
.btn:hover { border-color: var(--accent); }
.btn.primary { background: var(--accent); color: var(--accent-fg); border-color: transparent; }
.btn.ghost { background: transparent; box-shadow: none; }
.btn.danger { color: var(--danger); }
.btn:disabled { opacity: .55; cursor: not-allowed; }
.notice {
  border: 1px solid rgba(200,120,0,.45); background: rgba(200,120,0,.10);
  border-radius: 12px; padding: 11px 14px; font-size: .86rem; margin: 0 0 22px;
}
.notice b { font-weight: 700; }
h1 { font-size: clamp(1.45rem, 2.4vw, 2rem); letter-spacing: -.03em; margin: 0 0 .25rem; }
h2 { font-size: 1.05rem; margin: 0 0 .6rem; }
.sub { color: var(--muted); margin: 0 0 1.1rem; font-size: .9rem; }
.panel {
  background: var(--card); border: 1px solid var(--line); border-radius: 16px;
  padding: 16px; box-shadow: var(--shadow); margin-bottom: 24px;
}
form.create { display: grid; gap: 10px; grid-template-columns: 1fr; }
@media (min-width: 720px) { form.create { grid-template-columns: 1.2fr 2fr auto; align-items: end; } }
label.field { display: grid; gap: 5px; font-size: .8rem; color: var(--muted); font-weight: 600; }
input[type=text], textarea {
  width: 100%; border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px;
  background: var(--bg); color: var(--fg); font: inherit; min-height: 42px;
}
textarea { resize: vertical; min-height: 42px; }
.grid {
  display: grid; gap: 14px;
  grid-template-columns: repeat(auto-fill, minmax(158px, 1fr));
}
@media (max-width: 420px) { .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; } }
.card {
  background: var(--card); border: 1px solid var(--line); border-radius: 14px;
  overflow: hidden; text-decoration: none; display: flex; flex-direction: column;
  box-shadow: var(--shadow); transition: transform .12s ease, border-color .12s ease;
}
.card:hover { transform: translateY(-2px); border-color: var(--accent); }
.cover { aspect-ratio: 4/3; background: linear-gradient(135deg, var(--soft), transparent); display: grid; place-items: center; }
.cover img { width: 100%; height: 100%; object-fit: cover; display: block; }
.cover .ph { font-size: 1.6rem; color: var(--muted); font-weight: 700; }
.card .meta { padding: 10px 12px 12px; min-width: 0; }
.card .title { font-weight: 650; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.card .desc { color: var(--muted); font-size: .8rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 2px; }
.card .count { color: var(--muted); font-size: .76rem; margin-top: 6px; }
.album-head { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-start; margin-bottom: 16px; }
.album-head .grow { flex: 1; min-width: 220px; }
.album-head .desc { color: var(--muted); white-space: pre-wrap; margin: .2rem 0 0; max-width: 70ch; }
.dropzone {
  border: 2px dashed var(--line); border-radius: 16px; padding: 22px;
  text-align: center; background: var(--soft); transition: border-color .12s, background .12s;
}
.dropzone.drag { border-color: var(--accent); background: var(--soft); }
.dropzone p { margin: 6px 0; }
.dropzone .row { display: flex; gap: 10px; justify-content: center; flex-wrap: wrap; }
.dropzone .hint { color: var(--muted); font-size: .8rem; }
.progress { height: 8px; border-radius: 999px; background: var(--line); overflow: hidden; margin: 12px 0 0; display: none; }
.progress > i { display: block; height: 100%; width: 0; background: var(--accent); transition: width .1s; }
.upload-status { font-size: .86rem; margin-top: 10px; min-height: 1.2em; }
.upload-status.err { color: var(--danger); }
.upload-status.ok { color: var(--accent); }
.tile { position: relative; border-radius: 12px; overflow: hidden; background: var(--card); border: 1px solid var(--line); aspect-ratio: 1; }
.tile img { width: 100%; height: 100%; object-fit: cover; display: block; cursor: zoom-in; }
.tile .fileicon {
  position: absolute; inset: 0; display: grid; place-content: center; gap: 4px;
  text-align: center; cursor: pointer;
  background: linear-gradient(135deg, var(--soft), transparent);
}
.fileicon .ext { font-size: clamp(1.1rem, 4vw, 1.7rem); font-weight: 800; letter-spacing: .04em; line-height: 1; }
.fileicon small { color: var(--muted); font-size: .72rem; text-transform: uppercase; letter-spacing: .08em; }
.tile .play {
  position: absolute; top: 50%; left: 50%; width: 0; height: 0; margin: -11px 0 0 -7px;
  border-top: 11px solid transparent; border-bottom: 11px solid transparent;
  border-left: 16px solid rgba(255,255,255,.9); pointer-events: none;
  filter: drop-shadow(0 1px 2px rgba(0,0,0,.5));
}
.tile .del {
  position: absolute; top: 6px; right: 6px; width: 30px; height: 30px; border-radius: 999px;
  border: 0; background: rgba(0,0,0,.55); color: #fff; font-size: 18px; line-height: 1;
  cursor: pointer; display: grid; place-items: center; opacity: .9; z-index: 2;
}
.tile .del:hover { background: rgba(185,28,28,.9); }
.tile .name {
  position: absolute; inset: auto 0 0 0; padding: 20px 8px 7px; font-size: .72rem;
  color: #fff; background: linear-gradient(transparent, rgba(0,0,0,.65));
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; pointer-events: none;
}
.empty { color: var(--muted); text-align: center; padding: 46px 12px; border: 1px dashed var(--line); border-radius: 16px; }
dialog {
  border: 0; border-radius: 16px; padding: 0; background: var(--card); color: var(--fg);
  max-width: min(92vw, 460px); box-shadow: 0 18px 60px rgba(0,0,0,.35);
}
dialog::backdrop { background: rgba(0,0,0,.55); }
.dialog-body { padding: 20px; }
.dialog-body h3 { margin: 0 0 .5rem; font-size: 1.1rem; }
.dialog-body p { margin: 0 0 1.1rem; color: var(--muted); font-size: .92rem; }
.dialog-actions { display: flex; gap: 10px; justify-content: flex-end; flex-wrap: wrap; }
dialog.lightbox { max-width: 96vw; max-height: 94vh; background: #000; }
dialog.lightbox .stage { display: grid; place-items: center; min-width: min(92vw, 900px); }
dialog.lightbox img, dialog.lightbox video {
  max-width: 94vw; max-height: 82vh; object-fit: contain; display: block;
}
dialog.lightbox audio { width: min(80vw, 560px); }
.lb-file { padding: 34px 18px; text-align: center; color: #ddd; }
.lb-file .ext { font-size: 2.1rem; font-weight: 800; letter-spacing: .05em; display: block; }
.lb-file a { color: #9ec1ff; }
.lb-doc { padding: 16px; text-align: center; color: #ddd; }
.lb-doc img { max-width: 92vw; max-height: 76vh; object-fit: contain; background: #fff; border-radius: 4px; }
.lb-doc .note { margin: 8px 0 0; font-size: .8rem; color: #aaa; }
.lb-text {
  display: none; margin: 0; max-width: 92vw; max-height: 76vh; overflow: auto;
  padding: 16px 18px; background: #0b0d11; color: #e5e7eb;
  font: 12px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  white-space: pre-wrap; overflow-wrap: anywhere; text-align: left; border-radius: 4px;
}
dialog.lightbox .bar { display: flex; align-items: center; gap: 12px; padding: 8px 12px; color: #ddd; font-size: .82rem; }
dialog.lightbox .bar .spacer { flex: 1; }
dialog.lightbox .bar a { color: #9ec1ff; }
dialog.lightbox button { background: rgba(255,255,255,.12); color: #fff; border: 0; }
.lb-close { position: fixed; top: 10px; right: 14px; z-index: 2; font-size: 20px; width: 40px; height: 40px; }
.stat { color: var(--muted); font-size: .82rem; }
.mobile-bar { display: none; }
body.device-mobile .mobile-bar {
  display: flex; position: fixed; left: 0; right: 0; bottom: 0; z-index: 50;
  gap: 6px; padding: 8px max(10px, env(safe-area-inset-left))
           calc(8px + env(safe-area-inset-bottom)) max(10px, env(safe-area-inset-right));
  background: var(--card);
  background: color-mix(in srgb, var(--card) 94%, transparent);
  border-top: 1px solid var(--line); backdrop-filter: blur(12px);
}
.bar-btn {
  flex: 1; min-width: 0; min-height: 46px; border-radius: 10px; border: 1px solid var(--line);
  background: transparent; color: var(--fg); font: inherit; font-size: .72rem; font-weight: 650;
  text-decoration: none; cursor: pointer; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 1px; padding: 4px;
}
.bar-btn .glyph { font-size: 1rem; line-height: 1; }
.bar-btn.primary { background: var(--accent); color: var(--accent-fg); border-color: transparent; }
body.device-mobile .desktop-only { display: none !important; }
body.device-desktop .mobile-only { display: none !important; }
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
  function focusNewAlbum() {
    if (location.hash !== "#new") return;
    var title = document.getElementById("album-title");
    if (title) {
      title.focus({ preventScroll: true });
      var panel = document.getElementById("new");
      if (panel) panel.scrollIntoView({ block: "center" });
    }
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", focusNewAlbum);
  } else {
    setTimeout(focusNewAlbum, 0);
  }
  // The browser's own fragment navigation can run after this script and steal
  // focus; re-apply once the page has fully loaded and on hash changes.
  window.addEventListener("load", function () { setTimeout(focusNewAlbum, 80); });
  window.addEventListener("hashchange", function () { setTimeout(focusNewAlbum, 0); });
})();
"""

_ALBUM_JS = r"""
(function () {
  "use strict";
  var body = document.body;
  var BASE = body.dataset.base || "/albums";
  var ALBUM = body.dataset.album || "";
  var MAX_FILE = parseInt(body.dataset.maxFile || "0", 10);
  var MAX_FILES = parseInt(body.dataset.maxFiles || "50", 10);
  var MAX_REQUEST = parseInt(body.dataset.maxRequest || "0", 10);

  var fileInput = document.getElementById("file-input");
  var cameraInput = document.getElementById("camera-input");
  var drop = document.getElementById("dropzone");
  var bar = document.getElementById("bar");
  var barFill = bar ? bar.querySelector("i") : null;
  var statusEl = document.getElementById("upload-status");
  var grid = document.getElementById("photo-grid");

  function say(msg, cls) {
    if (!statusEl) return;
    statusEl.textContent = msg;
    statusEl.className = "upload-status" + (cls ? " " + cls : "");
  }
  function mb(n) {
    if (n >= 1073741824) return (n / 1073741824).toFixed(2) + " GB";
    return (n / 1048576).toFixed(1) + " MB";
  }

  function upload(files) {
    if (!files || !files.length) return;
    if (files.length > MAX_FILES) {
      say("Too many files (max " + MAX_FILES + " per upload).", "err"); return;
    }
    var list = [], total = 0;
    for (var i = 0; i < files.length; i++) {
      var f = files[i];
      if (MAX_FILE && f.size > MAX_FILE) {
        say("Skipped " + f.name + ": larger than " + mb(MAX_FILE) + ".", "err");
        continue;
      }
      total += f.size;
      list.push(f);
    }
    if (!list.length) return;
    if (MAX_REQUEST && total > MAX_REQUEST) {
      say("Selection is " + mb(total) + "; per-upload limit is " + mb(MAX_REQUEST) + ".", "err");
      return;
    }

    var fd = new FormData();
    for (var j = 0; j < list.length; j++) fd.append("files", list[j], list[j].name);

    var xhr = new XMLHttpRequest();
    xhr.open("POST", BASE + "/api/albums/" + encodeURIComponent(ALBUM) + "/upload");
    xhr.setRequestHeader("Accept", "application/json");
    bar.style.display = "block";
    barFill.style.width = "0%";
    say("Uploading " + list.length + " file" + (list.length === 1 ? "" : "s") + "…");
    xhr.upload.onprogress = function (e) {
      if (e.lengthComputable) barFill.style.width = Math.round((e.loaded / e.total) * 100) + "%";
    };
    xhr.onload = function () {
      bar.style.display = "none";
      var data = null;
      try { data = JSON.parse(xhr.responseText); } catch (e) {}
      if (xhr.status >= 200 && xhr.status < 300) {
        var n = data && data.assets ? data.assets.length : list.length;
        say("Uploaded " + n + " file" + (n === 1 ? "" : "s") + ". Loading…", "ok");
        setTimeout(function () { location.reload(); }, 350);
      } else if (xhr.status === 429) {
        var retry = xhr.getResponseHeader("Retry-After") || "60";
        say((data && data.error ? data.error : "Rate limit exceeded") + " Try again in " + retry + "s.", "err");
      } else {
        var errs = data && data.errors ? data.errors.join("; ") : "";
        say((data && data.error ? data.error : "Upload failed (HTTP " + xhr.status + ")") +
            (errs ? ": " + errs : ""), "err");
      }
    };
    xhr.onerror = function () { bar.style.display = "none"; say("Network error during upload.", "err"); };
    xhr.send(fd);
  }

  function openPicker(input) { if (input) input.click(); }
  document.querySelectorAll("[data-upload-trigger]").forEach(function (el) {
    el.addEventListener("click", function () { openPicker(fileInput); });
  });
  document.querySelectorAll("[data-camera-trigger]").forEach(function (el) {
    el.addEventListener("click", function () { openPicker(cameraInput || fileInput); });
  });
  if (fileInput) fileInput.addEventListener("change", function () { upload(fileInput.files); fileInput.value = ""; });
  if (cameraInput) cameraInput.addEventListener("change", function () { upload(cameraInput.files); cameraInput.value = ""; });
  if (drop) {
    ["dragenter", "dragover"].forEach(function (ev) {
      drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add("drag"); });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove("drag"); });
    });
    drop.addEventListener("drop", function (e) {
      if (e.dataTransfer && e.dataTransfer.files) upload(e.dataTransfer.files);
    });
  }

  // ---- confirm dialog (asset / album deletion) -------------------------
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
  if (confirmDlg) confirmDlg.addEventListener("close", function () { pendingAction = null; });

  function postJSON(url, bodyIn) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify(bodyIn || {})
    });
  }

  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-delete-asset]");
    if (btn) {
      var id = btn.getAttribute("data-delete-asset");
      var tile = btn.closest(".tile");
      ask("Hide this item?",
          "It disappears from this album, but the file stays on disk and is never deleted.",
          function () {
            postJSON(BASE + "/api/assets/" + encodeURIComponent(id) + "/delete").then(function (r) {
              if (!r.ok) throw new Error("HTTP " + r.status);
              if (tile) tile.remove();
              var count = document.querySelectorAll("#photo-grid .tile").length;
              var el = document.getElementById("photo-count");
              if (el) el.textContent = count + (count === 1 ? " item" : " items");
              if (count === 0) location.reload();
            }).catch(function (err) { say("Could not hide item: " + err.message, "err"); });
          });
      return;
    }
    var albumBtn = e.target.closest("[data-delete-album]");
    if (albumBtn) {
      ask("Hide this album?",
          "The album and its items disappear from the site. Nothing is deleted from disk.",
          function () {
            postJSON(BASE + "/api/albums/" + encodeURIComponent(ALBUM) + "/delete").then(function (r) {
              if (!r.ok) throw new Error("HTTP " + r.status);
              location.href = BASE;
            }).catch(function (err) { say("Could not hide album: " + err.message, "err"); });
          });
    }
  });

  // ---- lightbox --------------------------------------------------------
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
  var lbPrev = document.getElementById("lb-prev");
  var lbNext = document.getElementById("lb-next");
  var current = -1;
  var textToken = 0;

  function tiles() { return grid ? Array.prototype.slice.call(grid.querySelectorAll(".tile")) : []; }
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
    if (lbDocImg) { lbDocImg.removeAttribute("src"); }
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
    if (!lbText) { showFilePanel(name, tile.getAttribute("data-ext"), tile.getAttribute("data-full")); return; }
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
      lbText.textContent = "";
      showFilePanel(name, tile.getAttribute("data-ext"), tile.getAttribute("data-full"));
    });
  }
  function show(i) {
    var t = tiles();
    if (!t.length) return;
    current = (i + t.length) % t.length;
    var tile = t[current];
    var kind = tile.getAttribute("data-preview-kind") ||
               tile.getAttribute("data-kind") || "image";
    var full = tile.getAttribute("data-full");
    var previewUrl = tile.getAttribute("data-preview") || full;
    var thumb = tile.getAttribute("data-thumb");
    var name = tile.getAttribute("data-name") || "File";
    stopMedia();
    if (kind === "image" && lbImg) {
      lbImg.onerror = function () {
        // Last-resort fallback: the server-generated JPEG thumbnail.
        if (thumb && lbImg.src !== thumb) lbImg.src = thumb;
      };
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
          showFilePanel(name, tile.getAttribute("data-ext"), full);
        };
        lbDocImg.src = previewUrl;
        lbDocImg.alt = name;
      }
      if (lbDocDownload) lbDocDownload.href = full;
    } else if (kind === "text" || kind === "archive") {
      showTextPreview(tile, previewUrl, name);
    } else {
      showFilePanel(name, tile.getAttribute("data-ext"), full);
    }
    lbName.textContent = (current + 1) + " / " + t.length + " — " + name;
    lbOpen.href = full;
    if (lb && !lb.open) lb.showModal();
  }
  if (grid) grid.addEventListener("click", function (e) {
    if (e.target.closest(".del")) return;
    var tile = e.target.closest(".tile");
    if (!tile) return;
    show(tiles().indexOf(tile));
  });
  if (lbPrev) lbPrev.addEventListener("click", function () { show(current - 1); });
  if (lbNext) lbNext.addEventListener("click", function () { show(current + 1); });
  if (lb) {
    lb.addEventListener("keydown", function (e) {
      if (e.key === "ArrowLeft") { e.preventDefault(); show(current - 1); }
      if (e.key === "ArrowRight") { e.preventDefault(); show(current + 1); }
    });
    lb.addEventListener("close", stopMedia);
  }
  var lbClose = document.getElementById("lb-close");
  if (lbClose) lbClose.addEventListener("click", function () { lb.close(); });
})();
"""


def _fmt_size(n: int) -> str:
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < step or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= step
    return f"{n:.1f} TB"


def _page(
    title: str, body: str, *, device: dict, script: str = "", extra_attrs: str = ""
) -> str:
    attrs = (
        f'class="{_device_classes(device)}" '
        f'data-base="{html.escape(BASE)}"' + extra_attrs
    )
    return (
        "<!doctype html><html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">"
        f"<title>{html.escape(title)}</title>"
        "<style>" + _CSS + "</style>"
        "</head><body " + attrs + ">"
        + body
        + f"<script>{_DEVICE_JS}</script>"
        + (f"<script>{script}</script>" if script else "")
        + "</body></html>"
    )


def _header(device: dict, *, on_album: bool = False) -> str:
    all_albums = (
        f'<a class="btn ghost" href="{html.escape(BASE)}">All albums</a>'
        if on_album else ""
    )
    file_host = (
        f'<a class="btn ghost desktop-only" href="{html.escape(cfg.PREFIX + "/")}">'
        "file host</a>"
    )
    return (
        '<header class="top">'
        f'<a class="brand" href="{html.escape(BASE)}">files <span>· albums</span></a>'
        '<span class="spacer"></span>'
        '<div class="header-actions">'
        + all_albums
        + f'<a class="btn primary" href="{html.escape(BASE)}#new">New album</a>'
        + file_host
        + "</div></header>"
    )


def _mobile_bar(*, on_album: bool) -> str:
    if on_album:
        return (
            '<nav class="mobile-bar" aria-label="Album actions">'
            f'<a class="bar-btn" href="{html.escape(BASE)}"><span class="glyph">‹</span>Albums</a>'
            '<button class="bar-btn primary" type="button" data-upload-trigger>'
            '<span class="glyph">＋</span>Upload</button>'
            '<button class="bar-btn" type="button" data-camera-trigger>'
            '<span class="glyph">◉</span>Camera</button>'
            f'<a class="bar-btn" href="{html.escape(BASE)}#new">'
            '<span class="glyph">＋</span>New album</a>'
            "</nav>"
        )
    return (
        '<nav class="mobile-bar" aria-label="Album actions">'
        f'<a class="bar-btn primary" href="#new"><span class="glyph">＋</span>New album</a>'
        '<a class="bar-btn" href="?"><span class="glyph">↻</span>Refresh</a>'
        f'<a class="bar-btn" href="{html.escape(cfg.PREFIX + "/")}">'
        '<span class="glyph">▤</span>File host</a>'
        "</nav>"
    )


_NOTICE = (
    '<div class="notice"><b>Uploads are public.</b> Anyone with the link can view '
    'this album and its files. Any file type is accepted; images preview, other '
    'files download. Deleting only hides items — files are never removed from disk.</div>'
)


def render_albums_page(albums: list[dict], device: dict) -> str:
    cards = []
    for a in albums:
        album_url = f"{BASE}/{a['id']}"
        count = int(a.get("asset_count") or 0)
        cover = a.get("cover_id")
        if cover:
            cover_html = (
                f'<img src="{html.escape(BASE + "/thumb/" + cover)}" alt="" '
                'loading="lazy" decoding="async">'
            )
        else:
            cover_html = '<span class="ph" aria-hidden="true">ALBUM</span>'
        desc = a.get("description") or ""
        cards.append(
            f'<a class="card" href="{html.escape(album_url)}">'
            f'<div class="cover">{cover_html}</div>'
            '<div class="meta">'
            f'<div class="title">{html.escape(a["title"])}</div>'
            f'<div class="desc">{html.escape(desc)}</div>'
            f'<div class="count">{count} item{"s" if count != 1 else ""}</div>'
            "</div></a>"
        )
    grid = (
        '<div class="grid">' + "".join(cards) + "</div>"
        if cards else
        '<div class="empty">No albums yet. Tap <b>New album</b> to create the first one.</div>'
    )
    body = (
        '<div class="wrap">'
        + _header(device)
        + _NOTICE
        + "<h1>Albums</h1>"
        + '<p class="sub">Public shared albums. Anyone can view; anyone can add files.</p>'
        + '<section class="panel" id="new"><h2>Create a new album</h2>'
        + f'<form class="create" method="post" action="{html.escape(BASE)}">'
        + '<label class="field">Title<input id="album-title" type="text" name="title" '
          'maxlength="200" required placeholder="Weekend in Yosemite"></label>'
        + '<label class="field">Description (optional)<textarea name="description" '
          'maxlength="2000" rows="1" placeholder="A few words about this album"></textarea></label>'
        + '<button class="btn primary" type="submit">Create album</button>'
        + "</form></section>"
        + grid
        + "</div>"
        + _mobile_bar(on_album=False)
    )
    return _page("Albums · files", body, device=device)


def _tile(a: dict) -> str:
    aid = a["id"]
    name = a.get("original_name") or "file"
    kind = preview_mod.preview_kind(a)
    full = f"{BASE}/asset/{aid}"
    thumb = f"{BASE}/thumb/{aid}"
    preview = f"{BASE}/preview/{aid}"
    mime = a.get("mime") or "application/octet-stream"
    ext = (a.get("ext") or "file").upper()[:6]
    play = (
        '<span class="play" aria-hidden="true"></span>'
        if kind in ("video", "audio") else ""
    )
    inner = (
        f'<img src="{html.escape(thumb)}" alt="{html.escape(name)}" '
        'loading="lazy" decoding="async">' + play
    )
    return (
        f'<div class="tile" data-kind="{html.escape(kind)}" '
        f'data-preview-kind="{html.escape(kind)}" '
        f'data-full="{html.escape(full)}" data-preview="{html.escape(preview)}" '
        f'data-name="{html.escape(name)}" data-mime="{html.escape(mime)}" '
        f'data-ext="{html.escape(ext)}" data-thumb="{html.escape(thumb)}">'
        + inner
        + f'<button class="del" type="button" data-delete-asset="{html.escape(aid)}" '
          f'aria-label="Hide {html.escape(name)}">×</button>'
        + f'<span class="name">{html.escape(name)}</span>'
        + "</div>"
    )


def render_album_page(
    album: dict,
    assets: list[dict],
    *,
    max_file: int,
    max_files: int,
    max_request: int,
    device: dict,
) -> str:
    tiles = "".join(_tile(a) for a in assets)
    grid = (
        f'<div class="grid" id="photo-grid">{tiles}</div>'
        if tiles else
        '<div class="empty" id="photo-grid">No files yet. Upload something above.</div>'
    )
    count = len(assets)
    desc = album.get("description") or ""
    upload_label = "Choose Photos & Files" if device.get("ios") else "Choose files"
    camera_label = "Take Photo" if device.get("ios") else "Camera"
    body = (
        '<div class="wrap">'
        + _header(device, on_album=True)
        + _NOTICE
        + '<div class="album-head"><div class="grow">'
        + f'<h1>{html.escape(album["title"])}</h1>'
        + (f'<p class="desc">{html.escape(desc)}</p>' if desc else "")
        + f'<p class="stat" id="photo-count">{count} item{"s" if count != 1 else ""}</p>'
        + "</div>"
        + '<button class="btn danger" type="button" data-delete-album="1">Hide album</button>'
        + "</div>"
        + '<section class="panel"><div class="dropzone" id="dropzone">'
        + '<p><b>Drop files here</b> or use a button below.</p>'
        + '<div class="row">'
        + f'<button class="btn primary" type="button" data-upload-trigger>{html.escape(upload_label)}</button>'
        + f'<button class="btn mobile-only" type="button" data-camera-trigger>{html.escape(camera_label)}</button>'
        + "</div>"
        + '<input id="file-input" type="file" multiple hidden>'
        + '<input id="camera-input" type="file" accept="image/*,video/*" '
          'capture="environment" hidden>'
        + f'<p class="hint">Any file type · up to {max_files} files, '
          f'{_fmt_size(max_file)} each, {_fmt_size(max_request)} per upload.</p>'
        + '<div class="progress" id="bar"><i></i></div>'
        + '<div class="upload-status" id="upload-status" role="status" aria-live="polite"></div>'
        + "</div></section>"
        + grid
        + "</div>"
        + '<dialog id="confirm-dialog"><div class="dialog-body">'
        + '<h3 id="confirm-title">Are you sure?</h3>'
        + '<p id="confirm-text"></p>'
        + '<div class="dialog-actions">'
        + '<button class="btn ghost" type="button" id="confirm-no">Cancel</button>'
        + '<button class="btn danger" type="button" id="confirm-yes">Hide</button>'
        + "</div></div></dialog>"
        + '<dialog class="lightbox" id="lightbox"><div class="stage">'
        + '<img id="lb-img" alt="" style="display:none">'
        + '<video id="lb-video" controls playsinline style="display:none"></video>'
        + '<audio id="lb-audio" controls style="display:none"></audio>'
        + '<div class="lb-doc" id="lb-doc" style="display:none">'
        + '<img id="lb-doc-img" alt="">'
        + '<p class="note">Document preview (first page) · '
        + '<a id="lb-doc-download" href="#" download>Download original</a></p>'
        + "</div>"
        + '<pre class="lb-text" id="lb-text"></pre>'
        + '<div class="lb-file" id="lb-file" style="display:none">'
        + '<span class="ext" id="lb-file-ext">FILE</span>'
        + '<p id="lb-file-name"></p>'
        + '<a id="lb-download" href="#" download>Download file</a>'
        + "</div>"
        + '<div class="bar"><span id="lb-name"></span><span class="spacer"></span>'
        + '<a id="lb-open" href="#" target="_blank" rel="noopener">open</a>'
        + '<button class="btn" type="button" id="lb-prev" aria-label="Previous">‹</button>'
        + '<button class="btn" type="button" id="lb-next" aria-label="Next">›</button>'
        + "</div></div>"
        + '<button class="btn lb-close" type="button" id="lb-close" '
          'aria-label="Close">×</button></dialog>'
        + _mobile_bar(on_album=True)
    )
    attrs_extra = (
        f' data-album="{html.escape(album["id"])}"'
        f' data-max-file="{int(max_file)}" data-max-files="{int(max_files)}"'
        f' data-max-request="{int(max_request)}"'
    )
    return _page(
        f"{album['title']} · Albums",
        body,
        device=device,
        script=_ALBUM_JS,
        extra_attrs=attrs_extra,
    )


def render_not_found(
    message: str = "This album or file does not exist (or was hidden).",
    device: dict | None = None,
) -> str:
    device = device or {"ios": False, "android": False, "mobile": False}
    body = (
        '<div class="wrap">' + _header(device)
        + "<h1>Not found</h1>"
        + f'<p class="sub">{html.escape(message)}</p>'
        + f'<p><a class="btn" href="{html.escape(BASE)}">Back to albums</a></p>'
        + "</div>"
        + _mobile_bar(on_album=False)
    )
    return _page("Not found · Albums", body, device=device)

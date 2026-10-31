#!/usr/bin/env python3
"""TPDC (National Tibetan Plateau Data Center) search and download helper.

Supports:
  - public metadata search (no login)
  - recursive file listing (with a short-lived local cache)
  - open-data (sharePolicy A) no-login FTP download
  - login-assisted (sharePolicy B) FTP download via a browser cookie
  - resumable, segmented, multi-connection FTP download with a live progress bar
  - access probe (`status`): resolves what the current account can actually
    download, separating transient server errors from real denials
  - post-download verification against manifest.json
  - citations in BibTeX / RIS / GB/T 7714 / plain text

No third-party dependencies. Python 3.8+.
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import json
import os
import posixpath
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from ftplib import FTP, error_perm, error_temp
from pathlib import Path

__version__ = "0.5.0"

BASE = "https://data.tpdc.ac.cn"
UA = f"tpdc-fetch/{__version__} (+https://data.tpdc.ac.cn)"
CACHE_DIR = Path(os.environ.get("TPDC_CACHE_DIR", Path.home() / ".tpdc-fetch"))
CACHE_FILE = CACHE_DIR / "cache.json"
FILES_TTL = 3600  # file-tree cache lifetime, seconds
CRED_FILE = CACHE_DIR / "credentials.json"  # holds the access_token only
PROFILE_DIR = CACHE_DIR / "browser-profile"  # dedicated Chromium profile for auth
AUTH_PROBE_ID = "0e0a6819-7e18-4037-8893-dd165b23cde3"  # known policy-B dataset
FTP_WORKER_CAP = 5  # TPDC allows at most 5 connections per IP


# --------------------------------------------------------------------------- #
# Output helpers (Windows GBK console safety + CJK-aware padding)
# --------------------------------------------------------------------------- #
def _force_utf8_stdio():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def dwidth(s):
    """Display width of a string (CJK/full-width chars count as 2)."""
    return sum(2 if unicodedata.east_asian_width(c) in "FW" else 1 for c in s or "")


def pad(s, width):
    """Truncate/pad to a display width, appending an ellipsis when cut."""
    s = s or ""
    if dwidth(s) <= width:
        return s + " " * (width - dwidth(s))
    out, w = "", 0
    for c in s:
        cw = 2 if unicodedata.east_asian_width(c) in "FW" else 1
        if w + cw > width - 1:
            break
        out += c
        w += cw
    return out + "…"


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def http_json(url, method="GET", body=None, cookie=None, token=None, timeout=40, retries=2):
    headers = {"User-Agent": UA, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=UTF-8"
    if cookie:
        headers["Cookie"] = cookie
    if token:
        headers["Authorization"] = "Bearer " + token
    last_exc = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "ignore")
                return resp.status, json.loads(raw) if raw.strip().startswith(("{", "[")) else raw
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "ignore")
            try:
                return exc.code, json.loads(raw)
            except Exception:
                return exc.code, raw
        except Exception as exc:  # transient network error: retry
            last_exc = exc
            if attempt < retries:
                time.sleep(1 + attempt)
    return 0, {"error": f"{type(last_exc).__name__}: {last_exc}"}


def api_error(payload):
    if isinstance(payload, dict):
        return payload.get("message") or payload.get("error") or str(payload)[:300]
    return str(payload)[:300]


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #
def load_cache():
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(cache):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def remember(records):
    cache = load_cache()
    for r in records:
        if r.get("id"):
            old = cache.get(r["id"]) or {}
            cache[r["id"]] = {**old, **r}
    save_cache(cache)


# --------------------------------------------------------------------------- #
# Search / metadata
# --------------------------------------------------------------------------- #
def search(keyword, limit=10, page=1, english=False, sort="time"):
    """Returns (records, total)."""
    body = {
        "projectId": "",
        "title": keyword,
        "daid": "",
        "pageSize": limit,
        "pageNum": page,
        "downloadSort": sort == "downloads",
        "viewsSort": sort == "views",
        "tsUpdateTimeSort": sort == "time",
        "placeId": "",
        "subjectId": "",
        "themeId": "",
        "isImportant": False,
        "isEnglish": english,
    }
    status, payload = http_json(BASE + "/view/metadataView/page", "POST", body)
    if status != 200 or not isinstance(payload, dict) or payload.get("code") != "200":
        raise RuntimeError(f"TPDC 检索失败 (HTTP {status}): {api_error(payload)}")
    page_obj = payload.get("context", {}).get("metadataViewVOPage", {}) or {}
    return page_obj.get("records") or [], int(page_obj.get("total") or 0)


def find_record(obj):
    """Recursively find a dict that looks like a metadata record."""
    if isinstance(obj, dict):
        if obj.get("id") and (obj.get("title") or obj.get("doi")):
            return obj
        for v in obj.values():
            r = find_record(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_record(v)
            if r:
                return r
    return None


def fetch_record(metadata_id, cookie=None, token=None):
    """Fetch a single record via detailOpen (works with a policy-B cookie or token)."""
    body = {"metadataId": metadata_id}
    status, payload = http_json(
        BASE + "/view/metadataView/detailOpen", "POST", body, cookie=cookie, token=token
    )
    if status == 200 and isinstance(payload, dict) and str(payload.get("code")) == "200":
        rec = find_record(payload.get("context"))
        if rec and rec.get("id") == metadata_id:
            remember([rec])
            return rec
    return None


def get_record(metadata_id, cookie=None, token=None):
    """Cached record first; falls back to an online detailOpen fetch."""
    rec = load_cache().get(metadata_id)
    if rec and rec.get("description"):
        return rec
    fetched = fetch_record(metadata_id, cookie, token)
    if fetched:
        return fetched
    return rec or {}


def root_files(metadata_id):
    url = BASE + "/file/file/getRootFileDataList?metadataId=" + urllib.parse.quote(metadata_id)
    status, payload = http_json(url)
    if status != 200 or not isinstance(payload, dict) or payload.get("code") != "200":
        raise RuntimeError(f"获取文件列表失败 (HTTP {status}): {api_error(payload)}")
    return payload.get("data") or []


def child_files(parent_id):
    url = BASE + "/file/file/getFileDataList?parentId=" + urllib.parse.quote(parent_id)
    status, payload = http_json(url)
    if status != 200 or not isinstance(payload, dict) or payload.get("code") != "200":
        raise RuntimeError(f"获取子目录失败 (HTTP {status}): {api_error(payload)}")
    return payload.get("data") or []


def _files_cache_path(metadata_id):
    return CACHE_DIR / f"files-{metadata_id}.json"


def walk_files(metadata_id, use_cache=True):
    """Return a flat list of files with FTP-relative paths (1h local cache)."""
    cache_path = _files_cache_path(metadata_id)
    if use_cache:
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            if time.time() - data.get("ts", 0) < FILES_TTL:
                return data["files"]
        except Exception:
            pass

    out = []

    def recurse(items, prefix, depth=0):
        if depth > 10:
            return
        for item in items:
            name = item.get("name") or ""
            rel = f"{prefix}/{name}" if prefix else name
            if item.get("type") == "dir":
                recurse(child_files(item.get("id")), rel, depth + 1)
            else:
                out.append(
                    {
                        "id": item.get("id"),
                        "name": name,
                        "relpath": rel,
                        "size": int(item.get("size") or 0),
                        "ext": item.get("ext"),
                    }
                )

    recurse(root_files(metadata_id), "")
    if use_cache:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps({"ts": time.time(), "files": out}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass
    return out


def render_tree(files):
    """Render an ASCII tree with sizes."""
    root = {}
    for f in files:
        parts = f["relpath"].split("/")
        node = root
        for p in parts[:-1]:
            node = node.setdefault(p + "/", {})
        node[parts[-1]] = f["size"]

    lines = []

    def walk(node, prefix):
        items = sorted(node.items(), key=lambda kv: (not kv[0].endswith("/"), kv[0]))
        for i, (name, val) in enumerate(items):
            last = i == len(items) - 1
            conn = "└─ " if last else "├─ "
            if isinstance(val, dict):
                lines.append(f"{prefix}{conn}{name}")
                walk(val, prefix + ("   " if last else "│  "))
            else:
                lines.append(f"{prefix}{conn}{name}  ({human(val)})")

    walk(root, "")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# FTP credentials
# --------------------------------------------------------------------------- #
def parse_ftp(data):
    hosts = [h.strip() for h in re.split(r"[,;\s]+", data.get("ftpUrl") or "") if h.strip()]
    return {
        "hosts": hosts,
        "port": int(data.get("ftpPort") or 6201),
        "username": data.get("username"),
        "password": data.get("password"),
    }


def ftp_open(metadata_id):
    url = BASE + "/file/ftp/createNoLoginDownloadFtpUser?metadataId=" + urllib.parse.quote(metadata_id)
    status, payload = http_json(url, "POST", {"noToken": True})
    if status == 200 and isinstance(payload, dict) and payload.get("code") == "200":
        return parse_ftp(payload.get("data") or {})
    return None


def ftp_login(metadata_id, cookie, token=None):
    """Return (ftp_info, reason). reason explains a refusal from the server."""
    if not cookie and not token:
        return None, "未提供凭据"
    url = BASE + "/file/ftp/getDownloadFtpUserByMetadataId?metadataId=" + urllib.parse.quote(metadata_id)
    status, payload = http_json(url, "POST", {}, cookie=cookie, token=token)
    if status == 200 and isinstance(payload, dict) and str(payload.get("code")) == "200":
        return parse_ftp(payload.get("data") or {}), ""
    reason = ""
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, str) and data.strip():
            reason = data.strip()
        else:
            reason = payload.get("message") or ""
    elif payload:
        reason = str(payload)[:200]
    return None, reason or f"HTTP {status}"


def get_ftp(metadata_id, cookie=None, token=None):
    info = ftp_open(metadata_id)
    if info and info.get("username"):
        info["access"] = "A"
        return info
    info, reason = ftp_login(metadata_id, cookie, token)
    if info and info.get("username"):
        info["access"] = "B"
        return info
    hint = ""
    if not cookie and not token:
        hint = (
            "  登录后把浏览器 Cookie 传给 --cookie（或设置 TPDC_COOKIE）；\n"
            "  也可以传 Bearer 令牌：--token（或设置 TPDC_TOKEN），取自登录后\n"
            "  浏览器 DevTools → Application → Local Storage → access_token。\n"
        )
    elif reason and "未查询到" in reason:
        hint = (
            "  已登录，但服务端未为该账号生成 FTP 账户：说明这个数据集需要\n"
            "  先在网站上单独申请/获得授权，登录本身不够。\n"
        )
    raise RuntimeError(
        "该数据集不是开放数据，或当前账号没有下载权限。\n"
        f"  服务端返回: {reason or '无'}\n"
        f"  页面: {BASE}/zh-hans/data/{metadata_id}\n"
        f"{hint}"
        "  可用 `files <id>` 确认文件清单，用 `meta <id>` 读数据说明。"
    )


# --------------------------------------------------------------------------- #
# Access probe: the sharePolicy letter is a door sign, entitlements are the lock
# --------------------------------------------------------------------------- #
NO_ENTITLEMENT_MARK = "未查询到对应FTP账户"

STATE_TEXT = {
    "OPEN": ("✅", "开放数据，可直接下载", "直接 download，无需任何凭据。"),
    "ENTITLED": ("✅", "已授权，可下载", "带同一凭据 download 即可。"),
    "NO_ENTITLEMENT": (
        "❌",
        "已登录但无权限",
        "需要到数据集页面单独申请/获得授权；重试或换凭据都不会有用。",
    ),
    "NO_ENTITLEMENT_ONCE": (
        "⚠️",
        "疑似无权限（仅观测到一次）",
        "用 --attempts 3 再跑一次 status 确认；单次结果不可下结论。",
    ),
    "AUTH_INVALID": ("🔑", "凭据无效或过期", "重新登录，取新的 access_token 或 Cookie。"),
    "NEEDS_AUTH": (
        "🔑",
        "需要登录",
        "传 --token（推荐，取自登录后 localStorage 的 access_token）或 --cookie。",
    ),
    "UNKNOWN_TRANSIENT": (
        "⚠️",
        "未知（服务端瞬时错误）",
        "稍后重试；「操作失败」不等于无权限，不要据此下结论。",
    ),
}


def classify_login_payload(payload):
    """Classify a getDownloadFtpUserByMetadataId response.

    Returns (cls, reason, ftp_info); cls ∈ ENTITLED / NO_ENTITLEMENT /
    AUTH_INVALID / TRANSIENT / OTHER.
    """
    if not isinstance(payload, dict):
        return "TRANSIENT", str(payload)[:160], None
    code = str(payload.get("code"))
    data = payload.get("data")
    msg = payload.get("message") or ""
    if code == "200" and isinstance(data, dict) and data.get("username"):
        return "ENTITLED", "", parse_ftp(data)
    text = data.strip() if isinstance(data, str) and data.strip() else (msg or "")
    if NO_ENTITLEMENT_MARK in text:
        return "NO_ENTITLEMENT", text, None
    if "未登录" in text:
        return "AUTH_INVALID", text, None
    if code == "500":
        return "TRANSIENT", text or "操作失败", None
    return "OTHER", text or msg or f"code={code}", None


def probe_ftp(info, metadata_id, timeout=60):
    """Log in to FTP and SIZE one real file. Returns (result, detail)."""
    ftp = None
    try:
        ftp = connect_ftp(info, ctl_timeout=timeout)
    except Exception as exc:
        text = str(exc)
        if "421" in text:
            return "HOST_FULL", text[:160]
        return "ERROR", text[:160]
    try:
        entries = root_files(metadata_id)
        names = [e.get("name") for e in entries if isinstance(e, dict) and e.get("name")]
        if not names:
            return "OK", "目录为空，无法 SIZE，但 FTP 登录成功"
        size = ftp.size(names[0])
        return "OK", f"SIZE {names[0]} = {human(size) if size else size}"
    except error_perm as exc:
        text = str(exc)
        if "530" in text:
            return "LOGIN_FAILED", text[:160]
        return "ERROR", text[:160]
    except Exception as exc:
        return "ERROR", str(exc)[:160]
    finally:
        if ftp is not None:
            try:
                ftp.quit()
            except Exception:
                try:
                    ftp.close()
                except Exception:
                    pass


def probe_access(metadata_id, cookie=None, token=None, attempts=3, ftp_check=False):
    """Resolve what this account can actually do with this dataset.

    Never trust a single transient answer: `操作失败` is retried, and
    NO_ENTITLEMENT only becomes conclusive after two sightings.
    """
    rec = get_record(metadata_id, cookie=cookie, token=token)
    creds = "token" if token else ("cookie" if cookie else "none")

    no_login_info = ftp_open(metadata_id)
    no_login_open = bool(no_login_info and no_login_info.get("username"))

    login_class, reason, ftp_info = "NONE", "未探测", None
    no_ent_seen = 0
    tries = 0
    # Skip the login probe when the no-login endpoint already granted access.
    for i in range(max(1, attempts) if ((cookie or token) and not no_login_open) else 0):
        tries = i + 1
        _st, pl = http_json(
            BASE + "/file/ftp/getDownloadFtpUserByMetadataId?metadataId=" + urllib.parse.quote(metadata_id),
            "POST", {}, cookie=cookie, token=token,
        )
        login_class, reason, ftp_info = classify_login_payload(pl)
        if login_class in ("ENTITLED", "AUTH_INVALID"):
            break
        if login_class == "NO_ENTITLEMENT":
            no_ent_seen += 1
            if no_ent_seen >= 2:
                break
        time.sleep(1.5 * (i + 1))

    if no_login_open:
        state = "OPEN"
    elif login_class == "ENTITLED":
        state = "ENTITLED"
    elif login_class == "NO_ENTITLEMENT":
        state = "NO_ENTITLEMENT" if no_ent_seen >= 2 else "NO_ENTITLEMENT_ONCE"
    elif login_class == "AUTH_INVALID":
        state = "AUTH_INVALID"
    elif creds == "none":
        state = "NEEDS_AUTH"
    else:
        state = "UNKNOWN_TRANSIENT"

    ftp_result, ftp_detail = None, "未执行"
    if ftp_check:
        info = no_login_info if no_login_open else ftp_info
        if info and info.get("username"):
            ftp_result, ftp_detail = probe_ftp(info, metadata_id)
        else:
            ftp_result, ftp_detail = "SKIPPED", "没有可用的 FTP 账户"

    emoji, state_cn, advice = STATE_TEXT[state]
    can_download = state in ("OPEN", "ENTITLED") and ftp_result in (None, "OK")
    return {
        "metadata_id": metadata_id,
        "title": (rec or {}).get("title"),
        "label": (rec or {}).get("sharePolicy") or "?",
        "credentials": creds,
        "no_login": "GRANTED" if no_login_open else "REFUSED",
        "login_class": login_class,
        "login_reason": reason,
        "attempts": tries,
        "ftp_info": ftp_info,
        "ftp_check": {"result": ftp_result, "detail": ftp_detail} if ftp_check else None,
        "state": state,
        "state_cn": state_cn,
        "advice": advice,
        "can_download": can_download,
    }


def print_access_verdict(v):
    print(f"数据集: {v.get('title') or v['metadata_id']}")
    print(f"标签: {v['label']}   凭据: {v['credentials']}")
    print(f"无登录探测: {v['no_login']}")
    line = f"登录探测: {v['login_class']}"
    if v["attempts"]:
        line += f" ×{v['attempts']} 次"
    if v["login_reason"]:
        line += f"  「{v['login_reason']}」"
    print(line)
    if v.get("ftp_check"):
        print(f"FTP 验证: {v['ftp_check']['result']}  {v['ftp_check']['detail']}")
    emoji, _, _ = STATE_TEXT[v["state"]]
    print(f"结论: {emoji} {v['state_cn']}  [{v['state']}]  可下载: {'是' if v['can_download'] else '否'}")
    print(f"建议: {v['advice']}")


# --------------------------------------------------------------------------- #
# One-command login: launch a dedicated browser, grab the token, store it
# --------------------------------------------------------------------------- #
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _WS:
    """Minimal RFC 6455 client — just enough to talk to the DevTools socket."""

    def __init__(self, host, port, path, timeout=10):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            "GET " + path + " HTTP/1.1\r\nHost: " + host + ":" + str(port) + "\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: " + key + "\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("WebSocket 握手失败")
            resp += chunk
        head = resp.split(b"\r\n\r\n", 1)[0]
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError("WebSocket 握手被拒绝")
        self.buf = resp.split(b"\r\n\r\n", 1)[1]

    def _recv_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("WebSocket 连接中断")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send_text(self, text):
        payload = text.encode("utf-8")
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + payload)

    def recv_text(self):
        while True:
            b1, b2 = self._recv_exact(2)
            opcode = b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv_exact(8))[0]
            if b2 & 0x80:
                mask = self._recv_exact(4)
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(self._recv_exact(n)))
            else:
                data = self._recv_exact(n)
            if opcode == 0x9:  # ping -> pong
                self.send_text("")
                continue
            if opcode == 0x8:
                raise RuntimeError("WebSocket 已被关闭")
            if opcode in (0x1, 0x2):
                return data.decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.sendall(bytes([0x88, 0x80]) + os.urandom(4))
            self.sock.close()
        except Exception:
            pass


def _cdp_http(port, path):
    with urllib.request.urlopen("http://127.0.0.1:" + str(port) + path, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def cdp_eval_js(port, expression, timeout=10):
    """Evaluate JS in the data.tpdc.ac.cn page; None if no such target."""
    targets = _cdp_http(port, "/json")
    page = next(
        (t for t in targets if t.get("type") == "page" and "data.tpdc.ac.cn" in (t.get("url") or "")),
        None,
    )
    if not page or not page.get("webSocketDebuggerUrl"):
        return None
    ws_path = "/" + page["webSocketDebuggerUrl"].split("://", 1)[1].split("/", 1)[1]
    ws = _WS("127.0.0.1", port, ws_path, timeout=timeout)
    try:
        ws.send_text(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True},
        }))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(ws.recv_text())
            if msg.get("id") == 1:
                result = (msg.get("result") or {}).get("result") or {}
                return result.get("value")
        return None
    finally:
        ws.close()


def find_chromium():
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    lad = os.environ.get("LocalAppData", "")
    candidates = [
        pf + r"\Google\Chrome\Application\chrome.exe",
        pf86 + r"\Google\Chrome\Application\chrome.exe",
        lad + r"\Google\Chrome\Application\chrome.exe",
        pf + r"\Microsoft\Edge\Application\msedge.exe",
        pf86 + r"\Microsoft\Edge\Application\msedge.exe",
        lad + r"\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    for cand in candidates:
        if cand and os.path.exists(cand):
            return cand
    return shutil.which("chrome") or shutil.which("chromium") or shutil.which("msedge")


def ensure_browser(port, profile, url):
    """Launch (or reuse) a dedicated-profile Chromium with a debug port."""
    try:
        _cdp_http(port, "/json/version")
        return  # already running with this profile and port
    except Exception:
        pass
    exe = find_chromium()
    if not exe:
        raise RuntimeError("未找到 Chrome/Edge；请安装，或改用 --token / --token-file 手动传入")
    subprocess.Popen(
        [exe, "--remote-debugging-port=" + str(port), "--user-data-dir=" + profile,
         "--no-first-run", "--no-default-browser-check", "--window-size=1100,860", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        time.sleep(0.4)
        try:
            _cdp_http(port, "/json/version")
            return
        except Exception:
            continue
    raise RuntimeError("浏览器调试端口未就绪")


def wait_for_token(port, timeout=300):
    expr = "localStorage.getItem('access_token')||''"
    print("等待登录…（登录成功后会自动继续；Ctrl+C 放弃）", file=sys.stderr)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            val = cdp_eval_js(port, expr, timeout=8) or ""
            if str(val).startswith("eyJ"):
                return str(val)
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError("等待登录超时（" + str(timeout) + " 秒）")


def load_saved_token():
    try:
        return (json.loads(CRED_FILE.read_text(encoding="utf-8")) or {}).get("access_token") or None
    except Exception:
        return None


def save_token(token):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CRED_FILE.write_text(
        json.dumps({
            "access_token": token,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "note": "几小时有效的会话令牌；tpdc.py auth --logout 可清除",
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        os.chmod(CRED_FILE, 0o600)
    except Exception:
        pass


def token_is_accepted(token):
    """Any structured answer other than 用户未登录 proves the token authenticates."""
    _st, pl = http_json(
        BASE + "/file/ftp/getDownloadFtpUserByMetadataId?metadataId=" + AUTH_PROBE_ID,
        "POST", {}, token=token,
    )
    cls, _reason, _info = classify_login_payload(pl)
    return cls in ("ENTITLED", "NO_ENTITLEMENT")


AUTH_PORT_FILE = CACHE_DIR / "auth_port.txt"


def _remember_port(port):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        AUTH_PORT_FILE.write_text(str(port), encoding="utf-8")
    except OSError:
        pass


def ensure_auth_browser(url):
    """Reuse the dedicated auth browser if it is alive; otherwise start one.

    The debug port is remembered so later runs attach to the same,
    already-logged-in instance instead of spawning duplicates.
    """
    remembered = []
    try:
        remembered.append(int(AUTH_PORT_FILE.read_text(encoding="utf-8").strip()))
    except (OSError, ValueError):
        pass
    for port in remembered + list(range(9273, 9283)):
        try:
            _cdp_http(port, "/json/version")
            _remember_port(port)
            return port
        except Exception:
            continue
    exe = find_chromium()
    if not exe:
        raise RuntimeError("未找到 Chrome/Edge；请安装，或改用 --token / --token-file 手动传入")
    port = free_port()
    subprocess.Popen(
        [exe, "--remote-debugging-port=" + str(port), "--user-data-dir=" + str(PROFILE_DIR),
         "--no-first-run", "--no-default-browser-check", "--window-size=1100,860", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        time.sleep(0.4)
        try:
            _cdp_http(port, "/json/version")
            _remember_port(port)
            return port
        except Exception:
            continue
    raise RuntimeError(
        "浏览器调试端口未就绪；若之前由 tpdc.py 打开的浏览器窗口还在，请先关闭它再重试"
    )


def run_auth_flow(timeout=300):
    port = ensure_auth_browser(BASE + "/home")
    print("已在浏览器打开 TPDC；若未登录请登录，成功后这里会自动继续。", file=sys.stderr)
    token = wait_for_token(port, timeout=timeout)
    save_token(token)
    accepted = token_is_accepted(token)
    print("令牌已保存: " + str(CRED_FILE), file=sys.stderr)
    print(
        "有效性: 服务端认可 ✅" if accepted
        else "有效性: 已保存（探针数据集未确认，但令牌已可随请求使用）",
        file=sys.stderr,
    )
    return token


def resolve_token(explicit=None):
    """--token > --token-file (already merged) > env > saved credentials."""
    if explicit:
        return explicit
    env = os.environ.get("TPDC_TOKEN")
    if env:
        return env
    return load_saved_token()


def cmd_auth(args):
    if args.logout:
        removed = []
        if CRED_FILE.exists():
            CRED_FILE.unlink()
            removed.append("保存的令牌")
        if args.purge and PROFILE_DIR.exists():
            shutil.rmtree(PROFILE_DIR, ignore_errors=True)
            removed.append("浏览器登录配置")
        print("已清除: " + ("、".join(removed) if removed else "（本来就没有）"))
        return 0
    if args.show:
        token = load_saved_token()
        if not token:
            print("未保存令牌；运行 tpdc.py auth 获取。")
            return 2
        print("令牌: " + token[:10] + "…（已脱敏）  保存于 " + str(CRED_FILE))
        print("有效性: " + ("服务端认可 ✅" if token_is_accepted(token) else "已过期或无效（重新运行 auth）"))
        return 0
    run_auth_flow(timeout=max(30, args.timeout))
    print("之后 download / status / meta / cite 会自动使用它；过期时 download 会自动重开浏览器。")
    return 0


# --------------------------------------------------------------------------- #
# Progress
# --------------------------------------------------------------------------- #
class Progress:
    def __init__(self, total, enabled=True):
        self.total = max(int(total), 1)
        self.done = 0
        self.lock = threading.Lock()
        self.enabled = enabled
        self.start = time.time()
        self.last_t = self.start
        self.last_d = 0
        self.speed = 0.0
        self.stop = threading.Event()
        self.thread = None

    def add(self, n):
        with self.lock:
            self.done += n

    def _render(self):
        with self.lock:
            done = self.done
        now = time.time()
        dt = now - self.last_t
        if dt >= 1:
            inst = (done - self.last_d) / dt
            self.speed = inst if self.speed <= 0 else 0.7 * self.speed + 0.3 * inst
            self.last_t, self.last_d = now, done
        pct = min(100.0, 100.0 * done / self.total)
        width = 28
        filled = int(width * pct / 100)
        bar = "#" * filled + "-" * (width - filled)
        eta = (self.total - done) / self.speed if self.speed > 0 else 0
        eta_s = f"{int(eta // 60)}m{int(eta % 60):02d}s" if eta > 0 else "--"
        elapsed = int(time.time() - self.start)
        sys.stderr.write(
            f"\r[{bar}] {pct:5.1f}%  {human(done)} / {human(self.total)}  "
            f"{self.speed / 1e6:.2f} MB/s  ETA {eta_s}  已用 {elapsed // 60}m{elapsed % 60:02d}s   "
        )
        sys.stderr.flush()

    def _loop(self):
        while not self.stop.wait(1.0):
            self._render()

    def __enter__(self):
        if self.enabled:
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)
        if self.enabled:
            self._render()
            sys.stderr.write("\n")
            sys.stderr.flush()


# --------------------------------------------------------------------------- #
# Download engine
# --------------------------------------------------------------------------- #
def connect_ftp(info, ctl_timeout=60):
    errors = []
    for host in info["hosts"] or ["ftp3.tpdc.ac.cn"]:
        try:
            ftp = FTP()
            ftp.encoding = "utf-8"
            ftp.connect(host, info["port"], timeout=ctl_timeout)
            ftp.login(info["username"], info["password"])
            ftp.set_pasv(True)
            ftp.voidcmd("TYPE I")
            return ftp
        except Exception as exc:
            errors.append(f"{host}: {type(exc).__name__}: {exc}")
    raise RuntimeError("FTP 连接失败: " + "; ".join(errors))


def sha256_file(path, chunk=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def download_files(
    info,
    files,
    out_dir,
    workers=4,
    seg_mb=32,
    do_sha256=False,
    progress=True,
    sock_timeout=30,
    refresh=None,
):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    seg_size = max(1, int(seg_mb)) * 1024 * 1024

    total = sum(f["size"] for f in files)
    # Large files first so parallel workers stay busy.
    tasks = []
    for f in sorted(files, key=lambda x: x["size"], reverse=True):
        local = out_dir / Path(f["relpath"])
        local.parent.mkdir(parents=True, exist_ok=True)
        if local.exists() and local.stat().st_size == f["size"]:
            continue
        if f["size"] <= seg_size:
            tasks.append((f, 0, 0, f["size"], local, Path(str(local) + ".seg000")))
        else:
            nseg = (f["size"] + seg_size - 1) // seg_size
            for i in range(nseg):
                start = i * seg_size
                end = min(f["size"], start + seg_size)
                tasks.append((f, i, start, end, local, Path(str(local) + f".seg{i:03d}")))

    if not tasks:
        print("所有文件已存在，跳过下载。", file=sys.stderr)
        return []

    prog = Progress(total, enabled=progress)
    holder = {"info": info}
    refresh_lock = threading.Lock()
    refresh_used = {"n": 0}
    file_locks = {f["relpath"]: threading.Lock() for f in files}
    files_by_rel = {f["relpath"]: f for f in files}
    local = threading.local()

    class Worker:
        def __init__(self):
            self.ftp = None

        def ensure(self):
            if self.ftp is None:
                self.ftp = connect_ftp(holder["info"])

        def close(self):
            if self.ftp is not None:
                try:
                    self.ftp.quit()
                except Exception:
                    pass
                self.ftp = None

        def refresh_credentials(self):
            """Per-dataset FTP accounts expire; re-request up to 3 times."""
            if refresh is None:
                return False
            with refresh_lock:
                if refresh_used["n"] >= 3:
                    return False
                refresh_used["n"] += 1
            try:
                new_info = refresh()
            except Exception:
                return False
            if new_info and new_info.get("username"):
                holder["info"] = new_info
                return True
            return False

        def run(self, task):
            f, idx, start, end, local_path, seg_path = task
            expected = end - start
            if seg_path.exists() and seg_path.stat().st_size == expected:
                return
            existing = seg_path.stat().st_size if seg_path.exists() else 0
            if existing > expected:
                seg_path.unlink()
                existing = 0
            last_exc = None
            for attempt in range(8):
                try:
                    self.ensure()
                    rest = start + existing
                    conn = self.ftp.transfercmd(
                        "RETR " + f["relpath"], rest=rest if rest > 0 else None
                    )
                    conn.settimeout(sock_timeout)  # fail fast if the server stalls
                    mode = "ab" if existing else "wb"
                    with open(seg_path, mode) as fh:
                        remaining = expected - existing
                        while remaining > 0:
                            chunk = conn.recv(256 * 1024)
                            if not chunk:
                                break
                            if len(chunk) > remaining:
                                chunk = chunk[:remaining]
                            fh.write(chunk)
                            prog.add(len(chunk))
                            remaining -= len(chunk)
                    try:
                        conn.close()
                    except Exception:
                        pass
                    try:
                        self.ftp.voidresp()
                    except Exception:
                        pass
                    if seg_path.stat().st_size != expected:
                        existing = seg_path.stat().st_size
                        continue
                    return
                except (error_temp, error_perm, OSError, EOFError) as exc:
                    last_exc = exc
                    if "530" in str(exc):
                        self.refresh_credentials()
                    self.close()
                    time.sleep(2 + attempt * 2)
            raise RuntimeError(f"下载失败 {f['relpath']} seg{idx}: {last_exc}")

    def concat_if_done(relpath):
        f = files_by_rel[relpath]
        with file_locks[relpath]:
            local_path = out_dir / Path(relpath)
            if local_path.exists() and local_path.stat().st_size == f["size"]:
                return
            segs = sorted(local_path.parent.glob(local_path.name + ".seg*"))
            if len(segs) == 0:
                return
            expected_n = 1 if f["size"] <= seg_size else (f["size"] + seg_size - 1) // seg_size
            if len(segs) != expected_n:
                return
            if any(
                s.stat().st_size != min(seg_size, f["size"] - int(s.name.rsplit(".seg", 1)[1]) * seg_size)
                for s in segs
            ):
                return
            tmp = Path(str(local_path) + ".concat")
            with open(tmp, "wb") as out:
                for s in segs:
                    with open(s, "rb") as inp:
                        while True:
                            block = inp.read(1024 * 1024)
                            if not block:
                                break
                            out.write(block)
            os.replace(tmp, local_path)
            for s in segs:
                try:
                    s.unlink()
                except OSError:
                    pass
            print(f"\n完成: {relpath}  {human(f['size'])}", file=sys.stderr)

    def worker_fn(task):
        if not hasattr(local, "w"):
            local.w = Worker()
        try:
            local.w.run(task)
        finally:
            concat_if_done(task[0]["relpath"])

    with prog:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = [pool.submit(worker_fn, t) for t in tasks]
            for fut in futures:
                fut.result()

    result = []
    for f in files:
        local_path = out_dir / Path(f["relpath"])
        rec = {"path": f["relpath"], "size": f["size"], "local": str(local_path)}
        if local_path.exists() and local_path.stat().st_size == f["size"]:
            if do_sha256:
                rec["sha256"] = sha256_file(local_path)
        else:
            rec["status"] = "missing"
        result.append(rec)
    return result


# --------------------------------------------------------------------------- #
# Citation helpers
# --------------------------------------------------------------------------- #
def fmt_authors(raw, n=3):
    parts = [p.strip() for p in re.split(r"[,，;；]", raw or "") if p.strip()]
    if len(parts) > n:
        return ", ".join(parts[:n]) + ", 等"
    return ", ".join(parts)


def citation_text(rec, metadata_id):
    year = (rec.get("tsCreated") or "")[:4]
    authors = fmt_authors(rec.get("authorName"))
    title = rec.get("title") or ""
    doi = rec.get("doi") or ""
    accessed = time.strftime("%Y-%m-%d")
    url = f"{BASE}/zh-hans/data/{metadata_id}"
    lines = [
        f"{authors} ({year}). {title}. National Tibetan Plateau / Third Pole Environment Data Center. "
        f"https://doi.org/{doi}（引用日期：{accessed}）",
        "",
        "GB/T 7714:",
        f"{authors}. {title}[DS/OL]. 国家青藏高原科学数据中心, {year}[{accessed}]. "
        f"https://doi.org/{doi}.",
        f"数据页面: {url}",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Metadata file output
# --------------------------------------------------------------------------- #
def write_description(out_dir, rec):
    """Write the raw TPDC description; never invent CRS/datum."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    title = rec.get("title") or "TPDC dataset"
    text = (
        f"# {title}\n\n"
        f"- 数据集 ID: {rec.get('id')}\n"
        f"- DOI: {rec.get('doi') or ''}\n"
        f"- 访问级别: {rec.get('sharePolicy') or ''}\n"
        f"- 更新: {rec.get('tsUpdated') or ''}\n\n"
        f"## 数据集描述（TPDC 元数据原文）\n\n"
        f"{rec.get('description') or '（该数据集没有提供描述字段）'}\n"
    )
    path = out_dir / "DATASET_DESCRIPTION.md"
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# CLI commands
# --------------------------------------------------------------------------- #
def cmd_search(args):
    records, total = search(
        args.keyword,
        limit=args.limit,
        page=args.page,
        english=args.english,
        sort=args.sort,
    )
    remember(records)
    if args.json:
        print(json.dumps({"total": total, "records": records}, ensure_ascii=False, indent=2))
        return
    if not records:
        print("没有找到结果。")
        return
    print(f"{'ID':38}  {'访问':4}  {'下载':>6}  {'标题':46}  {'作者':18}  DOI")
    print("-" * 150)
    for r in records:
        print(
            f"{r.get('id',''):38}  {pad(r.get('sharePolicy') or '?', 4)}  "
            f"{str(r.get('download') or 0):>6}  {pad(r.get('title') or '', 46)}  "
            f"{pad(r.get('authorName') or '', 18)}  {r.get('doi') or ''}"
        )
    pages = (total + args.limit - 1) // args.limit if args.limit else 0
    print(
        f"\n第 {args.page}/{max(pages, 1)} 页，共 {total} 条"
        f"（A=开放免登录，B=需登录，C=需申请）"
    )


def cmd_list(args):
    cache = load_cache()
    if args.json:
        print(json.dumps(cache, ensure_ascii=False, indent=2))
        return
    if not cache:
        print("本地缓存为空；先运行 search。")
        return
    print(f"{'ID':38}  {'访问':4}  标题")
    print("-" * 110)
    for mid, r in cache.items():
        print(f"{mid:38}  {pad(r.get('sharePolicy') or '?', 4)}  {r.get('title') or ''}")
    print(f"\n共 {len(cache)} 条缓存记录")


def cmd_info(args):
    token = resolve_token(args.token)
    rec = get_record(args.metadata_id, cookie=args.cookie, token=token)
    files = walk_files(args.metadata_id)
    if args.json:
        print(json.dumps({"record": rec, "files": files}, ensure_ascii=False, indent=2))
        return
    if rec:
        print(f"标题: {rec.get('title')}")
        print(f"作者: {rec.get('authorName') or ''}")
        print(f"DOI:  {rec.get('doi') or ''}")
        print(f"访问: {rec.get('sharePolicy') or '?'}")
        print(f"更新: {rec.get('tsUpdated') or ''}")
        dl = rec.get("download")
        if dl is not None:
            print(f"下载次数: {dl}")
    else:
        print("（本地缓存和在线详情中都没有该数据集的元数据；可先运行 search）")
    if rec and rec.get("description"):
        desc = rec["description"].strip().replace("\n", " ")
        print(f"描述: {desc[:180]}{'…' if len(desc) > 180 else ''}")
        print("完整描述: python scripts/tpdc.py meta <metadataId>")
    total = sum(f["size"] for f in files)
    print(f"文件: {len(files)} 个，总计 {human(total)}")
    for f in files[:30]:
        print(f"  {human(f['size']):>10}  {f['relpath']}")
    if len(files) > 30:
        print(f"  ... 其余 {len(files)-30} 个文件")


def cmd_files(args):
    files = walk_files(args.metadata_id, use_cache=not args.no_cache)
    if args.json:
        print(json.dumps(files, ensure_ascii=False, indent=2))
        return
    total = sum(f["size"] for f in files)
    if args.tree:
        print(render_tree(files))
    else:
        for f in files:
            print(f"{human(f['size']):>10}  {f['relpath']}")
    print(f"\n{len(files)} 个文件，总计 {human(total)}")


def cmd_meta(args):
    token = resolve_token(args.token)
    rec = get_record(args.metadata_id, cookie=args.cookie, token=token)
    if not rec:
        print(
            "本地缓存和在线详情中都没有该数据集；请先运行 search，或对 B 类数据集提供 --cookie。",
            file=sys.stderr,
        )
        return 2
    if args.out:
        path = write_description(args.out, rec)
        print(f"已写入 {path}", file=sys.stderr)
    if args.json:
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        return
    print(f"标题: {rec.get('title')}")
    print(f"作者: {rec.get('authorName') or ''}")
    print(f"DOI:  {rec.get('doi') or ''}")
    print(f"访问: {rec.get('sharePolicy') or '?'}")
    print(f"更新: {rec.get('tsUpdated') or ''}")
    print()
    print(rec.get("description") or "（该数据集没有提供描述字段）")


# --------------------------------------------------------------------------- #
# Download progress state (file names and sizes only — no credentials)
# --------------------------------------------------------------------------- #
STATE_FILE = ".tpdc_progress.json"


def write_progress_state(out_dir, title, files, status="下载中"):
    """Write panel state next to the download. Contains no credentials."""
    try:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        payload = {
            "tool": f"tpdc-fetch {__version__}",
            "title": title,
            "total": sum(f["size"] for f in files),
            "files_total": len(files),
            "status": status,
            "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        if len(files) <= 2000:
            payload["files"] = [[f["relpath"], f["size"]] for f in files]
        (Path(out_dir) / STATE_FILE).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


def cmd_download(args):
    token = resolve_token(getattr(args, "token", None))
    rec = get_record(args.metadata_id, cookie=args.cookie, token=token)
    out = Path(args.out)
    if args.metadata_only:
        if not rec:
            print(
                "本地缓存和在线详情中都没有该数据集；请先运行 search，或提供 --cookie。",
                file=sys.stderr,
            )
            return 2
        path = write_description(out, rec)
        print(f"已写入 {path}", file=sys.stderr)
        return 0

    cookie = args.cookie or os.environ.get("TPDC_COOKIE")
    workers = min(max(1, args.workers), FTP_WORKER_CAP)
    # Resolve access before walking the tree: B/C datasets should fail fast
    # instead of recursing through a potentially huge file tree.
    info = None
    if (cookie or token) and not getattr(args, "no_probe", False):
        verdict = probe_access(
            args.metadata_id, cookie, token, attempts=getattr(args, "attempts", 3) or 3
        )
        if verdict["state"] == "AUTH_INVALID" and not getattr(args, "no_relogin", False):
            print("令牌无效或过期，自动打开浏览器重新登录…（登录后自动继续）", file=sys.stderr)
            try:
                token = run_auth_flow(timeout=300)
            except Exception as exc:
                print(f"自动重新登录失败: {exc}", file=sys.stderr)
                print_access_verdict(verdict)
                return 2
            verdict = probe_access(args.metadata_id, cookie, token, attempts=2)
        if verdict["state"] in ("NO_ENTITLEMENT", "AUTH_INVALID"):
            print_access_verdict(verdict)
            return 2
        vinfo = verdict.get("ftp_info")
        if verdict["state"] == "ENTITLED" and vinfo and vinfo.get("username"):
            info = dict(vinfo)
            info["access"] = "B"
        # OPEN / UNKNOWN_TRANSIENT / NO_ENTITLEMENT_ONCE: let get_ftp decide.
    if info is None:
        try:
            info = get_ftp(args.metadata_id, cookie, token)
        except RuntimeError as exc:
            if getattr(args, "no_relogin", False) or "未提供凭据" not in str(exc):
                raise
            print("该数据集需要登录：自动打开浏览器…（登录后自动继续）", file=sys.stderr)
            token = run_auth_flow(timeout=300)
            info = get_ftp(args.metadata_id, cookie, token)

    files = walk_files(args.metadata_id, use_cache=not args.no_cache)

    def match_any(f, pats):
        # Glob matches the FTP-relative path OR the bare filename.
        name = f.get("name") or ""
        return any(fnmatch.fnmatch(f["relpath"], p) or fnmatch.fnmatch(name, p) for p in pats)

    if args.include:
        files = [f for f in files if match_any(f, args.include)]
    if args.exclude:
        files = [f for f in files if not match_any(f, args.exclude)]
    if not files:
        print("没有匹配的文件。", file=sys.stderr)
        return 2
    total = sum(f["size"] for f in files)
    print(
        f"数据集: {rec.get('title') or args.metadata_id}\n"
        f"访问: {info['access']} 类  |  FTP: {', '.join(info['hosts'])}:{info['port']}"
        f"  |  并发: {workers}\n"
        f"文件: {len(files)} 个，总计 {human(total)}",
        file=sys.stderr,
    )
    if args.dry_run:
        for f in files:
            print(f"  {human(f['size']):>10}  {f['relpath']}")
        return 0

    write_progress_state(out, rec.get("title") or args.metadata_id, files)
    panel_url = None
    if getattr(args, "panel", False):
        try:
            from progress_panel import serve as panel_serve

            _server, panel_url = panel_serve(out, getattr(args, "panel_port", 8765))
            print(f"面板: {panel_url}  （随 download 进程存活）", file=sys.stderr)
        except Exception as exc:
            print(f"面板启动失败: {exc}", file=sys.stderr)

    def refresh():
        return get_ftp(args.metadata_id, cookie, token)

    results = download_files(
        info,
        files,
        args.out,
        workers=workers,
        seg_mb=args.seg_mb,
        do_sha256=args.sha256,
        progress=not args.no_progress,
        sock_timeout=args.sock_timeout,
        refresh=refresh,
    )
    write_progress_state(out, rec.get("title") or args.metadata_id, files, status="已完成")

    manifest = {
        "tool": f"tpdc-fetch {__version__}",
        "dataset_id": args.metadata_id,
        "title": rec.get("title"),
        "authors": rec.get("authorName"),
        "doi": rec.get("doi"),
        "description": rec.get("description"),
        "access": info["access"],
        "ftp_host": info["hosts"][0] if info["hosts"] else None,
        "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_bytes": sum(r["size"] for r in results),
        "files": results,
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if rec:
        write_description(out, rec)
        (out / "CITATION.txt").write_text(citation_text(rec, args.metadata_id), encoding="utf-8")
    print(f"\n完成，清单: {out / 'manifest.json'}", file=sys.stderr)
    return 0


def cmd_verify(args):
    mpath = Path(args.dir) / "manifest.json"
    if not mpath.exists():
        print(f"未找到 {mpath}", file=sys.stderr)
        return 2
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"manifest.json 解析失败: {exc}", file=sys.stderr)
        return 2
    files = manifest.get("files") or []
    if not files:
        print("清单里没有文件记录。")
        return 0
    out_dir = Path(args.dir)
    ok = missing = bad = 0
    for rec in files:
        p = out_dir / rec["path"]
        if not p.is_file():
            print(f"缺失      {rec['path']}")
            missing += 1
            continue
        actual = p.stat().st_size
        if actual != rec.get("size"):
            print(f"大小不符  {rec['path']}  清单 {rec.get('size')}  实际 {actual}")
            bad += 1
            continue
        if args.sha256 or rec.get("sha256"):
            h = sha256_file(p)
            if rec.get("sha256") and h != rec["sha256"]:
                print(f"SHA256不符 {rec['path']}")
                bad += 1
                continue
        ok += 1
    print(f"\n校验完成: {ok} 正常, {missing} 缺失, {bad} 异常")
    return 0 if (missing == 0 and bad == 0) else 1


def cmd_status(args):
    cookie = args.cookie or os.environ.get("TPDC_COOKIE")
    token = resolve_token(args.token)
    v = probe_access(
        args.metadata_id,
        cookie,
        token,
        attempts=max(1, args.attempts),
        ftp_check=args.ftp_check,
    )
    if args.json:
        printable = {k: val for k, val in v.items() if k != "ftp_info"}
        print(json.dumps(printable, ensure_ascii=False, indent=2))
        return 0 if v["can_download"] else 2
    print_access_verdict(v)
    return 0 if v["can_download"] else 2


def cmd_cite(args):
    token = resolve_token(args.token)
    rec = get_record(args.metadata_id, cookie=args.cookie, token=token)
    if not rec:
        print(
            "本地缓存和在线详情中都没有该数据集；请先运行 search，或提供 --cookie。",
            file=sys.stderr,
        )
        return 2
    year = (rec.get("tsCreated") or "")[:4]
    title = rec.get("title") or ""
    doi = rec.get("doi") or ""
    url = f"{BASE}/zh-hans/data/{args.metadata_id}"
    accessed = time.strftime("%Y-%m-%d")
    if args.format == "bibtex":
        key = "tpdc_" + re.sub(r"[^A-Za-z0-9]", "", args.metadata_id)[:12]
        authors = " and ".join(
            p.strip() for p in re.split(r"[,，;；]", rec.get("authorName") or "") if p.strip()
        )
        print(
            f"@misc{{{key},\n"
            f"  author    = {{{authors}}},\n"
            f"  title     = {{{title}}},\n"
            f"  year      = {{{year}}},\n"
            f"  publisher = {{National Tibetan Plateau / Third Pole Environment Data Center}},\n"
            f"  doi       = {{{doi}}},\n"
            f"  url       = {{{url}}},\n"
            f"  urldate   = {{{accessed}}}\n"
            f"}}"
        )
    elif args.format == "ris":
        parts = [p.strip() for p in re.split(r"[,，;；]", rec.get("authorName") or "") if p.strip()]
        ris = ["TY  - DATA"]
        ris += [f"AU  - {p}" for p in parts]
        ris += [
            f"TI  - {title}",
            f"PY  - {year}",
            "PB  - National Tibetan Plateau / Third Pole Environment Data Center",
            f"DO  - {doi}",
            f"UR  - {url}",
            f"DA  - {accessed}",
            "ER  -",
        ]
        print("\n".join(ris))
    elif args.format == "gb":
        authors = fmt_authors(rec.get("authorName"))
        print(
            f"{authors}. {title}[DS/OL]. 国家青藏高原科学数据中心, {year}[{accessed}]. "
            f"https://doi.org/{doi}."
        )
    else:
        print(citation_text(rec, args.metadata_id))


def main():
    _force_utf8_stdio()
    ap = argparse.ArgumentParser(
        prog="tpdc.py",
        description=f"TPDC Fetch v{__version__}：国家青藏高原科学数据中心检索与下载",
    )
    ap.add_argument("--version", action="version", version=f"tpdc-fetch {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("search", help="检索数据集")
    p.add_argument("keyword")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--english", action="store_true", help="检索英文元数据")
    p.add_argument(
        "--sort", choices=["time", "downloads", "views"], default="time",
        help="排序：更新时间 / 下载次数 / 浏览量",
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("list", help="列出本地缓存的数据集")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("info", help="查看数据集与文件概览")
    p.add_argument("metadata_id")
    p.add_argument("--cookie", help="浏览器 Cookie（在线补齐元数据用）")
    p.add_argument("--token", help="Bearer 令牌（B 类数据可用）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("files", help="列出文件")
    p.add_argument("metadata_id")
    p.add_argument("--tree", action="store_true", help="树状视图")
    p.add_argument("--no-cache", action="store_true", help="忽略 1 小时文件树缓存")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_files)

    p = sub.add_parser("meta", help="打印数据集完整描述")
    p.add_argument("metadata_id")
    p.add_argument("--cookie", help="浏览器 Cookie（在线补齐元数据用）")
    p.add_argument("--token", help="Bearer 令牌（B 类数据可用）")
    p.add_argument("--out", help="同时把描述写入指定目录的 DATASET_DESCRIPTION.md")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_meta)

    p = sub.add_parser("download", help="下载数据集")
    p.add_argument("metadata_id")
    p.add_argument("--out", required=True)
    p.add_argument("--workers", type=int, default=4, help="并发连接数（TPDC 上限 5/IP，自动钳制）")
    p.add_argument("--seg-mb", type=int, default=32, help="大文件分段大小 MB")
    p.add_argument("--sock-timeout", type=int, default=30, help="FTP 数据 socket 超时秒数")
    p.add_argument("--cookie", help="浏览器 Cookie（B 类数据需要）")
    p.add_argument("--token", help="Bearer 令牌（B 类数据可用；取自登录后的 access_token）")
    p.add_argument("--include", action="append", help="只下载匹配的文件，可多次")
    p.add_argument("--exclude", action="append", help="排除匹配的文件，可多次")
    p.add_argument("--dry-run", action="store_true", help="只列文件与 FTP 信息，不下载")
    p.add_argument("--metadata-only", action="store_true", help="只写 DATASET_DESCRIPTION.md，不下载")
    p.add_argument("--sha256", action="store_true", help="下载后计算 SHA256")
    p.add_argument("--no-cache", action="store_true", help="忽略 1 小时文件树缓存")
    p.add_argument("--no-probe", action="store_true", help="跳过下载前的权限预检")
    p.add_argument("--no-relogin", action="store_true", help="凭据失效时不自动打开浏览器重登")
    p.add_argument("--no-progress", action="store_true")
    p.add_argument("--panel", action="store_true", help="启动本机实时面板（仅绑定 127.0.0.1）")
    p.add_argument("--panel-port", type=int, default=8765)
    p.add_argument(
        "--token-file", help="从文件读取 Bearer 令牌（避免令牌进入 shell 历史）"
    )
    p.add_argument("--cookie-file", help="从文件读取 Cookie")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("status", help="探测当前账号对该数据集的实际下载权限")
    p.add_argument("metadata_id")
    p.add_argument("--cookie", help="浏览器 Cookie")
    p.add_argument("--token", help="Bearer 令牌（取自登录后的 access_token）")
    p.add_argument("--attempts", type=int, default=3, help="登录探测次数；瞬时错误会退避重试")
    p.add_argument("--ftp-check", action="store_true", help="再登录 FTP 并 SIZE 首个文件做最终验证")
    p.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    p.add_argument(
        "--token-file", help="从文件读取 Bearer 令牌（避免令牌进入 shell 历史）"
    )
    p.add_argument("--cookie-file", help="从文件读取 Cookie")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("auth", help="登录一次自动保存令牌；之后所有命令自动使用")
    p.add_argument("--logout", action="store_true", help="清除保存的令牌")
    p.add_argument("--purge", action="store_true", help="连同浏览器登录配置一起删除")
    p.add_argument("--show", action="store_true", help="查看保存的令牌状态（脱敏）")
    p.add_argument("--timeout", type=int, default=300, help="等待登录的秒数")
    p.set_defaults(func=cmd_auth)

    p = sub.add_parser("verify", help="对照 manifest.json 校验已下载的文件")
    p.add_argument("dir", help="下载目录（含 manifest.json）")
    p.add_argument("--sha256", action="store_true", help="即使清单没存哈希也计算 SHA256")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("cite", help="生成引用")
    p.add_argument("metadata_id")
    p.add_argument("--cookie", help="浏览器 Cookie（在线补齐元数据用）")
    p.add_argument("--token", help="Bearer 令牌（B 类数据可用）")
    p.add_argument("--format", choices=["bibtex", "ris", "gb", "text"], default="bibtex")
    p.set_defaults(func=cmd_cite)

    args = ap.parse_args()
    # Credential files keep secrets out of argv and shell history; values are
    # only held in memory for this process and never written anywhere.
    try:
        if getattr(args, "token_file", None):
            args.token = Path(args.token_file).read_text(encoding="utf-8").strip()
        if getattr(args, "cookie_file", None):
            args.cookie = Path(args.cookie_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        print(f"ERROR: 读取凭据文件失败: {exc}", file=sys.stderr)
        return 2
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

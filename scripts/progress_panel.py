#!/usr/bin/env python3
"""TPDC live download panel.

Two ways to run it:

  * embedded in a download:  tpdc.py download <id> --out ./data --panel
  * standalone watcher:      python scripts/progress_panel.py --dir ./data

It reads `.tpdc_progress.json` (written by tpdc.py: dataset title, total
bytes, expected file list, status) and watches the download directory for
in-flight `.segNNN` parts, `.concat` temporaries and finished files.

The server binds to 127.0.0.1 only and serves two things: the dashboard
HTML and a /progress JSON snapshot. It never serves file contents, and it
never sees credentials. No third-party dependencies. Python 3.8+.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATE_FILE = ".tpdc_progress.json"
DEFAULT_PORT = 8765
PANEL_HTML = "progress-panel.html"
MAX_ACTIVE = 6


def human_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024


def human_eta(seconds):
    if seconds is None or seconds <= 0 or seconds > 86400 * 7:
        return "—"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s"


def html_path():
    here = Path(__file__).resolve().parent
    for cand in (here.parent / "assets" / PANEL_HTML, here / PANEL_HTML):
        if cand.exists():
            return cand
    return None


def read_state(directory):
    try:
        with open(Path(directory) / STATE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def walk_files(directory):
    """Yield (relpath, size, mtime) for every file under directory."""
    for root, _dirs, names in os.walk(directory):
        for name in names:
            path = os.path.join(root, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            rel = os.path.relpath(path, directory).replace(os.sep, "/")
            yield rel, st.st_size, st.st_mtime


def scan_directory(directory, expected=None):
    """Account bytes in the download directory.

    Returns (downloaded, active_segments, files_done).
    `expected` maps relpath -> size when the state file lists the dataset;
    without it a heuristic is used (segs + concats + finals without segs).
    """
    segs, concats, finals = {}, {}, {}
    for rel, size, mtime in walk_files(directory):
        name = rel.rsplit("/", 1)[-1]
        if name == STATE_FILE:
            continue
        if ".seg" in name:
            base = rel.rsplit(".seg", 1)[0]
            segs.setdefault(base, []).append((rel, size, mtime))
        elif name.endswith(".concat"):
            concats[rel[: -len(".concat")]] = size
        else:
            finals[rel] = (size, mtime)

    downloaded = 0
    active = []
    files_done = 0

    if expected:
        for rel, total_size in expected.items():
            seg_list = segs.get(rel, [])
            if rel in finals and finals[rel][0] == total_size:
                downloaded += total_size
                files_done += 1
            elif seg_list:
                seg_sum = sum(s for _r, s, _m in seg_list)
                downloaded += seg_sum
                newest = max(seg_list, key=lambda x: x[2])
                active.append((newest[0], newest[1]))
            elif rel in concats:
                downloaded += concats[rel]
    else:
        seg_bases = set(segs) | set(concats)
        for base, seg_list in segs.items():
            seg_sum = sum(s for _r, s, _m in seg_list)
            downloaded += seg_sum
            newest = max(seg_list, key=lambda x: x[2])
            active.append((newest[0], newest[1]))
        for rel, size in concats.items():
            downloaded += size
        for rel, (size, _m) in finals.items():
            if rel not in seg_bases and size > 0:
                downloaded += size
                files_done += 1

    active.sort(key=lambda x: -x[1])
    return downloaded, active[:MAX_ACTIVE], files_done


class Tracker:
    """Scans the directory on demand and smooths the transfer speed."""

    def __init__(self, directory):
        self.directory = str(directory)
        self.lock = threading.Lock()
        self.last_t = time.time()
        self.last_b = 0
        self.speed = 0.0
        self._first = True

    def snapshot(self):
        with self.lock:
            state = read_state(self.directory)
            expected = None
            if isinstance(state.get("files"), list):
                expected = {}
                for item in state["files"]:
                    try:
                        expected[item[0]] = int(item[1])
                    except (TypeError, ValueError, IndexError):
                        continue
            downloaded, active, files_done = scan_directory(self.directory, expected)

            now = time.time()
            if self._first:
                self.last_t, self.last_b, self._first = now, downloaded, False
            dt = now - self.last_t
            if dt >= 0.5:
                inst = max(0.0, (downloaded - self.last_b) / dt)
                self.speed = inst if self.speed <= 0 else 0.7 * self.speed + 0.3 * inst
                self.last_t, self.last_b = now, downloaded

            total = int(state.get("total") or 0)
            files_total = int(state.get("files_total") or 0)
            percent = (100.0 * downloaded / total) if total else None
            eta = ((total - downloaded) / self.speed) if (total and self.speed > 0) else None

            if state.get("status") == "已完成" or (
                total and downloaded >= total * 0.999 and not active
            ):
                status = "已完成"
            elif active or (state.get("status") == "下载中"):
                status = "下载中"
            else:
                status = "空闲"

            return {
                "title": state.get("title") or Path(self.directory).name,
                "percent": round(percent, 2) if percent is not None else None,
                "downloaded_human": human_bytes(downloaded),
                "total_human": human_bytes(total) if total else "—",
                "speed_human": f"{self.speed / 1e6:.2f} MB/s" if self.speed > 0 else "—",
                "eta_human": human_eta(eta),
                "files_done": files_done,
                "files_total": files_total or None,
                "active": [{"name": n, "size_human": human_bytes(s)} for n, s in active],
                "status": status,
                "now": time.strftime("%H:%M:%S"),
            }


def make_handler(tracker):
    html = html_path()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, body, ctype, code=200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/progress":
                body = json.dumps(tracker.snapshot(), ensure_ascii=False)
                self._send(body, "application/json; charset=utf-8")
            elif path in ("/", "/index.html"):
                if html:
                    self._send(html.read_bytes(), "text/html; charset=utf-8")
                else:
                    self._send("progress-panel.html not found", "text/plain; charset=utf-8", 404)
            else:
                self.send_error(404)

        def log_message(self, *args):
            pass

    return Handler


def serve(directory, port=DEFAULT_PORT, host="127.0.0.1"):
    """Start the panel server. Returns (server, url); serve_forever is up to you."""
    tracker = Tracker(directory)
    handler = make_handler(tracker)
    server = None
    last_err = None
    for p in range(int(port), int(port) + 20):
        try:
            server = ThreadingHTTPServer((host, p), handler)
            port = p
            break
        except OSError as exc:
            last_err = exc
    if server is None:
        raise OSError(f"端口 {port}-{int(port) + 19} 都不可用: {last_err}")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://{host}:{port}/"


def main():
    ap = argparse.ArgumentParser(description="TPDC 下载实时面板（仅本机）")
    ap.add_argument("--dir", required=True, help="下载目录")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--json", action="store_true", help="输出一次快照 JSON 后退出")
    ap.add_argument("--open", action="store_true", help="启动后打开浏览器")
    args = ap.parse_args()

    tracker = Tracker(args.dir)
    if args.json:
        print(json.dumps(tracker.snapshot(), ensure_ascii=False, indent=2))
        return 0
    server, url = serve(args.dir, args.port, args.host)
    print(f"面板: {url}  (Ctrl+C 退出)", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""TPDC (National Tibetan Plateau Data Center) search and download helper.

Supports:
  - public metadata search (no login)
  - recursive file listing
  - open-data (sharePolicy A) no-login FTP download
  - login-assisted (sharePolicy B) FTP download via a browser cookie
  - resumable, segmented, multi-connection FTP download with a live progress bar

No third-party dependencies. Python 3.8+.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import posixpath
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from ftplib import FTP, error_perm, error_temp
from pathlib import Path

BASE = "https://data.tpdc.ac.cn"
UA = "tpdc-fetch/0.1 (+https://data.tpdc.ac.cn)"
CACHE_DIR = Path(os.environ.get("TPDC_CACHE_DIR", Path.home() / ".tpdc-fetch"))
CACHE_FILE = CACHE_DIR / "cache.json"


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def http_json(url, method="GET", body=None, cookie=None, timeout=40):
    headers = {"User-Agent": UA, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=UTF-8"
    if cookie:
        headers["Cookie"] = cookie
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
    except Exception as exc:  # network error
        return 0, {"error": f"{type(exc).__name__}: {exc}"}


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
            cache[r["id"]] = r
    save_cache(cache)


# --------------------------------------------------------------------------- #
# Search / metadata
# --------------------------------------------------------------------------- #
def search(keyword, limit=10, page=1, english=False):
    body = {
        "projectId": "",
        "title": keyword,
        "daid": "",
        "pageSize": limit,
        "pageNum": page,
        "downloadSort": False,
        "viewsSort": False,
        "tsUpdateTimeSort": True,
        "placeId": "",
        "subjectId": "",
        "themeId": "",
        "isImportant": False,
        "isEnglish": english,
    }
    status, payload = http_json(BASE + "/view/metadataView/page", "POST", body)
    if status != 200 or not isinstance(payload, dict) or payload.get("code") != "200":
        raise RuntimeError(f"TPDC 检索失败 (HTTP {status}): {api_error(payload)}")
    return payload.get("context", {}).get("metadataViewVOPage", {}).get("records", [])


def get_record(metadata_id):
    return load_cache().get(metadata_id, {})


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


def walk_files(metadata_id):
    """Return a flat list of files with FTP-relative paths."""
    out = []

    def recurse(items, prefix):
        for item in items:
            name = item.get("name") or ""
            rel = f"{prefix}/{name}" if prefix else name
            if item.get("type") == "dir":
                recurse(child_files(item.get("id")), rel)
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
    return out


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


def ftp_login(metadata_id, cookie):
    if not cookie:
        return None
    url = BASE + "/file/ftp/getDownloadFtpUserByMetadataId?metadataId=" + urllib.parse.quote(metadata_id)
    status, payload = http_json(url, "POST", {}, cookie=cookie)
    if status == 200 and isinstance(payload, dict) and payload.get("code") == "200":
        return parse_ftp(payload.get("data") or {})
    return None


def get_ftp(metadata_id, cookie=None):
    info = ftp_open(metadata_id)
    if info and info.get("username"):
        info["access"] = "A"
        return info
    info = ftp_login(metadata_id, cookie)
    if info and info.get("username"):
        info["access"] = "B"
        return info
    raise RuntimeError(
        "该数据集不是开放数据，需要登录/申请。\n"
        f"  页面: {BASE}/zh-hans/data/{metadata_id}\n"
        "  登录后把浏览器 Cookie 传给 --cookie（或设置 TPDC_COOKIE），再重试。"
    )


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
        sys.stderr.write(
            f"\r[{bar}] {pct:5.1f}%  {human(done)} / {human(self.total)}  "
            f"{self.speed / 1e6:.2f} MB/s  ETA {eta_s}   "
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


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024


# --------------------------------------------------------------------------- #
# Download engine
# --------------------------------------------------------------------------- #
def connect_ftp(info):
    errors = []
    for host in info["hosts"] or ["ftp3.tpdc.ac.cn"]:
        try:
            ftp = FTP()
            ftp.encoding = "utf-8"
            ftp.connect(host, info["port"], timeout=60)
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


def download_files(info, files, out_dir, workers=4, seg_mb=32, do_sha256=False, progress=True):
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
    file_locks = {f["relpath"]: threading.Lock() for f in files}
    files_by_rel = {f["relpath"]: f for f in files}
    local = threading.local()

    class Worker:
        def __init__(self):
            self.ftp = None

        def ensure(self):
            if self.ftp is None:
                self.ftp = connect_ftp(info)

        def close(self):
            if self.ftp is not None:
                try:
                    self.ftp.quit()
                except Exception:
                    pass
                self.ftp = None

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
                    conn.settimeout(30)  # fail fast if the server stops sending
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
            if any(s.stat().st_size != min(seg_size, f["size"] - int(s.name.rsplit(".seg", 1)[1]) * seg_size) for s in segs):
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
# CLI commands
# --------------------------------------------------------------------------- #
def cmd_search(args):
    records = search(args.keyword, limit=args.limit, page=args.page)
    remember(records)
    if args.json:
        print(json.dumps(records, ensure_ascii=False, indent=2))
        return
    if not records:
        print("没有找到结果。")
        return
    print(f"{'ID':38}  {'访问':4}  {'标题':46}  {'作者':18}  DOI")
    print("-" * 150)
    for r in records:
        title = (r.get("title") or "")[:44]
        author = (r.get("authorName") or "")[:16]
        print(
            f"{r.get('id',''):38}  {r.get('sharePolicy') or '?':4}  "
            f"{title:46}  {author:18}  {r.get('doi') or ''}"
        )
    print(f"\n共 {len(records)} 条（A=开放免登录，B=需登录，C=需申请）")


def cmd_info(args):
    rec = get_record(args.metadata_id)
    files = walk_files(args.metadata_id)
    if rec:
        print(f"标题: {rec.get('title')}")
        print(f"作者: {rec.get('authorName') or ''}")
        print(f"DOI:  {rec.get('doi') or ''}")
        print(f"访问: {rec.get('sharePolicy') or '?'}")
        print(f"更新: {rec.get('tsUpdated') or ''}")
    else:
        print("（本地缓存中没有该数据集的元数据；可先运行 search）")
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
    if args.json:
        print(json.dumps({"record": rec, "files": files}, ensure_ascii=False, indent=2))


def cmd_files(args):
    files = walk_files(args.metadata_id)
    if args.json:
        print(json.dumps(files, ensure_ascii=False, indent=2))
        return
    total = sum(f["size"] for f in files)
    for f in files:
        print(f"{human(f['size']):>10}  {f['relpath']}")
    print(f"\n{len(files)} 个文件，总计 {human(total)}")


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


def cmd_meta(args):
    rec = get_record(args.metadata_id)
    if not rec:
        print("本地缓存中没有该数据集；请先运行 search。", file=sys.stderr)
        return 2
    print(f"标题: {rec.get('title')}")
    print(f"作者: {rec.get('authorName') or ''}")
    print(f"DOI:  {rec.get('doi') or ''}")
    print(f"访问: {rec.get('sharePolicy') or '?'}")
    print(f"更新: {rec.get('tsUpdated') or ''}")
    print()
    print(rec.get("description") or "（该数据集没有提供描述字段）")
    if args.json:
        print(json.dumps(rec, ensure_ascii=False, indent=2))


def cmd_download(args):
    rec = get_record(args.metadata_id)
    out = Path(args.out)
    if args.metadata_only:
        if not rec:
            print("本地缓存中没有该数据集；请先运行 search。", file=sys.stderr)
            return 2
        path = write_description(out, rec)
        print(f"已写入 {path}", file=sys.stderr)
        return 0

    cookie = args.cookie or os.environ.get("TPDC_COOKIE")
    # Resolve access before walking the tree: B/C datasets should fail fast
    # instead of recursing through a potentially huge file tree.
    info = get_ftp(args.metadata_id, cookie)

    files = walk_files(args.metadata_id)
    if args.include:
        files = [f for f in files if any(fnmatch.fnmatch(f["relpath"], p) for p in args.include)]
    if args.exclude:
        files = [f for f in files if not any(fnmatch.fnmatch(f["relpath"], p) for p in args.exclude)]
    if not files:
        print("没有匹配的文件。", file=sys.stderr)
        return 2
    total = sum(f["size"] for f in files)
    print(
        f"数据集: {args.metadata_id}\n"
        f"访问: {info['access']} 类  |  FTP: {', '.join(info['hosts'])}:{info['port']}\n"
        f"文件: {len(files)} 个，总计 {human(total)}",
        file=sys.stderr,
    )
    if args.dry_run:
        for f in files:
            print(f"  {human(f['size']):>10}  {f['relpath']}")
        return 0

    results = download_files(
        info,
        files,
        args.out,
        workers=args.workers,
        seg_mb=args.seg_mb,
        do_sha256=args.sha256,
        progress=not args.no_progress,
    )

    manifest = {
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
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if rec:
        write_description(out, rec)
        year = (rec.get("tsCreated") or "")[:4]
        cite = (
            f"{rec.get('authorName') or ''} ({year}). {rec.get('title') or ''}. "
            f"National Tibetan Plateau Data Center. https://doi.org/{rec.get('doi') or ''}\n"
        )
        (out / "CITATION.txt").write_text(cite, encoding="utf-8")
    print(f"\n完成，清单: {out / 'manifest.json'}", file=sys.stderr)
    return 0


def cmd_cite(args):
    rec = get_record(args.metadata_id)
    if not rec:
        print("本地缓存中没有该数据集；请先运行 search。", file=sys.stderr)
        return 2
    year = (rec.get("tsCreated") or "")[:4]
    authors = rec.get("authorName") or ""
    title = rec.get("title") or ""
    doi = rec.get("doi") or ""
    url = f"{BASE}/zh-hans/data/{args.metadata_id}"
    if args.format == "bibtex":
        key = "tpdc_" + re.sub(r"[^A-Za-z0-9]", "", args.metadata_id)[:12]
        print(
            f"@misc{{{key},\n"
            f"  author    = {{{authors}}},\n"
            f"  title     = {{{title}}},\n"
            f"  year      = {{{year}}},\n"
            f"  publisher = {{National Tibetan Plateau Data Center}},\n"
            f"  doi       = {{{doi}}},\n"
            f"  url       = {{{url}}}\n"
            f"}}"
        )
    elif args.format == "ris":
        print(f"TY  - DATA\nAU  - {authors}\nTI  - {title}\nPY  - {year}\nPB  - National Tibetan Plateau Data Center\nDO  - {doi}\nUR  - {url}\nER  -")
    else:
        print(f"{authors} ({year}). {title}. National Tibetan Plateau Data Center. https://doi.org/{doi}")


def main():
    ap = argparse.ArgumentParser(description="TPDC Fetch：TPDC 数据检索与下载")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("search", help="检索数据集")
    p.add_argument("keyword")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("info", help="查看数据集与文件概览")
    p.add_argument("metadata_id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("files", help="列出文件")
    p.add_argument("metadata_id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_files)

    p = sub.add_parser("meta", help="打印数据集完整描述")
    p.add_argument("metadata_id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_meta)

    p = sub.add_parser("download", help="下载数据集")
    p.add_argument("metadata_id")
    p.add_argument("--out", required=True)
    p.add_argument("--workers", type=int, default=4, help="并发连接数（TPDC 上限 5/IP）")
    p.add_argument("--seg-mb", type=int, default=32, help="大文件分段大小 MB")
    p.add_argument("--cookie", help="浏览器 Cookie（B 类数据需要）")
    p.add_argument("--include", action="append", help="只下载匹配的文件，可多次")
    p.add_argument("--exclude", action="append", help="排除匹配的文件，可多次")
    p.add_argument("--dry-run", action="store_true", help="只列文件与 FTP 信息，不下载")
    p.add_argument("--metadata-only", action="store_true", help="只写 DATASET_DESCRIPTION.md，不下载")
    p.add_argument("--sha256", action="store_true", help="下载后计算 SHA256")
    p.add_argument("--no-progress", action="store_true")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("cite", help="生成引用")
    p.add_argument("metadata_id")
    p.add_argument("--format", choices=["bibtex", "ris", "text"], default="bibtex")
    p.set_defaults(func=cmd_cite)

    args = ap.parse_args()
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

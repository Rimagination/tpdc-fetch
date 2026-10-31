---
name: tpdc-fetch
description: Use when Codex needs to search or download datasets from the National Tibetan Plateau Data Center (TPDC, data.tpdc.ac.cn), including open no-login datasets and login-assisted datasets. Unofficial community tool.
---

# TPDC Fetch

> Unofficial community tool. Not affiliated with or endorsed by TPDC.

Search datasets on [data.tpdc.ac.cn](https://data.tpdc.ac.cn) and download them over FTP with resume, segmentation, a live progress bar, a manifest, and a citation file.

The script uses only the Python standard library (Python 3.8+). It talks to the same public endpoints the TPDC web app uses; these endpoints are undocumented and may change.

v0.2.0 additions: `list` / `verify` commands, GB/T 7714 citations, online metadata fallback (`--cookie` on `meta`/`info`/`cite`), search sorting and totals, file-tree caching, filename-aware `--include/--exclude`, 530 credential auto-refresh, UTF-8 safe output on Windows.

v0.2.1: Bearer token auth (`--token` / `TPDC_TOKEN`) for policy-B datasets, and access errors now surface the server's reason. `未查询到对应FTP账户` means the account is logged in but has no entitlement — the dataset needs a separate application; retrying or changing credentials will not help.

v0.4.0: `download --panel` local live dashboard (127.0.0.1 only) plus a standalone
watcher (`scripts/progress_panel.py`); `--token-file` / `--cookie-file` keep
secrets out of shell history; credentials are never written to disk.

v0.3.0: `status <id>` probe — three-stage check (no-login FTP → login FTP → optional FTP `SIZE`) that resolves what the current account can actually download. `download` runs the same probe first and refuses early on conclusive denials.

## Decide before you download

The `sharePolicy` letter (A/B/C) is the site's door sign, **not** the permission. What decides is whether the server issues an FTP account for *this account + this dataset*. Probe with `status`:

```bash
python scripts/tpdc.py status <metadataId> --token "eyJ..."        # or --cookie / TPDC_TOKEN
python scripts/tpdc.py status <metadataId> --token "eyJ..." --ftp-check --json
```

| state | meaning | what to do |
|---|---|---|
| `OPEN` | no-login endpoint grants FTP | download without credentials |
| `ENTITLED` | login endpoint grants FTP | download with the same credentials |
| `NO_ENTITLEMENT` | `未查询到对应FTP账户` seen twice | login is not enough; the dataset needs a separate application. Do not retry. |
| `NO_ENTITLEMENT_ONCE` | seen once | inconclusive; rerun `status --attempts 3` |
| `AUTH_INVALID` | `用户未登录` | refresh the token/cookie |
| `NEEDS_AUTH` | probed without credentials | pass `--token` (localStorage `access_token`) or `--cookie` |
| `UNKNOWN_TRANSIENT` | `code=500 / 操作失败` | server hiccup; retry later — **never** report this as "unavailable" |

Rules for agents:

1. Run `status` before `download` on any B/C dataset (download does it automatically; `--no-probe` skips).
2. A single `操作失败` proves nothing. Retry with backoff before drawing conclusions.
3. Report the state plus the server's own reason verbatim; never translate a transient error into "the data is unavailable".
4. Verdicts are account-specific: the same dataset can be downloadable for one account and not another.

## Access model

TPDC datasets carry a `sharePolicy`:

| Policy | Meaning | What this skill does |
|---|---|---|
| **A** | Open, no login | Calls `createNoLoginDownloadFtpUser` and downloads directly |
| **B** | Login required | Calls `getDownloadFtpUserByMetadataId` with a browser cookie you provide |
| **C** | Application required | Stops and points you to the dataset page; never bypasses the application |

The skill tries A first. If A fails and a cookie is available, it tries B. It never imitates a login and never stores credentials.

## Quick start

```bash
# 1. Search (prints ID, access policy, downloads, title, authors, DOI)
python scripts/tpdc.py search "沙漠" --limit 10
python scripts/tpdc.py search "冰川" --sort downloads   # by download count
python scripts/tpdc.py search "desert" --english        # English metadata

# 2. Inspect files and total size (tree view; 1h local cache)
python scripts/tpdc.py files <metadataId> --tree

# 3. Read the dataset description (projection, scale, attributes)
python scripts/tpdc.py meta <metadataId>

# 4. Dry run: resolve FTP credentials and list files without downloading
python scripts/tpdc.py download <metadataId> --out ./data --dry-run

# 5. Download
python scripts/tpdc.py download <metadataId> --out ./data

# 6. Verify a finished download against manifest.json
python scripts/tpdc.py verify ./data
```

`meta`, `info`, and `cite` also work without a prior `search`: they try the
`detailOpen` endpoint online (pass `--cookie` for policy-B datasets) and fall
back to the local cache.

`search` caches records in `~/.tpdc-fetch/cache.json`, so `info`, `download`, and `cite` can reuse the title, authors, DOI, and access policy. `list` shows everything cached.

## Metadata first

Always read the dataset's `description` before interpreting projection, datum, attributes, or scale:

```bash
python scripts/tpdc.py meta <metadataId>
```

The skill reports what the metadata says. It never invents a CRS or datum. If the description says only **“大地坐标系”** and does not name a datum, report that; do not assign one.

`download` writes the raw description to `DATASET_DESCRIPTION.md`; `--metadata-only` writes it without downloading.

`search` caches records in `~/.tpdc-fetch/cache.json`, so `info`, `download`, and `cite` can reuse the title, authors, DOI, and access policy.

## Login-assisted downloads (policy B)

1. Log in at <https://data.tpdc.ac.cn>.
2. Open DevTools → Network → any request to `data.tpdc.ac.cn` → copy the `Cookie` request header.
3. Pass it once:

```bash
python scripts/tpdc.py download <metadataId> --out ./data --cookie "SESSION=...; token=..."
# or
export TPDC_COOKIE="SESSION=...; token=..."
python scripts/tpdc.py download <metadataId> --out ./data
```

If the session cookie is `httpOnly` (browsers refuse to expose it to page scripts), use
the Bearer token instead — DevTools → Application → Local Storage → `access_token`:

```bash
python scripts/tpdc.py download <metadataId> --out ./data --token "eyJ..."
# or
export TPDC_TOKEN="eyJ..."
python scripts/tpdc.py download <metadataId> --out ./data
```

If the API returns `用户未登录`, the cookie/token expired; copy a fresh one. The skill does not persist credentials.

## Live progress panel

`download` writes `.tpdc_progress.json` (dataset title, total bytes, file
names/sizes — no credentials) next to the download and can serve a local
dashboard:

```bash
python scripts/tpdc.py download <id> --out ./data --panel              # http://127.0.0.1:8765
python scripts/tpdc.py download <id> --out ./data --panel --panel-port 8800
python scripts/progress_panel.py --dir ./data                          # watch a running download
python scripts/progress_panel.py --dir ./data --json                   # one snapshot, for scripts
```

The server binds to 127.0.0.1 only and serves the dashboard plus a
`/progress` snapshot; it never serves file contents. The panel lives as
long as the download process.

## Credentials and privacy

* `--cookie` / `--token` (and `TPDC_COOKIE` / `TPDC_TOKEN`) are held in
  memory for the process only — never written to disk, logs, manifest.json
  or the panel.
* Prefer `--token-file` / `--cookie-file` so secrets stay out of shell
  history; values are read once and kept in memory.
* `.tpdc_progress.json` contains dataset title, file names and sizes only.
* The panel binds to 127.0.0.1; nothing is exposed to the network.

## Download options

| Option | Default | Notes |
|---|---|---|
| `--out` | required | Destination directory; relative paths are preserved |
| `--workers` | `4` | Clamped to 1–5; TPDC allows at most **5 connections per IP** |
| `--seg-mb` | `32` | Segment size for large files |
| `--sock-timeout` | `30` | FTP data-socket timeout in seconds |
| `--include` | — | Glob on the FTP-relative path **or bare filename**; repeatable |
| `--exclude` | — | Glob on the FTP-relative path **or bare filename**; repeatable |
| `--dry-run` | off | Resolve credentials and list files, download nothing |
| `--metadata-only` | off | Write `DATASET_DESCRIPTION.md` only; no download |
| `--sha256` | off | Compute SHA256 after each file (slower) |
| `--no-cache` | off | Ignore the 1-hour file-tree cache |
| `--no-progress` | off | Disable the live terminal bar |

Examples:

```bash
# Only shapefiles (matches the filename anywhere in the tree)
python scripts/tpdc.py download <id> --out ./data --include "*.shp"

# Skip the PDF and documentation
python scripts/tpdc.py download <id> --out ./data --exclude "*.pdf" --exclude "*.doc"

# Resume after an interruption (complete files are skipped, partial segments resumed)
python scripts/tpdc.py download <id> --out ./data
```

## Outputs

Every download writes:

- `DATASET_DESCRIPTION.md` — the raw TPDC description (projection, scale, attributes) exactly as the metadata provides it.
- `manifest.json` — dataset ID, title, authors, DOI, raw `description`, access policy, FTP host, download time, per-file size/SHA256, tool version.
- `CITATION.txt` — a ready-to-paste citation plus a GB/T 7714 line.

## Verification

```bash
python scripts/tpdc.py verify ./data          # size check (+SHA256 where stored)
python scripts/tpdc.py verify ./data --sha256 # recompute SHA256 for every file
```

Prints per-file problems and exits non-zero if anything is missing or mismatched.

## Citation

```bash
python scripts/tpdc.py cite <metadataId> --format bibtex   # BibTeX (authors joined with "and", urldate)
python scripts/tpdc.py cite <metadataId> --format ris      # RIS
python scripts/tpdc.py cite <metadataId> --format gb       # GB/T 7714 (中文国标)
python scripts/tpdc.py cite <metadataId> --format text     # plain text + GB/T 7714 line
```

## Behaviour and limits

- **FTP hosts**: tries `ftp2.tpdc.ac.cn`, then `ftp3.tpdc.ac.cn`. `ftp2` is often full (`421`); the fallback is normal.
- **Connection cap**: 5 per IP. `--workers` is clamped to at most 5.
- **Credential expiry**: per-dataset FTP accounts can expire mid-download (`530`); the engine re-requests credentials automatically (up to 3 times).
- **Bandwidth**: TPDC is slow during the day and fast roughly 02:00–10:00 local time. Large datasets can take tens of minutes.
- **Resume**: complete files are skipped by exact size; `.segNNN` files are resumed with `REST`.
- **Paths**: Chinese names and the full-width colon in `1：10万沙漠分布图` are handled; output is forced to UTF-8 so Windows GBK consoles cannot crash on CJK text.
- **No invented CRS**: the skill does not create a `.prj` or guess a datum. If the description is incomplete, say so and verify against a known boundary.
- **Detail endpoint**: `/metadataView/detailOpen` may return `614 验证已过期` without a token. `meta`/`info`/`cite` fall back to the search cache; pass `--cookie` to try online.
- **File-tree cache**: `files`/`download` cache the tree for 1 hour in `~/.tpdc-fetch/`; use `--no-cache` to force a refresh.
- **No official API**: the endpoints in `references/api-notes.md` are the web app's internal endpoints. They may change without notice. If they do, search and download will fail loudly rather than silently produce wrong data.

## Etiquette

- Do not bypass login or application requirements; policy C datasets must be requested through the website.
- Do not share accounts or cookies.
- Cache search results and avoid hammering the search endpoint.
- Cite the dataset and follow its license. TPDC datasets often require the data provider's citation and acknowledgement.

## Files

- `scripts/tpdc.py` — search, list, info, files, meta, download, verify, cite.
- `references/api-notes.md` — endpoints, request shapes, response examples, known quirks.
- `examples/desert-1-100000.md` — end-to-end example for the 1:100,000 desert dataset.

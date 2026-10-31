---
name: tpdc-fetch
description: Use when Codex needs to search or download datasets from the National Tibetan Plateau Data Center (TPDC, data.tpdc.ac.cn), including open no-login datasets and login-assisted datasets. Unofficial community tool.
---

# TPDC Fetch

> Unofficial community tool. Not affiliated with or endorsed by TPDC.

Search datasets on [data.tpdc.ac.cn](https://data.tpdc.ac.cn) and download them over FTP with resume, segmentation, a live progress bar, a manifest, and a citation file.

The script uses only the Python standard library (Python 3.8+). It talks to the same public endpoints the TPDC web app uses; these endpoints are undocumented and may change.

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
# 1. Search (prints ID, access policy, title, authors, DOI)
python scripts/tpdc.py search "沙漠" --limit 10

# 2. Inspect files and total size
python scripts/tpdc.py files <metadataId>

# 3. Read the dataset description (projection, scale, attributes)
python scripts/tpdc.py meta <metadataId>

# 4. Dry run: resolve FTP credentials and list files without downloading
python scripts/tpdc.py download <metadataId> --out ./data --dry-run

# 5. Download
python scripts/tpdc.py download <metadataId> --out ./data
```

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

If the API returns `用户未登录`, the cookie expired; copy a fresh one. The skill does not persist cookies.

## Download options

| Option | Default | Notes |
|---|---|---|
| `--out` | required | Destination directory; relative paths are preserved |
| `--workers` | `4` | TPDC allows at most **5 connections per IP**; 4 is the safe default |
| `--seg-mb` | `32` | Segment size for large files |
| `--include` | — | Glob on the FTP-relative path; repeatable |
| `--exclude` | — | Glob on the FTP-relative path; repeatable |
| `--dry-run` | off | Resolve credentials and list files, download nothing |
| `--metadata-only` | off | Write `DATASET_DESCRIPTION.md` only; no download |
| `--sha256` | off | Compute SHA256 after each file (slower) |
| `--no-progress` | off | Disable the live terminal bar |

Examples:

```bash
# Only shapefiles
python scripts/tpdc.py download <id> --out ./data --include "*.shp"

# Skip the PDF and documentation
python scripts/tpdc.py download <id> --out ./data --exclude "*.pdf" --exclude "*.doc"

# Resume after an interruption (complete files are skipped, partial segments resumed)
python scripts/tpdc.py download <id> --out ./data
```

## Outputs

Every download writes:

- `DATASET_DESCRIPTION.md` — the raw TPDC description (projection, scale, attributes) exactly as the metadata provides it.
- `manifest.json` — dataset ID, title, authors, DOI, raw `description`, access policy, FTP host, download time, per-file size/SHA256.
- `CITATION.txt` — a ready-to-paste citation.

## Citation

```bash
python scripts/tpdc.py cite <metadataId> --format bibtex
python scripts/tpdc.py cite <metadataId> --format ris
python scripts/tpdc.py cite <metadataId> --format text
```

## Behaviour and limits

- **FTP hosts**: tries `ftp2.tpdc.ac.cn`, then `ftp3.tpdc.ac.cn`. `ftp2` is often full (`421`); the fallback is normal.
- **Connection cap**: 5 per IP. Keep `--workers` at 4 unless you know the server is idle.
- **Bandwidth**: TPDC is slow during the day and fast roughly 02:00–10:00 local time. Large datasets can take tens of minutes.
- **Resume**: complete files are skipped by exact size; `.segNNN` files are resumed with `REST`.
- **Paths**: Chinese names and the full-width colon in `1：10万沙漠分布图` are handled; keep the script on UTF-8.
- **No invented CRS**: the skill does not create a `.prj` or guess a datum. If the description is incomplete, say so and verify against a known boundary.
- **Detail endpoint**: `/metadataView/detailOpen` may return `614 验证已过期` without a token. Search and file listing still work; use `search` to populate the cache.
- **No official API**: the endpoints in `references/api-notes.md` are the web app's internal endpoints. They may change without notice. If they do, search and download will fail loudly rather than silently produce wrong data.

## Etiquette

- Do not bypass login or application requirements; policy C datasets must be requested through the website.
- Do not share accounts or cookies.
- Cache search results and avoid hammering the search endpoint.
- Cite the dataset and follow its license. TPDC datasets often require the data provider's citation and acknowledgement.

## Files

- `scripts/tpdc.py` — search, info, files, download, cite.
- `references/api-notes.md` — endpoints, request shapes, response examples, known quirks.
- `examples/desert-1-100000.md` — end-to-end example for the 1:100,000 desert dataset.

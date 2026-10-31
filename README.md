<p align="center">
  <img src="assets/banner.png" alt="tpdc-fetch — TPDC dataset search and download" width="100%">
</p>

# tpdc-fetch

Unofficial CLI / agent skill for searching and downloading datasets from the
**National Tibetan Plateau Data Center (TPDC, [data.tpdc.ac.cn](https://data.tpdc.ac.cn))**.

> Not affiliated with or endorsed by TPDC. The tool uses the same undocumented
> web endpoints as the TPDC web app; they may change without notice. It fails
> loudly rather than guessing when a response shape changes.

## Features

- **Search** TPDC metadata by keyword, with access policy (A/B/C), authors, and DOI.
- **Read the dataset description** (projection, scale, attributes) before interpreting the data.
- **List files** recursively with sizes and total.
- **Download open data (A)** without login.
- **Login-assisted downloads (B)** with a browser cookie you provide; the tool never stores it.
- **Robust FTP transfer**: `ftp2` → `ftp3` fallback, segmented downloads, resume, 4 connections by default (TPDC caps at 5/IP), data-socket timeout.
- **Terminal progress bar**, `manifest.json`, `DATASET_DESCRIPTION.md`, and `CITATION.txt`.
- **No third-party dependencies** — Python standard library only.

## Requirements

- Python 3.8+
- Network access to `data.tpdc.ac.cn` and its FTP servers.

## Install

As a ZCode / Codex skill:

```bash
git clone https://github.com/Rimagination/tpdc-fetch.git ~/.zcode/skills/tpdc-fetch
```

Or use the script directly:

```bash
python scripts/tpdc.py --help
```

## Quick start

```bash
# 1. Search
python scripts/tpdc.py search "沙漠" --limit 10

# 2. Read the dataset description
python scripts/tpdc.py meta <metadataId>

# 3. List files
python scripts/tpdc.py files <metadataId>

# 4. Download (open data, policy A)
python scripts/tpdc.py download <metadataId> --out ./data

# 5. Cite
python scripts/tpdc.py cite <metadataId> --format bibtex
```

## Access model

TPDC datasets carry a `sharePolicy`:

| Policy | Meaning | What this tool does |
|---|---|---|
| **A** | Open, no login | Calls `createNoLoginDownloadFtpUser` and downloads directly |
| **B** | Login required | Calls `getDownloadFtpUserByMetadataId` with a browser cookie you provide |
| **C** | Application required | Stops and points you to the dataset page; never bypasses the application |

For policy B:

1. Log in at <https://data.tpdc.ac.cn>.
2. Open DevTools → Network → any request to `data.tpdc.ac.cn` → copy the `Cookie` request header.
3. Pass it once:

```bash
python scripts/tpdc.py download <metadataId> --out ./data --cookie "SESSION=...; token=..."
# or
export TPDC_COOKIE="SESSION=...; token=..."
python scripts/tpdc.py download <metadataId> --out ./data
```

## Metadata first

Always read the dataset's `description` before interpreting projection, datum,
attributes, or scale:

```bash
python scripts/tpdc.py meta <metadataId>
```

The tool reports what the metadata says. It **never invents a CRS or datum**.
If the description says only “大地坐标系” and does not name a datum, report
that; do not assign one.

`download` writes the raw description to `DATASET_DESCRIPTION.md` and includes
it in `manifest.json`. `--metadata-only` writes the description without
downloading.

## Download options

| Option | Default | Notes |
|---|---|---|
| `--out` | required | Destination directory; relative paths are preserved |
| `--workers` | `4` | TPDC allows at most 5 connections per IP |
| `--seg-mb` | `32` | Segment size for large files |
| `--include` | — | Glob on the FTP-relative path; repeatable |
| `--exclude` | — | Glob on the FTP-relative path; repeatable |
| `--dry-run` | off | Resolve credentials and list files, download nothing |
| `--metadata-only` | off | Write `DATASET_DESCRIPTION.md` only; no download |
| `--sha256` | off | Compute SHA256 after each file (slower) |
| `--no-progress` | off | Disable the terminal progress bar |

## Outputs

Every download writes:

- `DATASET_DESCRIPTION.md` — the raw TPDC description (projection, scale, attributes).
- `manifest.json` — dataset ID, title, authors, DOI, raw description, access policy, FTP host, download time, per-file size/SHA256.
- `CITATION.txt` — a ready-to-paste citation.

## Security

- The tool **does not store credentials**.
- For policy B, pass the cookie via `--cookie` or `TPDC_COOKIE`; never commit it.
- The no-login FTP credentials returned by TPDC are per-dataset and are not
  hardcoded in this repository.
- The progress page and FTP traffic are local; no data is sent anywhere except
  `data.tpdc.ac.cn` and its FTP servers.

See [SECURITY.md](SECURITY.md) for details.

## Limitations

- TPDC has **no public API**. This tool uses the web app's internal endpoints
  (`/view/metadataView/page`, `/file/file/...`, `/file/ftp/...`); they may
  change.
- TPDC is slow during the day and faster roughly 02:00–10:00 local time.
- The detail endpoint may return `614 验证已过期` without a session token;
  search metadata is still sufficient for access classification and citation.
- The tool does not bypass login or application requirements.

## License

MIT. See [LICENSE](LICENSE).

## Acknowledgements

Dataset metadata and files are provided by the National Tibetan Plateau Data
Center. Please cite the original datasets and follow their licenses.

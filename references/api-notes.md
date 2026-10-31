# TPDC endpoints used by this skill

> **These are the TPDC web app's internal endpoints, not a documented public API.**
> They worked on 2026-10-31. They can change without notice. The skill fails loudly
> rather than guessing if a response shape changes.

Base: `https://data.tpdc.ac.cn`

## 1. Search metadata

```http
POST /view/metadataView/page
Content-Type: application/json; charset=UTF-8

{
  "projectId": "",
  "title": "沙漠",
  "daid": "",
  "pageSize": 10,
  "pageNum": 1,
  "downloadSort": false,
  "viewsSort": false,
  "tsUpdateTimeSort": true,
  "placeId": "",
  "subjectId": "",
  "themeId": "",
  "isImportant": false,
  "isEnglish": false
}
```

Response:

```json
{
  "code": "200",
  "message": "操作成功",
  "context": {
    "metadataViewVOPage": {
      "records": [
        {
          "id": "122c9ac2-53ee-4b9a-ae87-1a980b131c9b",
          "title": "中国1:10万沙漠(沙地)分布数据集",
          "doi": "10.3972/westdc.006.2013.db",
          "authorName": "王建华,王一谋,颜长珍,祁元",
          "sharePolicy": "A",
          "tsCreated": "2013-...",
          "tsUpdated": "2020-07-17 16:27:35",
          "download": 1234
        }
      ],
      "total": 9
    }
  }
}
```

Notes:

- `sharePolicy` is `A` (open), `B` (login), or `C` (application).
- Send the body as UTF-8 JSON. In Git Bash, `curl -d '{"title":"沙漠"}'` can be
  re-encoded and produce `400` or `total=0`; use the Python script instead.
- `description` is the dataset's data description (projection, scale, attributes). Read it before interpreting the data; do not infer a datum/CRS that it does not state.
- `search` caches records in `~/.tpdc-fetch/cache.json`.

## 2. Detail

```http
POST /view/metadataView/detailOpen
Content-Type: application/json

{"metadataId": "122c9ac2-53ee-4b9a-ae87-1a980b131c9b"}
```

Without a session token this often returns:

```json
{"code": "614", "message": "验证已过期", "context": null}
```

Search already returns the fields needed for citation and access classification, so
the skill does not depend on this endpoint.

## 3. File tree

Root:

```http
GET /file/file/getRootFileDataList?metadataId=<metadataId>
```

Children:

```http
GET /file/file/getFileDataList?parentId=<parentId>
```

Response items:

```json
{
  "id": "dce95034-fd73-4412-aa0f-67352d1dda5b",
  "name": "1：10万沙漠分布图",
  "type": "dir",
  "size": 1544004130,
  "path": "disk1/WestDC/thematic/desert/<metadataId>/1：10万沙漠分布图",
  "metadataId": "<metadataId>",
  "parentId": "..."
}
```

Notes:

- The double `/file/file/` is intentional: the enclosure service is mounted at
  `/file`, and the controller path itself starts with `/file`.
- `/file/getRootFileDataList` (single `/file`) returns 404.
- `type` is `dir` or `file`; recurse with `parentId`.

## 4. Open-data FTP (policy A)

```http
POST /file/ftp/createNoLoginDownloadFtpUser?metadataId=<metadataId>
Content-Type: application/json

{"noToken": true}
```

Response:

```json
{
  "code": "200",
  "flag": true,
  "data": {
    "ftpUrl": "ftp2.tpdc.ac.cn , ftp3.tpdc.ac.cn",
    "ftpPort": 6201,
    "username": "<ftp-username>",
    "password": "<ftp-password>",
    "metadataId": "",
    "userId": "",
    "userType": ""
  }
}
```

Notes:

- `ftpUrl` contains **two hosts** separated by a comma and spaces; try them in order.
- The account is per-dataset and chrooted to the dataset root. Paths from the file
  tree are relative to that root, e.g. `1：10万沙漠分布图/ahsm.dbf`.
- A `GET` returns `405 Method Not Allowed`; it must be `POST`.
- The server allows **5 FTP connections per IP**. `421 Too many connections` means
  another client or worker holds a slot; retry with backoff or reduce `--workers`.

## 5. Login-assisted FTP (policy B)

```http
POST /file/ftp/getDownloadFtpUserByMetadataId?metadataId=<metadataId>
Content-Type: application/json
Cookie: <browser cookie>
```

Response shape is the same as policy A. Without a cookie:

```json
{"code": "500", "flag": false, "message": "操作失败", "data": "用户未登录"}
```

The skill reads the cookie from `--cookie` or `TPDC_COOKIE` and never stores it.

## 6. Known quirks

| Symptom | Cause | Fix |
|---|---|---|
| Search returns `total=0` | Body was not UTF-8, or the keyword is genuinely absent | Use the Python script; try a shorter keyword |
| Search returns `400` | Same encoding/parameter issue | Use the Python script with `json.dumps(..., ensure_ascii=False)` |
| Detail returns `614` | No session token | Use `search` metadata; do not block on detail |
| FTP `421` | 5 connections per IP reached | Retry; use `--workers 4`; wait for `ftp2` to free a slot |
| FTP `530` | Per-dataset FTP account expired | Re-run the credential request |
| `550` on `RETR` | Path prefix mismatch | The FTP root is chrooted; use the file tree's relative path |

## 7. Terms and safety

- Use the endpoints only to access datasets you are entitled to.
- Policy C datasets must go through the website's application flow.
- Do not share accounts or cookies; do not persist cookies in the skill.
- Cache search results; avoid high-frequency requests.
- Cite the dataset and follow its license and acknowledgement requirements.

<div align="center">
  <img src="./assets/banner.png" alt="tpdc-fetch" width="100%" />

  <a href="./LICENSE"><img alt="License" src="https://img.shields.io/badge/license-MIT-0EA5E9?style=flat-square" /></a>
  <img alt="Python" src="https://img.shields.io/badge/python-3.8%2B-0EA5E9?style=flat-square" />
  <img alt="Unofficial" src="https://img.shields.io/badge/unofficial-community%20tool-64748B?style=flat-square" />

  不用登录也能下开放数据；要登录的，把浏览器 Cookie 交给它。

  [这是什么](#这是什么) · [能做什么](#能做什么) · [开始使用](#开始使用) · [常见问题](#常见问题) · [使用边界](#使用边界)
</div>

## 这是什么

TPDC Fetch 是一个 TPDC（国家青藏高原科学数据中心）的数据获取助手。你告诉它数据集名称或 DOI，它会：

- 检索元数据，告诉你访问级别（A 开放 / B 登录 / C 申请）；
- 读出数据集描述（投影、比例尺、属性表），**不替你猜**；
- 开放数据直接下；需要登录的，用你浏览器的 Cookie 下；
- 支持断点续传、分段下载、manifest 和引用。

**你不需要记 FTP 地址、端口、账号。**

### 适合谁

- 需要批量下 TPDC 数据的研究生、老师和科研人员；
- 不想在网页、FTP 客户端、登录之间来回切的人；
- 需要保留数据说明和引用的复现型研究。

### 不适合谁

- 需要 C 类（申请制）数据、但不愿走官方申请流程的人；
- 需要图形界面、不想用命令行或 Agent 的人。

## 能做什么

| 你想要 | 你可以这样说 | 它会做什么 |
|---|---|---|
| **找数据** | “搜一下 TPDC 的沙漠分布数据” | 返回 ID、访问级别、作者、DOI |
| **看说明** | “这个数据集的投影是什么？” | 读 `description`，原样输出，不猜 datum |
| **下开放数据** | “把 `122c9ac2...` 下到 `./data`” | 免登录 FTP，分段、续传、进度条 |
| **下登录数据** | “用我的 Cookie 下这个数据集” | 用 `--cookie` 或 `TPDC_COOKIE` |
| **只要说明** | “只把数据说明写出来” | `--metadata-only` 写 `DATASET_DESCRIPTION.md` |
| **引用** | “给我这个数据集的 BibTeX” | 生成 `CITATION.txt` / BibTeX |

## 开始使用

### 作为 Agent skill

```bash
git clone https://github.com/Rimagination/tpdc-fetch.git ~/.zcode/skills/tpdc-fetch
```

装好后对 Agent 说一句话：

> 帮我用 tpdc-fetch 搜一下“沙漠”数据，看看能不能直接下。

### 作为命令行

```bash
python scripts/tpdc.py search "沙漠" --limit 10
python scripts/tpdc.py meta <metadataId>
python scripts/tpdc.py download <metadataId> --out ./data
```

Python 3.8+，无第三方依赖。

常用参数：`--workers 4` 并发、`--include/--exclude` 过滤文件、`--metadata-only` 只写说明、`--dry-run` 只试跑不下载。

## 你会得到什么

- `DATASET_DESCRIPTION.md` — TPDC 原始数据说明（投影、比例尺、属性表）；
- `manifest.json` — 数据集 ID、DOI、访问级别、逐文件大小 / SHA256；
- `CITATION.txt` — 可直接粘贴的引用；
- 下载的文件按远程相对路径保存，中断后重跑同一条命令即可续传。

## 访问权限

| 级别 | 含义 | 工具怎么做 |
|---|---|---|
| **A** | 开放，免登录 | 直接拿 FTP 账号下载 |
| **B** | 需要登录 | 用你浏览器的 Cookie：`--cookie "SESSION=...; token=..."` 或 `TPDC_COOKIE` |
| **C** | 需要申请 | 只给数据集页面链接，不绕过申请 |

B 类拿 Cookie：登录 TPDC → DevTools → Network → 任意请求 → 复制 `Cookie` 请求头。工具不会保存它。

## 常见问题

**为什么下载慢？**
TPDC 白天限速，单连接约 100 KB/s，4 路合计约 0.4 MB/s；02:00–10:00 通常快很多。

**为什么默认只有 4 路？**
TPDC 每 IP 最多 5 个连接，第 6 个会直接 `421 Too Many Connections`。

**为什么没有 `.prj`？**
数据集本身没带；说明只写“大地坐标系”，没有 datum。工具不会替你猜，候选 WKT 放在 `crs_candidates/` 并标为“非数据自带信息”。

**下载中断了怎么办？**
重跑同一条 `download` 命令：完成的文件会跳过，`.segNNN` 分段会从断点续传。

**需要登录但没 Cookie？**
先在网页登录，再复制 Cookie。工具不模拟登录、不保存凭据。

## 使用边界

- **非官方工具**，与 TPDC 无隶属关系，也未获其背书；
- 用的是 TPDC 网页前端的内部接口，**可能随时变更**；接口变了会明确报错，不会静默返回错误数据；
- 不绕过登录/申请，不存储账号、Cookie 或 FTP 凭据；
- 数据版权、许可和引用要求以 TPDC 数据集页面为准。

安全问题见 [SECURITY.md](SECURITY.md)。

## 许可证

MIT。见 [LICENSE](LICENSE)。

## 进阶参考

- [SKILL.md](SKILL.md) — Agent 使用说明与完整参数；
- [references/api-notes.md](references/api-notes.md) — 内部接口、请求体、已知坑；
- [examples/desert-1-100000.md](examples/desert-1-100000.md) — 1:10 万沙漠数据集完整示例。

<div align="center">
  <img src="./assets/banner.webp" alt="tpdc-fetch" width="100%" />

  <a href="./LICENSE"><img alt="License" src="https://img.shields.io/badge/license-MIT-0EA5E9?style=flat-square" /></a>
  <img alt="Python" src="https://img.shields.io/badge/python-3.8%2B-0EA5E9?style=flat-square" />
  <img alt="Unofficial" src="https://img.shields.io/badge/unofficial-community%20tool-64748B?style=flat-square" />

  用自然语言获取 TPDC 科学数据的 AI Agent Skill。

  [这是什么](#这是什么) · [能做什么](#能做什么) · [开始使用](#开始使用) · [常见问题](#常见问题) · [使用边界](#使用边界)
</div>

## 这是什么

TPDC Fetch 是一个面向[国家青藏高原科学数据中心](https://data.tpdc.ac.cn)（TPDC）的 Skill：检索、筛选、查阅、下载数据集，一句话就能跑完，Claude Code、Codex、Cursor、ZCode、Qoder、WorkBuddy 等支持 Skill 的 AI Agent 均可直接调用。

**你不需要知道数据集 ID，也不需要记 FTP 地址、端口和账号。你只管用中文说出需求，剩下的交给 Agent。**

## 能做什么

| 你想要 | 你可以这样说 |
|---|---|
| 找数据 | “搜一下 TPDC 上有多少跟沙漠有关的数据集” |
| 判断能不能下 | “这个数据集我的账号能下载吗？” |
| 看数据说明 | “这个数据集的投影和坐标系是什么？” |
| 下载开放数据 | “把这个数据集下载到 ./data 目录” |
| 下载需要登录的数据 | “用我登录后的身份下载这个数据集”（Cookie 或令牌都行，Agent 会引导你） |
| 实时看进度 | “显示 tpdc-fetch 下载任务实时面板” |
| 核对下载结果 | “校验一下刚才下载的文件完不完整” |
| 生成引用 | “给我这个数据集的 GB/T 7714 引用” |

## 开始使用

**一句话安装到 Agent：**

> 帮我安装这个skill：https://github.com/Rimagination/tpdc-fetch

**查找数据：**

> 使用tpdc-fetch检索一下有多少跟沙漠有关的数据集？

**下载数据：**

> 使用tpdc-fetch下载中国1:200万沙漠分布图（1974）

<a href="./assets/desert-download-result.webp"><img src="./assets/desert-download-result-thumb.webp" width="620" alt="下载完成：43 个文件、7.3 MB，附数据说明、manifest 与引用"></a>

**下载时想看进度，直接说：**

> 显示tpdc-fetch下载任务实时面板

Agent 会在本机起一块实时面板（只监听本机回环地址，不暴露到网络），分段进度、已传文件数、写入速度一目了然：

<a href="./assets/progress-panel.webp"><img src="./assets/progress-panel-thumb.webp" width="480" alt="tpdc-fetch 下载任务实时面板"></a>

<details>
<summary><b>命令行参考（可选，不装 Agent 也能用）</b></summary>

```bash
# Python 3.8+，无第三方依赖
python scripts/tpdc.py search "沙漠" --limit 10      # 检索
python scripts/tpdc.py meta <metadataId>             # 数据说明
python scripts/tpdc.py files <metadataId> --tree     # 文件清单
python scripts/tpdc.py status <metadataId>           # 探测下载权限
python scripts/tpdc.py download <metadataId> --out ./data   # 下载
python scripts/tpdc.py verify ./data                 # 完整性校验
python scripts/tpdc.py cite <metadataId> --format gb # 引用
```

常用参数：`--workers 4`、`--include/--exclude`、`--metadata-only`、`--dry-run`、`--sha256`、`--panel`；登录数据用 `--cookie` / `--token`（或 `--cookie-file` / `--token-file`）。

</details>

## 你会得到什么

每次下载，Agent 会自动替你准备好三样东西：

- **数据说明** — TPDC 原始描述（投影、比例尺、属性字段），原样转写，绝不臆测坐标系；
- **下载清单** — 数据集 ID、DOI、访问级别，以及每个文件的大小和 SHA256 校验值；
- **引用文本** — 可直接粘贴进论文的引用（支持 BibTeX / RIS / GB/T 7714）。

## 访问权限

| 级别 | 含义 | 它会怎么做 |
|---|---|---|
| **A** | 开放，免登录 | 直接下载 |
| **B** | 需要登录 | 告诉 Agent 你的登录凭据（浏览器 Cookie 或访问令牌），它替你完成；凭据只在内存里使用 |
| **C** | 需要申请 | 只给你数据集页面链接，不绕过申请流程 |

标签不等于实际权限：有的 B 类数据集登录后仍提示「未查询到对应 FTP 账户」——那说明还需要单独申请；也有的 C 类数据集对已授权账号可以直接下。拿不准就让 Agent「探测一下这个数据集我的实际下载权限」，它会把服务端返回的原因原样报给你。

## 常见问题

**为什么慢？** TPDC 白天限速，单连接约 100 KB/s，4 路约 0.4 MB/s；凌晨 02:00–10:00 更快。

**下载中断了怎么办？** 把同一句话再说一遍，已完成的文件自动跳过，没传完的从断点续传。

**为什么有的数据没有投影文件？** 数据集本身没带，说明里也只写「大地坐标系」，没有指明基准——工具宁可留空也不猜。

**下载完怎么确认没坏？** 让 Agent「校验一下下载结果」，它会逐文件核对大小和 SHA256。

## 使用边界

非官方工具，与 TPDC 无隶属关系；用的是网页内部接口，可能随时变更；不绕过登录/申请。凭据（Cookie / 令牌）只在进程内存里使用，不落盘、不进日志；实时面板只监听本机，不提供文件内容。数据版权与引用以 TPDC 页面为准。

## 许可证

MIT · [LICENSE](LICENSE) · 详细用法见 [SKILL.md](SKILL.md)

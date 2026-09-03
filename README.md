# 组播 IPTV 聚合列表

每天自动抓取组播源，合并成一个可直接喂给播放器（IPTV / VLC / TiviMate 等）的 `output/cq.m3u`。

> 数据源有 **JS 安全挑战（反爬）+ 频率限制**，所以必须用「真实无头 Chrome（Playwright）」驱动抓取，纯 `requests` 会被 403。本仓库已把验证过的抓取链路全部固化。

---

## 抓取链路（已对真实页面逐一验证）

```
数据源首页
  └─ 过 JS 安全挑战，拿到会话 cookie
列表第一页  index.php?t=all&province=cq&limit=20&page=1
  └─ 解析每行： <a class="ip-link" onclick="gotoIP('TOKEN','multicast')">IP</a>
        → 得到 IP / TOKEN / 类型
        → TOKEN 去重（省份下拉框已限定范围）
对每个 IP（带延时规避“操作过于频繁”限流）：
  详情页   index.php?p=TOKEN&t=multicast
    └─ 从「📺 查看频道列表」链接提取 s= 参数
  频道列表 index.php?s=STOKEN&t=multicast
    └─ M3U 接口 index.php?s=STOKEN&t=multicast&channels=1&download=m3u
        → 用浏览器上下文直接拉取 M3U 文本（自动带 cookie）
合并：按 URL 去重；后处理阶段再按显示名合并同名频道（多网关线路 → 单条目多 URL 备播）
输出：output/cq.m3u  +  output/cq.json
```

M3U 内容是数据源已生成好的标准格式，示例：

```
#EXTM3U x-tvg-url="https://fy.188766.xyz/e.xml"
#EXTINF:-1 tvg-id="CCTV1高清" tvg-logo="..." group-title="示例 联通",CCTV1高清
http://113.119.215.53:4022/rtp/229.58.190.151:5000
```

> **只抓第一页**：按需求，爬虫不翻页，只处理列表当前页（`page=1`）。想多抓可把 `LIMIT` 调大，或以后再加翻页。

---

## 仓库结构

```
iptv-aggregator/
├── .github/workflows/update.yml   # GitHub Actions：push/定时/手动跑，自动提交输出
├── src/crawler.py                  # 爬虫本体（Playwright 驱动）
├── src/process.py                 # 后处理：黑名单过滤 + 同名合并 + 排序（抓取后、提交前运行）
├── blacklist.txt                  # 黑名单规则（用户维护；# 开头为注释）
├── output/
│   ├── cq.m3u                      # 生成的播放列表（已排序+过滤，Actions 提交回仓库）
│   └── cq.json                     # 结构化统计（来源 IP / 频道数 / 失败记录 / 屏蔽数）
├── cache/                         # 爬虫缓存（actions/cache 管理，不进 git）
├── requirements.txt               # playwright==1.62.0
└── README.md
```

---

## 在 GitHub Actions 上运行

前置（仅首次）：
1. 进入仓库 **Settings → Actions → General → Workflow permissions**，确认 **Read and write permissions**（工作流需要写权限才能把 output 提交回去）。
2. 进入仓库 **Settings → Secrets and variables → Actions → New repository secret**，添加：
   - Name：`SOURCE_BASE`
   - Value：数据源首页地址（即爬虫要访问的站点根地址，含 `https://`）

   > 数据源地址不放进代码/配置明文，只通过此 Secret 注入，避免暴露。

触发方式（三选一，均已配置）：
- **提交代码**：push 到 `main` 分支即自动运行；
- **定时**：默认每天北京时间 06:00 自动跑；
- **手动**：进入 **Actions → 更新IPTV列表 → Run workflow** 立即跑一次。

跑完后 `output/cq.m3u` 与 `output/cq.json` 会自动 commit 回仓库（提交信息带 `[skip ci]`，不会再次触发本工作流），直接下载或 raw 引用即可。

播放器里填入的原始地址示例（把仓库名/分支名换成你的实际值）：

```
https://raw.githubusercontent.com/<你的用户名>/<仓库名>/main/output/cq.m3u
```

---

## 可调环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SOURCE_BASE` | （无，必填） | 数据源首页地址，通过 Actions Secret 注入（见上） |
| `REGION` | （空） | 类型列过滤关键字；默认空=不过滤，依赖省份下拉框；如需过滤填站点类型列关键字 |
| `PROVINCE` | `cq` | 列表省份参数（驱动下拉框过滤） |
| `LIMIT` | `20` | 每页 IP 数（决定第一页抓多少） |
| `MAX_IPS` | `0` | `0`=不限制；调试时可设小值（如 `2`） |
| `DELAY` | `3` | 每个 IP 之间的延时（秒），**务必保留以规避限流** |
| `PAGE_DELAY` | `2` | 页面加载后等待（秒） |
| `HEADLESS` | `true` | 是否无头；本地调试可设 `false` 看浏览器窗口 |
| `OUTPUT_DIR` | `output` | 输出目录 |
| `CACHE_DIR` | `cache` | 爬虫缓存目录（不进 git、不进 output，由 Actions cache 管理） |
| `CACHE_TTL` | `3600` | 缓存有效期（秒）= 1 小时；命中有效缓存直接复用，跳过全部网络请求 |
| `COOLDOWN` | `30` | 命中“操作过于频繁”时的降温暂停（秒） |
| `USE_CACHE` | `true` | 是否启用缓存；设 `false` 强制每次实时抓取 |
| `NO_CACHE` | `false` | 同 `USE_CACHE=false`，命令行可用 `--no-cache` |
| `BLACKLIST` | （空） | 后处理临时屏蔽：逗号分隔的「显示名关键字」，无需改 `blacklist.txt` 即可测试 |

---

## 本地运行（可选）

需先装好 Playwright + Chromium，并准备数据源地址：

```bash
python -m venv .venv && .venv\Scripts\activate      # Windows
pip install -r requirements.txt
playwright install chromium

# 跑（只抓第一页，限制 2 个 IP 快速验证）
set SOURCE_BASE=https://你的数据源地址
LIMIT=20 MAX_IPS=2 DELAY=4 HEADLESS=true python src/crawler.py
```

> 本地网络若频繁出现 `ERR_CONNECTION_CLOSED`，是数据源/网络抖动，多跑几次或交给 GitHub Actions（数据机房网络更稳定）即可。

---

## 排序与黑名单过滤（后处理）

`src/process.py` 在爬虫生成 `output/cq.m3u` 之后、提交之前运行，对列表做三件事：

1. **黑名单过滤**：按规则丢弃频道，支持四个维度（规则详见 `blacklist.txt`）：
   - `name:关键字` —— 显示名【含】“关键字”即屏蔽（默认，大小写不敏感）
   - `name$:关键字` —— 显示名【以】“关键字”**结尾**即屏蔽（用于“XX 结尾”类需求，如 `name$:SD` 去全部 SD）
   - `name^:关键字` —— 显示名【以】“关键字”开头即屏蔽
   - `name=:关键字` —— 显示名【完全等于】“关键字”才屏蔽
   - `tvgid:xxx` / `host:1.2.3.4:port` / `group:关键字` —— 同理作用于对应维度（`host` 用于剔除坏网关）
   - 任意前缀均可加 `$/^/=`（如 `tvgid$:xxx`）；纯文本（无前缀）等价于 `name:`
   - 规则来源：仓库根 `blacklist.txt` + 可选环境变量 `BLACKLIST`（逗号分隔，便于临时测试，无需改文件）
2. **合并同名频道**：同一频道在多个网关各有 1 条线路（如 `CCTV1HD` 在 6 个网关各 1 条），合并为 **1 个 `#EXTINF` + 多条 URL** 的标准多源格式（播放器自动回退备播），而非每个网关重复一条。合并键 = 显示名；数据源偶发的「重庆市重庆市组播…」重复前缀会被归一为「重庆组播…」。
3. **排序**：大类（`CCTV` → `卫视` → `地方台` → `其他`）→ 频道名自然排序（数字感知，如 `CCTV1 < CCTV2 < CCTV10`）。

> 已预置屏蔽：`4K`（全部）、`CETV`、`SD`（全部）以及 29 个指定 `HD` 频道（见 `blacklist.txt`）。
> 排序大类顺序在 `src/process.py` 的 `CATEGORY_ORDER` 调整；中文名按 Unicode 码位排序（非拼音），如需拼音序可后续增加拼音库。

## 缓存机制（1 小时窗口）

抓取结果会缓存到 `cache/`（**不在 `output/`、不进 git**）。每次运行：

1. **命中有效缓存（1 小时内）** → 直接复用，跳过整个浏览器/网络抓取，秒级出结果；
2. 缓存未命中/已过期 → 实时抓取，成功后写入缓存；
3. GitHub Actions 用 `actions/cache` 跨运行保存 `cache/`，缓存键按**北京小时**滚动，
   天然把复用窗口约束在 1 小时；本地运行则靠 `CACHE_TTL` 计时。

> 想强制刷新（忽略缓存）：设 `NO_CACHE=true` 或命令行加 `--no-cache`。

## 容错设计

- **过挑战失败 / 连接被重置**：`goto_with_retry` 自动退避重试。
- **单个 IP 失败**：跳过并记录到 `cq.json` 的 `failed_ips`，不影响其它 IP。
- **频率限制（“操作过于频繁”）**：步骤间已加 `DELAY` 延时；命中限流时再主动暂停 `COOLDOWN` 秒降温。
- **封 IP / 禁止访问**：检测到封禁关键字立即**停止继续请求**（避免对已被封的 IP 持续施压），并回退到历史缓存兜底，保证仍有输出。
- **输出统计**：运行结束打印 `数据来源(缓存/实时) / 抓取 / 成功 / 失败` 与频道总数。

---

## 防循环说明

工作流由 `push` / `schedule` / `workflow_dispatch` 三种方式触发；自动提交输出的 commit message 带有 `[skip ci]`，GitHub 会跳过该次 push 触发的运行，因此**提交代码 → 抓取 → 自动提交 output** 不会无限循环。

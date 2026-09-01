#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
组播 IPTV 聚合爬虫
================================

流程（均已对数据源真实页面验证）:

  1. 用 Playwright 驱动「真实无头 Chrome」（stealth 绕过 JS 安全挑战），
     先访问首页过挑战、拿到会话 cookie。
  2. 进入 IP 列表页（首页即列表），只收集「当前页」的 IP 行（不翻页）：
       每行: <a class="ip-link" onclick="gotoIP('TOKEN','multicast')">IP</a>
     解析出 IP / TOKEN / 类型。
  3. 按区域过滤（默认不过滤，依赖省份下拉框），对 TOKEN 去重。
  4. 对每个 IP（带延时规避“操作过于频繁”限流）:
       详情页  index.php?p=TOKEN&t=multicast
         -> 取「查看频道列表」链接中的 s= 参数
       频道列表 index.php?s=STOKEN&t=multicast
         -> M3U 接口  index.php?s=STOKEN&t=multicast&channels=1&download=m3u
       用浏览器上下文请求（自动带 cookie）直接拿 M3U 文本。
  5. 合并全部 M3U（按 URL 去重，保留同名频道的不同线路），
     输出 output/cq.m3u 与 output/cq.json。

缓存与封 IP 防护
---------------
  - 抓取结果会写入 CACHE_DIR（默认仓库根 cache/，不进 git、不进 output/）。
  - 命中「有效期内」缓存时直接复用，跳过全部网络请求；
    有效期由 CACHE_TTL（默认 3600s = 1 小时）控制，
    在 GitHub Actions 中由按“北京小时”的缓存键进一步约束为 1 小时窗口。
  - 抓取过程中若检测到「操作过于频繁」会主动降温（暂停 COOLDOWN 秒）；
    若检测到「封 IP / 禁止访问」则立即停止继续请求，并回退到（即便已过期的）
    历史缓存兜底，避免空输出、也避免对已被封的 IP 持续施压。

本脚本设计为在「带 Chrome 的 Docker 环境」中运行
（GitHub Actions 用 mcr.microsoft.com/playwright/python 作为 container），
本地亦可用相同镜像 `docker run` 测试。
"""

import os
import re
import sys
import time
import json
import argparse
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright, Error as PWError


class BannedError(Exception):
    """抓取过程中检测到 IP 被封/被限流到不可用，主动中断。"""
    pass


# 数据源首页地址通过环境变量 SOURCE_BASE 注入（避免在代码中硬编码真实地址），
# 默认值仅为占位，真实值由运行环境（如 GitHub Actions Secret）提供。
BASE = os.environ.get("SOURCE_BASE", "https://example.invalid")
HOME = BASE + "/"

# ---- 可配置项（环境变量优先，其次命令行，最后默认值）----
def env_or(name, default):
    v = os.environ.get(name)
    return v if v is not None else default

REGION = env_or("REGION", "")               # 区域过滤关键字（默认空=不过滤，依赖省份下拉框；如需过滤填站点类型列关键字）
TYPE = env_or("TYPE", "multicast")         # 类型: multicast / hotel / migu / other
PROVINCE = env_or("PROVINCE", "cq")        # 列表省份过滤（不一定生效，故仍按 REGION 二次过滤）
LIMIT = int(env_or("LIMIT", "20"))         # 每页 IP 数
MAX_IPS = int(env_or("MAX_IPS", "0"))      # 0 = 不限制；测试时可设小值
DELAY = float(env_or("DELAY", "3.0"))      # 每个 IP 之间的延时（秒），规避限流
PAGE_DELAY = float(env_or("PAGE_DELAY", "2.0"))
HEADLESS = env_or("HEADLESS", "true").lower() in ("1", "true", "yes", "y")
OUTPUT_DIR = env_or("OUTPUT_DIR", os.path.join(os.path.dirname(__file__), "..", "output"))

# ---- 缓存与封 IP 防护配置 ----
CACHE_DIR = env_or("CACHE_DIR", os.path.join(os.path.dirname(__file__), "..", "cache"))
CACHE_TTL = int(env_or("CACHE_TTL", "3600"))       # 缓存有效期（秒），默认 1 小时
USE_CACHE = env_or("USE_CACHE", "true").lower() in ("1", "true", "yes", "y")
NO_CACHE = env_or("NO_CACHE", "false").lower() in ("1", "true", "yes", "y")
COOLDOWN = float(env_or("COOLDOWN", "30"))         # 命中“操作过于频繁”时的降温暂停（秒）

# 封 IP / 禁止访问 关键字（命中即视为被封，立即停止）
BAN_KEYWORDS = ["封ip", "ip被封", "ip 被封", "禁止访问", "访问被禁止",
                "access denied", "forbidden", "403 forbidden", "您的ip", "已封", "封禁"]
# 限流关键字（命中视为“操作过于频繁”，降温后继续，不直接中断）
THROTTLE_KEYWORDS = ["操作过于频繁", "请求过于频繁", "过于频繁"]

# 只抓取第一页（不翻页）
LIST_URL = f"{BASE}/index.php?t=all&province={PROVINCE}&limit={LIMIT}&page=1"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 过 JS 安全挑战用的 stealth 注入：抹掉 webdriver/cdc_ 等机器人特征
STEALTH_JS = """
() => {
  try { Object.defineProperty(navigator, 'webdriver', { get: () => false }); } catch(e){}
  try { window.navigator.chrome = { runtime: {}, app: {} }; } catch(e){}
  for (const k of Object.getOwnPropertyNames(window)) {
    if (/^\\$cdc_/.test(k) || /__webdriver|__driver|__selenium|__fxdriver/.test(k)) {
      try { delete window[k]; } catch(e){}
    }
  }
}
"""


def log(*a):
    print(f"[{datetime.now().strftime('%H:%M:%S')}]", *a, flush=True)


def wait_real(page, timeout=30000):
    """等挑战重定向结束、真实内容出现。判定：非挑战页，且出现以下任一真实特征：
       - 含 IP 地址（列表页）
       - 含「查看频道列表」/ s= 链接（详情页 / 频道列表页）
    """
    page.wait_for_timeout(3000)
    try:
        page.wait_for_function(
            """() => {
                const b = document.body; if (!b) return false;
                const t = b.innerText || '';
                if (/安全验证|访问被拒绝|Just a moment|正在验证|Checking your browser/.test(t)) return false;
                return /\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}/.test(t)
                    || /查看频道列表/.test(t)
                    || /[?&]s=/.test(t);
            }""",
            timeout=timeout,
        )
        return True
    except PWError:
        return False


def check_block(page):
    """检测页面是否显示限流/封 IP。返回 None / 'throttle' / 'ban'。"""
    try:
        t = page.evaluate("() => { const b = document.body; return b ? (b.innerText || '') : ''; }") or ""
    except Exception:
        return None
    low = t.lower()
    for k in BAN_KEYWORDS:
        if k in low:
            return "ban"
    for k in THROTTLE_KEYWORDS:
        if k in low:
            return "throttle"
    return None


def goto_with_retry(page, url, tries=4, delay=DELAY):
    """带重试的导航：应对挑战重定向偶发失败 / “操作过于频繁”限流。"""
    last = None
    for i in range(tries):
        try:
            page.goto(url, wait_until="load", timeout=60000)
            if wait_real(page):
                return True
            last = "wait_real 超时(可能仍处挑战/限流)"
        except PWError as e:
            last = str(e)
        # 退避 + 抖动
        time.sleep(delay * (i + 1) + (i % 2))
        log(f"    ↻ 导航重试({i+1}/{tries}): {last}")
    return False


def parse_rows(page):
    """从当前列表页表格解析出 (ip, token, type)。"""
    return page.evaluate("""() => {
        const out = [];
        document.querySelectorAll('table.iptv-table tbody tr').forEach(tr => {
            const a = tr.querySelector('a.ip-link');
            if (!a) return;
            const oc = a.getAttribute('onclick') || '';
            let token = null, kind = null;
            const s = oc.indexOf("gotoIP('");
            if (s >= 0) {
                const rest = oc.slice(s + 8);  // 跳过 "gotoIP('"
                const e1 = rest.indexOf("'");
                token = rest.slice(0, e1);
                const c = rest.indexOf("', '");
                if (c >= 0) { const r2 = rest.slice(c + 4); const e2 = r2.indexOf("'"); kind = r2.slice(0, e2); }
            }
            const cells = Array.from(tr.querySelectorAll('td')).map(td => td.innerText.trim());
            out.push({ ip: (cells[0] || '').replace(/\\s/g, ''), token: token, kind: kind, type: cells[2] || '' });
        });
        return out;
    }""")


def wait_real_retry(page, tries=3):
    """wait_real 重试（应对下拉切换后挑战偶发未过）。"""
    for i in range(tries):
        if wait_real(page):
            return True
        page.wait_for_timeout(2000)
        log(f"    ↻ 等待真实内容重试({i+1}/{tries})")
    return False


def collect_ips(page):
    """只收集「当前列表页」的 IP 行（不翻页）。

    关键：URL 直传 province 参数服务端不一定过滤（实测返回全国“新上线”混排），
    因此必须在浏览器里真正驱动「省份」下拉框选 PROVINCE，触发站内过滤。
    """
    log("进入 IP 列表页（仅当前页）...")
    if not goto_with_retry(page, LIST_URL):
        log("  ⚠ 列表页加载失败，终止。")
        return []
    page.wait_for_timeout(PAGE_DELAY * 1000)

    # 驱动省份下拉，强制服务端按 PROVINCE 过滤（URL 直传 province 参数不一定生效）
    try:
        sel = page.query_selector("select[name='province']")
        if sel is not None:
            cur = sel.input_value()
            if cur != PROVINCE:
                log(f"  切换省份下拉: {cur or '(空)'} -> {PROVINCE}")
                sel.select_option(value=PROVINCE)
                page.wait_for_timeout(PAGE_DELAY * 1000)
                wait_real_retry(page)
            else:
                log(f"  省份下拉已是 {PROVINCE}")
        else:
            log("  ⚠ 未找到省份下拉，沿用 URL 参数过滤结果")
    except PWError as e:
        log(f"  ⚠ 省份下拉操作异常(忽略，继续): {e}")

    rows = parse_rows(page)
    seen = {}
    for r in rows:
        if not r["token"]:
            continue
        if r["token"] not in seen:
            seen[r["token"]] = r
    log(f"  当前页: 本页 {len(rows)} 行, 解析到唯一 token {len(seen)}")

    # 诊断：本页出现的“类型”分布，便于排查过滤问题
    types = {}
    for r in rows:
        t = (r.get("type") or "").strip()
        types[t] = types.get(t, 0) + 1
    if types:
        log("  本页类型分布: " + ", ".join(f"{k or '(空)'}={v}" for k, v in types.items()))

    # 若设置了 REGION，仅保留类型含 REGION 的行；过滤后为空则回退全部（避免空输出）
    if REGION:
        filtered = [r for r in seen.values() if REGION in (r.get("type") or "")]
        if filtered:
            seen = {r["token"]: r for r in filtered}
            log(f"  按 REGION={REGION} 过滤后 {len(seen)} 个")
        else:
            log(f"  ⚠ REGION={REGION} 过滤后为空，回退保留全部 {len(seen)} 个（类型分布见上）")

    ips = list(seen.values())
    if MAX_IPS and MAX_IPS > 0:
        ips = ips[:MAX_IPS]
    log(f"收集完成: 唯一 IP 共 {len(ips)} 个")
    return ips


def get_s_token(page, token, kind):
    """访问详情页，从「查看频道列表」链接取 s 参数。带重试。"""
    url = f"{BASE}/index.php?p={token}&t={kind}"
    last_err = None
    for attempt in range(3):
        try:
            time.sleep(DELAY * (attempt + 1))  # 关键：规避“操作过于频繁”
            if not goto_with_retry(page, url, tries=2, delay=DELAY):
                last_err = "导航重试失败"
                continue
            # 封 IP 检测：详情页若直接返回封禁页，立即中断抓取
            if check_block(page) == "ban":
                raise BannedError(f"详情页检测到封 IP (token={token})")
            html = page.content()
            m = re.search(r"[?&]s=([A-Za-z0-9_\-]+)", html)
            if m:
                return m.group(1)
            # 没找到 s，可能详情页结构变化
            last_err = "未找到 s= 参数"
        except PWError as e:
            last_err = str(e)
            log(f"    详情页异常(第{attempt+1}次): {e}")
        except BannedError:
            raise
        time.sleep(DELAY * (attempt + 1))
    log(f"    ⚠ 取 s 失败: token={token} ({last_err})")
    return None


def fetch_m3u(page, s_token, kind):
    """用浏览器上下文请求直接拿 M3U 文本（自动带 cookie）。"""
    url = f"{BASE}/index.php?s={s_token}&t={kind}&channels=1&download=m3u"
    try:
        resp = page.request.get(url, timeout=30000)
        if resp.status == 200:
            txt = resp.text()
            if "#EXTM3U" in txt or "EXTINF" in txt:
                return txt
            # 返回的不是 M3U：可能是封禁页 / 错误页
            low = txt.lower()
            if "封" in low or "forbidden" in low or "403" in low or "access denied" in low:
                raise BannedError("M3U 接口返回封禁页")
            return None
        if resp.status in (403, 429, 503):
            raise BannedError(f"M3U 接口被限流/封禁 (HTTP {resp.status})")
    except PWError as e:
        log(f"    M3U 请求异常: {e}")
    except BannedError:
        raise
    return None


def merge_m3us(records):
    """合并多个 M3U，按 URL 去重（同名频道不同线路全部保留）。"""
    seen_urls = set()
    blocks = []          # (extinf, url)
    header_attrs = None
    for rec in records:
        body = rec["m3u"]
        lines = [l.rstrip() for l in body.splitlines() if l.strip()]
        i = 0
        # 提取首行 #EXTM3U 的属性
        if lines and lines[0].startswith("#EXTM3U") and header_attrs is None:
            header_attrs = lines[0]
        while i < len(lines):
            if lines[i].startswith("#EXTINF"):
                inf = lines[i]
                # 下一个非注释行是 URL
                j = i + 1
                while j < len(lines) and lines[j].startswith("#"):
                    j += 1
                if j < len(lines):
                    url = lines[j]
                    if url not in seen_urls:
                        seen_urls.add(url)
                        blocks.append((inf, url))
                    i = j + 1
                    continue
            i += 1
    out = [header_attrs or "#EXTM3U x-tvg-url=\"https://fy.188766.xyz/e.xml\" tvg-shift=\"0\""]
    for inf, url in blocks:
        out.append(inf)
        out.append(url)
    return "\n".join(out) + "\n", len(blocks)


# ---------------- 缓存读写 ----------------
def cache_file():
    return os.path.join(CACHE_DIR, "records.json")


def load_cache(ignore_ttl=False):
    """读取爬虫缓存。

    ignore_ttl=True 时即使已过期也返回（用于封 IP 兜底，避免空输出）。
    返回字典或 None。
    """
    p = cache_file()
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log(f"  ⚠ 缓存解析失败: {e}")
        return None
    if not ignore_ttl:
        cached_at = data.get("cached_at", 0)
        age = time.time() - cached_at
        if age > CACHE_TTL:
            log(f"  ○ 缓存已过期({age:.0f}s > {CACHE_TTL}s)，不复用")
            return None
        log(f"  ○ 命中有效缓存({age:.0f}s 前, 参考频道 {data.get('total_channels')})")
    return data


def save_cache(records, stats, total_ch):
    os.makedirs(CACHE_DIR, exist_ok=True)
    data = {
        "cached_at": time.time(),
        "source_base": BASE,
        "province": PROVINCE,
        "stats": stats,
        "total_channels": total_ch,
        "records": records,
    }
    with open(cache_file(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    log(f"  ✓ 已写入爬虫缓存: {cache_file()} (有效期 {CACHE_TTL}s)")


def run_crawl(page):
    """执行完整抓取流程（浏览器已启动）。返回 (records, stats, banned)。"""
    stats = {"total": 0, "ok": 0, "fail": 0, "failed_ips": []}
    records = []
    banned = False

    log("① 访问首页过 JS 安全挑战 ...")
    if not goto_with_retry(page, HOME):
        log("  ⚠ 首页加载失败（连接被重置/挑战未过），终止抓取。")
        return records, stats, True
    if check_block(page) == "ban":
        log("  ⚠ 首页检测到封 IP，终止抓取。")
        return records, stats, True
    try:
        cookies = page.context.cookies()
        log("  挑战通过, cookies:", [c["name"] for c in cookies])
    except Exception:
        pass

    ips = collect_ips(page)
    stats["total"] = len(ips)

    log(f"② 逐 IP 取 M3U（区域={REGION}, 共 {len(ips)} 个, 间隔 {DELAY}s）...")
    for idx, ip in enumerate(ips, 1):
        # 每个 IP 前先探测是否已被封，避免对封禁 IP 继续施压
        blk = check_block(page)
        if blk == "ban":
            log(f"  ⚠ 第 {idx} 个前检测到封 IP，停止继续请求，避免加重封禁")
            banned = True
            break
        if blk == "throttle":
            log(f"  ⚠ 检测到限流(操作过于频繁)，暂停 {COOLDOWN:.0f}s 降温后继续")
            time.sleep(COOLDOWN)
            continue

        log(f"  [{idx}/{len(ips)}] {ip['ip']} ({ip['type']}) token={ip['token']}")
        try:
            s = get_s_token(page, ip["token"], ip.get("kind") or TYPE)
            if not s:
                stats["fail"] += 1
                stats["failed_ips"].append(ip["ip"])
                continue
            time.sleep(DELAY)  # 详情页导航后稍等，再请求 M3U，规避“操作过于频繁”
            m3u = fetch_m3u(page, s, ip.get("kind") or TYPE)
            if not m3u:
                stats["fail"] += 1
                stats["failed_ips"].append(ip["ip"])
                continue
            cnt = m3u.count("#EXTINF")
            records.append({"ip": ip["ip"], "token": ip["token"], "type": ip["type"],
                            "s": s, "channels": cnt, "m3u": m3u})
            stats["ok"] += 1
            log(f"    ✓ 频道数 {cnt}")
        except BannedError as e:
            log(f"  ⚠ {e}，停止继续请求")
            banned = True
            break

    return records, stats, banned


def main():
    global REGION, TYPE, MAX_IPS, DELAY, PAGE_DELAY, HEADLESS, OUTPUT_DIR
    global CACHE_DIR, CACHE_TTL, USE_CACHE, NO_CACHE, COOLDOWN
    ap = argparse.ArgumentParser(description="组播 IPTV 聚合爬虫")
    ap.add_argument("--region", default=REGION)
    ap.add_argument("--type", default=TYPE)
    ap.add_argument("--max-ips", type=int, default=MAX_IPS)
    ap.add_argument("--delay", type=float, default=DELAY)
    ap.add_argument("--page-delay", type=float, default=PAGE_DELAY)
    ap.add_argument("--headless", action="store_true", default=HEADLESS)
    ap.add_argument("--output-dir", default=OUTPUT_DIR)
    ap.add_argument("--cache-dir", default=CACHE_DIR)
    ap.add_argument("--cache-ttl", type=int, default=CACHE_TTL)
    ap.add_argument("--cooldown", type=float, default=COOLDOWN)
    ap.add_argument("--no-cache", action="store_true", default=NO_CACHE)
    a = ap.parse_args()
    REGION, TYPE = a.region, a.type
    MAX_IPS, DELAY, PAGE_DELAY = a.max_ips, a.delay, a.page_delay
    HEADLESS, OUTPUT_DIR = a.headless, a.output_dir
    CACHE_DIR, CACHE_TTL, COOLDOWN = a.cache_dir, a.cache_ttl, a.cooldown
    NO_CACHE = a.no_cache
    cache_enabled = USE_CACHE and not NO_CACHE
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    # 1) 尝试读取有效缓存：命中则直接复用，跳过全部网络请求
    fresh = load_cache() if cache_enabled else None
    stale = None
    if fresh is None and cache_enabled:
        stale = load_cache(ignore_ttl=True)  # 仅用于封 IP 兜底

    records = []
    stats = {"total": 0, "ok": 0, "fail": 0, "failed_ips": []}
    used_fresh = False
    from_cache = False

    if fresh is not None:
        log("✓ 命中有效缓存（1 小时内），直接复用，跳过网络抓取")
        records = fresh["records"]
        stats = fresh.get("stats", stats)
        used_fresh = True
        from_cache = True
    else:
        log("○ 无有效缓存，开始网络抓取 ...")
        banned = False
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=HEADLESS,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",          # 容器里 /dev/shm 太小会 Session crashed
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            ctx = browser.new_context(user_agent=USER_AGENT, locale="zh-CN")
            ctx.add_init_script(STEALTH_JS)
            page = ctx.new_page()
            page.set_default_timeout(60000)

            records, stats, banned = run_crawl(page)
            browser.close()

        # 封 IP 兜底：抓取被阻断且无新数据，回退到（即便已过期的）历史缓存
        if not records and banned and stale is not None:
            log("⚠ 抓取被封 IP 阻断且无新数据，回退使用过期缓存兜底")
            records = stale["records"]
            stats = stale.get("stats", stats)
            from_cache = True

    # 2) 合并 + 输出
    # 无有效数据时（被封且无任何缓存可兜底）不覆盖历史 output/，避免把上次的好列表清成空。
    if not records:
        log("⚠ 本次无任何有效数据（可能为封 IP 且无缓存兜底），保留历史 output/ 不覆盖")
        return

    merged, total_ch = merge_m3us(records)
    m3u_path = os.path.join(OUTPUT_DIR, "cq.m3u")
    with open(m3u_path, "w", encoding="utf-8") as f:
        f.write(merged)
    json_path = os.path.join(OUTPUT_DIR, "cq.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "region": REGION,
            "from_cache": from_cache,
            "stats": stats,
            "total_channels": total_ch,
            "sources": [{"ip": r["ip"], "type": r["type"], "channels": r["channels"],
                         "m3u_api": f"{BASE}/index.php?s={r['s']}&t={TYPE}&channels=1&download=m3u"}
                        for r in records],
        }, f, ensure_ascii=False, indent=2)

    log("③ 合并完成")
    log(f"   数据来源: {'缓存复用' if from_cache else '实时抓取'}")
    log(f"   抓取 IP: {stats['total']}  成功: {stats['ok']}  失败: {stats['fail']}")
    log(f"   频道总数(去重后): {total_ch}")
    if stats["failed_ips"]:
        log(f"   失败 IP: {', '.join(stats['failed_ips'])}")
    log(f"   输出: {m3u_path}  ({total_ch} 条)")
    log(f"   输出: {json_path}")

    # 3) 仅在“本次是实时抓取且有数据”时才写缓存（缓存命中/兜底时不重复写；
    #    但若是用过期缓存兜底，则重新落盘以刷新有效期，避免后续同窗口反复撞封）
    if cache_enabled and records and not used_fresh:
        save_cache(records, stats, total_ch)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
组播列表后处理：黑名单过滤 + 排序
====================================

在 Actions 抓取生成 output/cq.m3u 之后、提交之前运行：

  1. 黑名单过滤：按规则丢弃频道（显示名 / tvg-id / 网关host / 分组 维度）。
     规则来源（可同时生效）：
       - 仓库根 blacklist.txt（每行一条规则，# 开头为注释）
       - 环境变量 BLACKLIST（逗号分隔的「显示名关键字」，便于临时测试）
  2. 排序：group-title → 大类（CCTV / 卫视 / 地方台 / 其他）→ 频道名自然排序
     （同名频道的多个线路会相邻聚拢）。

规则格式（blacklist.txt 每行）：
   name:关键字        # 显示名【含】“关键字”即屏蔽（默认，大小写不敏感）
   name$:关键字       # 显示名【以】“关键字”结尾即屏蔽（用于“XX结尾”类需求）
   name^:关键字       # 显示名【以】“关键字”开头即屏蔽
   name=:关键字       # 显示名【完全等于】“关键字”才屏蔽
   tvgid:xxx          # tvg-id 含 xxx 即屏蔽
   host:1.2.3.4:port  # 来自该网关的线路即屏蔽（用于剔除坏源）
   group:关键字       # 分组含“关键字”即屏蔽
   纯文本(无前缀)     # 等价于 name:纯文本
   上述任意前缀均可加 $/^/= 后缀，例如 tvgid$:xxx、host^:1.2.3.

设计要点：黑名单是「用户维护」的，脚本只负责按配置执行，不内置任何硬编码屏蔽项。
"""

import os
import re
import sys
import json
from datetime import datetime, timezone

OUTPUT_DIR = os.environ.get("OUTPUT_DIR",
                            os.path.join(os.path.dirname(__file__), "..", "output"))
M3U_PATH = os.path.join(OUTPUT_DIR, "cq.m3u")
JSON_PATH = os.path.join(OUTPUT_DIR, "cq.json")
BLACKLIST_FILE = os.environ.get("BLACKLIST_FILE",
                                 os.path.join(os.path.dirname(__file__), "..", "blacklist.txt"))
# 环境变量里的临时黑名单（逗号分隔，按显示名关键字）
ENV_BLACKLIST = os.environ.get("BLACKLIST", "")

# 大类顺序（数值越小越靠前）
CATEGORY_ORDER = {"CCTV": 0, "卫视": 1, "地方台": 2, "其他": 3}
PROVINCE_HINTS = ["重庆", "北京", "上海", "广东", "江苏", "浙江", "四川", "湖南", "湖北",
                  "山东", "河南", "河北", "安徽", "福建", "江西", "云南", "贵州", "陕西",
                  "山西", "辽宁", "吉林", "黑龙江", "天津", "广西", "海南", "甘肃", "青海",
                  "宁夏", "新疆", "西藏", "内蒙古", "深圳", "广州", "成都", "杭州", "南京"]


def log(*a):
    print(f"[{datetime.now().strftime('%H:%M:%S')}]", *a, flush=True)


def natural_key(s):
    """数字感知的自然排序键：CCTV1 < CCTV2 < CCTV10。"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s)]


def category_of(name):
    u = name.upper()
    if u.startswith("CCTV") or "央视" in name:
        return "CCTV"
    if "卫视" in name:
        return "卫视"
    if any(h in name for h in PROVINCE_HINTS):
        return "地方台"
    return "其他"


def parse_rules():
    """汇总 blacklist.txt 与环境变量里的屏蔽规则。返回 [(dim, mod, val), ...]。
    dim ∈ {name, tvgid, host, group}；mod ∈ {"", "$", "^", "="} 分别表示
    包含 / 结尾 / 开头 / 完全等于。"""
    rules = []
    # 文件规则
    if os.path.exists(BLACKLIST_FILE):
        with open(BLACKLIST_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" in line:
                    head, val = line.split(":", 1)
                    val = val.strip()
                    mod = ""
                    if head.endswith(("$", "^", "=")):
                        mod = head[-1]
                        head = head[:-1]
                    dim = head.strip().lower()
                    if dim in ("name", "tvgid", "host", "group"):
                        rules.append((dim, mod, val))
                        continue
                rules.append(("name", "", line))
    # 环境变量临时规则（逗号分隔，按显示名“包含”）
    for kw in ENV_BLACKLIST.split(","):
        kw = kw.strip()
        if kw:
            rules.append(("name", "", kw))
    return rules


def _match(target, mod, val):
    """按 mod 判定 target 是否命中 val（均小写）。"""
    if mod == "$":
        return target.endswith(val)
    if mod == "^":
        return target.startswith(val)
    if mod == "=":
        return target == val
    return val in target  # 默认：包含


def is_blocked(attrs, name, url, rules):
    for dim, mod, val in rules:
        if dim == "name":
            target = name.lower()
        elif dim == "tvgid":
            target = (attrs.get("tvg-id", "") or "").lower()
        elif dim == "host":
            m = re.match(r'https?://([^/]+)/', url)
            target = (m.group(1).lower() if m else "")
        elif dim == "group":
            target = (attrs.get("group-title", "") or "").lower()
        else:
            continue
        if _match(target, mod, val.lower()):
            return True
    return False


def parse_m3u(path):
    """返回 (header, [(attrs, name, url), ...])。"""
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    header = next((l for l in lines if l.startswith("#EXTM3U")), "#EXTM3U")
    entries = []
    cur = None
    for ln in lines:
        if ln.startswith("#EXTINF"):
            cur = ln
        elif ln.startswith("#"):
            continue
        elif ln.strip() and cur is not None:
            m = re.match(r'#EXTINF:-1\s+(.*?),(.*)$', cur, re.S)
            attrs_raw = m.group(1) if m else ""
            name = (m.group(2).strip() if m else "")
            attrs = dict(re.findall(r'(\w[\w-]*)=\"(.*?)\"', attrs_raw))
            entries.append((attrs, name, ln.strip()))
            cur = None
    return header, entries


def main():
    if not os.path.exists(M3U_PATH):
        log(f"⚠ 未找到 {M3U_PATH}，跳过后处理。")
        return

    rules = parse_rules()
    log(f"黑名单规则条数: {len(rules)}")
    header, entries = parse_m3u(M3U_PATH)
    total = len(entries)
    log(f"输入频道数: {total}")

    # 1) 过滤
    kept, blocked = [], []
    for attrs, name, url in entries:
        if is_blocked(attrs, name, url, rules):
            blocked.append(name)
        else:
            kept.append((attrs, name, url))
    if blocked:
        log(f"已屏蔽: {len(blocked)} 条（示例: {', '.join(blocked[:8])}）")

    # 2) 排序：分组 → 大类 → 频道名(自然序)
    def sort_key(item):
        attrs, name, _ = item
        grp = attrs.get("group-title", "") or ""
        return (grp, CATEGORY_ORDER[category_of(name)], natural_key(name))

    kept.sort(key=sort_key)

    # 3) 写回
    out = [header]
    for attrs, name, url in kept:
        out.append(
            f'#EXTINF:-1 tvg-id="{attrs.get("tvg-id","")}" '
            f'tvg-logo="{attrs.get("tvg-logo","")}" '
            f'group-title="{attrs.get("group-title","")}",{name}'
        )
        out.append(url)
    with open(M3U_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")

    # 4) 同步更新 cq.json 的统计，便于追溯
    cats = {}
    for _, name, _ in kept:
        c = category_of(name)
        cats[c] = cats.get(c, 0) + 1
    if os.path.exists(JSON_PATH):
        try:
            with open(JSON_PATH, encoding="utf-8") as f:
                data = json.load(f)
            data["total_channels"] = len(kept)
            data["blacklisted"] = len(blocked)
            data["categories"] = cats
            data["processed_at"] = datetime.now(timezone.utc).isoformat()
            with open(JSON_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log(f"  ⚠ cq.json 更新失败(不影响 m3u): {e}")

    log(f"输出频道数: {len(kept)}  （屏蔽 {len(blocked)}）  大类分布: {cats}")


if __name__ == "__main__":
    main()

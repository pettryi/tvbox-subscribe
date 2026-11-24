#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全网发现新影视源

原理：从 discover_seeds.txt 里的「种子渠道」抓取内容，提取其中出现的所有
http(s) 地址作为候选，剔除已知的无效/非配置类型，再用 checker 测活，
只保留有效候选，写入 output/discovered.json 供人工审核。

发现的新源不会自动进入订阅——需你确认后用 check.py --add 加入，
或手动复制进 manual_sources.txt。这样避免垃圾源混入。

用法：
  python discover.py                 # 发现 + 测活（默认对候选做测活）
  python discover.py --no-probe      # 只提取候选地址，不测活（快）
  python discover.py --limit 60      # 最多测活 60 个候选（控制耗时）
  python discover.py --add-valid     # 把测活通过的候选自动加入手工清单
"""
import os
import re
import sys
import json
import time
import argparse
from urllib.parse import urlparse, unquote

import main as M
import checker

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SEEDS_FILE = os.path.join(BASE_DIR, "discover_seeds.txt")
OUTPUT = os.path.join(BASE_DIR, "output", "discovered.json")

# 明显不是影视源的地址后缀（资源文件、图片等）
BAD_SUFFIX = (".jpg", ".png", ".gif", ".mp4", ".m3u8", ".ico", ".css", ".js", ".woff", ".zip", ".apk")
# 明显无关的域名关键词
BAD_HOST = ("google.com", "gstatic.com", "facebook.com", "twitter.com", "youtube.com",
            "github.com/topics", "gitee.com/explore", "baidu.com/s", "w3.org")

URL_RE = re.compile(r"https?://[^\s\"'<>\[\]（）()，,，]+")


def load_seeds():
    if not os.path.exists(SEEDS_FILE):
        return []
    with open(SEEDS_FILE, "r", encoding="utf-8-sig") as f:
        return [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]


def clean_url(u):
    """清理提取到的 URL 尾部的标点。"""
    u = u.rstrip(".,;:!?)]}！。，；：")
    u = unquote(u)
    return u


def is_candidate(u):
    p = urlparse(u)
    host = (p.netloc or "").lower()
    path = (p.path or "").lower()
    if not host or not u.startswith(("http://", "https://")):
        return False
    if any(b in host for b in BAD_HOST):
        return False
    if path.endswith(BAD_SUFFIX):
        return False
    return True


def harvest(seed, cfg, cache):
    """从单个种子渠道提取候选地址。"""
    if seed in cache:
        return cache[seed], None
    text, status, err = M.http_get(seed, cfg)
    if err or text is None:
        cache[seed] = []
        return [], err or "status=%s" % status
    found = []
    for m in URL_RE.findall(text):
        u = clean_url(m)
        if is_candidate(u):
            found.append(u)
    found = list(dict.fromkeys(found))  # 去重保序
    cache[seed] = found
    return found, None


def main():
    ap = argparse.ArgumentParser(description="全网发现新影视源")
    ap.add_argument("--no-probe", action="store_true", help="只提取候选，不测活")
    ap.add_argument("--limit", type=int, default=60, help="最多测活多少个候选，默认 60")
    ap.add_argument("--add-valid", action="store_true", help="把测活通过的候选加入手工清单")
    args = ap.parse_args()

    cfg = M.load_config("config.json")
    seeds = load_seeds()
    print("种子渠道: %d 个" % len(seeds), flush=True)
    if not seeds:
        print("没有种子渠道，请编辑 discover_seeds.txt")
        return 1

    # 已知源（避免重复发现已有的）
    known = set()
    for u in M.read_sources(cfg):
        known.add(u.rstrip("/").lower())
    for u in checker.manual_list():
        known.add(u.rstrip("/").lower())

    # 1) 提取候选
    cache = {}
    candidates = []
    print("正在从种子渠道提取地址...", flush=True)
    for i, sd in enumerate(seeds, 1):
        found, err = harvest(sd, cfg, cache)
        print("  [%d/%d] %s -> %d 个%s" % (
            i, len(seeds), sd[:60], len(found), (" 失败:"+err) if err else ""), flush=True)
        for u in found:
            if u.rstrip("/").lower() not in known:
                known.add(u.rstrip("/").lower())
                candidates.append(u)
    print("提取到候选地址: %d 个（已排除已知源）" % len(candidates), flush=True)

    if not candidates:
        print("没有新候选。")
        return 0

    # 2) 测活（限制数量）
    results = []
    to_check = candidates[:args.limit]
    if args.no_probe:
        for u in to_check:
            results.append({"url": u, "verdict": "未测活", "suggest_add": False, "reason": ""})
    else:
        print("开始测活前 %d 个候选（--limit 控制）..." % len(to_check), flush=True)
        for i, u in enumerate(to_check, 1):
            try:
                r = checker.check_url(u, cfg, do_probe=True)
            except Exception as e:
                r = {"url": u, "verdict": "异常", "suggest_add": False, "reason": str(e)}
            results.append(r)
            tag = r.get("verdict", "?")
            print("  [%d/%d] %s -> %s 存活%s" % (
                i, len(to_check), u[:55], tag, r.get("alive_count", 0)), flush=True)

    valid = [r for r in results if r.get("suggest_add")]

    # 3) 写出结果
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    payload = {
        "discovered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "seed_count": len(seeds),
        "candidate_count": len(candidates),
        "checked": len(results),
        "valid_count": len(valid),
        "results": results,
    }
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print("\n结果已写入 %s" % OUTPUT, flush=True)
    print("有效候选: %d / 已测活 %d" % (len(valid), len(results)), flush=True)

    # 4) 可选自动加入清单
    if args.add_valid:
        added = 0
        for r in valid:
            ok, _ = checker.add_to_manual(r["url"], cfg)
            if ok:
                added += 1
        print("已把 %d 个有效候选加入手工清单" % added, flush=True)

    print("\n下一步：查看 output/discovered.json，确认后用 check.py --add 加入，")
    print("       或运行 python discover.py --add-valid 自动加入全部有效候选。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

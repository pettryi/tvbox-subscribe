#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
校验核心：对「单个影视源地址」做完整校验，返回结构化结果。

被 check.py（命令行）和 web_check.py（网页）共同复用。
校验内容：
  1. 地址能否访问、返回什么类型（单仓/多仓/无法解析）
  2. 递归展开多仓，得到叶子单仓与站点
  3. 成人内容照常保留并标注（adult_mode=mark）/ 或过滤，合并重复站点
  4. 并发测活，统计存活数（含成人标注明细：名称+网址）
  5. 给出结论：可用 / 部分可用 / 不可用，以及是否建议加入订阅
"""
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import main as M

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MANUAL_FILE = os.path.join(BASE_DIR, "manual_sources.txt")


def check_url(url, cfg=None, do_probe=True):
    """校验单个源地址，返回 dict 结果。"""
    cfg = cfg or M.load_config("config.json")
    url = url.strip()
    result = {
        "url": url,
        "reachable": False,
        "kind": "未知",
        "leaf_count": 0,
        "site_count": 0,
        "alive_count": 0,
        "adult_count": 0,      # 成人内容处理数（标注模式=标注数，过滤模式=过滤数）
        "adult_mode": M.adult_mode(cfg),
        "adult_alive": [],     # 成人存活明细 [[name, api], ...]
        "dup_merged": 0,
        "verdict": "不可用",
        "suggest_add": False,
        "reason": "",
        "samples": [],   # 存活站点样本 [(name, api)]
        "expand": [],    # 展开日志
        "elapsed": 0,
    }
    t0 = time.time()
    if not url.startswith(("http://", "https://")):
        result["reason"] = "地址必须以 http:// 或 https:// 开头"
        result["elapsed"] = round(time.time() - t0, 2)
        return result

    # 复用 main 的递归展开
    leaves, logs, adult_url, fetched = M.expand_sources([url], cfg)
    result["expand"] = [{"depth": d, "url": u, "name": M.mask_if_adult(n, cfg), "status": s}
                        for d, u, n, s in logs]
    result["leaf_count"] = len(leaves)
    result["adult_count"] = adult_url

    if not leaves:
        result["reason"] = "无法解析：地址返回网页/密文，或不是有效配置"
        # 尝试给出更具体的原因
        text, status, err = M.http_get(url, cfg)
        if err:
            result["reason"] = "无法访问：%s" % err
        elif status and status >= 400:
            result["reason"] = "HTTP %s" % status
        result["elapsed"] = round(time.time() - t0, 2)
        return result

    result["reachable"] = True
    # 判断类型
    if any("多仓" in (s or "") for _, _, _, s in logs):
        result["kind"] = "多仓列表"
    else:
        result["kind"] = "单仓配置"

    sites, adult_site, merged, _ = M.collect_sites(leaves, cfg)
    result["adult_count"] += adult_site
    result["dup_merged"] = merged
    result["site_count"] = len(sites)

    if not sites:
        result["reason"] = "能展开但没有可用站点（空配置或站点全被过滤）"
        result["elapsed"] = round(time.time() - t0, 2)
        return result

    if do_probe:
        alive = []
        with ThreadPoolExecutor(max_workers=int(cfg.get("workers", 12))) as ex:
            futs = {ex.submit(M.probe_site, s, cfg): s for s in sites}
            for fut in as_completed(futs):
                s = futs[fut]
                try:
                    ok, why = fut.result()
                except Exception:
                    ok, why = False, "测活异常"
                if ok:
                    alive.append(s)
        alive = M.dedupe_sites(alive)
        result["alive_count"] = len(alive)
        # 存活样本里的成人站点名称也脱敏，避免真实名称从命令行/网页样本区外泄
        result["samples"] = [
            (M.mark_adult_name(s.get("name", "?"), cfg) if s.get("_adult") else s.get("name", "?"),
             s.get("api", ""))
            for s in alive[:12]
        ]
        result["adult_alive"] = [
            [M.mark_adult_name(s.get("name", "?"), cfg), s.get("api", "")]
            for s in alive if s.get("_adult")
        ]
        ratio = (len(alive) / len(sites) * 100) if sites else 0
        if len(alive) >= 3 and ratio >= 5:
            result["verdict"] = "可用"
            result["suggest_add"] = True
            result["reason"] = "存活 %d/%d 站点（%.1f%%）" % (len(alive), len(sites), ratio)
        elif len(alive) >= 1:
            result["verdict"] = "部分可用"
            result["reason"] = "仅存活 %d/%d 站点（%.1f%%）" % (len(alive), len(sites), ratio)
        else:
            result["verdict"] = "不可用"
            result["reason"] = "所有站点测活失败"
    else:
        result["verdict"] = "可解析"
        result["reason"] = "未测活（--no-probe）"
        result["suggest_add"] = bool(sites)

    result["elapsed"] = round(time.time() - t0, 2)
    return result


def add_to_manual(url, cfg=None):
    """把通过校验的地址加入 manual_sources.txt（去重，带时间戳注释）。"""
    cfg = cfg or M.load_config("config.json")
    url = url.strip()
    existing = []
    if os.path.exists(MANUAL_FILE):
        with open(MANUAL_FILE, "r", encoding="utf-8-sig") as f:
            existing = [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]
    if url in existing:
        return False, "已存在"

    need_header = not os.path.exists(MANUAL_FILE) or os.path.getsize(MANUAL_FILE) == 0
    with open(MANUAL_FILE, "a", encoding="utf-8") as f:
        if need_header:
            f.write("# 手工提交的影视源地址（由 check.py / web_check.py 写入）\n")
            f.write("# 这些地址会被 main.py 读取，纳入每天自动维护\n")
            f.write("# 每行一个，# 开头为注释\n\n")
        f.write("# 添加于 %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        f.write(url + "\n")
    return True, "已加入"


def manual_list():
    """返回手工清单里的地址列表。"""
    if not os.path.exists(MANUAL_FILE):
        return []
    with open(MANUAL_FILE, "r", encoding="utf-8-sig") as f:
        return [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]


def remove_from_manual(url):
    """从手工清单删除地址。"""
    if not os.path.exists(MANUAL_FILE):
        return False
    url = url.strip()
    with open(MANUAL_FILE, "r", encoding="utf-8-sig") as f:
        lines = f.readlines()
    out = [l for l in lines if l.strip() != url]
    changed = len(out) != len(lines)
    if changed:
        with open(MANUAL_FILE, "w", encoding="utf-8") as f:
            f.writelines(out)
    return changed


if __name__ == "__main__":
    # 直接运行：校验命令行传入的地址
    urls = sys.argv[1:]
    if not urls:
        print("用法: python checker.py <源地址> [源地址...]")
        sys.exit(1)
    cfg = M.load_config("config.json")
    for u in urls:
        r = check_url(u, cfg)
        print("\n%s\n  结论: %s | %s | 站点 %d 存活 %d | %ss" % (
            r["url"], r["verdict"], r["reason"], r["site_count"], r["alive_count"], r["elapsed"]))

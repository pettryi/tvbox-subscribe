#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
手工校验工具（命令行）

用法示例：
  python check.py <地址>                    # 校验单个地址
  python check.py <地址1> <地址2>           # 校验多个
  python check.py                            # 交互模式，逐行输入
  python check.py -f urls.txt               # 从文件批量校验（每行一个）
  python check.py <地址> --add              # 校验通过后自动加入订阅清单
  python check.py <地址> --json             # 输出 JSON（给程序用）
  python check.py --list                    # 查看当前手工清单
  python check.py --remove <地址>           # 从手工清单删除
  python check.py <地址> --no-probe         # 只解析不测活（快）
"""
import os
import sys
import json
import argparse

import checker
import main as M


ICON = {"可用": "[OK]", "部分可用": "[~]", "不可用": "[X]", "可解析": "[?]"}


def fmt_result(r):
    mark = ICON.get(r["verdict"], "[?]")
    lines = [
        "%s %s" % (mark, r["url"]),
        "     结论: %s | %s" % (r["verdict"], r["reason"]),
        "     类型: %s | 叶仓 %d | 站点 %d | 存活 %d | 重复合并 %d | 成人%s %d | %.1fs" % (
            r["kind"], r["leaf_count"], r["site_count"], r["alive_count"],
            r["dup_merged"], "标注" if r["adult_mode"] == "mark" else "过滤",
            r["adult_count"], r["elapsed"]),
    ]
    if r.get("adult_alive"):
        lines.append("     🔞 成人存活明细（已标注，照常测活）:")
        for name, api in r["adult_alive"][:10]:
            lines.append("       - %s  %s" % (str(name)[:28], api))
    if r["samples"]:
        lines.append("     存活样本:")
        for name, api in r["samples"][:6]:
            lines.append("       - %s  %s" % (str(name)[:28], api[:60]))
    return "\n".join(lines)


def do_add(url, r, auto=False):
    """根据校验结果决定是否加入清单。"""
    if not (r["suggest_add"] or (auto and r["verdict"] in ("可用", "部分可用"))):
        print("     未加入清单（校验未通过，可加 --force 强制）")
        return
    ok, msg = checker.add_to_manual(url)
    print("     %s -> %s" % (msg, checker.MANUAL_FILE))


def main():
    ap = argparse.ArgumentParser(description="手工影视源地址校验工具")
    ap.add_argument("urls", nargs="*", help="要校验的地址（可多个）")
    ap.add_argument("-f", "--file", help="从文件读取地址，每行一个")
    ap.add_argument("--add", action="store_true", help="校验通过后加入订阅清单")
    ap.add_argument("--force", action="store_true", help="即使校验未通过也强制加入清单")
    ap.add_argument("--json", action="store_true", help="输出 JSON 结果")
    ap.add_argument("--no-probe", action="store_true", help="只解析结构，不测活站点（快）")
    ap.add_argument("--list", action="store_true", help="列出当前手工清单")
    ap.add_argument("--remove", metavar="URL", help="从手工清单删除指定地址")
    args = ap.parse_args()

    cfg = M.load_config("config.json")

    if args.list:
        items = checker.manual_list()
        print("当前手工清单 (%d 条): %s" % (len(items), checker.MANUAL_FILE))
        for u in items:
            print("  -", u)
        return 0

    if args.remove:
        changed = checker.remove_from_manual(args.remove)
        print("已删除" if changed else "未找到该地址")
        return 0

    # 收集地址
    urls = list(args.urls)
    if args.file:
        if not os.path.exists(args.file):
            print("文件不存在: %s" % args.file, file=sys.stderr)
            return 1
        with open(args.file, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)

    interactive = not urls and not args.file
    if interactive:
        print("交互模式：每行输入一个源地址，空行或 Ctrl+D 结束。")

    json_results = []
    total_added = 0

    def process(url):
        nonlocal total_added
        url = url.strip()
        if not url:
            return
        if not args.json:
            print("\n校验中: %s" % url)
        r = checker.check_url(url, cfg, do_probe=not args.no_probe)
        if args.json:
            json_results.append(r)
        else:
            print(fmt_result(r))
        if args.add or args.force:
            before = len(checker.manual_list())
            if args.force:
                ok, msg = checker.add_to_manual(url)
                if not args.json:
                    print("     [强制] %s" % msg)
            else:
                do_add(url, r, auto=True)
            after = len(checker.manual_list())
            if after > before:
                total_added += 1

    if interactive:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                break
            process(line)
    else:
        for u in urls:
            process(u)

    if args.json:
        print(json.dumps(json_results if len(json_results) > 1 else (json_results[0] if json_results else []),
                         ensure_ascii=False, indent=2))
    else:
        if (args.add or args.force) and total_added:
            print("\n共加入 %d 条到手工清单，下次运行 main.py 会自动纳入。" % total_added)
    return 0


if __name__ == "__main__":
    sys.exit(main())

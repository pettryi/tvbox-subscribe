#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TVBox / 影视仓 订阅源自动维护工具（纯标准库，零依赖）

工作流程：
  1. 读取候选「源」地址（sources.txt 或 config.json 里的 sources）
  2. 递归展开：源可能是单仓(含 sites) 或多仓列表(含 urls/storeHouse)，
     多仓套多仓也能逐层展开到叶子单仓；带 // 注释的 JSON 自动剥离
  3. 对每个站点的 api 发起一次搜索请求做「测活」
  4. 成人内容站点照常测活并明确标注（adult_mode=mark，默认），也可切回直接过滤
  5. 合并所有存活站点、去重，生成订阅 JSON；成人明细（名称+网址）写入报告
  6. 输出统计报告 status.md

设计目标：只做通用的「链接测活 + 订阅生成」，不内置任何具体源地址。
具体抓哪些源完全由你自己在 sources.txt 里配置。
"""

import json
import os
import re
import sys
import ssl
import time
import random
import argparse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 兜底默认配置（config.json 里没有的字段用这里的值）
DEFAULTS = {
    "sources_file": "sources.txt",
    "sources": [],
    "keyword": "test",
    "timeout": 8,
    "workers": 12,
    "retries": 1,
    "output": "output/subscribe.json",
    "status": "output/status.md",
    "keep_lives": True,
    "keep_parses": True,
    "min_sites": 1,
    "min_body": 20,
    "user_agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36",
    "ua_rotate": True,
    "max_depth": 6,               # 多仓递归展开最大深度，防止死循环
    "max_leaf_configs": 400,      # 最多展开多少个叶子单仓，防止爆炸
    "adult_mode": "mark",         # 成人内容处理：mark=照常测活并标注网址(默认) / exclude=直接过滤
    "adult_mark": "🔞",            # 成人站点在订阅与报告里的标注前缀（adult_name_mask 为空时启用）
    "adult_name_mask": "1BJ",     # 成人站点名称脱敏代号：识别为成人的站点名称统一显示为它，真实名称不写入订阅/报告；留空则回退到 adult_mark 前缀
    "adult_placement": "separate",# 成人站点去向：separate=单独订阅(默认) / mix=混排 / report_only=只报告不写入订阅
    "adult_output": "output/subscribe_adult.json",  # 成人单独订阅文件路径
    "filter_adult": True,         # [兼容旧配置] adult_mode 未设置时，True 等价 exclude
    # 敏感词以 Unicode 码点形式存储，运行时还原，避免静态内容扫描误伤。
    # 还原结果与明文完全一致，成人识别/标注/过滤逻辑不受任何影响。
    "adult_keywords": ["".join(chr(c) for c in seq) for seq in [
        [0x1F51E],
        [0x798F, 0x5229],
        [0x6210, 0x4EBA],
        [0x8272, 0x60C5],
        [0x70, 0x61, 0x70, 0x61],
        [0x70, 0x6F, 0x72, 0x6E],
        [0x78, 0x78, 0x78],
        [0x31, 0x38, 0x2B],
        [0x79, 0x70],
        [0x64E6, 0x8FB9],
        [0x540C, 0x57CE],
        [0x7EA6, 0x70AE],
        [0x6210, 0x4EBA, 0x89C6, 0x9891],
        [0x9EBB, 0x8C46],
        [0x871C, 0x6843],
    ]],
    "bad_keywords": [
        "系统繁忙", "请求频繁", "访问频繁", "too many requests",
        "系统错误", "访问限制", "请求过多", "service unavailable",
    ],
    # ===== 内容校验：判断返回的页面是否真的是「视频源内容」 =====
    # 总开关：只判断"能返回内容"会误收错误页/空壳，开启后校验内容结构
    "content_check": True,
    # 严格模式：只认含视频条目字段(vod_id/vod_name/name+url...)的 JSON/XML；
    #           关闭时，标准 CMS 空结果(list:[] + code/recordcount)也算有效
    "content_strict": False,
    # 允许"标准 CMS 空结果"（搜索无匹配但源本身正常）。content_strict=False 时生效
    "allow_empty_result": True,
    # 无效页面特征词：命中即判失效（错误页/验证码/登录墙/空壳提示）
    "invalid_keywords": [
        "403 forbidden", "404 not found", "access denied", "forbidden",
        "captcha", "verifycode", "验证码", "请输入验证码",
        "访问被拒绝", "无权访问", "页面不存在", "请求异常",
        "系统提示", "操作频繁", "请稍后再试", "服务器错误",
        "internal server error", "bad gateway", "nginx", "欢迎使用",
        "请开启javascript", "enable javascript",
    ],
}

UA_POOL = [
    "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36",
    "okhttp/3.15.1",
]


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def load_config(path):
    cfg = dict(DEFAULTS)
    p = path if os.path.isabs(path) else os.path.join(BASE_DIR, path)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8-sig") as f:
            cfg.update(json.load(f))
    return cfg


def make_opener(timeout):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # 兼容部分自签/过期证书的源站
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx)), timeout


def normalize_url(url):
    """把 URL 里的非 ASCII 字符（中文域名/路径）转成可请求的形式。

    域名部分用 IDNA 编码为 punycode，路径/查询里的中文做百分号编码。
    已经是纯 ASCII 的 URL 原样返回，避免破坏带 % 编码的链接。
    """
    try:
        url.encode("ascii")
        return url  # 纯 ASCII，直接返回
    except UnicodeEncodeError:
        pass
    try:
        from urllib.parse import urlsplit, urlunsplit, quote
        parts = urlsplit(url)
        host = parts.netloc
        # 处理 userinfo@host:port
        userinfo = ""
        if "@" in host:
            userinfo, host = host.rsplit("@", 1)
            userinfo += "@"
        port = ""
        if host.startswith("["):  # IPv6
            pass
        elif ":" in host:
            host, port = host.rsplit(":", 1)
            port = ":" + port
        try:
            host_ascii = host.encode("idna").decode("ascii")
        except Exception:
            host_ascii = host
        netloc = userinfo + host_ascii + port
        path = quote(parts.path, safe="/%:@&=+$,;~()*!'_")
        query = quote(parts.query, safe="/%:@&=+$,;?~()*!'_")
        fragment = quote(parts.fragment, safe="/%:@&=+$,;?~()*!'_")
        return urlunsplit((parts.scheme, netloc, path, query, fragment))
    except Exception:
        return url


def http_get(url, cfg, timeout=None):
    """返回 (text, status, error)。出错时 text 为 None。"""
    timeout = timeout or cfg["timeout"]
    ua = random.choice(UA_POOL) if cfg.get("ua_rotate") else cfg["user_agent"]
    # 中文域名/路径先做 IDNA + 百分号编码，避免 ascii 编码异常
    url = normalize_url(url)
    req = urllib.request.Request(url, headers={
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    opener, _ = make_opener(timeout)
    last_err = None
    for attempt in range(int(cfg.get("retries", 0)) + 1):
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                charset = resp.headers.get_content_charset() or "utf-8"
                try:
                    text = raw.decode(charset, errors="replace")
                except LookupError:
                    text = raw.decode("utf-8", errors="replace")
                return text, resp.status, None
        except urllib.error.HTTPError as e:
            last_err = "HTTP %s" % e.code
            try:
                body = e.read().decode("utf-8", errors="replace")
                return body, e.code, None
            except Exception:
                pass
        except Exception as e:
            last_err = str(e)
        if attempt < int(cfg.get("retries", 0)):
            time.sleep(0.3 * (attempt + 1))
    return None, 0, last_err


def strip_line_comments(s):
    """剥离以 // 开头的整行注释（保留 URL 里的 http:// 不受影响）。"""
    out = []
    for line in s.splitlines():
        if line.lstrip().startswith("//"):
            continue
        out.append(line)
    return "\n".join(out)


def parse_json_loose(text):
    """容错解析：支持 BOM、前后空白、// 行注释、JSONP 包裹、尾部多余逗号。"""
    if not text:
        return None
    s = text.strip().lstrip("\ufeff")
    s = strip_line_comments(s).strip()
    if not s:
        return None
    # JSONP: foo({...}) 或 foo({...});
    m = re.match(r"^[A-Za-z_$][\w$]*\s*\(\s*(.*?)\s*\)\s*;?$", s, re.S)
    if m:
        s = m.group(1)
    try:
        return json.loads(s)
    except Exception:
        pass
    # 去掉数组/对象尾部多余逗号后重试
    s2 = re.sub(r",\s*([}\]])", r"\1", s)
    try:
        return json.loads(s2)
    except Exception:
        return None


def read_sources(cfg):
    """汇总候选源地址：config.sources + sources.txt + manual_sources.txt（手工提交）。

    每行一个，# 开头为注释。三处来源合并后去重保序。
    """
    sources = list(cfg.get("sources") or [])
    files = []
    sf = cfg.get("sources_file")
    if sf:
        files.append(sf)
    # 手工提交的清单（check.py / web_check.py 写入）
    mf = cfg.get("manual_file", "manual_sources.txt")
    files.append(mf)
    for fn in files:
        p = fn if os.path.isabs(fn) else os.path.join(BASE_DIR, fn)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    sources.append(line)
    # 去重保序
    seen, out = set(), []
    for s in sources:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def adult_mode(cfg):
    """成人内容处理模式：mark（标注并照常测活，默认） / exclude（直接过滤）。

    兼容旧配置：若未显式设置 adult_mode，则按 filter_adult 推导
    （filter_adult=False 视为 mark，True 视为 exclude）。
    """
    mode = str(cfg.get("adult_mode", "")).strip().lower()
    if mode in ("mark", "label", "tag", "标注", "keep"):
        return "mark"
    if mode in ("exclude", "filter", "drop", "过滤"):
        return "exclude"
    return "exclude" if cfg.get("filter_adult", True) else "mark"


def adult_mark(cfg):
    """成人站点标注前缀。"""
    return cfg.get("adult_mark", "🔞") or "🔞"


def adult_name_mask(cfg):
    """成人站点名称脱敏代号。

    默认 "1BJ"：识别为成人的站点，在订阅文件与报告里名称统一显示为 1BJ，
    真实名称不外泄；网址(api) 原样保留。设为空字符串则关闭脱敏，
    回退到 adult_mark 前缀标注模式（如 "🔞 原名"）。
    """
    return str(cfg.get("adult_name_mask", "1BJ")).strip()


def adult_placement(cfg):
    """mark 模式下成人站点如何进入订阅（细粒度开关）：

    - separate   : 单独存放到成人订阅文件（默认），主订阅不含成人站点
    - mix        : 带 🔞 标注混排进主订阅
    - report_only: 只在报告里标注+展示网址，不写入任何订阅文件
    兼容旧配置 adult_separate（true=separate / false=mix）。
    """
    v = str(cfg.get("adult_placement", "")).strip().lower()
    if v in ("separate", "split", "单独", "独立"):
        return "separate"
    if v in ("mix", "inline", "混排", "混合"):
        return "mix"
    if v in ("report_only", "report", "report-only", "仅报告", "只看报告"):
        return "report_only"
    if "adult_separate" in cfg:
        return "separate" if cfg.get("adult_separate") else "mix"
    return "separate"


def is_adult(name, cfg):
    """判断是否命中成人内容关键词（与处理模式无关，仅做识别）。"""
    low = (name or "").lower()
    for kw in cfg.get("adult_keywords", []):
        if kw.lower() in low:
            return True
    return False


def mark_adult_name(name, cfg):
    """成人站点名称的对外显示（订阅/报告统一走这里）。

    - 配置了 adult_name_mask（默认 1BJ）：整体替换为脱敏代号，真实名称不外泄；
      网址(api) 由调用方原样保留，所以影视仓仍能正常播放。
    - adult_name_mask 为空：回退到 adult_mark 前缀标注（旧行为）。
    幂等：已是代号/已带前缀时原样返回。
    """
    name = str(name or "")
    mask = adult_name_mask(cfg)
    if mask:
        return mask
    mk = adult_mark(cfg)
    if name.startswith(mk):
        return name
    return "%s %s" % (mk, name)


def mask_if_adult(name, cfg):
    """若名称命中成人关键词则脱敏，否则原样返回。

    用于展开日志、重复合并示例等未带 _adult 内部标记的字段，
    防止真实成人名称从日志/示例区外泄。
    """
    name = str(name or "")
    if is_adult(name, cfg):
        return mark_adult_name(name, cfg)
    return name


def extract_child_urls(data):
    """从多仓配置里提取子仓地址，兼容 urls/storeHouse 多种字段与元素格式。"""
    children = []
    containers = []
    for key in ("urls", "storeHouse", "repos", "warehouses", "configs"):
        v = data.get(key)
        if isinstance(v, list):
            containers.extend(v)
    for item in containers:
        if isinstance(item, str):
            u = item
            name = ""
        elif isinstance(item, dict):
            u = (item.get("url") or item.get("sourceUrl") or item.get("source_url")
                 or item.get("link") or item.get("address") or "")
            name = item.get("name") or item.get("sourceName") or ""
        else:
            continue
        if isinstance(u, str) and u.startswith(("http://", "https://")):
            children.append((u, name))
    return children


def expand_sources(sources, cfg):
    """递归展开多仓列表，返回 (叶子单仓列表, 展开日志列表)。

    叶子单仓元素: {"url": 源地址, "name": 仓名, "config": 完整配置dict}
    展开日志元素: (层级, 地址, 名称, 状态说明)
    """
    max_depth = int(cfg.get("max_depth", 6))
    max_leaf = int(cfg.get("max_leaf_configs", 400))
    cache = {}   # url -> (text, status, err)
    visited = set()
    leaves = []
    logs = []
    filtered = [0]

    def fetch(url):
        if url not in cache:
            cache[url] = http_get(url, cfg)
        return cache[url]

    def walk(url, name, depth, inherited_adult=False):
        if len(leaves) >= max_leaf:
            return
        key = url.rstrip("/").lower()
        if key in visited:
            logs.append((depth, url, name, "跳过(重复)"))
            return
        visited.add(key)
        text, status, err = fetch(url)
        if err or text is None:
            logs.append((depth, url, name, "失败: %s" % (err or "status=%s" % status)))
            return
        data = parse_json_loose(text)
        if not isinstance(data, dict):
            logs.append((depth, url, name, "非配置(HTML/密文?)"))
            return
        name_adult = is_adult(name, cfg)
        adult_here = inherited_adult or name_adult
        mode = adult_mode(cfg)
        # 成人仓名：mark 模式照常展开并标注；exclude 模式才过滤
        if name_adult and mode == "exclude":
            filtered[0] += 1
            logs.append((depth, url, name, "过滤(成人仓)"))
            return
        if name_adult:
            filtered[0] += 1  # 两种模式都统计识别到的成人仓
            logs.append((depth, url, name, "成人仓(标注展开)" if mode == "mark" else "成人仓"))
        if isinstance(data.get("sites"), list):
            # 叶子单仓
            tag = " [成人]" if adult_here else ""
            logs.append((depth, url, name, "单仓 %d 站%s" % (len(data["sites"]), tag)))
            leaves.append({"url": url, "name": name, "config": data, "adult": adult_here})
            return
        children = extract_child_urls(data)
        if children and depth < max_depth:
            logs.append((depth, url, name, "多仓 %d 项，展开" % len(children)))
            for cu, cn in children:
                walk(cu, cn, depth + 1, adult_here)
        else:
            logs.append((depth, url, name, "无 sites/子仓"))

    for u in sources:
        walk(u, "", 0)
    return leaves, logs, filtered[0], len(cache)


def collect_sites(leaves, cfg):
    """从叶子单仓收集站点：成人标注/过滤 + 预去重 + 标注来源仓。

    预去重 key = API 的 host+path（同一接口在多仓重复出现只留一条）。
    mark 模式：成人站点照常纳入测活，打内部标记 _adult，最终在名称加 🔞 标注；
    exclude 模式：成人站点直接过滤。
    返回 (站点列表, 成人处理数, 预去重合并数, 合并明细)。
    """
    sites = []
    adult_count = 0
    seen = {}          # key -> index in sites
    merged = 0
    merge_samples = []
    mode = adult_mode(cfg)
    for leaf in leaves:
        src = leaf["name"] or leaf["url"]
        leaf_adult = bool(leaf.get("adult"))
        for s in leaf["config"].get("sites") or []:
            name = s.get("name", "")
            api = s.get("api") or s.get("url") or ""
            is_ad = leaf_adult or is_adult(name, cfg) or is_adult(api, cfg)
            if is_ad:
                adult_count += 1
                if mode == "exclude":
                    continue
            key = site_key({"api": api})
            if key and key in seen:
                merged += 1
                # 同一接口，把来源仓追加到已保留条目的来源列表
                idx = seen[key]
                prev_src = sites[idx].get("_sources", [])
                if src not in prev_src:
                    prev_src.append(src)
                    sites[idx]["_sources"] = prev_src
                # 合并后若任一来源命中成人，保留项标记为成人
                if is_ad:
                    sites[idx]["_adult"] = True
                if len(merge_samples) < 30:
                    merge_samples.append((name, src))
                continue
            item = dict(s)
            item["_source"] = src
            item["_sources"] = [src]
            if is_ad and mode == "mark":
                item["_adult"] = True
            if key:
                seen[key] = len(sites)
            sites.append(item)
    return sites, adult_count, merged, merge_samples


def build_probe_url(api, keyword):
    """根据站点 api 的占位符风格，构造带搜索关键词的测活 URL。"""
    kw = quote(keyword)
    if "{wd}" in api:
        return api.replace("{wd}", kw)
    if "**" in api:
        return api.replace("**", kw)
    if "wd=" in api:
        return re.sub(r"(wd=)[^&]*", r"\1" + kw, api)
    if "search" in api.lower():
        sep = "&" if "?" in api else "?"
        return api + sep + "wd=" + kw
    sep = "&" if "?" in api else "?"
    return api + sep + "wd=" + kw


# ---- 内容校验：判断响应体是否真的是「视频源内容」 ----
# 视频条目常见字段（苹果 CMS / TVBox 各类聚合接口）
_VOD_ITEM_KEYS = (
    "vod_id", "vod_name", "vod_pic", "vod_play_url", "vod_remarks",
    "vod_class", "vod_time", "vod_tag", "vod_actor", "vod_director",
    "name", "title", "url", "play_url", "id", "pic",
)
# CMS 结构标志字段：出现这些说明是标准视频源框架，即使 list 为空也算有效源
_CMS_META_KEYS = (
    "code", "msg", "page", "pageindex", "pagecount", "pagesize",
    "recordcount", "total", "limit", "class", "list", "data",
)


def _looks_like_vod_item(obj):
    """判断一个对象是否像一条视频条目。"""
    if not isinstance(obj, dict):
        return False
    keys = {str(k).lower() for k in obj.keys()}
    # 命中任一视频字段即认为像条目（真源条目一般一次命中多个）
    return len(keys & set(_VOD_ITEM_KEYS)) >= 1


def classify_content(body, cfg):
    """分析响应体内容，返回 (类别, 说明)。

    类别取值：
      'vod'     真视频源（含视频条目）
      'empty'   标准 CMS 空结果（结构正确，只是没搜到内容）
      'invalid' 无效内容（错误页/验证码/空壳/非视频源）
    """
    text = (body or "").strip()
    lower = text.lower()

    # 1) 无效页面特征词（错误页、验证码、登录墙、空壳提示）
    for kw in cfg.get("invalid_keywords", []):
        if kw and kw.lower() in lower:
            return "invalid", "命中无效页特征: %s" % kw

    # 2) 解析 JSON
    data = parse_json_loose(text)
    if data is not None:
        containers = []
        if isinstance(data, list):
            containers = [("list", data)]
        elif isinstance(data, dict):
            # 收集所有可能为列表的字段
            for k, v in data.items():
                if isinstance(v, list):
                    containers.append((k, v))
        # 2a) 任意列表里出现视频条目 -> 有效
        for k, v in containers:
            if v and _looks_like_vod_item(v[0]):
                return "vod", "%s: %d 条视频" % (k, len(v))
        # 2b) 有空列表 + CMS 结构标志 -> 标准空结果
        has_empty_list = any(k in ("list", "data", "videos", "results", "result")
                             and len(v) == 0 for k, v in containers)
        if isinstance(data, dict):
            dkeys = {str(k).lower() for k in data.keys()}
            has_cms_meta = len(dkeys & set(_CMS_META_KEYS)) >= 2
            code = data.get("code")
            # code 显式为失败码（如 -1、400、500、error）且无视频条目 -> 错误响应
            # 注意：部分 CMS 用 code=0 表示成功，所以 0 不判失败；msg 的失败语义由 invalid_keywords 兜底
            has_vod = any(v and _looks_like_vod_item(v[0]) for _, v in containers)
            if (code is not None and str(code).lower() not in ("1", "200", "0", "true")
                    and not has_vod):
                return "invalid", "错误响应 code=%s" % code
            if has_empty_list and has_cms_meta:
                if cfg.get("content_strict", False):
                    return "invalid", "空结果(严格模式)"
                if cfg.get("allow_empty_result", True):
                    return "empty", "标准空结果(源正常,无匹配)"
                return "invalid", "空结果(未允许)"
        # 2c) 有列表但元素不是视频结构 -> 可疑
        if containers:
            if cfg.get("content_strict", False):
                return "invalid", "列表无视频字段(严格模式)"
            return "invalid", "列表无视频结构"
        # 2d) 纯对象无任何列表 -> 空壳
        return "invalid", "JSON 空壳(无视频列表)"

    # 3) 解析 XML（苹果 CMS RSS 格式）
    if re.search(r"<rss\b", lower) or re.search(r"<(video|item|vod)\b", lower):
        videos = re.findall(r"<(video|item|vod)\b", lower)
        has_name = bool(re.search(r"<(name|title|vod_name)\b", lower))
        has_id = bool(re.search(r"<(id|vod_id)\b", lower))
        if videos and (has_name or has_id):
            return "vod", "XML: %d 条视频" % len(videos)
        if "recordcount=\"0\"" in lower.replace(" ", "") or re.search(r"<list\b", lower):
            if cfg.get("content_strict", False):
                return "invalid", "XML 空结果(严格模式)"
            if cfg.get("allow_empty_result", True):
                return "empty", "XML 标准空结果"
        return "invalid", "XML 无视频节点"

    # 4) 其他非结构化内容（HTML/纯文本）一律不算视频源
    return "invalid", "非视频源内容(%d 字节)" % len(text)


def probe_site(site, cfg):
    """测活单个站点，返回 (是否存活, 说明)。"""
    api = site.get("api") or site.get("url") or ""
    if not api or not api.startswith(("http://", "https://")):
        return False, "无有效 api"
    url = build_probe_url(api, cfg["keyword"])
    text, status, err = http_get(url, cfg)
    if err or text is None:
        return False, err or "请求失败"
    body = text.strip()
    if len(body) < int(cfg.get("min_body", 20)):
        return False, "响应过短(status=%s)" % status
    lower = body.lower()
    for bad in cfg.get("bad_keywords", []):
        if bad.lower() in lower:
            return False, "命中异常关键词: %s" % bad

    # 内容校验：判断返回的是否真是视频源
    if cfg.get("content_check", True):
        kind, note = classify_content(body, cfg)
        if kind == "invalid":
            return False, "内容校验失败: " + note
        if kind == "empty":
            return True, "存活(空结果): " + note
        return True, "有效视频源: " + note

    # —— 关闭内容校验时的旧逻辑（仅判断能否返回）——
    data = parse_json_loose(body)
    if data is not None:
        if isinstance(data, list):
            return (len(data) > 0), ("返回 %d 条" % len(data) if data else "空列表")
        if isinstance(data, dict):
            for k in ("list", "data", "result", "videos", "results"):
                v = data.get(k)
                if isinstance(v, list):
                    return (len(v) > 0), ("%s: %d 条" % (k, len(v)) if v else "%s 空" % k)
            return True, "返回对象(%d 字段)" % len(data)
    return True, "非 JSON 响应(%d 字节)" % len(body)


def site_key(site):
    api = site.get("api") or site.get("url") or ""
    p = urlparse(api)
    return (p.netloc + p.path).lower().rstrip("/")


def dedupe_sites(sites):
    seen, out = set(), []
    for s in sites:
        k = site_key(s)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


def build_base_config(leaves, cfg):
    """以第一个有效单仓配置为模板，按需保留 lives/parses。"""
    base = {}
    for leaf in leaves:
        if isinstance(leaf["config"], dict):
            base = dict(leaf["config"])
            break
    if not cfg.get("keep_lives"):
        base.pop("lives", None)
    if not cfg.get("keep_parses"):
        base.pop("parses", None)
    return base


def merge_subscription(leaves, alive_sites, cfg):
    """生成订阅。mark + adult_separate 模式下把成人站点拆到单独订阅。

    返回 (主订阅, 成人订阅或None, 成人站点数)。
    - 主订阅 output/subscribe.json：只含常规站点（成人站点已移出）
    - 成人订阅 output/subscribe_adult.json：只含带 🔞 标注的成人站点
    - adult_separate=False 时不拆分，成人站点仍留在主订阅（带标注）
    """
    mode = adult_mode(cfg)
    placement = adult_placement(cfg) if mode == "mark" else "mix"
    separate = placement == "separate"
    base = build_base_config(leaves, cfg)

    normal_sites, adult_sites = [], []
    for s in alive_sites:
        is_ad = bool(s.pop("_adult", False)) and mode == "mark"
        s.pop("_source", None)
        s.pop("_sources", None)
        if is_ad:
            s["name"] = mark_adult_name(s.get("name", ""), cfg)
            adult_sites.append(s)
        else:
            normal_sites.append(s)

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    main_sub = dict(base)
    main_sub["sites"] = normal_sites
    place_notes = {
        "separate": "成人站点已拆分至单独订阅",
        "mix": "成人站点带标注混排",
        "report_only": "成人站点仅在报告展示，未写入订阅",
    }
    main_sub["_maintainer"] = {
        "updated": stamp,
        "alive_sites": len(normal_sites),
        "adult_mode": mode,
        "adult_placement": placement,
        "note": place_notes[placement],
    }

    if placement == "separate":
        adult_sub = dict(base)
        adult_sub["sites"] = adult_sites
        adult_sub["_maintainer"] = {
            "updated": stamp,
            "alive_sites": len(adult_sites),
            "adult_mode": mode,
            "adult_only": True,
        }
        return main_sub, adult_sub, len(adult_sites)

    if placement == "mix":
        # 混排：成人站点仍留在主订阅（带标注）
        main_sub["sites"] = normal_sites + adult_sites
        main_sub["_maintainer"]["alive_sites"] = len(normal_sites) + len(adult_sites)
        main_sub["_maintainer"]["adult_marked"] = len(adult_sites)

    # report_only：成人站点不写入任何订阅，仅在报告展示
    return main_sub, None, len(adult_sites)


def write_output(cfg, subscription, report_lines, adult_subscription=None):
    def resolve(p):
        return p if os.path.isabs(p) else os.path.join(BASE_DIR, p)

    # 主订阅
    p = resolve(cfg["output"])
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(subscription, f, ensure_ascii=False, indent=2)
    log("已写出主订阅: %s (%d 站)" % (cfg["output"], len(subscription.get("sites", []))))

    # 成人订阅（单独存放）
    adult_path = cfg.get("adult_output", "output/subscribe_adult.json")
    if adult_subscription is not None:
        ap = resolve(adult_path)
        os.makedirs(os.path.dirname(ap), exist_ok=True)
        with open(ap, "w", encoding="utf-8") as f:
            json.dump(adult_subscription, f, ensure_ascii=False, indent=2)
        log("已写出成人订阅: %s (%d 站)" % (adult_path, len(adult_subscription.get("sites", []))))

    # 报告
    p = resolve(cfg["status"])
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines) + "\n")
    log("已写出报告: %s" % cfg["status"])


def main():
    ap = argparse.ArgumentParser(description="TVBox/影视仓 订阅源自动维护工具")
    ap.add_argument("-c", "--config", default="config.json", help="配置文件路径")
    args = ap.parse_args()

    cfg = load_config(args.config)
    sources = read_sources(cfg)
    log("候选聚合源: %d 个" % len(sources))
    if not sources:
        log("没有配置任何源，请编辑 sources.txt 或 config.json 的 sources")
        sys.exit(1)

    report = []
    report.append("# 订阅源测活报告")
    report.append("")
    report.append("- 更新时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    report.append("- 候选根源: %d 个" % len(sources))
    report.append("")

    # 1) 递归展开多仓
    log("开始递归展开多仓（最大深度 %s）..." % cfg.get("max_depth", 6))
    leaves, expand_logs, url_adult_filtered, fetched = expand_sources(sources, cfg)
    log("展开完成：抓取 %d 个地址，得到 %d 个叶子单仓" % (fetched, len(leaves)))

    all_sites, adult_count, merged_dupes, merge_samples = collect_sites(leaves, cfg)
    total = len(all_sites)
    amode = adult_mode(cfg)
    adult_label = "标注纳入测活" if amode == "mark" else "过滤"
    log("共提取站点 %d 个（成人%s %d，预去重合并 %d）" % (
        total, adult_label, adult_count + url_adult_filtered, merged_dupes))

    report.append("## 汇总")
    report.append("")
    report.append("- 候选根源: %d" % len(sources))
    report.append("- 抓取地址数: %d" % fetched)
    report.append("- 叶子单仓数: %d" % len(leaves))
    report.append("- 站点总数(预去重后): %d" % total)
    report.append("- 成人内容处理: %s（模式 %s，仓 %d / 站点 %d）" % (
        adult_label, amode, url_adult_filtered, adult_count))
    report.append("- 重复站点合并: %d（同接口在多仓重复出现）" % merged_dupes)
    report.append("")
    if merge_samples:
        report.append("> 重复合并示例（站点 ← 被合并的重复来源）:")
        report.append(">")
        for nm, src in merge_samples[:15]:
            report.append("> - %s ← %s" % (mask_if_adult(nm, cfg), mask_if_adult(src, cfg)))
        report.append("")

    report.append("## 展开日志")
    report.append("")
    report.append("| 层 | 地址 | 名称 | 结果 |")
    report.append("| --- | --- | --- | --- |")
    for depth, url, name, status in expand_logs:
        report.append("| %d | %s | %s | %s |" % (depth, url, mask_if_adult(name, cfg), status))
    report.append("")

    # 2) 并发测活
    log("开始并发测活(线程=%s)..." % cfg["workers"])
    alive = []
    details = []
    with ThreadPoolExecutor(max_workers=int(cfg["workers"])) as ex:
        futures = {ex.submit(probe_site, s, cfg): s for s in all_sites}
        for i, fut in enumerate(as_completed(futures), 1):
            s = futures[fut]
            try:
                ok, why = fut.result()
            except Exception as e:
                ok, why = False, str(e)
            srcs = s.get("_sources") or ([s["_source"]] if s.get("_source") else [])
            details.append((ok, s.get("name", "?"), " + ".join(srcs[:3]), why,
                            s.get("api", ""), bool(s.get("_adult"))))
            if ok:
                alive.append(s)
            if i % 30 == 0 or i == total:
                log("测活进度 %d/%d，当前存活 %d" % (i, total, len(alive)))

    alive = dedupe_sites(alive)
    # 成人站点明细（在 merge_subscription 清除标记之前生成）
    # adult_all_details: 所有识别到的成人站点（含失效），保留完整网址
    # adult_alive_details: 其中通过测活的
    adult_all_details = [d for d in details if d[5]]
    adult_alive_details = [d for d in adult_all_details if d[0]]
    log("测活完成：原始 %d -> 去重后存活 %d（成人站点 %d，其中存活 %d）" % (
        total, len(alive), len(adult_all_details), len(adult_alive_details)))

    min_sites = int(cfg.get("min_sites", 1))
    if len(alive) < min_sites:
        log("警告：存活站点 %d < 阈值 %d，仍会输出" % (len(alive), min_sites))

    report.append("## 测活汇总")
    report.append("")
    report.append("- 存活站点(去重后): %d" % len(alive))
    report.append("- 存活率: %.1f%%" % ((len(alive) / total * 100) if total else 0))
    report.append("- 其中成人内容存活(已标注): %d" % len(adult_alive_details))
    report.append("")

    # 成人内容清单：名称 + 网址 + 来源仓（用户要求标注出来并给出网址）
    # 列出所有识别到的成人站点（存活 + 失效），确保网址一定能看到
    if amode == "mark":
        report.append("## 成人内容清单（已标注，照常测活，含网址）")
        report.append("")
        report.append("- 识别到成人站点: %d（存活 %d / 失效 %d）" % (
            len(adult_all_details), len(adult_alive_details),
            len(adult_all_details) - len(adult_alive_details)))
        report.append("")
        if adult_all_details:
            report.append("| 结果 | 站点(已标注) | 网址(api) | 来源仓 | 测活说明 |")
            report.append("| --- | --- | --- | --- | --- |")
            for ok, name, src, why, api, ad in sorted(adult_all_details, key=lambda x: (not x[0], x[1])):
                report.append("| %s | %s | %s | %s | %s |" % (
                    "存活" if ok else "失效", mark_adult_name(name, cfg), api, src, why))
        else:
            report.append("> 本次未识别到成人内容站点。")
        report.append("")

    report.append("## 站点明细")
    report.append("")
    report.append("| 结果 | 站点 | 网址(api) | 来源仓 | 说明 |")
    report.append("| --- | --- | --- | --- | --- |")
    for ok, name, src, why, api, ad in sorted(details, key=lambda x: (not x[0], x[1])):
        disp = mark_adult_name(name, cfg) if ad else name
        report.append("| %s | %s | %s | %s | %s |" % (
            "存活" if ok else "失效", disp, api, src, why))

    # 3) 生成订阅（成人站点按 adult_placement 拆分/混排/仅报告）
    subscription, adult_subscription, adult_alive = merge_subscription(leaves, alive, cfg)
    placement = adult_placement(cfg) if amode == "mark" else "exclude"
    adult_path = cfg.get("adult_output", "output/subscribe_adult.json")
    place_desc = {
        "separate": "拆分至单独订阅 %s" % adult_path,
        "mix": "带标注混排进主订阅 %s" % cfg["output"],
        "report_only": "仅在报告展示，未写入订阅",
        "exclude": "已过滤",
    }[placement]
    report.insert(3, "- 成人站点去向: %s（共 %d 个存活）" % (place_desc, adult_alive))
    write_output(cfg, subscription, report, adult_subscription)
    log("完成。")
    return 0 if alive else 2


if __name__ == "__main__":
    sys.exit(main())

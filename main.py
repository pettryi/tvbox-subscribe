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


def md_cell(v):
    """转义 Markdown 表格单元格，避免值里的 | / 换行撑破表格。"""
    s = "" if v is None else str(v)
    return s.replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


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


def alt_scheme(url):
    """返回切换 http/https 后的地址，无法切换时返回空串。"""
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    if url.startswith("https://"):
        return "http://" + url[len("https://"):]
    return ""


def http_get(url, cfg, timeout=None):
    """返回 (text, status, error)。出错时 text 为 None。

    抗抖动：每次重试轮换 UA；纯网络错误（超时/DNS/连接重置）时自动尝试
    切换 http/https 再试一次，避免单线路故障把有效源误判为失效。
    HTTP 4xx/5xx 属于服务端明确响应，不做协议切换。
    """
    timeout = timeout or cfg["timeout"]
    url = normalize_url(url)
    retries = int(cfg.get("retries", 0))
    opener, _ = make_opener(timeout)
    last_err = None
    tried = set()

    def do_request(target):
        ua = random.choice(UA_POOL) if cfg.get("ua_rotate") else cfg["user_agent"]
        req = urllib.request.Request(normalize_url(target), headers={
            "User-Agent": ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
            try:
                text = raw.decode(charset, errors="replace")
            except LookupError:
                text = raw.decode("utf-8", errors="replace")
            return text, resp.status, None

    target = url
    for attempt in range(retries + 1):
        try:
            return do_request(target)
        except urllib.error.HTTPError as e:
            last_err = "HTTP %s" % e.code
            try:
                body = e.read().decode("utf-8", errors="replace")
                return body, e.code, None
            except Exception:
                pass
        except Exception as e:
            last_err = str(e)
            # 纯网络错误：尝试切换协议再要一次（只切一次）
            if cfg.get("probe_scheme_fallback", True):
                alt = alt_scheme(target)
                if alt and alt not in tried:
                    tried.add(target)
                    target = alt
                    last_err = str(e) + " (重试切换协议)"
                    continue
        if attempt < retries:
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
            # 叶子单仓：把 spider 包地址补进每个站点，供爬虫源测活使用
            spider_url = data.get("spider") or ""
            for st in data["sites"]:
                if isinstance(st, dict):
                    # 站点未显式给 spider 时，继承仓级 spider 字段
                    if not (st.get("spider") or st.get("spiderUrl")) and spider_url:
                        st.setdefault("_leaf_spider", spider_url)
                        if isinstance(spider_url, str):
                            st["spider"] = spider_url
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
            # 记录叶子配置地址，供爬虫源解析 ./ext/xxx.js 这类相对路径
            item["_source_url"] = leaf.get("url", "")
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


def build_home_urls(api):
    """影视仓加载「首页数据源」时实际请求的列表接口地址（按优先级）。

    关键：影视仓预检数据源用的是采集列表接口（api 本身或 ?ac=videolist），
    不是搜索接口 wd=xxx。搜索能返回视频 ≠ 首页能列出视频，这正是
    「订阅显示很多站、影视仓只列出少数」的根因。
    返回去重后的候选 URL 列表。
    """
    urls = [api]
    p = urlparse(api)
    base = "%s://%s%s" % (p.scheme, p.netloc, p.path)
    urls.append(base + "?ac=videolist&pg=1")
    out, seen = [], set()
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def probe_home_visible(api, cfg):
    """按影视仓加载首页的方式（列表接口）判活一个 http api。

    返回 (是否拿到真视频, 说明, 耗时ms, 是否网络错误)。
    只要任一列表接口形态返回真实视频条目，即认为影视仓首页可见。
    """
    t0 = time.time()

    def ms():
        return int(round((time.time() - t0) * 1000))

    last_note = "列表接口无有效响应"
    any_http = False  # 是否至少有一个形态真正拿到了 HTTP 响应（区别于全网络错误）
    for url in build_home_urls(api):
        text, status, err = http_get(url, cfg)
        if err or text is None:
            last_note = err or "请求失败"
            continue
        any_http = True
        body = text.strip()
        if len(body) < int(cfg.get("min_body", 20)):
            last_note = "响应过短(status=%s)" % status
            continue
        lower = body.lower()
        hit_bad = False
        for bad in cfg.get("bad_keywords", []):
            if bad and bad.lower() in lower:
                last_note = "命中异常关键词: %s" % bad
                hit_bad = True
                break
        if hit_bad:
            continue
        kind, note = classify_content(body, cfg)
        if kind == "vod":
            return True, note, ms(), False
        last_note = note
    # 所有形态都网络失败 → 标记网络错误，调用方跳过搜索兜底（避免白等超时）
    return False, last_note, ms(), not any_http


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


def _probe_http(api, keyword, cfg):
    """用单个关键词探测一个 http api，返回 (ok, note, kind)。

    kind: 'vod' 真视频 / 'empty' 标准空结果 / 'invalid' 内容无效 /
          'error' 网络或请求失败。ok 仅代表本次请求拿到可用响应。
    """
    url = build_probe_url(api, keyword)
    text, status, err = http_get(url, cfg)
    if err or text is None:
        return False, err or "请求失败", "error"
    body = text.strip()
    if len(body) < int(cfg.get("min_body", 20)):
        return False, "响应过短(status=%s)" % status, "error"
    lower = body.lower()
    for bad in cfg.get("bad_keywords", []):
        if bad.lower() in lower:
            return False, "命中异常关键词: %s" % bad, "error"

    if cfg.get("content_check", True):
        kind, note = classify_content(body, cfg)
        if kind == "vod":
            return True, "有效视频源: " + note, "vod"
        if kind == "empty":
            return True, "标准空结果: " + note, "empty"
        return False, "内容校验失败: " + note, "invalid"

    # —— 关闭内容校验时的旧逻辑（仅判断能否返回）——
    data = parse_json_loose(body)
    if data is not None:
        if isinstance(data, list):
            return (len(data) > 0), ("返回 %d 条" % len(data) if data else "空列表"), "vod"
        if isinstance(data, dict):
            for k in ("list", "data", "result", "videos", "results"):
                v = data.get(k)
                if isinstance(v, list):
                    return (len(v) > 0), ("%s: %d 条" % (k, len(v)) if v else "%s 空" % k), "vod"
            return True, "返回对象(%d 字段)" % len(data), "vod"
    return True, "非 JSON 响应(%d 字节)" % len(body), "vod"


def probe_http_api(api, cfg, site=None):
    """对一个 http api 用「多关键词」测活（换词后再判）。

    返回 (是否存活, 说明, 是否曾拿到真实视频)。
    - 任一关键词拿到真视频条目 -> 存活
    - 全部关键词都只是标准空结果 -> 由 allow_empty_result / content_strict 决定
    - 否则失效
    同时把本次响应耗时(ms)写入 site["_latency_ms"]，供速度排序使用。
    """
    t0 = time.time()
    kws = [cfg.get("keyword", "test")]
    for k in cfg.get("probe_keywords", []) or []:
        if k and k not in kws:
            kws.append(k)
    limit = int(cfg.get("probe_keyword_limit", 3))
    kws = kws[:max(1, limit)]

    empty_notes, bad_notes, first_err = [], [], ""

    def _finish(ok, note, got_vod):
        if site is not None:
            site["_latency_ms"] = int(round((time.time() - t0) * 1000))
            site["_probe_kind"] = "vod" if got_vod else ("empty" if ok else "fail")
        return ok, note, got_vod

    for idx, kw in enumerate(kws):
        ok, note, kind = _probe_http(api, kw, cfg)
        if kind == "vod" and ok:
            return _finish(True, note, True)
        if kind == "empty" and ok:
            empty_notes.append(note)
            continue
        # 网络错误（超时/DNS/连接失败）：换关键词改变不了网络可达性，
        # 直接判定，避免每个失效站点都空等 N 次超时拖垮整体耗时
        if kind == "error":
            if not first_err:
                first_err = note
            bad_notes.append("%s:%s" % (kw, note[:24]))
            break
        if not first_err:
            first_err = note
        bad_notes.append("%s:%s" % (kw, note[:24]))

    # 多关键词都只返回标准空结果
    if empty_notes and not bad_notes:
        if cfg.get("content_strict", False):
            return _finish(False, "多关键词均为空结果(严格模式)", False)
        if cfg.get("allow_empty_result", True):
            return _finish(True, "存活(换词仍空结果，源结构正常)", False)
        return _finish(False, "多关键词均为空结果(未允许)", False)
    # 有真视频已被上面的 return 捕获；剩下是失败（可能部分词空、部分失败）
    if empty_notes and cfg.get("allow_empty_result", True) and not cfg.get("content_strict", False):
        return _finish(True, "存活(部分关键词空结果)", False)
    return _finish(False, (first_err or "多关键词均未拿到有效视频"), False)


# —— 爬虫源（csp_ / .js）：从脚本还原真实接口 + 依赖可达性兜底 ——
_SCRIPT_CACHE = {}


def _load_script(url, cfg):
    """下载爬虫脚本/jar 文本，带缓存与大小上限。返回 (text, err)。"""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return "", "非 http 脚本地址"
    if url in _SCRIPT_CACHE:
        return _SCRIPT_CACHE[url]
    max_bytes = int(cfg.get("spider_max_bytes", 4 * 1024 * 1024))
    text, status, err = http_get(url, cfg, timeout=int(cfg.get("spider_timeout", 10)))
    if err or text is None:
        _SCRIPT_CACHE[url] = ("", err or "status=%s" % status)
    elif len(text) > max_bytes:
        _SCRIPT_CACHE[url] = ("", "脚本过大(%d字节)" % len(text))
    else:
        _SCRIPT_CACHE[url] = (text, None)
    return _SCRIPT_CACHE[url]


def extract_api_from_script(script, cfg):
    """从 csp/js 爬虫脚本里提取可能的真实视频接口地址。

    csp_/drpy 脚本内部通常写死了真实 CMS 接口（api.php/provide/vod、
    https://域名/api.php/vod 等）。用正则把这些地址抠出来再交给普通测活。
    返回去重后的候选 api 列表。
    """
    if not script:
        return []
    pats = cfg.get("spider_api_patterns", [
        r"https?://[^\s\"'<>{}|,;\\`\)\(]+?/api\.php/provide/vod[^\s\"'<>{}|,;\\`\)\(]*",
        r"https?://[^\s\"'<>{}|,;\\`\)\(]+?/api\.php/vod[^\s\"'<>{}|,;\\`\)\(]*",
        r"https?://[^\s\"'<>{}|,;\\`\)\(]+?/api\.php[^\s\"'<>{}|,;\\`\)\(]*",
        r"https?://[^\s\"'<>{}|,;\\`\)\(]+?/provide/vod[^\s\"'<>{}|,;\\`\)\(]*",
    ])
    found = []
    for p in pats:
        try:
            found.extend(re.findall(p, script, flags=re.I))
        except re.error:
            continue
    out, seen = [], set()
    for u in found:
        u = u.rstrip("/.,;)]}")
        u = u.replace("\\/", "/").replace("&amp;", "&")
        if len(u) < 12 or u in seen:
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= int(cfg.get("spider_api_max", 8)):
            break
    return out


def probe_spider_site(site, cfg):
    """测活非 http 的爬虫源（csp_ / .js 等）。返回 (是否保留, 说明)。

    两级策略：
      1) 依赖可达：站点 ext / spider 字段里的脚本能下载 -> 视为"待验证爬虫源"保留
      2) 真实接口还原：从脚本里抠出 http api，用普通测活验证 -> 确认为有效源
    两级都不满足才判失效。
    """
    api = site.get("api") or site.get("url") or ""
    ext = site.get("ext") or ""
    spider = site.get("spider") or ""

    def abs_url(u, base):
        u = (u or "").strip()
        if not u:
            return ""
        if u.startswith(("http://", "https://")):
            return u
        if u.startswith("//") and base:
            return base.split("://", 1)[0] + ":" + u
        # 相对路径（./ext/xxx.js）：以叶子配置地址为基准解析
        if base:
            try:
                from urllib.parse import urljoin
                return urljoin(base, u)
            except Exception:
                return ""
        return ""

    base = site.get("_source_url", "")
    candidates = []
    for raw in (ext, spider):
        u = abs_url(raw, base)
        if u and u not in candidates:
            candidates.append(u)

    # 脚本优先用 ext（配置脚本），其次 spider 包
    loaded = []
    for u in candidates[:3]:
        txt, err = _load_script(u, cfg)
        if txt:
            loaded.append((u, txt))

    # 策略 1：真实接口还原
    if cfg.get("spider_extract_api", True):
        for u, script in loaded:
            for real_api in extract_api_from_script(script, cfg):
                ok, note, got_vod = probe_http_api(real_api, cfg, site=site)
                if ok and got_vod:
                    site["api"] = real_api
                    site["_restored_from"] = api
                    return True, "爬虫源·接口还原有效(%s): %s" % (u.rsplit("/", 1)[-1][:20], note)

    # 策略 2：依赖可达即保留（未测真实内容，速度不可比，排序时降到最后）
    if loaded:
        u = loaded[0][0]
        site["_crawler"] = True
        site["_probe_kind"] = "crawler"
        return True, "爬虫源·依赖可达未测内容(%s)" % u.rsplit("/", 1)[-1][:24]

    # 站点带 spider 包字段，但脚本取不到（可能站点本身即标识）
    if api.startswith("csp_") and cfg.get("spider_keep_if_unreachable", True):
        site["_crawler"] = True
        site["_probe_kind"] = "crawler"
        return True, "爬虫源·依赖未取到(csp包)，按配置保留"

    if candidates:
        return False, "爬虫源·脚本不可达"
    return False, "爬虫源·无依赖地址可验证"


def probe_site(site, cfg):
    """测活单个站点，返回 (是否存活, 说明)。

    http api：多关键词测活 + 网络抖动重试（见 probe_http_api / http_get）
    非 http：csp/js 爬虫源走接口还原 + 依赖可达兜底（见 probe_spider_site）

    副作用：向 site 写入 _latency_ms(响应耗时ms) 与 _probe_kind
    （vod 拿到真实视频 / empty 空结果 / crawler 爬虫源未测内容 / fail 失败），
    供订阅按「速度从高到低」排序使用。返回签名不变，checker.py 可继续复用。
    """
    site["_latency_ms"] = 0
    site["_probe_kind"] = "fail"
    site["_home_visible"] = False
    api = site.get("api") or site.get("url") or ""
    if not api:
        return False, "无 api"
    if not api.startswith(("http://", "https://")):
        t0 = time.time()
        ok, note = probe_spider_site(site, cfg)
        # 爬虫源的耗时含脚本下载，不代表视频接入速度；记录但排序时降到最后
        if not site.get("_latency_ms"):
            site["_latency_ms"] = int(round((time.time() - t0) * 1000))
        return ok, note

    # 影视仓标准：先用「列表接口」判活（首页能否列出视频）。
    # 只有列表接口拿到真视频，才标记 _home_visible=True，才可能进主订阅。
    if cfg.get("home_list_check", True):
        vis, vis_note, vis_ms, net_err = probe_home_visible(api, cfg)
        if vis:
            site["_latency_ms"] = vis_ms
            site["_probe_kind"] = "vod"
            site["_home_visible"] = True
            return True, "影视仓可见(列表接口): " + vis_note
        if net_err:
            # 列表接口全网络失败：仅用单关键词回退搜索一次，让站至少进全量订阅保命，
            # 但不做多关键词重试，避免每个失效站白等 N 次超时拖慢整体。
            site["_latency_ms"] = vis_ms
            fallback_cfg = dict(cfg)
            fallback_cfg["probe_keyword_limit"] = 1
            ok, note, _ = probe_http_api(api, fallback_cfg, site=site)
            return ok, note

    # 列表接口有响应但拿不到视频 → 回退搜索接口兜底（仍算存活，但不进主订阅）
    ok, note, _ = probe_http_api(api, cfg, site=site)
    return ok, note


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


def speed_sort_key(site):
    """速度排序键：拿到真视频 > 空结果 > 爬虫源未测内容；同级按响应耗时升序。

    TVBox 展示订阅时逐个加载站点，响应越快的站体验越流畅。爬虫源没有测过
    真实视频接入速度（只验证了依赖可达），因此排在最后，不占用快速名额。
    """
    kind = site.get("_probe_kind", "")
    if kind == "vod":
        tier = 0
    elif kind == "empty":
        tier = 1
    elif kind == "crawler":
        tier = 2
    else:
        tier = 3
    return (tier, int(site.get("_latency_ms", 0)))


def sort_sites_by_speed(sites):
    """按接入速度从高到低排序（真视频优先、耗时小者优先）。"""
    return sorted(sites, key=speed_sort_key)


def main_rank_key(site):
    """主订阅优先级排序键。

    影视仓可见真视频 > 搜索接口真视频 > 标准空结果站 > 爬虫/其他；同级按响应耗时升序。
    「可见站优先」保证影视仓能列出的站一定排在最前，其余存活站按顺序补足到 60。
    """
    visible = bool(site.get("_home_visible"))
    kind = site.get("_probe_kind", "")
    if visible and kind == "vod":
        tier = 0
    elif kind == "vod":
        tier = 1
    elif kind == "empty":
        tier = 2
    else:
        tier = 3
    return (tier, int(site.get("_latency_ms", 0) or 0))


def pick_main_sites(alive_sites, cfg, include_adult=False):
    """从全部存活站里选出主订阅候选。

    先按 main_rank_key 分层排序（影视仓可见站优先），再取前 main_top_n 个。
    存活站不足 top_n 时有多少取多少。默认只在非成人站中选（成人单独订阅）。
    返回 (完整分层排序列表, Top N 列表)，均为站点对象引用（尚未清理内部字段）。
    """
    top_n = int(cfg.get("main_top_n", 60) or 0)
    pool = [s for s in alive_sites if include_adult or not s.get("_adult")]
    ranked = sorted(pool, key=main_rank_key)
    return ranked, (ranked[:top_n] if top_n > 0 else list(ranked))


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


def _clean_site_for_output(s, cfg):
    """清理站点的内部字段并加标注（脱敏/爬虫标记），返回 (站点, 是否成人, 是否爬虫, 是否还原)。"""
    mode = adult_mode(cfg)
    is_ad = bool(s.pop("_adult", False)) and mode == "mark"
    is_crawler = bool(s.pop("_crawler", False))
    restored = bool(s.pop("_restored_from", None))
    s.pop("_source", None)
    s.pop("_sources", None)
    s.pop("_source_url", None)
    s.pop("_leaf_spider", None)
    s.pop("_latency_ms", None)   # 速度数据不写进订阅文件
    s.pop("_probe_kind", None)
    s.pop("_home_visible", None)
    if is_ad:
        s["name"] = mark_adult_name(s.get("name", ""), cfg)
    # 爬虫源在名称前加标注，便于在影视仓里识别（接口已还原的不标）。
    # 放在成人脱敏之后，避免成人站的 1BJ 代号覆盖掉爬虫标注。
    crawler_tag = cfg.get("crawler_name_tag", "[爬]")
    if is_crawler and not restored and crawler_tag:
        nm = str(s.get("name", ""))
        if crawler_tag not in nm:
            s["name"] = crawler_tag + nm
    return s, is_ad, is_crawler, restored


def _make_sub(base, sites, cfg, placement, stamp, extra=None):
    """按统一模板构造一个订阅对象。"""
    mode = adult_mode(cfg)
    sub = dict(base)
    sub["sites"] = sites
    notes = {
        "separate": "成人站点已拆分至单独订阅",
        "mix": "成人站点带标注混排",
        "report_only": "成人站点仅在报告展示，未写入订阅",
        "all": "全量存活站点（按接入速度从高到低排序）",
    }
    info = {
        "updated": stamp,
        "site_count": len(sites),
        "adult_mode": mode,
        "adult_placement": placement,
        "note": notes.get(placement, ""),
    }
    if extra:
        info.update(extra)
    sub["_maintainer"] = info
    return sub


def merge_subscription(leaves, alive_sites, cfg):
    """生成三份订阅：主订阅(Top N)、全量订阅、成人订阅。

    alive_sites 需已按速度排序（sort_sites_by_speed）。
    返回 (主订阅, 全量订阅, 成人订阅或None, 成人站点数)。
    - 主订阅 output/subscribe.json：常规站点里速度最快的前 main_top_n 个（默认60）
    - 全量订阅 output/subscribe_all.json：全部常规存活站点，按速度排序
    - 成人订阅 output/subscribe_adult.json：带 🔞 标注的成人站点（不受 Top N 限制）
    """
    mode = adult_mode(cfg)
    placement = adult_placement(cfg) if mode == "mark" else "mix"
    base = build_base_config(leaves, cfg)
    top_n = int(cfg.get("main_top_n", 60) or 0)

    # 清理前先按「影视仓可见优先 + 分层速度」选出主订阅入选站（常规站，对象引用）
    _, main_selected = pick_main_sites(alive_sites, cfg, include_adult=False)

    normal_sites, adult_sites = [], []
    adult_pool = []          # 混排模式下主订阅的成人可见站
    cleaned_by_id = {}
    home_visible_count = 0   # 影视仓可见真视频站总数（常规）
    for s in alive_sites:
        # 必须在 _clean_site_for_output 清除内部标记之前读取
        visible = bool(s.get("_home_visible"))
        kind = s.get("_probe_kind", "")
        if visible and kind == "vod" and not s.get("_adult"):
            home_visible_count += 1
        cleaned, is_ad, _is_crawler, _restored = _clean_site_for_output(s, cfg)
        cleaned_by_id[id(s)] = cleaned
        if is_ad:
            adult_sites.append(cleaned)
            if visible and kind == "vod":
                adult_pool.append(cleaned)
        else:
            normal_sites.append(cleaned)

    # 主订阅入选站：按分层优先级顺序（可见站优先，其余存活站补足），取清理后的对象
    main_pool = [cleaned_by_id[id(s)] for s in main_selected]

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    # 全量订阅：所有常规站点，按传入顺序（已按速度排序）
    full_sub = _make_sub(base, normal_sites, cfg, "all", stamp,
                         {"total_before_top": len(normal_sites)})
    # 主订阅：分层补足到 Top N（可见真视频站优先 → 搜索真视频 → 空结果 → 爬虫）
    main_sub = _make_sub(base, main_pool, cfg, placement, stamp,
                         {"top_n": top_n if top_n > 0 else None,
                          "full_site_count": len(normal_sites),
                          "home_visible_pool": home_visible_count})

    if placement == "separate":
        adult_sub = _make_sub(base, adult_sites, cfg, "separate", stamp,
                              {"adult_only": True})
        return main_sub, full_sub, adult_sub, len(adult_sites)

    if placement == "mix":
        # 混排：全量订阅含所有站；主订阅 = 分层补足的常规 Top N + 可见成人站
        mixed = normal_sites + adult_sites
        main_mixed = main_pool + list(adult_pool)
        main_sub = _make_sub(base, main_mixed, cfg, "mix", stamp,
                             {"adult_marked": len(adult_sites),
                              "home_visible_pool": home_visible_count})
        full_sub = _make_sub(base, mixed, cfg, "all", stamp, {"adult_marked": len(adult_sites)})
        return main_sub, full_sub, None, len(adult_sites)

    # report_only：成人站点不写入任何订阅，仅在报告展示
    return main_sub, full_sub, None, len(adult_sites)


def write_output(cfg, subscription, report_lines, adult_subscription=None, full_subscription=None):
    def resolve(p):
        return p if os.path.isabs(p) else os.path.join(BASE_DIR, p)

    # 主订阅（Top N）
    p = resolve(cfg["output"])
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(subscription, f, ensure_ascii=False, indent=2)
    log("已写出主订阅: %s (%d 站)" % (cfg["output"], len(subscription.get("sites", []))))

    # 全量订阅（所有存活站，按速度排序）
    all_path = cfg.get("all_output", "output/subscribe_all.json")
    if full_subscription is not None:
        ap = resolve(all_path)
        os.makedirs(os.path.dirname(ap), exist_ok=True)
        with open(ap, "w", encoding="utf-8") as f:
            json.dump(full_subscription, f, ensure_ascii=False, indent=2)
        log("已写出全量订阅: %s (%d 站)" % (all_path, len(full_subscription.get("sites", []))))

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
        report.append("| %d | %s | %s | %s |" % (
            depth, md_cell(url), md_cell(mask_if_adult(name, cfg)), md_cell(status)))
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
    alive = sort_sites_by_speed(alive)   # 按接入速度从高到低排序
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
    crawler_alive = [d for d in details if d[0] and "爬虫源" in (d[3] or "")]
    crawler_restored = [d for d in crawler_alive if "接口还原有效" in (d[3] or "")]
    report.append("- 存活站点(去重后): %d" % len(alive))
    report.append("- 存活率: %.1f%%" % ((len(alive) / total * 100) if total else 0))
    report.append("- 其中成人内容存活(已标注): %d" % len(adult_alive_details))
    report.append("- 其中爬虫源保留: %d（接口还原有效 %d / 依赖可达待验证 %d）" % (
        len(crawler_alive), len(crawler_restored), len(crawler_alive) - len(crawler_restored)))

    top_n = int(cfg.get("main_top_n", 60) or 0)
    normal_alive = [s for s in alive if not s.get("_adult")]
    home_pool = [s for s in normal_alive
                 if s.get("_home_visible") and s.get("_probe_kind") == "vod"]
    ranked_normal, main_selected = pick_main_sites(alive, cfg, include_adult=False)
    main_count = len(main_selected)
    # 主订阅入选里各层数量
    n_visible = sum(1 for s in main_selected
                    if s.get("_home_visible") and s.get("_probe_kind") == "vod")
    n_vod = sum(1 for s in main_selected
                if (not s.get("_home_visible")) and s.get("_probe_kind") == "vod")
    n_empty = sum(1 for s in main_selected if s.get("_probe_kind") == "empty")
    n_crawler = main_count - n_visible - n_vod - n_empty
    report.append("- 影视仓可见站(列表接口验证有视频): %d 站" % len(home_pool))
    report.append("- 主订阅(展示用): 补足至 %d 站（可见真视频 %d + 搜索真视频 %d + 空结果 %d + 其他 %d；常规存活 %d）" % (
        main_count, n_visible, n_vod, n_empty, n_crawler, len(normal_alive)))
    report.append("- 全量订阅: 全部 %d 站，按接入速度从高到低排序" % len(normal_alive))
    report.append("")

    # 速度排行榜：主订阅入选的快速站点（与订阅文件严格一致）
    if cfg.get("speed_report", True):
        report.append("## 最快站点 Top %d（主订阅入选，可见站优先+分层补足，按响应耗时升序）" % main_count)
        report.append("")
        report.append("| 排名 | 站点 | 响应耗时 | 测活结果 | 网址(api) |")
        report.append("| --- | --- | --- | --- | --- |")
        for rank, s in enumerate(main_selected, 1):
            lat = s.get("_latency_ms", 0)
            kind = s.get("_probe_kind", "")
            kind_label = {"vod": "有效视频", "empty": "空结果", "crawler": "爬虫未测内容"}.get(kind, kind or "-")
            if s.get("_home_visible") and kind == "vod":
                kind_label = "影视仓可见·" + kind_label
            elif kind == "vod":
                kind_label = "搜索验证·" + kind_label
            disp = mark_adult_name(s.get("name", "?"), cfg) if s.get("_adult") else s.get("name", "?")
            report.append("| %d | %s | %d ms | %s | %s |" % (
                rank, md_cell(disp), lat, kind_label, md_cell(s.get("api", ""))))
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
                    "存活" if ok else "失效", md_cell(mark_adult_name(name, cfg)),
                    md_cell(api), md_cell(src), md_cell(why)))
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
            "存活" if ok else "失效", md_cell(disp), md_cell(api), md_cell(src), md_cell(why)))

    # 3) 生成订阅（主订阅Top N + 全量订阅 + 成人订阅）
    subscription, full_subscription, adult_subscription, adult_alive = merge_subscription(leaves, alive, cfg)
    placement = adult_placement(cfg) if amode == "mark" else "exclude"
    adult_path = cfg.get("adult_output", "output/subscribe_adult.json")
    all_path = cfg.get("all_output", "output/subscribe_all.json")
    place_desc = {
        "separate": "拆分至单独订阅 %s" % adult_path,
        "mix": "带标注混排进主订阅 %s" % cfg["output"],
        "report_only": "仅在报告展示，未写入订阅",
        "exclude": "已过滤",
    }[placement]
    report.insert(3, "- 成人站点去向: %s（共 %d 个存活）" % (place_desc, adult_alive))
    report.insert(4, "- 订阅文件: 主订阅(Top N) %s；全量订阅 %s" % (cfg["output"], all_path))
    write_output(cfg, subscription, report, adult_subscription, full_subscription)
    log("完成。")
    return 0 if alive else 2


if __name__ == "__main__":
    sys.exit(main())

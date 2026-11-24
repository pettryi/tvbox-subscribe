#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地网页校验界面（纯标准库，零依赖）

启动： python web_check.py
然后浏览器打开： http://127.0.0.1:8899

功能：粘贴/批量提交影视源地址 -> 自动校验 -> 展示结果与存活明细
      -> 一键加入手工订阅清单 -> 查看/删除清单
"""
import os
import sys
import json
import socket
import argparse
import threading
import http.server
import socketserver
from urllib.parse import urlparse, parse_qs

import checker
import main as M

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>影视源校验工具</title>
<style>
  :root{--bg:#f7f7f5;--card:#fff;--line:#e3e3df;--txt:#222;--muted:#666;
        --ok:#1d9e75;--warn:#ba7517;--bad:#d4537e;--brand:#378add;--brandbg:#e6f1fb}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
       font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
       font-size:14px;line-height:1.6}
  .wrap{max-width:860px;margin:0 auto;padding:28px 18px 60px}
  h1{font-size:20px;font-weight:600;margin:0 0 4px}
  .sub{color:var(--muted);font-size:13px;margin-bottom:22px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;
        padding:20px;margin-bottom:18px}
  textarea{width:100%;min-height:96px;border:1px solid var(--line);border-radius:8px;
        padding:12px;font-size:13px;resize:vertical;font-family:inherit;outline:none}
  textarea:focus{border-color:var(--brand)}
  .row{display:flex;gap:10px;align-items:center;margin-top:12px;flex-wrap:wrap}
  button{background:var(--brand);color:#fff;border:none;border-radius:8px;
        padding:9px 18px;font-size:14px;cursor:pointer}
  button:hover{opacity:.9}
  button.ghost{background:#fff;color:var(--txt);border:1px solid var(--line)}
  button:disabled{opacity:.5;cursor:not-allowed}
  label.chk{display:flex;align-items:center;gap:6px;color:var(--muted);cursor:pointer;font-size:13px}
  .result{border-left:3px solid var(--line);padding:10px 14px;margin:12px 0;
        background:#fafaf8;border-radius:0 8px 8px 0}
  .result.ok{border-color:var(--ok)}
  .result.warn{border-color:var(--warn)}
  .result.bad{border-color:var(--bad)}
  .rtitle{font-weight:600;word-break:break-all}
  .badge{display:inline-block;padding:1px 9px;border-radius:99px;font-size:12px;
        color:#fff;margin-right:8px;vertical-align:middle}
  .badge.ok{background:var(--ok)}.badge.warn{background:var(--warn)}.badge.bad{background:var(--bad)}
  .meta{color:var(--muted);font-size:12px;margin-top:4px}
  .samples{font-size:12px;color:#444;margin-top:6px;max-height:130px;overflow:auto}
  .samples div{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .url{color:var(--brand);word-break:break-all}
  .adultbox{margin-top:8px;background:#fff7ed;border:1px solid #f0d9b5;
            border-radius:8px;padding:8px 12px}
  .adultbox .hd{font-size:12px;color:var(--warn);font-weight:600;margin-bottom:4px}
  .adultbox .samples{color:#7a5b22;max-height:150px}
  .adultbox .samples div{white-space:normal;word-break:break-all;margin:2px 0}
  ul.manual{margin:8px 0 0;padding-left:20px}
  ul.manual li{margin:3px 0;word-break:break-all}
  .rm{color:var(--bad);cursor:pointer;font-size:12px;margin-left:8px}
  .hint{color:var(--muted);font-size:12px}
  #status{color:var(--muted);font-size:13px;margin-top:8px}
  .subbox{background:var(--brandbg);border:1px solid #bcd8f5;border-radius:12px;
          padding:18px 20px;margin-bottom:18px}
  .subbox h2{font-size:15px;margin:0 0 6px;color:#1a5c96}
  .subbox p{color:var(--muted);font-size:12px;margin:0 0 10px}
  .subline{display:flex;gap:8px;align-items:center;margin:6px 0}
  .subline code{flex:1;background:#fff;border:1px solid var(--line);border-radius:6px;
          padding:7px 10px;font-size:12px;word-break:break-all;user-select:all}
  .copybtn{background:var(--brand);color:#fff;border:none;border-radius:6px;
          padding:7px 12px;font-size:12px;cursor:pointer;white-space:nowrap}
</style>
</head>
<body>
<div class="wrap">
  <h1>影视源校验工具</h1>
  <div class="sub">提交地址自动展开、测活、去重；通过的源可加入每日自动维护清单。</div>

  <div class="subbox" id="subbox">
    <h2>📺 影视仓订阅地址</h2>
    <p id="subhint">手机与本机在同一 WiFi 下，复制下面链接填入影视仓「设置 → 配置地址」。</p>
    <div class="subline"><code id="suburl">—</code><button class="copybtn" id="copymain">复制主订阅</button></div>
    <div class="subline"><code id="suburladult">—</code><button class="copybtn" id="copyadult">复制成人订阅</button></div>
  </div>

  <div class="card">
    <textarea id="urls" placeholder="粘贴影视源地址，每行一个。例如：&#10;https://example.com/config.json&#10;https://example.com/dc.txt"></textarea>
    <div class="row">
      <button id="run">开始校验</button>
      <label class="chk"><input type="checkbox" id="noprob"> 只解析不测活（更快）</label>
      <label class="chk"><input type="checkbox" id="addon"> 通过的源自动加入清单</label>
      <button class="ghost" id="clear">清空</button>
    </div>
    <div id="status"></div>
  </div>

  <div id="results"></div>

  <div class="card">
    <div class="row" style="justify-content:space-between;margin:0">
      <strong>手工订阅清单</strong>
      <button class="ghost" id="refresh">刷新</button>
    </div>
    <div id="manual"></div>
  </div>

  <div class="card hint">
    提示：本工具只做链接可用性检测与配置生成，不托管任何影视内容。
    通过校验的地址写入 manual_sources.txt，下次运行 main.py 时自动纳入订阅。
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
function cls(v){return v==='可用'?'ok':v==='部分可用'?'warn':'bad'}
function esc(s){return String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}

$('run').onclick = async () => {
  const text = $('urls').value.trim();
  if(!text){$('status').textContent='请先粘贴地址';return}
  const urls = text.split(/\n+/).map(s=>s.trim()).filter(Boolean);
  const noProbe = $('noprob').checked, addOn = $('addon').checked;
  $('run').disabled=true; $('status').textContent=`正在校验 ${urls.length} 个地址…`;
  $('results').innerHTML='';
  let all=[];
  for(let i=0;i<urls.length;i++){
    $('status').textContent=`校验中 ${i+1}/${urls.length}: ${urls[i]}`;
    try{
      const r = await fetch('/api/check',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({url:urls[i],probe:!noProbe})}).then(x=>x.json());
      all.push(r);
      const div=document.createElement('div');
      div.className='result '+cls(r.verdict);
      let s=`<div class="rtitle"><span class="badge ${cls(r.verdict)}">${r.verdict}</span>${esc(r.url)}</div>`;
      const adultWord=(r.adult_mode==='mark'?'成人标注':'成人过滤');
      s+=`<div class="meta">${esc(r.reason)} · 类型 ${esc(r.kind)} · 叶仓 ${r.leaf_count} · 站点 ${r.site_count} · 存活 ${r.alive_count} · 合并 ${r.dup_merged} · ${adultWord} ${r.adult_count} · ${r.elapsed}s</div>`;
      if(r.samples&&r.samples.length){
        s+=`<div class="samples">`+r.samples.map(x=>`<div>· ${esc(String(x[0]).slice(0,30))} — ${esc(String(x[1]).slice(0,70))}</div>`).join('')+`</div>`;
      }
      if(r.adult_alive&&r.adult_alive.length){
        s+=`<div class="adultbox"><div class="hd">🔞 成人内容存活明细（已标注，照常测活）· ${r.adult_alive.length} 条</div><div class="samples">`
           +r.adult_alive.map(x=>`<div>🔞 ${esc(String(x[0]).slice(0,30))} — ${esc(String(x[1]))}</div>`).join('')
           +`</div></div>`;
      }
      if(r.suggest_add){
        s+=`<div class="row" style="margin-top:8px"><button class="ghost addbtn" data-u="${esc(r.url)}">加入清单</button></div>`;
      }
      div.innerHTML=s; $('results').appendChild(div);
      if(addOn && r.suggest_add){
        await fetch('/api/add',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({url:r.url})});
      }
    }catch(e){
      const div=document.createElement('div');div.className='result bad';
      div.innerHTML=`<div class="rtitle"><span class="badge bad">错误</span>${esc(urls[i])}</div><div class="meta">${esc(String(e))}</div>`;
      $('results').appendChild(div);
    }
  }
  $('run').disabled=false; $('status').textContent=`完成，共校验 ${all.length} 个地址`;
  loadManual();
  document.querySelectorAll('.addbtn').forEach(b=>b.onclick=async e=>{
    await fetch('/api/add',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({url:e.target.dataset.u})});
    e.target.textContent='已加入';e.target.disabled=true;loadManual();
  });
};
$('clear').onclick=()=>{$('urls').value='';$('results').innerHTML='';$('status').textContent=''};
$('refresh').onclick=loadManual;
async function loadManual(){
  const data=await fetch('/api/manual').then(x=>x.json());
  const ul=document.createElement('ul');ul.className='manual';
  if(!data.length){ul.innerHTML='<li class="hint">（空）</li>'}
  data.forEach(u=>{
    const li=document.createElement('li');
    li.innerHTML=`<span class="url">${esc(u)}</span><span class="rm" data-u="${esc(u)}">删除</span>`;
    ul.appendChild(li);
  });
  $('manual').innerHTML='';$('manual').appendChild(ul);
  document.querySelectorAll('.rm').forEach(x=>x.onclick=async e=>{
    await fetch('/api/remove',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({url:e.target.dataset.u})});
    loadManual();
  });
}
loadManual();

// 填充影视仓订阅地址（局域网）
(async () => {
  try{
    const info = await fetch('/api/info').then(x=>x.json());
    const base = info.base || location.origin;
    $('suburl').textContent = base + '/subscribe';
    $('suburladult').textContent = base + '/subscribe_adult';
    if(info.lan){
      $('subhint').textContent = '手机与本机在同一 WiFi 下，复制下面链接填入影视仓「设置 → 配置地址」（电脑需放行防火墙 8899 端口）。';
    }else{
      $('subhint').textContent = '当前仅本机可访问。启动时加 --lan 参数即可用手机在同一 WiFi 下订阅。';
    }
  }catch(e){ $('suburl').textContent=location.origin+'/subscribe'; $('suburladult').textContent=location.origin+'/subscribe_adult'; }
  const cp=(txt,btn)=>{ navigator.clipboard.writeText(txt).then(()=>{const o=btn.textContent;btn.textContent='已复制';setTimeout(()=>btn.textContent=o,1500);}).catch(()=>{}); };
  $('copymain').onclick=()=>cp($('suburl').textContent,$('copymain'));
  $('copyadult').onclick=()=>cp($('suburladult').textContent,$('copyadult'));
})();
</script>
</body>
</html>"""


def get_lan_ip():
    """探测本机局域网 IP（UDP 连接外部地址不真正发包，仅用于拿本地出口网卡 IP）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"
    finally:
        s.close()


class Handler(http.server.BaseHTTPRequestHandler):
    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/manual":
            self.send_json(checker.manual_list())
        elif path == "/api/info":
            lan = bool(getattr(self.server, "lan_mode", False))
            ip = get_lan_ip()
            base = ("http://%s:%d" % (ip, self.server.server_address[1])) if lan else (
                "http://127.0.0.1:%d" % self.server.server_address[1])
            self.send_json({"lan": lan, "ip": ip, "base": base})
        elif path in ("/subscribe", "/subscribe_adult"):
            # 直接提供订阅文件，供影视仓订阅（绕开 Gitee raw 的内容审查）
            fname = "subscribe.json" if path == "/subscribe" else "subscribe_adult.json"
            p = os.path.join(BASE_DIR, "output", fname)
            if not os.path.exists(p):
                self.send_json({"error": "订阅文件尚未生成，请先运行 main.py"}, 404)
                return
            with open(p, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        cfg = M.load_config("config.json")
        try:
            data = self.read_json()
        except Exception:
            self.send_json({"error": "请求格式错误"}, 400)
            return
        if path == "/api/check":
            url = data.get("url", "")
            probe = data.get("probe", True)
            try:
                r = checker.check_url(url, cfg, do_probe=bool(probe))
                self.send_json(r)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        elif path == "/api/add":
            url = data.get("url", "")
            ok, msg = checker.add_to_manual(url, cfg)
            self.send_json({"ok": ok, "msg": msg})
        elif path == "/api/remove":
            url = data.get("url", "")
            changed = checker.remove_from_manual(url)
            self.send_json({"ok": changed})
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass


def main():
    ap = argparse.ArgumentParser(description="影视源校验网页界面")
    ap.add_argument("-p", "--port", type=int, default=8899, help="端口，默认 8899")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机")
    ap.add_argument("--lan", action="store_true", help="允许局域网访问（手机订阅用），等价于 --host 0.0.0.0")
    args = ap.parse_args()

    host = "0.0.0.0" if args.lan else args.host

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True

    with Server((host, args.port), Handler) as httpd:
        httpd.lan_mode = args.lan
        ip = get_lan_ip()
        print("影视源校验界面已启动： http://%s:%d" % (host, args.port), flush=True)
        print("按 Ctrl+C 停止", flush=True)
        if args.lan:
            print("=" * 56, flush=True)
            print("📺 影视仓订阅地址（手机连同一 WiFi 即可用）：", flush=True)
            print("   主订阅：   http://%s:%d/subscribe" % (ip, args.port), flush=True)
            print("   成人订阅： http://%s:%d/subscribe_adult" % (ip, args.port), flush=True)
            print("   若手机连不上，请在 Windows 防火墙放行 %d 端口。" % args.port, flush=True)
            print("=" * 56, flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止", flush=True)


if __name__ == "__main__":
    main()

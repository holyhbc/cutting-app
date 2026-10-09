# -*- coding: utf-8 -*-
"""扫码报工 · Web 服务（阶段 1）。

只用标准库，与 hbc-print 保持一致，不引入额外依赖。
不连 SQL Server，数据来自本地 SQLite 快照（决策 D-009）。

启动：
  python app.py                # 监听 127.0.0.1:10080（默认，推荐）
  PORT=9000 python app.py                    # 换端口
  BIND_HOST=0.0.0.0 python app.py           # 监听所有网卡（需自行确认防火墙）

接口：
  GET  /                      说明页
  GET  /s/{short_id}          扫码落地页（工人用）
  GET  /api/bundle/{short_id} 一扎的工序/已报/剩余
  POST /api/report            提交报工
  GET  /health                健康检查
  GET  /admin/pending         待同步报工（调试用）
"""
import json
import os
import sys
import io
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db as D
import rules as R

PORT = int(os.environ.get("PORT", "10080"))
# 默认只监听本机，由 nginx 反向代理对外提供 HTTPS。
# 只有确实需要局域网直连（例如车间内网自测）时才设 BIND_HOST=0.0.0.0。
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
# 局域网同步器推送快照时用的令牌。没配就拒绝该接口，不给未鉴权的写入口。
SYNC_TOKEN = os.environ.get("SYNC_TOKEN", "")
MAX_SNAPSHOT_BYTES = int(os.environ.get("MAX_SNAPSHOT_BYTES", str(64 * 1024 * 1024)))

CSS = """
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{max-width:100%;overflow-x:hidden}
body{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
     background:#f2f3f5;color:#1a1a1a;padding:0 0 40px}
.wrap{width:100%;max-width:560px;margin:0 auto;padding:16px}
.card{background:#fff;border-radius:14px;padding:18px;margin-bottom:14px;
      box-shadow:0 2px 10px rgba(0,0,0,.06)}
h1{font-size:20px;margin:0 0 6px}
h2{font-size:17px;margin:0 0 12px}
.sub{color:#888;font-size:13px;margin:0 0 14px;line-height:1.6}
.kv{display:flex;justify-content:space-between;gap:10px;padding:9px 0;
    border-bottom:1px solid #f0f0f0;font-size:15px}
.kv:last-child{border-bottom:0}
.kv .k{color:#888;flex:0 0 auto}
.kv .v{font-weight:600;flex:1 1 auto;min-width:0;text-align:right;word-break:break-all}
.big{font-size:30px;font-weight:700;color:#0a84ff}
label{display:block;font-size:14px;color:#666;margin:14px 0 6px}
input{width:100%;padding:12px;font-size:17px;border:1px solid #ddd;border-radius:10px;
      background:#fafafa}
input:focus{outline:0;border-color:#0a84ff;background:#fff}
button{width:100%;padding:14px;font-size:17px;font-weight:600;border:0;border-radius:12px;
       background:#0a84ff;color:#fff;margin-top:16px}
button:disabled{background:#ccc}
button.ghost{background:#f0f0f0;color:#333}
.proc{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:13px;
      border:1.5px solid #eee;border-radius:11px;margin-bottom:9px;cursor:pointer}
.proc>div{flex:1 1 auto;min-width:0}
.proc.sel{border-color:#0a84ff;background:#f0f8ff}
.proc.done{opacity:.5}
.pn{font-weight:600;font-size:16px;word-break:break-all}
.ps{font-size:12px;color:#888;margin-top:3px}
.pill{font-size:12px;padding:4px 9px;border-radius:20px;background:#f0f0f0;color:#666;
      white-space:nowrap;flex:0 0 auto}
.tip{margin-top:8px;font-size:13px;color:#999}
.tip.ok{color:#28a745}
.tip.warn{color:#d9534f}
/* 弹窗 */
.mask{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);
      align-items:center;justify-content:center;z-index:99;padding:20px}
.modal{background:#fff;border-radius:16px;padding:24px 20px;max-width:380px;
       width:100%;text-align:center;box-shadow:0 8px 30px rgba(0,0,0,.25)}
.mico{width:60px;height:60px;border-radius:50%;background:#28a745;color:#fff;
      font-size:36px;line-height:60px;margin:0 auto 12px}
.mico.warn{background:#d9534f}
.mtitle{font-size:19px;font-weight:700;margin-bottom:10px}
.mbody{font-size:15px;color:#444;line-height:1.7;margin-bottom:18px;
       word-break:break-all}
.mbtn{width:100%;padding:13px;font-size:16px;font-weight:600;border:0;
      border-radius:11px;background:#0a84ff;color:#fff;margin-top:8px}
.mbtn.ghostm{background:#f0f0f0;color:#333}
#cam{width:100%;border-radius:11px;background:#000;aspect-ratio:1;object-fit:cover}
.pill.ok{background:#e8f7ed;color:#28a745}
.pill.no{background:#ffeaea;color:#d9534f}
.err{background:#ffeaea;color:#c0392b;padding:12px;border-radius:10px;
     font-size:14px;margin-bottom:12px;line-height:1.6}
.ok-msg{background:#e8f7ed;color:#1e7e34;padding:12px;border-radius:10px;
        font-size:14px;margin-bottom:12px;line-height:1.6}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:7px 5px;border-bottom:1px solid #f0f0f0;text-align:left}
th{color:#888;font-weight:500}
.muted{color:#999;font-size:12px}
"""


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def page_scan(smap, view, err="", done=""):
    """扫码落地页。工人只需：填工号 → 选工序 → 填数量 → 提交。"""
    procs = view["processes"]
    rows = []
    for p in procs:
        cls = "proc" + (" done" if p["done"] else "")
        pill = ('<span class="pill ok">已做完</span>' if p["done"] else
                f'<span class="pill no">剩 {p["remain"]}</span>')
        rows.append(
            f'<div class="{cls}" data-gx="{p["gx"]}" data-remain="{p["remain"]}" '
            f'data-name="{esc(p["gxname"])}">'
            f'<div><div class="pn">{p["gx"]}. {esc(p["gxname"])}</div>'
            f'<div class="ps">单价 {p["dj"]:.4f} 元/件</div></div>{pill}</div>')

    plan = view["plan_qty"]
    # 成功弹窗的正文要真的带上回执内容，否则工人只看到空的"报工成功"。
    ok_mask = ""
    if done:
        ok_mask = (
            '<div class="mask" id="okmask"><div class="modal">'
            '<div class="mico">✓</div>'
            '<div class="mtitle">报工成功</div>'
            f'<div class="mbody" id="okbody">{esc(done)}</div>'
            '<button class="mbtn" onclick="closeOk()">继续报这扎</button>'
            '<button class="mbtn ghostm" onclick="closeOk();openScan()">扫下一扎</button>'
            '</div></div>')
    err_mask = ""
    if err and not done:
        err_mask = (
            '<div class="mask" id="errmask" style="display:flex"><div class="modal">'
            '<div class="mico warn">!</div>'
            '<div class="mtitle">提交失败</div>'
            f'<div class="mbody">{esc(err)}</div>'
            '<button class="mbtn" onclick="closeErr()">知道了</button>'
            '</div></div>')
    return f"""<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>扫码报工</title><style>{CSS}</style></head><body><div class="wrap">

<div class="card">
  <h1>扫码报工</h1>
  <p class="sub">点工序会自动填好剩余数量，按需修改后提交。</p>
  <div class="kv"><span class="k">定单</span><span class="v">{esc(smap["zdno"])}</span></div>
  <div class="kv"><span class="k">扎号</span><span class="v">第 {smap["zh"]} 扎（{smap["cc"]} 层）</span></div>
  <div class="kv"><span class="k">应做数量</span><span class="v big">{plan} 件</span></div>
</div>

{"<div class='err'>" + esc(err) + "</div>" if err else ""}

<form method="POST" action="/api/report" id="f">
<input type="hidden" name="short_id" value="{esc(smap['short_id'])}">
<input type="hidden" name="zdno" value="{esc(smap['zdno'])}">
<input type="hidden" name="cc" value="{smap['cc']}">
<input type="hidden" name="zh" value="{smap['zh']}">
<input type="hidden" name="gx" id="gx" value="">

<div class="card">
  <h2>1. 你的工号</h2>
  <input name="ygno" id="ygno" placeholder="例如 A001" autocomplete="off"
         inputmode="text" required>
</div>

<div class="card">
  <h2>2. 选工序</h2>
  <div id="plist">{''.join(rows) if rows else '<p class="muted">这扎没有工序数据</p>'}</div>
</div>

<div class="card">
  <h2>3. 报工数量</h2>
  <label>件数（不是手数）</label>
  <input name="js" id="js" type="number" inputmode="numeric" min="1" step="1"
         placeholder="点上方工序会自动填入" required>
  <div class="tip" id="tip">还没选工序</div>
  <button type="submit" id="btn">提交报工</button>
  <button type="button" class="ghost" onclick="openScan()">📷 扫下一扎</button>
</div>
</form>

<div class="card">
  <p class="muted">标签二维码短ID：{esc(smap['short_id'])}</p>
</div>
</div>

{ok_mask}
{err_mask}

<div class="mask" id="scanmask">
  <div class="modal">
    <div class="mtitle">扫下一扎</div>
    <video id="cam" playsinline muted autoplay></video>
    <div class="mbody" id="scanmsg">把摄像头对准标签左下角的二维码</div>
    <button class="mbtn ghostm" onclick="closeScan()">取消</button>
  </div>
</div>

<script>
var okShown = {"true" if done else "false"};
function closeOk(){{ document.getElementById('okmask').style.display='none'; }}
function closeErr(){{ document.getElementById('errmask').style.display='none'; }}
document.addEventListener('DOMContentLoaded', function(){{
  if(okShown) closeOk();
  var m=document.getElementById('errmask'); if(m) m.style.display='flex';
}});

// 点工序：选中 + 自动填剩余数（不自动提交，避免误触）
document.querySelectorAll('#plist .proc').forEach(function(el){{
  el.onclick=function(){{
    if(el.classList.contains('done')){{
      var t=document.getElementById('tip');
      t.textContent='这道工序已做完，换一道吧';
      t.className='tip warn';
      return;
    }}
    document.querySelectorAll('#plist .proc').forEach(function(x){{x.classList.remove('sel');}});
    el.classList.add('sel');
    var gx=el.dataset.gx;
    document.getElementById('gx').value=gx;
    var remain=el.dataset.remain, nm=el.dataset.name;
    var js=document.getElementById('js');
    if(remain && parseInt(remain)>0){{ js.value=remain; }}
    var t=document.getElementById('tip');
    t.textContent='已选「'+nm+'」，剩余 '+remain+' 件';
    t.className='tip ok';
  }};
}});

// ---- 扫一扫 ----
var stream=null, raf=null, det=null;
function openScan(){{
  var m=document.getElementById('scanmask');
  m.style.display='flex';
  var v=document.getElementById('cam');
  var msg=document.getElementById('scanmsg');
  if(!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia){{
    msg.textContent='这个浏览器不支持摄像头，请用微信右上角「···」再扫一次';
    return;
  }}
  navigator.mediaDevices.getUserMedia({{video:{{facingMode:'environment'}}}}).then(function(s){{
    stream=s; v.srcObject=s;
    if('BarcodeDetector' in window){{
      det=new BarcodeDetector({{formats:['qr_code']}});
      tick();
    }} else {{
      msg.textContent='此浏览器不支持直接识别，请点右上角「···」用微信扫一扫';
    }}
  }}).catch(function(e){{
    msg.textContent='无法打开摄像头：'+e.message;
  }});
}}
function tick(){{
  var v=document.getElementById('cam');
  if(!det) return;
  det.detect(v).then(function(codes){{
    if(codes && codes.length){{
      var raw=codes[0].rawValue||'';
      var m=raw.match(/[/]s[/]([A-Z2-7]{8})/);
      if(m){{ location.href='/s/'+m[1]; return; }}
      location.href=raw; return;
    }}
    raf=requestAnimationFrame(tick);
  }}).catch(function(){{ raf=requestAnimationFrame(tick); }});
}}
function closeScan(){{
  document.getElementById('scanmask').style.display='none';
  if(stream){{ stream.getTracks().forEach(function(t){{t.stop();}}); stream=null; }}
  if(raf) cancelAnimationFrame(raf);
  var v=document.getElementById('cam'); if(v) v.srcObject=null;
}}
</script>
</body></html>"""



def page_notfound(sid=""):
    """二维码无效页。要说清原因，别只丢一句'无效'让工人一头雾水。"""
    return f"""<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>标签未同步</title><style>{CSS}</style></head><body><div class="wrap">
<div class="card">
  <div style="font-size:44px;text-align:center;line-height:1.2">🔄</div>
  <h1 style="text-align:center">这个标签还没同步</h1>
  <p class="sub" style="text-align:center">
    标签是在扫码服务器上找的。如果刚印了新标签或新建了定单，<br>
    需要班组长先同步一次才能扫。
  </p>
</div>
<div class="card">
  <h2>怎么办</h2>
  <p class="sub" style="margin:0">
    ① 先确认标签是不是刚印的<br>
    ② 找班组长在电脑上点「同步到扫码服务器」<br>
    ③ 同步完成后重新扫一次<br><br>
    如果已经同步过还是打不开，请把这串编号报给班组长：
  </p>
  <div class="kv"><span class="k">标签编号</span>
    <span class="v">{esc(sid) or '（无法识别）'}</span></div>
</div>
<div class="card">
  <button onclick="history.back()">← 返回上一页</button>
  <button class="ghost" onclick="openScan()">📷 直接扫一扫</button>
</div>
</div>
<div class="mask" id="scanmask">
  <div class="modal"><div class="mtitle">扫一扫</div>
  <video id="cam" playsinline muted autoplay></video>
  <div class="mbody" id="scanmsg">把摄像头对准标签左下角的二维码</div>
  <button class="mbtn ghostm" onclick="closeScan()">取消</button></div>
</div>
<script>
var stream=null,raf=null,det=null;
function openScan(){{
  var m=document.getElementById('scanmask'); m.style.display='flex';
  var v=document.getElementById('cam'),msg=document.getElementById('scanmsg');
  if(!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia){{
    msg.textContent='这个浏览器不支持摄像头，请用微信右上角「···」再扫一次'; return; }}
  navigator.mediaDevices.getUserMedia({{video:{{facingMode:'environment'}}}}).then(function(s){{
    stream=s; v.srcObject=s;
    if('BarcodeDetector' in window){{ det=new BarcodeDetector({{formats:['qr_code']}}); tick(); }}
    else msg.textContent='此浏览器不支持直接识别，请点右上角「···」用微信扫一扫';
  }}).catch(function(e){{ msg.textContent='无法打开摄像头：'+e.message; }});
}}
function tick(){{
  var v=document.getElementById('cam'); if(!det) return;
  det.detect(v).then(function(codes){{
    if(codes&&codes.length){{
      var raw=codes[0].rawValue||'';
      var mm=raw.match(/[/]s[/]([A-Z2-7]{8})/);
      location.href= mm?('/s/'+mm[1]) : raw; return;
    }}
    raf=requestAnimationFrame(tick);
  }}).catch(function(){{ raf=requestAnimationFrame(tick); }});
}}
function closeScan(){{
  document.getElementById('scanmask').style.display='none';
  if(stream){{ stream.getTracks().forEach(function(t){{t.stop();}}); stream=null; }}
  if(raf) cancelAnimationFrame(raf);
  var v=document.getElementById('cam'); if(v) v.srcObject=null;
}}
</script>
</body></html>"""


def page_home(st):
    return f"""<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>扫码报工服务</title><style>{CSS}</style></head><body><div class="wrap">
<div class="card">
  <h1>扫码报工服务</h1>
  <p class="sub">用微信扫描裁剪标签左下角的二维码即可报工。</p>
  <div class="kv"><span class="k">扫码映射</span><span class="v">{st['scan_map']}</span></div>
  <div class="kv"><span class="k">扎快照</span><span class="v">{st['bundle']}</span></div>
  <div class="kv"><span class="k">工序快照</span><span class="v">{st['process']}</span></div>
  <div class="kv"><span class="k">待同步</span><span class="v">{st['pending']}</span></div>
  <div class="kv"><span class="k">已同步</span><span class="v">{st['synced']}</span></div>
</div>
<div class="card"><p class="muted">健康检查：<a href="/health">/health</a></p></div>
</div></body></html>"""


def page_pending(rows):
    trs = "".join(
        f"<tr><td>{r['id']}</td><td>{esc(r['zdno'])}</td><td>{r['cc']}</td>"
        f"<td>{r['zh']}</td><td>{r['gx']} {esc(r['gxname'])}</td>"
        f"<td>{esc(r['ygno'])}</td><td>{r['js']}</td>"
        f"<td>{esc(r['gzdate'])}</td><td>{r['status']}</td></tr>" for r in rows)
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>待同步</title><style>{CSS}</style></head><body><div class="wrap">
<div class="card"><h2>待同步报工（{len(rows)} 条）</h2>
<table><tr><th>ID</th><th>定单</th><th>cc</th><th>扎</th><th>工序</th>
<th>工号</th><th>数量</th><th>时间</th><th>状态</th></tr>{trs}</table></div>
</div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "ScanReport/1.0"

    def _send(self, code, body, ctype="text/html; charset=utf-8", head_only=False):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        # HEAD 只回响应头，不写正文（否则客户端会一直等 body）
        if not head_only:
            self.wfile.write(data)

    def _json(self, obj, code=200, head_only=False):
        self._send(code, json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8", head_only=head_only)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---------------------------------------------------------- GET
    def do_GET(self):
        self._route_get(head_only=False)

    def do_HEAD(self):
        # `curl -sI` 走的就是 HEAD。BaseHTTPRequestHandler 对未实现的方法
        # 默认回 501 Not Implemented，会被误判成服务没起来，所以必须实现。
        self._route_get(head_only=True)

    def _route_get(self, head_only=False):
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        try:
            if path == "/":
                return self._send(200, page_home(D.stats()), head_only=head_only)
            if path == "/health":
                return self._json({"ok": True, "stats": D.stats()}, head_only=head_only)
            if path == "/api/pending":
                return self._handle_pending()
            if path == "/api/scanmap":
                return self._handle_scanmap()
            if path == "/admin/pending":
                return self._send(200, page_pending(D.list_pending(500)),
                                  head_only=head_only)
            if path.startswith("/api/bundle/"):
                sid = path.rsplit("/", 1)[-1]
                smap = D.get_scan_map(sid)
                if not smap:
                    return self._json({"ok": False, "msg": "二维码无效"}, 404,
                                      head_only=head_only)
                return self._json({"ok": True, "bundle": R.bundle_view(
                    smap["zdno"], smap["cc"], smap["zh"]), "map": smap}, head_only=head_only)
            if path.startswith("/s/"):
                sid = path.rsplit("/", 1)[-1]
                err = parse_qs(u.query).get("e", [""])[0]
                smap = D.get_scan_map(sid)
                if not smap:
                    return self._send(404, page_notfound(sid), head_only=head_only)
                view = R.bundle_view(smap["zdno"], smap["cc"], smap["zh"])
                return self._send(200, page_scan(smap, view, err=err), head_only=head_only)
            return self._send(404, "<h1>404</h1>", head_only=head_only)
        except Exception as e:
            D.log_audit("error", f"GET {path}: {e}", self.client_address[0])
            return self._send(500, f"<h1>500</h1><pre>{esc(e)}</pre>", head_only=head_only)

    def _auth_sync(self):
        """校验同步令牌。返回 True 表示通过。"""
        if not SYNC_TOKEN:
            D.log_audit("sync_denied", "服务端未配置 SYNC_TOKEN",
                        self.client_address[0])
            self._json({"ok": False, "msg": "服务端未配置 SYNC_TOKEN"}, 503)
            return False
        if self.headers.get("Authorization", "") != f"Bearer {SYNC_TOKEN}":
            D.log_audit("sync_denied", "令牌不正确", self.client_address[0])
            self._json({"ok": False, "msg": "令牌不正确"}, 401)
            return False
        return True

    def _handle_scanmap(self):
        """打印端问：某个定单同步过来没有？缺哪些扎？"""
        if not self._auth_sync():
            return
        u = urlparse(self.path)
        zdno = (parse_qs(u.query).get("zdno", [""])[0] or "").strip()
        if not zdno:
            return self._json({"ok": False, "msg": "缺少 zdno 参数"}, 400)
        rows = D.get_scan_map_by_zdno(zdno)
        return self._json({"ok": True, "zdno": zdno, "count": len(rows),
                           "zhs": [r["zh"] for r in rows]})

    def _handle_pending(self):
        """局域网同步器拉取待回写的报工。"""
        if not self._auth_sync():
            return
        u = urlparse(self.path)
        try:
            lim = int(parse_qs(u.query).get("limit", ["200"])[0])
            lim = max(1, min(lim, 2000))
        except ValueError:
            lim = 200
        rows = D.list_pending(lim)
        out = [{
            "id": r["id"], "short_id": r["short_id"],
            "zdno": r["zdno"], "cc": r["cc"], "zh": r["zh"],
            "gx": r["gx"], "gxname": r["gxname"],
            "ygno": r["ygno"], "ygname": r["ygname"],
            "js": r["js"], "dj": r["dj"],
            "gzdate": r["gzdate"], "created_at": r["created_at"],
        } for r in rows]
        self._json({"ok": True, "count": len(out), "reports": out})

    def _handle_ack(self):
        """同步器回报写入结果。"""
        if not self._auth_sync():
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n <= 0 or n > MAX_SNAPSHOT_BYTES:
                return self._json({"ok": False, "msg": f"体积异常 {n}"}, 413)
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
            done = fail = 0
            for r in payload.get("results") or []:
                rid = r.get("id")
                if not rid:
                    continue
                if r.get("status") == "synced":
                    D.mark_report(rid, "synced", jfgzid=r.get("jfgzid"))
                    done += 1
                else:
                    D.mark_report(rid, "failed", err=r.get("err", ""))
                    fail += 1
            D.log_audit("ack", f"synced={done} failed={fail}",
                        self.client_address[0])
            return self._json({"ok": True, "synced": done, "failed": fail,
                               "stats": D.stats()})
        except Exception as e:
            D.log_audit("ack_error", str(e), self.client_address[0])
            return self._json({"ok": False, "msg": str(e)}, 400)

    def _handle_snapshot(self):
        """局域网同步器推送只读快照。需 Bearer 令牌，防止外部乱写。"""
        ip = self.client_address[0]
        if not self._auth_sync():
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n <= 0 or n > MAX_SNAPSHOT_BYTES:
                return self._json({"ok": False, "msg": f"体积异常 {n}"}, 413)
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
            replace = str(payload.get("replace", "")).lower() in ("1", "true", "yes")
            counts = D.apply_snapshot(payload, replace=replace)
            D.log_audit("snapshot", f"replace={replace} {counts}", ip)
            return self._json({"ok": True, "applied": counts, "stats": D.stats()})
        except Exception as e:
            D.log_audit("snapshot_error", str(e), ip)
            return self._json({"ok": False, "msg": str(e)}, 400)

    # ---------------------------------------------------------- POST
    def do_POST(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/")
        if path == "/api/snapshot":
            return self._handle_snapshot()
        if path == "/api/ack":
            return self._handle_ack()
        if path != "/api/report":
            return self._send(404, "<h1>404</h1>")
        try:
            n = int(self.headers.get("Content-Length", 0))
            form = parse_qs(self.rfile.read(n).decode("utf-8"))
            get = lambda k: (form.get(k) or [""])[0].strip()
            sid = get("short_id")
            ip = self.client_address[0]

            smap = D.get_scan_map(sid)
            if not smap:
                return self._send(404, page_notfound(sid))

            view = R.bundle_view(smap["zdno"], smap["cc"], smap["zh"])
            try:
                emp = R.find_employee(get("ygno"))
                info = R.check_and_build_report(
                    sid, get("zdno") or smap["zdno"], get("cc") or smap["cc"],
                    get("zh") or smap["zh"], get("gx"), emp["ygno"], get("js"))
            except R.RuleError as e:
                return self._send(400, page_scan(
                    smap, R.bundle_view(smap["zdno"], smap["cc"], smap["zh"]),
                    err=e.message))

            res = D.create_report(
                short_id=info["short_id"], zdno=info["zdno"], cc=info["cc"],
                zh=info["zh"], gx=info["gx"], gxname=info["gxname"],
                ygno=emp["ygno"], ygname=emp["ygname"], js=info["js"],
                dj=info["dj"], ip=ip)

            view2 = R.bundle_view(smap["zdno"], smap["cc"], smap["zh"])
            if res["duplicated"]:
                msg = "这条报工刚才已经提交过了，没有重复计入。"
            else:
                msg = (f"已记录：{emp['ygname']} {info['gxname']} {info['js']} 件，"
                       f"金额 {info['js'] * info['dj']:.2f} 元。等待同步到服务器。")
            return self._send(200, page_scan(smap, view2, done=msg))

        except Exception as e:
            D.log_audit("error", f"POST: {e}", self.client_address[0])
            return self._send(500, f"<h1>500</h1><pre>{esc(e)}</pre>")


def main():
    D.init_db()
    st = D.stats()
    srv = ThreadingHTTPServer((BIND_HOST, PORT), Handler)
    shown = "127.0.0.1" if BIND_HOST == "0.0.0.0" else BIND_HOST
    print(f"扫码报工服务已启动  http://{shown}:{PORT}")
    print(f"  监听地址: {BIND_HOST}  (对外请走 nginx + HTTPS，不要直接暴露 10080)")
    print(f"  扫码页示例: http://{shown}:{PORT}/s/77ZMWCHC")
    print(f"  数据库: {D.DB_PATH}")
    print(f"  快照: 映射{st['scan_map']} 扎{st['bundle']} 工序{st['process']} "
          f"待同步{st['pending']}")
    if BIND_HOST == "0.0.0.0":
        print("  ⚠️ 警告：正监听所有网卡，请确认防火墙未放行该端口")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()

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
/* 定单总览条 */
.total{margin-top:12px;padding:11px 13px;background:#f0f8ff;border-radius:10px;
       display:flex;flex-wrap:wrap;align-items:baseline;gap:4px}
.tk{font-size:13px;color:#666}
.tv{font-size:19px;font-weight:700;color:#0a84ff;margin:0 2px 0 1px}
.tu{font-size:13px;color:#666;margin-right:2px}
.tdiv{color:#ccc;margin:0 4px}
.tall{flex-basis:100%;font-size:12px;color:#888;margin-top:5px}
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
/* 登录态 */
.who{background:linear-gradient(135deg,#e8f4ff,#f0f8ff);border:1px solid #cfe6ff}
.whobar{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.wface{font-size:22px;flex:0 0 auto}
.wname{font-size:17px;font-weight:700;white-space:nowrap;flex:0 0 auto}
.wno{font-size:13px;color:#666;background:#fff;padding:2px 8px;border-radius:10px;
     white-space:nowrap;flex:0 0 auto}
.wbtn{margin-left:auto;padding:6px 13px;font-size:13px;border:0;border-radius:9px;
      background:#fff;color:#0a84ff;border:1px solid #cfe6ff;flex:0 0 auto}
.lbtn{width:100%;padding:13px;font-size:16px;font-weight:600;border:0;border-radius:11px;
      background:#0a84ff;color:#fff;margin-top:11px}
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
.mbtn.warnm{background:#fff3e0;color:#e65100}
.apbadge{display:inline-block;min-width:19px;height:19px;line-height:19px;
         padding:0 6px;border-radius:10px;background:#ff3b30;color:#fff;
         font-size:12px;font-weight:700;text-align:center}
.cha{background:#fff;border-radius:11px;padding:14px;margin-bottom:10px;
     box-shadow:0 1px 4px rgba(0,0,0,.06)}
.cha .cn{font-weight:700;font-size:15px;margin-bottom:5px}
.cha .cd{font-size:13px;color:#555;line-height:1.7}
.cha .cf{font-size:12px;color:#999;margin-top:5px}
.dl{font-weight:700;font-size:17px}
.dl.up{color:#28a745}.dl.dn{color:#d9534f}
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


def page_scan(smap, view, err="", done="", me=None, prefer_gx=None, token="", last_report_id=0):
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

    # 记忆命中且那道还有余量 -> 自动选中
    auto_gx = None
    if prefer_gx is not None:
        for p in procs:
            if p["gx"] == prefer_gx and p["remain"] > 0:
                auto_gx = prefer_gx
                break

    plan = view["plan_qty"]
    # 本扎颜色/尺码：可能有多行（不同颜色×尺码）
    colors = view.get("colors") or []
    if colors:
        ctext = "  ".join(
            f"{c['yn']} {c['cm']}".strip() for c in colors)
        cnote = f"（{len(colors)} 种配色）" if len(colors) > 1 else ""
    else:
        ctext, cnote = "—", ""
    # 定单总览
    sm = view.get("summary") or {}
    zh_count = sm.get("zh_count", 0)
    qty_total = sm.get("qty_total", 0)
    all_colors = sm.get("colors") or []
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
            '<button class="mbtn warnm" onclick="chg(' + str(last_report_id) + '">报错了，要改量</button>'
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
  <div class="kv"><span class="k">颜色尺码</span><span class="v">{esc(ctext)} {cnote}</span></div>
  <div class="kv"><span class="k">本扎应做</span><span class="v big">{plan} 件</span></div>
  <div class="total">
    <div><span class="tk">本单共</span><span class="tv">{zh_count}</span><span class="tu">扎</span>
         <span class="tdiv">·</span>
         <span class="tv">{qty_total}</span><span class="tu">件</span></div>
    {'<div class="tall">全单颜色：' + esc("、".join(all_colors[:8])) + ('等' if len(all_colors) > 8 else '') + '</div>' if all_colors else ''}
  </div>
</div>

{"<div class='err'>" + esc(err) + "</div>" if err else ""}

<form method="POST" action="/api/report" id="f">
<input type="hidden" name="short_id" value="{esc(smap['short_id'])}">
<input type="hidden" name="zdno" value="{esc(smap['zdno'])}">
<input type="hidden" name="cc" value="{smap['cc']}">
<input type="hidden" name="zh" value="{smap['zh']}">
<input type="hidden" name="gx" id="gx" value="">

{f'<div class="card who"><h2>1. 当前员工</h2>'
 f'<div class="whobar"><span class="wface">👤</span>'
 f'<span class="wname">{esc(me["ygname"])}</span>'
 f'<span class="wno">{esc(me["ygno"])}</span>'
 f'<button type="button" class="wbtn" onclick="logout()">切换</button></div>'
 if me else
 '<div class="card"><h2>1. 首次登录</h2>'
 '<p class="sub" style="margin:0 0 10px">填「工号 或 手机号」都可以，'
 '初始 PIN 是手机号后 4 位。</p>'
 '<input id="account" placeholder="工号 A001 或 手机号" autocomplete="off" required>'
 '<input id="pin" type="tel" inputmode="numeric" maxlength="6" placeholder="4位 PIN" required>'
 '<button type="button" class="lbtn" onclick="doLogin()">登 录</button>'}
<input type="hidden" name="ygno" id="ygno" value="{esc(me["ygno"]) if me else ""}">
<input type="hidden" name="token" value="{esc(token)}">

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
var TOKEN = "{esc(token)}";
function closeOk(){{ document.getElementById('okmask').style.display='none'; }}
function chg(rid){{ location.href='/change?report_id='+rid+'&t='+TOKEN; }}
function closeErr(){{ document.getElementById('errmask').style.display='none'; }}
document.addEventListener('DOMContentLoaded', function(){{
  if(okShown) closeOk();
  var m=document.getElementById('errmask'); if(m) m.style.display='flex';
}});

// ---- 登录 ----
function doLogin(){{
  var acc=document.getElementById('account').value.trim();
  var pin=document.getElementById('pin').value.trim();
  if(!acc||!pin){{ alert('请填写工号或手机号，以及 PIN'); return; }}
  fetch('/api/login',{{method:'POST',headers:{{'Content-Type':'application/json'}},
        body:JSON.stringify({{account:acc,pin:pin}})}})
    .then(function(r){{return r.json();}})
    .then(function(d){{
      if(d.ok){{ try{{ localStorage.setItem('scan_token', d.token); }}catch(e){{}} location.reload(); }}
      else alert(d.msg||'登录失败');
    }}).catch(function(){{ alert('网络错误'); }});
}}
function logout(){{
  var t=(document.getElementById('token')||{{}}).value||localStorage.getItem('scan_token');
  fetch('/api/logout',{{method:'POST',headers:{{'Content-Type':'application/json'}},
        body:JSON.stringify({{token:t}})}})
    .then(function(){{ try{{ localStorage.removeItem('scan_token'); }}catch(e){{}} location.href='/'; }});
}}

// 记忆命中：自动选中并预填剩余数
var AUTO_GX = "{auto_gx if auto_gx else ""}";
function autoPick(){{
  if(!AUTO_GX) return;
  var el=document.querySelector('#plist .proc[data-gx="'+AUTO_GX+'"]');
  if(el && !el.classList.contains('done')) el.click();
}}

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

document.addEventListener('DOMContentLoaded', autoPick);

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


# ==================== 改量申请页 / 主管审批页 ====================

def _page_shell(title, body, extra_head=""):
    return f"""<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title><style>{CSS}</style>{extra_head}</head>
<body><div class="wrap">{body}</div></body></html>"""


def page_change_apply(rep, token, err="", mine=None):
    """工人申请改量。"""
    body = [f'<div class="card"><h1>修改报工数量</h1>'
            f'<p class="sub">改量需要主管审批，通过后才生效。</p>'
            f'<div class="kv"><span class="k">员工</span>'
            f'<span class="v">{esc(rep["ygname"])}（{esc(rep["ygno"])}）</span></div>'
            f'<div class="kv"><span class="k">定单</span><span class="v">{esc(rep["zdno"])}</span></div>'
            f'<div class="kv"><span class="k">扎号</span>'
            f'<span class="v">第 {rep["zh"]} 扎（{rep["cc"]} 层）</span></div>'
            f'<div class="kv"><span class="k">工序</span>'
            f'<span class="v">{rep["gx"]}. {esc(rep["gxname"])}</span></div>'
            f'<div class="kv"><span class="k">原报数量</span>'
            f'<span class="v big">{rep["js"]} 件</span></div></div>']

    if mine:
        st = mine["status"]
        label = {"pending": "待主管审批", "approved": "已通过",
                 "rejected": "被驳回"}[st]
        cls = {"pending": "", "approved": "ok-msg", "rejected": "err"}[st]
        body.append(
            f'<div class="card {cls}"><h2>我的申请</h2>'
            f'<p>{rep["js"]} 件 → {mine["new_num"]} 件（{label}）</p>'
            f'<p class="muted">理由：{esc(mine["reason"])}</p>'
            + (f'<p class="muted">驳回原因：{esc(mine["approve_note"] or "")}</p>'
               if st == "rejected" else "")
            + f'<p class="muted">{esc(mine["created_at"])}</p></div>')
        body.append('<div class="card"><a href="/m?short_id='
                    + esc(rep["short_id"] or "") + '&t=' + esc(token) + '">'
                    '<button class="ghost">← 返回扫码页</button></a></div>')
        return _page_shell("修改报工数量", "".join(body))

    body.append(
        f'<form class="card" method="POST" action="/change">'
        f'<input type="hidden" name="report_id" value="{rep["id"]}">'
        f'<input type="hidden" name="token" value="{esc(token)}">'
        f'<label>改成多少件</label>'
        f'<input name="new_js" type="number" inputmode="numeric" min="1" '
        f'value="{rep["js"]}" required>'
        f'<label>改量理由（必填，主管要凭这个判断）</label>'
        f'<input name="reason" placeholder="例如：少算了 5 件，实际只裁了 45 件" required>'
        f'<button type="submit">提交审批</button>'
        f'<a href="/m?short_id=' + esc(rep["short_id"] or "") + '&t=' + esc(token) + '">'
        f'<button type="button" class="ghost">取消</button></a></form>')
    if err:
        body.insert(1, f'<div class="err">{esc(err)}</div>')
    return _page_shell("修改报工数量", "".join(body))


def page_approve(me, changes, pending, token, tab="pending"):
    """主管审批页。"""
    body = [f'<div class="card"><h1>改量审批</h1>'
            f'<p class="sub">主管：{esc(me["ygname"])}（{esc(me["ygno"])}）</p>'
            f'<div class="kv"><span class="k">待审批</span>'
            f'<span class="v"><span class="apbadge">{pending}</span> 条</span></div>'
            f'<a href="/approve?status=pending&t=' + esc(token) + '">'
            f'<button class="ghost">待审（{pending}）</button></a>'
            f'<a href="/approve?status=all&t=' + esc(token) + '">'
            f'<button class="ghost">全部记录</button></a></div>']

    if not changes:
        body.append('<div class="card"><p class="muted">没有记录</p></div>')
        return _page_shell("改量审批", "".join(body))

    for c in changes:
        d = c["delta"]
        arrow = "↑" if d > 0 else "↓"
        dcls = "up" if d > 0 else "dn"
        s = c["status"]
        stxt = {"pending": '<span class="pill">待审</span>',
                "approved": '<span class="pill ok">已通过</span>',
                "rejected": '<span class="pill no">已驳回</span>'}[s]
        body.append(
            f'<div class="cha"><div class="cn">{esc(c["ygname"])}（{esc(c["ygno"])}） '
            f'{stxt}</div>'
            f'<div class="cd">{esc(c["zdno"])} 扎{c["zh"]} · {c["gx"]}.{esc(c["gxname"])}</div>'
            f'<div class="cd">{c["old_num"]} 件 → '
            f'<span class="dl {dcls}">{c["new_num"]} 件（{arrow}{abs(d)}）</span></div>'
            f'<div class="cd">理由：{esc(c["reason"])}</div>'
            f'<div class="cf">{esc(c["created_at"])}'
            + (f' · 审批：{esc(c["approve_gh"] or "")} '
               f'{esc(c["approve_at"] or "")}' if c.get("approve_at") else "")
            + (f'<br>驳回原因：{esc(c["approve_note"] or "")}'
               if s == "rejected" and c.get("approve_note") else "")
            + '</div>')
        if s == "pending":
            body.append(
                f'<div style="display:flex;gap:9px;margin-top:11px">'
                f'<button style="flex:1;background:#28a745" '
                f"onclick=\"dec({c['id']},1)\">通过</button>"
                f'<button style="flex:1;background:#d9534f" '
                f"onclick=\"dec({c['id']},0)\">驳回</button></div>")
        body.append('</div>')

    body.append(
        '<script>\n'
        'function dec(id,ap){'
        '  var note="";'
        '  if(!ap){note=prompt("驳回原因（工人会看到）")||"";'
        '    if(note===null)return;}'
        '  fetch("/api/change/decide",{method:"POST",'
        '    headers:{"Content-Type":"application/json"},'
        '    body:JSON.stringify({token:TOKEN,id:id,approve:ap,note:note})})'
        '   .then(function(r){return r.json();})'
        '   .then(function(d){ if(d.ok){location.reload();} '
        '     else {alert(d.msg||"操作失败");} })'
        '   .catch(function(){alert("网络错误");});'
        '}\n'
        f'var TOKEN = "{esc(token)}";\n'
        '</script>')
    return _page_shell("改量审批", "".join(body))


# ==================== 阶段4 · 报表页（手机） ====================

def page_report_home(me, meta, token, is_sup):
    """报表首页：选类型 + 数据截至时间。"""
    synced = D.get_sync_meta("jfgz_synced_at", "")
    body = [f'<div class="card"><h1>📊 统计报表</h1>'
            f'<p class="sub">{esc(me["ygname"])}（{esc(me["ygno"])}）</p>'
            f'<div class="kv"><span class="k">报工记录</span>'
            f'<span class="v">{meta["n"]:,}</span></div>'
            f'<div class="kv"><span class="k">定单数</span>'
            f'<span class="v">{meta["nzd"]:,}</span></div>'
            f'<div class="kv"><span class="k">数据范围</span>'
            f'<span class="v">{esc(meta["d0"] or "-")} ~ {esc(meta["d1"] or "-")}</span></div>'
            f'<div class="kv"><span class="k">数据截至</span>'
            f'<span class="v">{esc(synced or "尚未同步")}</span></div></div>']
    if not meta["n"]:
        body.append('<div class="card"><p class="muted">'
                    '暂无报工数据，请班组长在电脑上运行「报表数据推送」。</p></div>')
        return _page_shell("统计报表", "".join(body))

    body.append(
        f'<div class="card"><h2>查什么</h2>'
        f'<a href="/report/mine?t={esc(token)}">'
        f'<button>👤 我的报工</button></a>'
        + (f'<a href="/report/progress?t={esc(token)}">'
           f'<button>📈 定单进度</button></a>' if is_sup else "")
        + (f'<a href="/report/wage?t={esc(token)}">'
           f'<button>💰 计件工资</button></a>' if is_sup else "")
        + '</div>')
    body.append(f'<div class="card"><a href="/"><button class="ghost">← 返回首页</button></a></div>')
    return _page_shell("统计报表", "".join(body))


def page_report_table(title, sub, headers, rows, token, back, total=None):
    """通用报表表格页（手机卡片式，不用宽表格）。"""
    body = [f'<div class="card"><h1>{esc(title)}</h1>'
            f'<p class="sub">{esc(sub)}</p>'
            f'<a href="{esc(back)}?t={esc(token)}">'
            f'<button class="ghost">← 返回</button></a></div>']
    if not rows:
        body.append('<div class="card"><p class="muted">没有数据</p></div>')
        return _page_shell(title, "".join(body))
    if total:
        body.append(f'<div class="card"><p class="sub">合计</p>'
                    f'<p style="font-size:20px;font-weight:700">{total}</p></div>')
    cells = "".join(f"<th>{esc(h)}</th>" for h in headers)
    trs = ""
    for r in rows:
        tds = "".join(f"<td>{esc(str(c))}</td>" for c in r)
        trs += f"<tr>{tds}</tr>"
    body.append(f'<div class="card" style="overflow-x:auto">'
                f'<table><tr>{cells}</tr>{trs}</table></div>')
    return _page_shell(title, "".join(body))


def page_report_prompt(title, token, back, fields, is_sup):
    """报表参数选择页（日期/工号），提交到对应报表页。"""
    opts = "".join(f'<option value="{esc(v)}">{esc(t)}</option>' for v, t in fields)
    body = [f'<div class="card"><h1>{esc(title)}</h1>'
            f'<a href="{esc(back)}?t={esc(token)}">'
            f'<button class="ghost">← 返回</button></a></div>',
            '<div class="card"><form method="GET" action="' + esc(back) + '">'
            f'<input type="hidden" name="t" value="{esc(token)}">'
            '<p class="sub">开始日期</p>'
            '<input type="date" name="from" value="__FROM__" style="width:100%">'
            '<p class="sub" style="margin-top:10px">结束日期</p>'
            '<input type="date" name="to" value="__TO__" style="width:100%">'
            '<p class="sub" style="margin-top:10px">汇总维度</p>'
            f'<select name="dim" style="width:100%">{opts}</select>'
            + ('<p class="sub" style="margin-top:10px">工号（留空=全部）</p>'
               '<input type="text" name="ygno" placeholder="如 A001" style="width:100%">'
               if is_sup else "")
            + '<button style="margin-top:14px;width:100%">查询</button>'
            '</form></div>']
    html = _page_shell(title, "".join(body))
    return (html.replace("__FROM__", _today_minus(30))
                .replace("__TO__", _today()))


def _today():
    import datetime
    return datetime.date.today().isoformat()


def _today_minus(n):
    import datetime
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()


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
            if path == "/api/change":
                return self._handle_change_list()
            if path == "/api/changes":
                return self._handle_changes_export()
            if path == "/approve":
                me = R.whoami((parse_qs(u.query).get("t", [""])[0]) or "")
                if not me:
                    return self._send(404, page_notfound(""))
                if not R.is_supervisor(me["ygno"]):
                    return self._send(403, _page_shell("无权限",
                        '<div class="card"><h1>无权限</h1><p class="sub">'
                        '只有主管可以打开审批页。</p></div>'))
                tab = parse_qs(u.query).get("status", ["pending"])[0] or "pending"
                rows = D.list_changes(status=(None if tab == "all" else tab), limit=300)
                tok = parse_qs(u.query).get("t", [""])[0]
                return self._send(200, page_approve(
                    me, [R.change_view(r) for r in rows],
                    D.count_pending_changes(), tok, tab), head_only=head_only)
            if path == "/change":
                return self._handle_change_form()
            if path == "/report":
                return self._handle_report_home()
            if path == "/report/mine":
                return self._handle_report_mine()
            if path == "/report/progress":
                return self._handle_report_progress()
            if path == "/report/wage":
                return self._handle_report_wage()
            if path == "/admin/pending":
                return self._send(200, page_pending(D.list_pending(500)),
                                  head_only=head_only)
            if path.startswith("/api/bundle/"):
                sid = path.rsplit("/", 1)[-1]
                smap = D.get_scan_map(sid)
                if not smap:
                    return self._json({"ok": False, "msg": "二维码无效"}, 404,
                                      head_only=head_only)
                tok = parse_qs(u.query).get("token", [""])[0]
                me = R.whoami(tok)
                view = R.bundle_view(smap["zdno"], smap["cc"], smap["zh"])
                return self._json({"ok": True, "bundle": view, "map": smap,
                                   "me": me,
                                   "prefer_gx": R.recall_pref(
                                       me["ygno"], smap["zdno"], smap["cc"], smap["zh"])
                                   if me else None}, head_only=head_only)
            if path.startswith("/s/"):
                sid = path.rsplit("/", 1)[-1]
                err = parse_qs(u.query).get("e", [""])[0]
                smap = D.get_scan_map(sid)
                if not smap:
                    return self._send(404, page_notfound(sid), head_only=head_only)
                view = R.bundle_view(smap["zdno"], smap["cc"], smap["zh"])
                tok = parse_qs(u.query).get("t", [""])[0]
                me = R.whoami(tok)
                pref = R.recall_pref(me["ygno"], smap["zdno"], smap["cc"], smap["zh"]) if me else None
                return self._send(200, page_scan(smap, view, err=err, me=me,
                                                  prefer_gx=pref, token=tok),
                                  head_only=head_only)
            return self._send(404, "<h1>404</h1>", head_only=head_only)
        except Exception as e:
            D.log_audit("error", f"GET {path}: {e}", self.client_address[0])
            return self._send(500, f"<h1>500</h1><pre>{esc(e)}</pre>", head_only=head_only)

    def _handle_login(self):
        """登录：工号 或 手机号 + PIN。"""
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            ok, token, emp, must_change = R.login(data.get("account"), data.get("pin"))
            D.log_audit("login", f"{emp['ygno']} {emp['ygname']}", self.client_address[0])
            self._json({"ok": True, "token": token, "ygno": emp["ygno"],
                        "ygname": emp["ygname"], "must_change": must_change})
        except R.RuleError as e:
            self._json({"ok": False, "msg": e.message, "code": e.code}, 401)
        except Exception as e:
            self._json({"ok": False, "msg": str(e)}, 400)

    def _handle_logout(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            R.logout(data.get("token"))
            self._json({"ok": True})
        except Exception as e:
            self._json({"ok": False, "msg": str(e)}, 400)

    def _tok_user(self, data=None):
        """从请求里取登录态，返回员工信息或 None。

        GET 请求没有 body，令牌要从 query 里取；
        POST 才从 body 取。
        """
        if data is None:
            if self.command == "POST":
                data = self._read_json()
            else:
                q = parse_qs(urlparse(self.path).query)
                data = {"token": (q.get("t", [""])[0] or q.get("token", [""])[0])}
        return R.whoami(data.get("token") or "")

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return {}

    def _handle_change_form(self):
        """GET 展示申请页；POST 提交申请。"""
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if self.command == "POST":
            data = self._read_json_form()
            token = data.get("token", "")
            try:
                me = R.whoami(token)
                if not me:
                    return self._send(403, _page_shell("请先登录",
                        '<div class="card"><h1>请先登录</h1></div>'))
                rep, new_js = R.check_change_request(
                    int(data.get("report_id", 0)), data.get("new_js"),
                    data.get("reason"), me["ygno"])
                cid = D.create_change(rep["id"], me["ygno"], me["ygname"],
                                      new_js, str(data.get("reason", "")).strip())
                mine = D.list_changes(status=None, ygno=me["ygno"], limit=1)
                mine = mine[0] if mine and mine[0]["id"] == cid else {"status": "pending",
                        "new_num": new_js, "reason": str(data.get("reason", "")),
                        "created_at": "", "approve_note": ""}
                return self._send(200, page_change_apply(rep, token, mine=mine))
            except R.RuleError as e:
                rep = D.get_report(int(data.get("report_id", 0) or 0))
                if not rep:
                    return self._send(400, _page_shell("出错了",
                        f'<div class="card"><h1>出错了</h1><p>{esc(e.message)}</p></div>'))
                return self._send(400, page_change_apply(rep, token, err=e.message))
            except Exception as e:
                return self._send(500, _page_shell("出错了",
                    f'<div class="card"><h1>出错了</h1><p>{esc(str(e))}</p></div>'))
        rid = int(q.get("report_id", ["0"])[0] or 0)
        token = q.get("t", [""])[0] or ""
        me = R.whoami(token)
        if not me:
            return self._send(403, _page_shell("请先登录",
                '<div class="card"><h1>请先登录</h1>'
                '<p class="sub">请从扫码页登录后再改量。</p></div>'))
        rep = D.get_report(rid)
        if not rep:
            return self._send(404, page_notfound(""))
        if rep["ygno"] != me["ygno"]:
            return self._send(403, _page_shell("无权限",
                '<div class="card"><h1>无权限</h1>'
                '<p class="sub">只能修改自己的报工。</p></div>'))
        mine = D.list_changes(status=None, ygno=me["ygno"], limit=50)
        mine = next((m for m in mine if m["report_id"] == rid), None)
        return self._send(200, page_change_apply(rep, token, mine=mine))

    def _read_json_form(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            return parse_qs(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _handle_change_apply(self):
        """工人申请改量。"""
        try:
            data = self._read_json()
            me = R.whoami(data.get("token", ""))
            if not me:
                return self._json({"ok": False, "msg": "请先登录"}, 401)
            rep, new_js = R.check_change_request(
                int(data.get("report_id", 0)), data.get("new_js"),
                data.get("reason"), me["ygno"])
            cid = D.create_change(rep["id"], me["ygno"], me["ygname"],
                                  new_js, str(data.get("reason", "")).strip())
            self._json({"ok": True, "id": cid,
                        "msg": f"申请已提交：{rep['js']} 件 → {new_js} 件，等主管审批"})
        except R.RuleError as e:
            self._json({"ok": False, "msg": e.message, "code": e.code}, 400)
        except ValueError as e:
            self._json({"ok": False, "msg": str(e)}, 400)
        except Exception as e:
            self._json({"ok": False, "msg": str(e)}, 500)

    def _handle_change_list(self):
        """主管看待审（或指定状态），工人只看自己的。"""
        try:
            me = self._tok_user()
            if not me:
                return self._json({"ok": False, "msg": "请先登录"}, 401)
            u = urlparse(self.path)
            q = parse_qs(u.query)
            status = (q.get("status", ["pending"])[0] or "pending").strip()
            if status == "all":
                status = None
            if R.is_supervisor(me["ygno"]):
                rows = D.list_changes(status=status, limit=300)
            else:
                rows = D.list_changes(status=status, ygno=me["ygno"], limit=100)
            self._json({"ok": True, "me": me, "is_supervisor": R.is_supervisor(me["ygno"]),
                        "pending": D.count_pending_changes(),
                        "changes": [R.change_view(r) for r in rows]})
        except Exception as e:
            self._json({"ok": False, "msg": str(e)}, 400)

    def _handle_change_decide(self):
        """主管审批。"""
        try:
            data = self._read_json()
            me = R.whoami(data.get("token", ""))
            if not me:
                return self._json({"ok": False, "msg": "请先登录"}, 401)
            if not R.is_supervisor(me["ygno"]):
                return self._json({"ok": False, "msg": "只有主管可以审批"}, 403)
            cid = int(data.get("id", 0))
            approve = 1 if str(data.get("approve")) in ("1", "true", "True") else 0
            ok, msg = D.decide_change(cid, bool(approve), me["ygno"],
                                      str(data.get("note", "")).strip())
            if not ok:
                return self._json({"ok": False, "msg": msg}, 400)
            D.log_audit("change_decide", f"#{cid} {msg} by {me['ygno']}",
                        self.client_address[0])
            self._json({"ok": True, "msg": msg, "pending": D.count_pending_changes()})
        except Exception as e:
            self._json({"ok": False, "msg": str(e)}, 400)

    def _handle_changes_export(self):
        """同步器取已审批待写回的改量。"""
        if not self._auth_sync():
            return
        u = urlparse(self.path)
        status = (parse_qs(u.query).get("status", ["approved"])[0] or "approved")
        rows = D.list_changes(status=status, limit=200)
        if status == "approved":
            rows = [r for r in rows if not r["synced"]]
        self._json({"ok": True, "count": len(rows),
                    "changes": [R.change_view(r) for r in rows]})

    def _handle_changes_ack(self):
        """同步器回报改量写回结果。"""
        if not self._auth_sync():
            return
        data = self._read_json()
        ok = bad = 0
        for r in data.get("results") or []:
            if r.get("id") is None:
                continue
            D.mark_change_synced(int(r["id"]), bool(r.get("ok")), r.get("msg", ""))
            ok += 1 if r.get("ok") else 0
            bad += 0 if r.get("ok") else 1
        D.log_audit("changes_ack", f"ok={ok} bad={bad}", self.client_address[0])
        self._json({"ok": True, "synced": ok, "failed": bad})

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

    # ---------------------------------------------------------- 报表
    def _report_user(self):
        """取登录态；没登录返回 None。"""
        tok = parse_qs(urlparse(self.path).query).get("t", [""])[0]
        return R.whoami(tok), tok

    def _handle_report_home(self):
        me, tok = self._report_user()
        if not me:
            return self._send(404, page_notfound(""))
        return self._send(200, page_report_home(
            me, D.report_meta(), tok, R.is_supervisor(me["ygno"])))

    def _q(self, key, default=""):
        v = parse_qs(urlparse(self.path).query).get(key, [default])[0]
        return (v or default).strip()

    def _handle_report_mine(self):
        """工人自助查询：只看自己的报工。"""
        me, tok = self._report_user()
        if not me:
            return self._send(404, page_notfound(""))
        dfrom = self._q("from", _today_minus(30)) or _today_minus(30)
        dto = self._q("to", _today()) or _today()
        rows = D.report_mine(dfrom, dto, me["ygno"], 200)
        trs = [[r["gzdate"][:16], r["zdno"], r["gx"],
                f'{r["cc"]}-{r["zh"]}', r["js"], f'{r["je"]:.2f}'] for r in rows]
        sub = f"{dfrom} ~ {dto}　共 {len(rows)} 条"
        return self._send(200, page_report_table(
            "我的报工", sub, ["时间", "定单", "工序", "扎", "件数", "金额"],
            trs, tok, "/report"))

    def _handle_report_progress(self):
        """定单进度（仅主管）。"""
        me, tok = self._report_user()
        if not me:
            return self._send(404, page_notfound(""))
        if not R.is_supervisor(me["ygno"]):
            return self._send(403, _page_shell("无权限",
                '<div class="card"><h1>无权限</h1><p class="sub">'
                '只有主管可以查看定单进度。</p></div>'))
        zdno = self._q("zdno")
        if not zdno:
            return self._send(200, page_report_prompt(
                "定单进度", tok, "/report/progress",
                [("zdno", "定单进度")], True))
        rows = D.report_progress(zdno)
        trs = [[r["gx"], r["recs"], r["pcs"], f'{r["amt"]:.2f}'] for r in rows]
        sub = f"{zdno}　共 {len(rows)} 道工序"
        return self._send(200, page_report_table(
            "定单进度", sub, ["工序", "报工次数", "件数", "金额"], trs, tok, "/report"))

    def _handle_report_wage(self):
        """计件工资（口径 A：SUM(je)）。主管看全部，工人看自己。"""
        me, tok = self._report_user()
        if not me:
            return self._send(404, page_notfound(""))
        is_sup = R.is_supervisor(me["ygno"])
        dfrom = self._q("from", "") or _today_minus(30)
        dto = self._q("to", "") or _today()
        dim = self._q("dim", "ygno") or "ygno"
        ygno = self._q("ygno") or None
        # 权限：非主管强制只看自己
        if not is_sup:
            ygno = me["ygno"]
            dim = "ygno"
        if dim not in ("zdno", "ygno", "gx", "day"):
            dim = "ygno"
        rows = D.report_group(dim, dfrom, dto, 500)
        if ygno:
            rows = [r for r in rows if r.get("k") == ygno]
        trs = [[r["k"], r.get("kname") or "", r["recs"], r["pcs"], f'{r["amt"]:.2f}']
               for r in rows]
        total = f'共 {len(rows)} 组'
        dimname = {"zdno": "定单", "ygno": "员工", "gx": "工序", "day": "日期"}[dim]
        sub = f"{dfrom} ~ {dto}　按{dimname}　{total}"
        return self._send(200, page_report_table(
            "计件工资" if is_sup else "我的工资", sub,
            [dimname, "姓名", "次数", "件数", "金额"], trs, tok, "/report"))

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

    def _handle_reports_push(self):
        """局域网同步器推送 jfgz 报工明细（阶段4报表数据源）。需 Bearer 令牌。"""
        ip = self.client_address[0]
        if not self._auth_sync():
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n <= 0 or n > MAX_SNAPSHOT_BYTES:
                return self._json({"ok": False, "msg": f"体积异常 {n}"}, 413)
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
            rows = payload.get("rows", [])
            total = int(payload.get("total", len(rows)))
            max_id = int(payload.get("max_id", 0))
            applied = D.apply_report_snapshot(rows, total, max_id)
            D.log_audit("reports_push", f"{applied}", ip)
            return self._json({"ok": True, "applied": applied, "stats": D.stats()})
        except Exception as e:
            D.log_audit("reports_push_error", str(e), ip)
            return self._json({"ok": False, "msg": str(e)}, 400)

    # ---------------------------------------------------------- POST
    def do_POST(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/")
        if path == "/api/snapshot":
            return self._handle_snapshot()
        if path == "/api/reports/push":
            return self._handle_reports_push()
        if path == "/api/ack":
            return self._handle_ack()
        if path == "/api/login":
            return self._handle_login()
        if path == "/api/logout":
            return self._handle_logout()
        if path == "/api/change/apply":
            return self._handle_change_apply()
        if path == "/api/changes/ack":
            return self._handle_changes_ack()
        if path == "/change":
            return self._handle_change_form()
        if path == "/api/change/decide":
            return self._handle_change_decide()
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
            tok = get("token")
            me = R.whoami(tok)
            try:
                # 已登录的以令牌为准，防冒名；未登录的退回工号校验（过渡期）
                emp = me if me else R.find_employee(get("ygno"))
                info = R.check_and_build_report(
                    sid, get("zdno") or smap["zdno"], get("cc") or smap["cc"],
                    get("zh") or smap["zh"], get("gx"), emp["ygno"], get("js"))
            except R.RuleError as e:
                m2 = R.whoami(tok)
                return self._send(400, page_scan(
                    smap, R.bundle_view(smap["zdno"], smap["cc"], smap["zh"]),
                    err=e.message, me=m2, token=tok))

            res = D.create_report(
                short_id=info["short_id"], zdno=info["zdno"], cc=info["cc"],
                zh=info["zh"], gx=info["gx"], gxname=info["gxname"],
                ygno=emp["ygno"], ygname=emp["ygname"], js=info["js"],
                dj=info["dj"], ip=ip)

            try:
                R.remember_pref(emp["ygno"], info["zdno"], info["cc"], info["zh"], info["gx"])
            except Exception as e:
                print(f"记录工序偏好失败：{e}")
            view2 = R.bundle_view(smap["zdno"], smap["cc"], smap["zh"])
            if res["duplicated"]:
                msg = "这条报工刚才已经提交过了，没有重复计入。"
            else:
                msg = (f"已记录：{emp['ygname']} {info['gxname']} {info['js']} 件，"
                       f"金额 {info['js'] * info['dj']:.2f} 元。等待同步到服务器。")
            pref2 = R.recall_pref(emp["ygno"], smap["zdno"], smap["cc"], smap["zh"])
            return self._send(200, page_scan(smap, view2, done=msg, me=me,
                                             prefer_gx=pref2, token=tok,
                                             last_report_id=res.get("id") or 0))

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

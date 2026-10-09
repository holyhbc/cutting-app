# -*- coding: utf-8 -*-
"""扫码报工 · Web 服务（阶段 1）。

只用标准库，与 hbc-print 保持一致，不引入额外依赖。
不连 SQL Server，数据来自本地 SQLite 快照（决策 D-009）。

启动：
  python app.py                # 监听 0.0.0.0:8000
  PORT=9000 python app.py

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

PORT = int(os.environ.get("PORT", "8000"))

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
            f'<div class="{cls}" data-gx="{p["gx"]}">'
            f'<div><div class="pn">{p["gx"]}. {esc(p["gxname"])}</div>'
            f'<div class="ps">单价 {p["dj"]:.4f} 元/件</div></div>{pill}</div>')

    plan = view["plan_qty"]
    return f"""<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>扫码报工</title><style>{CSS}</style></head><body><div class="wrap">

<div class="card">
  <h1>扫码报工</h1>
  <p class="sub">选好工序后填数量提交，首次报工直接生效。</p>
  <div class="kv"><span class="k">定单</span><span class="v">{esc(smap["zdno"])}</span></div>
  <div class="kv"><span class="k">扎号</span><span class="v">第 {smap["zh"]} 扎（{smap["cc"]} 层）</span></div>
  <div class="kv"><span class="k">应做数量</span><span class="v big">{plan} 件</span></div>
</div>

{"<div class='err'>" + esc(err) + "</div>" if err else ""}
{"<div class='ok-msg'>" + esc(done) + "</div>" if done else ""}

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
         placeholder="例如 236" required>
  <button type="submit" id="btn">提交报工</button>
</div>
</form>

<div class="card">
  <p class="muted">标签二维码短ID：{esc(smap['short_id'])}</p>
</div>
</div>
<script>
document.querySelectorAll('#plist .proc').forEach(function(el){{
  el.onclick=function(){{
    document.querySelectorAll('#plist .proc').forEach(x=>x.classList.remove('sel'));
    el.classList.add('sel');
    document.getElementById('gx').value=el.dataset.gx;
    var f=document.getElementById('f');
    if(f.checkValidity()) f.submit();
  }};}});
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

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---------------------------------------------------------- GET
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        try:
            if path == "/":
                return self._send(200, page_home(D.stats()))
            if path == "/health":
                st = D.stats()
                return self._json({"ok": True, "stats": st})
            if path == "/admin/pending":
                return self._send(200, page_pending(D.list_pending(500)))
            if path.startswith("/api/bundle/"):
                sid = path.rsplit("/", 1)[-1]
                smap = D.get_scan_map(sid)
                if not smap:
                    return self._json({"ok": False, "msg": "二维码无效"}, 404)
                return self._json({"ok": True, "bundle": R.bundle_view(
                    smap["zdno"], smap["cc"], smap["zh"]), "map": smap})
            if path.startswith("/s/"):
                sid = path.rsplit("/", 1)[-1]
                err = parse_qs(u.query).get("e", [""])[0]
                smap = D.get_scan_map(sid)
                if not smap:
                    return self._send(404, page_scan(
                        {"zdno": "?", "cc": 0, "zh": 0, "short_id": sid},
                        {"plan_qty": 0, "processes": []},
                        err="二维码无效或已过期，请重新打印标签"))
                view = R.bundle_view(smap["zdno"], smap["cc"], smap["zh"])
                return self._send(200, page_scan(smap, view, err=err))
            return self._send(404, "<h1>404</h1>")
        except Exception as e:
            D.log_audit("error", f"GET {path}: {e}", self.client_address[0])
            return self._send(500, f"<h1>500</h1><pre>{esc(e)}</pre>")

    # ---------------------------------------------------------- POST
    def do_POST(self):
        u = urlparse(self.path)
        if u.path.rstrip("/") != "/api/report":
            return self._send(404, "<h1>404</h1>")
        try:
            n = int(self.headers.get("Content-Length", 0))
            form = parse_qs(self.rfile.read(n).decode("utf-8"))
            get = lambda k: (form.get(k) or [""])[0].strip()
            sid = get("short_id")
            ip = self.client_address[0]

            smap = D.get_scan_map(sid)
            if not smap:
                return self._send(400, page_scan(
                    {"zdno": "?", "cc": 0, "zh": 0, "short_id": sid},
                    {"plan_qty": 0, "processes": []},
                    err="二维码无效或已过期，请重新打印标签"))

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
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"扫码报工服务已启动  http://0.0.0.0:{PORT}")
    print(f"  扫码页示例: http://127.0.0.1:{PORT}/s/77ZMWCHC")
    print(f"  数据库: {D.DB_PATH}")
    print(f"  快照: 映射{st['scan_map']} 扎{st['bundle']} 工序{st['process']} "
          f"待同步{st['pending']}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()

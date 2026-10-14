# -*- coding: utf-8 -*-
"""HTTP 接口测试（打本地临时端口，不联网）。

覆盖历史上真实踩过的坑：
  - 未登录访问报表跳登录页（以前错跳到「标签未同步」页）
  - 工人访问 /admin 必须 403（以前完全没鉴权，全厂报工裸奔）
  - 点「查看我的报工」不能误触发报工表单提交
  - 报工成功后不能自己跳到 404
  - 开放重定向防护
"""

import json
import urllib.parse
import socket
import unittest
import urllib.error
import urllib.request

from base import ScanTestCase, start_local_server


class HTTPTestCase(ScanTestCase):
    def setUp(self):
        super().setUp()
        import app
        self.app = app
        # 服务端每请求打一行日志，20 个用例能把输出淹掉
        self._real_log = app.Handler.log_message
        app.Handler.log_message = lambda *a, **k: None
        # 端口由内核分配并在绑定失败时自动重试，避免偶发失败
        self.srv = start_local_server(app)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.app.Handler.log_message = self._real_log
        super().tearDown()

    # ---------- HTTP 助手 ----------

    def get(self, path, token=None, follow=True):
        url = self.base + path
        if token:
            url += ("&" if "?" in path else "?") + "t=" + token
        req = urllib.request.Request(url)
        op = urllib.request.build_opener()
        if not follow:
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *a, **k):
                    return None
            op = urllib.request.build_opener(NoRedirect)
        try:
            r = op.open(req, timeout=20)
            return r.getcode(), r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def post_json(self, path, payload, token=None):
        body = json.dumps(payload).encode()
        url = self.base + path
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            r = urllib.request.urlopen(req, timeout=20)
            return r.getcode(), json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))

    def login(self, account="C001", pin="5555"):
        code, d = self.post_json("/api/login", {"account": account, "pin": pin})
        self.assertTrue(d.get("ok"), f"登录失败: {d}")
        return d["token"]


class TestLoginApi(HTTPTestCase):
    def test_登录接口(self):
        self.add_employee("C001", "测试主管")
        code, d = self.post_json("/api/login", {"account": "C001", "pin": "5555"})
        self.assertEqual(code, 200)
        self.assertTrue(d["ok"])
        self.assertEqual(d["ygno"], "C001")

    def test_错PIN返回明确错误(self):
        self.add_employee("C001")
        code, d = self.post_json("/api/login", {"account": "C001", "pin": "0000"})
        self.assertFalse(d["ok"])
        self.assertEqual(d["code"], "bad_pin")


class TestAuthRedirect(HTTPTestCase):
    """未登录不能看业务数据，但也不能甩一个驴唇不对马嘴的页面。"""

    def _location(self, path):
        """取 302 的 Location（body 是空的，只能看 header）。"""
        req = urllib.request.Request(self.base + path)
        op = urllib.request.build_opener(NoRedirectHandler)
        try:
            op.open(req, timeout=20)
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Location", "")
        return 200, ""

    def test_未登录访问报表跳登录页(self):
        for p in ("/report", "/report/mine", "/report/wage", "/approve", "/admin"):
            code, loc = self._location(p)
            self.assertEqual(code, 302, f"{p} 未登录时应跳转，实际 {code}")
            self.assertIn("/login", loc, f"{p} 未跳登录页")

    def test_未登录重定向带next参数(self):
        code, loc = self._location("/report/wage")
        self.assertEqual(code, 302)
        self.assertIn("next=/report/wage", loc)

    def test_未登录不返回标签未同步页(self):
        """以前 /report 未登录会显示「标签未同步」，完全驴唇不对马嘴。"""
        code, body = self.get("/report")
        self.assertNotIn("标签还没同步", body)

    def test_登录页可匿名访问(self):
        code, _ = self.get("/login")
        self.assertEqual(code, 200)


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class TestAdminAuth(HTTPTestCase):
    """后台鉴权 —— 以前 /admin/pending 完全没鉴权。"""

    def setUp(self):
        super().setUp()
        self.add_employee("C001", "测试主管")
        self.add_employee("C002", "员工测试")
        self.sup = self.login("C001")
        self.worker = self.login("C002")

    def test_主管可进后台(self):
        code, body = self.get("/admin", self.sup)
        self.assertEqual(code, 200)
        self.assertIn("主管后台", body)

    def test_普通工人被拒(self):
        for p in ("/admin", "/admin/pending", "/admin/failed", "/admin/synced"):
            code, body = self.get(p, self.worker)
            self.assertEqual(code, 403, f"{p} 普通工人不该放行")
            self.assertIn("无权限", body)

    def test_未登录被拒(self):
        code, _ = self.get("/admin", follow=False)
        self.assertEqual(code, 302)

    def test_普通工人看不到主管报表(self):
        """工人可以看工资页，但只能看自己的；不能看进度。"""
        self.add_process("ZD1", 1, "打洞纽扣", 0.5)
        self.add_bundle("ZD1", 1, 1, 100)
        self.push_reports([
            (1, "2026-10-10 08:00:00", "C001", "测试主管", "ZD1", 1, 1, 1, 100, 0.5, 50.0, 1),
            (2, "2026-10-10 09:00:00", "C002", "员工测试", "ZD1", 1, 1, 1, 50, 0.5, 25.0, 1),
        ])
        code, body = self.get("/report/wage", self.worker)
        self.assertEqual(code, 200)
        self.assertIn("我的工资", body)          # 标题变成「我的工资」
        code, body = self.get("/report/progress?zdno=ZD1", self.worker)
        self.assertEqual(code, 403)

    def test_工人看不到全厂数字(self):
        self.push_reports([
            (1, "2026-10-10 08:00:00", "C001", "测试主管", "ZD1", 1, 1, 1, 100, 0.5, 50.0, 1),
        ])
        code, body = self.get("/report", self.worker)
        self.assertEqual(code, 200)
        self.assertIn("我的统计", body)
        self.assertNotIn("全厂报工记录", body)


class TestScanPage(HTTPTestCase):
    """扫码页 —— 登录、入口、误提交那几个坑。"""

    def setUp(self):
        super().setUp()
        self.add_employee("C002", "员工测试")
        self.add_process("ZD1", 1, "打洞纽扣", 0.5)
        self.add_bundle("ZD1", 1, 1, 100)
        self.db.upsert_scan_map("SID0001", "ZD1", 1, 1)

    def test_未登录显示登录框(self):
        code, body = self.get("/s/SID0001")
        self.assertEqual(code, 200)
        self.assertIn("首次登录", body)

    def test_已登录显示员工信息(self):
        tok = self.login("C002")
        code, body = self.get("/s/SID0001", tok)
        self.assertIn("当前员工", body)
        self.assertIn("员工测试", body)

    def test_入口链接不带嵌套button(self):
        """回归：<a><button> 嵌套会让浏览器当提交按钮，
        点「查看我的报工」弹出「要填写报工数量」。"""
        tok = self.login("C002")
        code, body = self.get("/s/SID0001", tok)
        self.assertIn("查看我的报工", body)
        import re
        for m in re.finditer(r"<a [^>]*>(.*?)</a>", body, re.S):
            self.assertNotIn("<button", m.group(1),
                             "链接里不能嵌套 button")

    def test_无效二维码返回说明页(self):
        code, body = self.get("/s/NOPE0000")
        self.assertEqual(code, 404)
        self.assertIn("还没同步", body)

    def test_自动补令牌只在扫码页生效(self):
        """回归：这段代码无条件跳转会把报工成功页换成 GET /api/report，
        而它只注册了 do_POST，GET 过去必然 404。"""
        tok = self.login("C002")
        code, body = self.get("/s/SID0001", tok)
        self.assertIn("location.pathname.indexOf('/s/')", body)
        # GET /api/report 确实没有实现（这是前提）
        code, _ = self.get("/api/report")
        self.assertEqual(code, 404)


class TestOpenRedirect(HTTPTestCase):
    def test_外部地址被拦(self):
        for evil in ("//evil.com/x", "http://evil.com", "https://evil.com"):
            code, body = self.get("/login?next=" +
                                  urllib.parse.quote(evil), follow=False)
            loc = ""
            if code == 302:
                loc = body
            code, body = self.get("/login?next=" + urllib.parse.quote(evil))
            self.assertNotIn('NEXT = "http', body)
            self.assertNotIn('NEXT = "//', body)


class TestSyncEndpoints(HTTPTestCase):
    """推送接口必须校验 Bearer 令牌。"""

    def test_无令牌被拒(self):
        body = json.dumps({"rows": [], "total": 0, "max_id": 0}).encode()
        req = urllib.request.Request(
            self.base + "/api/reports/push", data=body, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            r = urllib.request.urlopen(req, timeout=20)
            got = json.loads(r.read().decode())
            self.assertFalse(got.get("ok"))
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)

    def test_错令牌被拒(self):
        body = json.dumps({"rows": [], "total": 0, "max_id": 0}).encode()
        req = urllib.request.Request(
            self.base + "/api/reports/push", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer wrong"})
        try:
            urllib.request.urlopen(req, timeout=20)
            self.fail("错令牌竟然被接受")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)

    def test_正确令牌可推送(self):
        body = json.dumps({
            "rows": [[1, "2026-10-10 08:00:00", "C001", "测试主管",
                      "ZD1", 1, 1, 1, 100, 0.5, 50.0, 1]],
            "total": 1, "max_id": 1}).encode()
        req = urllib.request.Request(
            self.base + "/api/reports/push", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer test-token-for-unit-test"})
        r = urllib.request.urlopen(req, timeout=20)
        got = json.loads(r.read().decode())
        self.assertTrue(got["ok"])
        self.assertEqual(got["applied"]["rows"], 1)


if __name__ == "__main__":
    import urllib.parse
    os.environ["HBC_QUIET_MIGRATE"] = "1"
    unittest.main(verbosity=2)

class TestDiscardApi(HTTPTestCase):
    """作废接口的权限边界。"""

    def setUp(self):
        super().setUp()
        self.add_employee("C001", "测试主管")
        self.add_employee("C002", "员工测试")
        self.add_process("ZD1", 1, "打洞纽扣", 0.5)
        self.add_bundle("ZD1", 1, 1, 100)
        rid = self.db.create_report("S", "ZD1", 1, 1, 1, "打洞纽扣",
                                    "C002", "员工测试", 150, 0.5)["id"]
        self.db.get_conn().execute(
            "UPDATE report SET status='failed', last_err='超量' WHERE id=?",
            (rid,))
        self.db.get_conn().commit()
        self.rid = rid
        self.sup = self.login("C001")
        self.worker = self.login("C002")

    def test_工人作废自己的(self):
        code, d = self.post_json("/api/report/discard",
                                 {"id": self.rid, "token": self.worker})
        self.assertEqual(code, 200)
        self.assertTrue(d["ok"], d)
        self.assertEqual(self.db.get_report(self.rid)["status"], "discarded")

    def test_工人不能作废别人的(self):
        rid2 = self.db.create_report("S2", "ZD1", 1, 1, 1, "打洞纽扣",
                                     "C001", "测试主管", 10, 0.5)["id"]
        code, d = self.post_json("/api/report/discard",
                                 {"id": rid2, "token": self.worker})
        self.assertFalse(d["ok"])
        self.assertIn("自己的", d["msg"])

    def test_未登录不能作废(self):
        code, d = self.post_json("/api/report/discard", {"id": self.rid})
        self.assertFalse(d["ok"])
        self.assertIn("登录", d["msg"])

    def test_已同步的不能作废(self):
        self.db.get_conn().execute(
            "UPDATE report SET status='synced', jfgzid=1 WHERE id=?", (self.rid,))
        self.db.get_conn().commit()
        code, d = self.post_json("/api/report/discard",
                                 {"id": self.rid, "token": self.sup})
        self.assertFalse(d["ok"])
        self.assertIn("改量", d["msg"])

    def test_失败列表有作废按钮(self):
        code, body = self.get("/admin/failed", self.sup)
        self.assertEqual(code, 200)
        self.assertIn("作废", body)
        self.assertIn("/api/report/discard", body)


class TestOverLimitGuard(HTTPTestCase):
    """回归：报满的工序要能在前端挡住，不能提交了才被服务器拒。

    原来前端只自动填剩余数，不校验上限，
    工人手动改大数量 → 提交被拒 → 留下一条 failed 记录，
    还得找主管作废，非常别扭。
    """

    def setUp(self):
        super().setUp()
        self.add_employee("C002", "员工测试")
        self.add_process("ZD1", 1, "四针六线衣", 0.7)
        self.add_process("ZD1", 2, "四针六线裤", 0.77)
        self.add_bundle("ZD1", 1, 11, 40)
        self.db.upsert_scan_map("SID", "ZD1", 1, 11)
        self.tok = self.login("C002")

    def _page(self):
        code, body = self.get("/s/SID", self.tok)
        self.assertEqual(code, 200)
        return body

    def test_显示已报和剩余(self):
        body = self._page()
        self.assertIn("剩 ", body)
        self.assertIn("本扎应做 40 件", body, "工人要知道这扎应做多少")

    def test_有提交前上限校验(self):
        body = self._page()
        self.assertIn("超过剩余数了", body)
        self.assertIn("js.max=remain", body, "输入框上限要跟着剩余量走")
        self.assertIn("addEventListener('submit'", body)

    def test_未选工序时提交按钮禁用(self):
        self.assertIn('id="btn" disabled', self._page())

    def test_已做完的工序有标记且点不动(self):
        # 先把工序1 报满
        self.db.create_report("X", "ZD1", 1, 11, 1, "四针六线衣",
                              "C002", "员工测试", 40, 0.7)
        self.db.get_conn().execute("UPDATE report SET status='synced', jfgzid=1")
        self.db.get_conn().commit()
        self.db.apply_report_snapshot(
            [(1, "2026-10-10 10:00:00", "C002", "员工测试", "ZD1", 1, 1, 11, 40,
              0.7, 28.0, 1)], 1, 1)
        body = self._page()
        self.assertIn("已做完", body)
        self.assertIn("这道工序已做完", body, "点了要有明确反馈")

    def test_后端仍然兜得住(self):
        """前端只是提示，后端校验不能被绕过。"""
        with self.assertRaises(self.rules.RuleError) as ctx:
            self.rules.check_and_build_report("SID", "ZD1", 1, 11, 1, "C002", 999)
        self.assertEqual(ctx.exception.code, "over_limit")


class TestChangeEntryPoints(HTTPTestCase):
    """工人申请改量 / 主管代改 的两个入口。"""

    def setUp(self):
        super().setUp()
        self.add_employee("C001", "测试主管")
        self.add_employee("C002", "员工测试")
        self.add_employee("C003", "工人丙")
        self.add_process("ZD1", 1, "打洞纽扣", 0.5)
        self.add_bundle("ZD1", 1, 11, 100)
        rid = self.db.create_report("S", "ZD1", 1, 11, 1, "打洞纽扣",
                                    "C002", "员工测试", 100, 0.5)["id"]
        self.db.get_conn().execute(
            "UPDATE report SET status='synced', jfgzid=9001 WHERE id=?", (rid,))
        self.db.get_conn().execute(
            "INSERT OR REPLACE INTO report_baseline VALUES (?,1,11,1,?,?)",
            ("ZD1", 100, self.db.now_str()))
        self.db.get_conn().commit()
        # 已同步的报工在「我的报工」里走 jfgz_report 历史那条路径，
        # 光在 report 表里标 synced 是看不到的
        self.push_reports([(9001, "2026-10-12 10:00:00", "C002", "员工测试",
                            "ZD1", 1, 1, 11, 100, 0.5, 50.0, 1)])
        self.rid = rid
        self.sup = self.login("C001")
        self.worker = self.login("C002")

    def _post_change(self, token, rid, new_js, reason):
        import urllib.parse as up
        body = up.urlencode({"report_id": rid, "new_js": new_js,
                             "reason": reason, "token": token}).encode()
        req = urllib.request.Request(self.base + "/change", data=body,
                                     method="POST",
                                     headers={"Content-Type":
                                              "application/x-www-form-urlencoded"})
        try:
            r = urllib.request.urlopen(req, timeout=20)
            return r.getcode(), r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def test_工人申请后进待审批(self):
        code, body = self._post_change(self.worker, self.rid, 80, "点数有误")
        self.assertEqual(code, 200)
        self.assertIn("待主管审批", body)
        self.assertEqual(self.db.count_pending_changes(), 1)

    def test_我的报工页有申请改量入口(self):  # noqa: D401
        """回归：改量入口以前只在报工成功那一瞬的弹窗里，
        工人离开页面后就再也找不到了。"""
        code, body = self.get("/report/mine", self.worker)
        self.assertEqual(code, 200)
        self.assertIn("/change?report_id=", body)
        self.assertIn("申请改量", body)

    def test_主管页面显示主管改量(self):
        code, body = self.get("/report/mine?ygno=C002", self.sup)
        self.assertIn("主管代改", body)
        self.assertIn("主管改量", body)

    def test_主管能看别人的报工(self):
        code, body = self.get("/report/mine?ygno=C002", self.sup)
        self.assertEqual(code, 200)
        self.assertIn("C002", body)
        self.assertIn("看谁的报工", body, "主管要有选择器")

    def test_工人不能看别人的报工(self):
        """工人传 ygno 也只能看自己 —— 防止越权查别人工资。"""
        code, body = self.get("/report/mine?ygno=C003", self.worker)
        self.assertEqual(code, 200)
        self.assertNotIn("工人丙", body)

    def test_主管代改直接生效不进待审批(self):
        code, body = self._post_change(self.sup, self.rid, 80, "主管核实后调整")
        self.assertEqual(code, 200, body[:400])
        self.assertIn("已通过", body)
        self.assertEqual(self.db.count_pending_changes(), 0,
                         "代改不该再等审批")

    def test_代改落到approved并记录审批人(self):
        self._post_change(self.sup, self.rid, 80, "主管核实后调整")
        rows = self.db.list_changes(status=None, ygno="C002")
        self.assertEqual(rows[0]["status"], "approved")
        self.assertEqual(rows[0]["approve_gh"], "C001")

    def test_其他工人不能代改(self):
        other = self.login("C003")
        code, body = self._post_change(other, self.rid, 80, "我要改")
        self.assertEqual(code, 403)
        self.assertIn("无权限", body)

    def test_代改页面文案区分(self):
        code, body = self.get(f"/change?report_id={self.rid}", self.sup)
        self.assertIn("主管代改报工", body)
        self.assertIn("直接生效", body)
        code2, body2 = self.get(f"/change?report_id={self.rid}", self.worker)
        self.assertIn("申请修改报工数量", body2)


class TestNoDeadLinks(HTTPTestCase):
    """回归：页面上的每个链接都必须有对应路由。

    后台首页的「待处理」条目曾经指向 /admin/report 和 /admin/change，
    这两个路由压根没注册，点进去就是 404。
    """

    def setUp(self):
        super().setUp()
        self.add_employee("C001", "测试主管")
        self.sup = self.login("C001")

    def test_后台待处理链接都能打开(self):
        import re
        code, body = self.get("/admin", self.sup)
        self.assertEqual(code, 200)
        links = set(re.findall(r'href="(/admin/[^"?]+|/approve[^"?]*)', body))
        self.assertTrue(links, "后台首页应该有入口链接")
        for lk in links:
            sub = self.get(lk, self.sup)
            self.assertNotEqual(sub[0], 404, f"{lk} 是死链")
            self.assertNotEqual(sub[0], 500, f"{lk} 报错了")

    def test_所有已注册路由(self):
        import os
        import re as _re
        import _bootstrap
        src = open(os.path.join(_bootstrap.ROOT, "webapp", "app.py"),
                   encoding="utf-8").read()
        routes = set(_re.findall(r'path == "([^"]+)"', src))
        code, body = self.get("/admin", self.sup)
        for lk in set(_re.findall(r'href="(/[^"?]+)', body)):
            base = lk.rstrip("/") or "/"
            if not (base in routes or any(r.startswith(base + "/")
                                           for r in routes)):
                self.fail(f"死链 {lk}，已注册路由：{sorted(routes)}")


class TestMineTableNotBroken(HTTPTestCase):
    """回归：操作列的 HTML 被 esc 掉，表格显示成一串 &lt;a&gt; 源码。"""

    def setUp(self):
        super().setUp()
        self.add_employee("C002", "员工测试")
        self.add_process("ZD1", 1, "打洞纽扣", 0.5)
        self.add_bundle("ZD1", 1, 11, 100)
        rid = self.db.create_report("S", "ZD1", 1, 11, 1, "打洞纽扣",
                                    "C002", "员工测试", 100, 0.5)["id"]
        self.db.get_conn().execute(
            "UPDATE report SET status='synced', jfgzid=9001 WHERE id=?", (rid,))
        self.db.get_conn().commit()
        self.push_reports([(9001, "2026-10-12 10:00:00", "C002", "员工测试",
                            "ZD1", 1, 1, 11, 100, 0.5, 50.0, 1)])
        self.tok = self.login("C002")

    def test_操作列是活的链接不是源码(self):
        import re
        code, body = self.get("/report/mine", self.tok)
        self.assertEqual(code, 200)
        self.assertNotIn("&lt;a href", body, "操作列的 HTML 被转义了")
        self.assertNotIn("&lt;span", body, "操作列的 HTML 被转义了")
        self.assertIn("/change?report_id=", body)

    def test_行列数一致(self):
        import re
        code, body = self.get("/report/mine", self.tok)
        nhead = len(re.findall(r"<th>", body))
        for m in re.finditer(r'<tr class="r-\w+">(.*?)</tr>', body, re.S):
            ncell = len(re.findall(r"<td", m.group(1)))
            self.assertEqual(ncell, nhead, "表格列数不一致会错行")

    def test_数据列仍然转义防注入(self):
        """操作列免转义，但数据列必须转义。"""
        self.db.get_conn().execute(
            "INSERT OR REPLACE INTO ygzl VALUES (?,?,0)",("C002","员工测试"))
        self.db.get_conn().commit()
        self.push_reports([(9002, "2026-10-12 10:00:00", "C002",
                            "<script>x</script>", "ZD<script>", 1, 1, 11, 5,
                            0.5, 2.5, 1)])
        code, body = self.get("/report/mine", self.tok)
        self.assertNotIn("<script>x</script>", body, "数据列没转义，有注入风险")
        self.assertIn("&lt;script&gt;", body)

    def test_状态列不是源码(self):
        """回归：状态小字（待同步/没报上）曾塞在「时间」列里，
        跟着数据列一起被 esc，显示成 &lt;div&gt; 源码。
        现在状态单独成列且免转义。"""
        self.db.create_report("S2", "ZD1", 1, 11, 2, "裤线头",
                              "C002", "员工测试", 20, 0.3)
        code, body = self.get("/report/mine", self.tok)
        self.assertEqual(code, 200)
        self.assertNotIn("&lt;div", body)
        self.assertNotIn("&lt;span", body)
        self.assertNotIn("&lt;a", body)
        self.assertIn("<th>状态</th>", body)
        self.assertIn("待同步", body)

    def test_失败行标没报上且无转义残留(self):
        rid = self.db.create_report("S3", "ZD1", 1, 11, 1, "打洞纽扣",
                                    "C002", "员工测试", 999, 0.5)["id"]
        self.db.get_conn().execute(
            "UPDATE report SET status='failed', last_err='超量' WHERE id=?", (rid,))
        self.db.get_conn().commit()
        code, body = self.get("/report/mine", self.tok)
        self.assertEqual(code, 200)
        self.assertIn("没报上", body)
        self.assertNotIn("&lt;", body, "不该有任何转义残留")

    def test_状态列与操作列都不转义(self):
        self.db.create_report("S4", "ZD1", 1, 11, 2, "裤线头",
                              "C002", "员工测试", 20, 0.3)
        import re
        code, body = self.get("/report/mine", self.tok)
        nhead = len(re.findall(r"<th>", body))
        for m in re.finditer(r'<tr class="r-\w+">(.*?)</tr>', body, re.S):
            self.assertEqual(len(re.findall(r"<td", m.group(1))), nhead)
        # 状态列必须是活的标签而不是文字
        self.assertIn('<span style="font-size:11px;color:#b26a00">待同步</span>',
                      body)


class TestChangeLinkId(HTTPTestCase):
    """回归：「申请改量」链接曾把 jfgzid 当成 report_id 传出去。

    /change?report_id= 查的是本地 report 表，
    传 jfgzid 进去查不到记录，返回的却是
    「标签未同步」页 —— 和二维码问题毫无关系，纯粹误导。
    """

    def setUp(self):
        super().setUp()
        self.add_employee("C002", "员工测试")
        self.add_process("ZD1", 1, "打洞纽扣", 0.5)
        self.add_bundle("ZD1", 1, 11, 100)
        self.rid = self.db.create_report(
            "S", "ZD1", 1, 11, 1, "打洞纽扣", "C002", "员工测试", 40, 0.5)["id"]
        self.db.get_conn().execute(
            "UPDATE report SET status='synced', jfgzid=129432 WHERE id=?",
            (self.rid,))
        self.db.get_conn().commit()
        self.push_reports([(129432, "2026-10-12 10:00:00", "C002", "员工测试",
                            "ZD1", 1, 1, 11, 40, 0.5, 20.0, 1)])
        self.tok = self.login("C002")

    def test_链接用的是本地report_id(self):
        import re
        code, body = self.get("/report/mine", self.tok)
        m = re.search(r"/change\?report_id=(\d+)", body)
        self.assertTrue(m, "应该有申请改量链接")
        self.assertEqual(int(m.group(1)), self.rid)
        self.assertNotEqual(int(m.group(1)), 129432, "不能把 jfgzid 当 report_id")

    def test_链接点进去不是404也不是标签未同步(self):
        import re
        code, body = self.get("/report/mine", self.tok)
        rid = re.search(r"/change\?report_id=(\d+)", body).group(1)
        code, body = self.get(f"/change?report_id={rid}", self.tok)
        self.assertEqual(code, 200)
        self.assertNotIn("标签还没同步", body, "改量页不该显示二维码的提示页")
        self.assertIn("申请修改报工数量", body)

    def test_别处报的历史工不给改量入口(self):
        """本系统没有对应 report 行的历史数据，不该给改量入口。"""
        self.push_reports([(999999, "2026-10-11 10:00:00", "C002", "员工测试",
                            "ZD2", 1, 1, 1, 30, 0.5, 15.0, 1)])
        self.db.get_conn().execute(
            "INSERT OR REPLACE INTO process VALUES ('ZD2',1,'压下摆',0.5)")
        self.db.get_conn().commit()
        import re
        code, body = self.get("/report/mine", self.tok)
        links = re.findall(r"/change\?report_id=(\d+)", body)
        self.assertNotIn("999999", links)


class TestChangeVisibleAtOnce(HTTPTestCase):
    """回归：改量提交后，列表要立刻反映新数量并标明来源。"""

    def setUp(self):
        super().setUp()
        self.add_employee("C001", "测试主管")
        self.add_employee("C002", "员工测试")
        self.add_process("ZD1", 2, "四针六线裤", 0.77)
        self.add_bundle("ZD1", 1, 10, 100)
        rid = self.db.create_report("S", "ZD1", 1, 10, 2, "四针六线裤",
                                    "C002", "员工测试", 20, 0.77)["id"]
        self.db.get_conn().execute(
            "UPDATE report SET status='synced', jfgzid=555 WHERE id=?", (rid,))
        self.db.get_conn().commit()
        self.push_reports([(555, "2026-10-13 10:00:00", "C002", "员工测试",
                            "ZD1", 2, 1, 10, 20, 0.77, 15.4, 1)])
        self.rid = rid
        self.sup = self.login("C001")
        self.worker = self.login("C002")

    def test_改量后列表立刻显示新数(self):
        self.db.create_change(self.rid, "C002", "员工测试", 11, "多扫",
                              approved_by="C001")
        code, body = self.get("/report/mine", self.worker)
        self.assertEqual(code, 200)
        self.assertIn("已改量", body, "要标明这是改过的")
        self.assertIn("原 20", body)
        import re
        row = re.search(r'<tr class="r-sync">(.*?)</tr>', body, re.S).group(1)
        tds = [re.sub("<[^>]+>", "", t).strip()
for t in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        self.assertIn("11", tds, f"件数应显示新值，实际 {tds}")

    def test_改量中不再给申请入口(self):
        self.db.create_change(self.rid, "C002", "员工测试", 11, "多扫")
        code, body = self.get("/report/mine", self.worker)
        self.assertIn("改量审批中", body)
        self.assertNotIn(f"/change?report_id={self.rid}", body,
                         "已在改量流程中就不该再给入口")

    def test_同步之后不再显示等同步(self):
        """线上症状：后台已经同步过一次了，列表还是「已改量（原 20），等同步」
        加「改量中」，主管每次点开都以为同步没生效。
        """
        cid = self.db.create_change(self.rid, "C002", "员工测试", 11, "多扫",
                                    approved_by="C001")
        self.db.mark_change_synced(cid, True)
        code, body = self.get("/report/mine", self.worker)
        self.assertEqual(code, 200)
        self.assertIn("已同步", body, "已回写的改量要显示为已同步")
        self.assertNotIn("等同步", body, "同步过了还提示等同步")
        self.assertNotIn("改量中", body, "同步过了还说改量中")

    def test_同步之后恢复申请入口(self):
        """改量彻底办完，这条报工就该能再改了。"""
        cid = self.db.create_change(self.rid, "C002", "员工测试", 11, "多扫",
                                    approved_by="C001")
        self.db.mark_change_synced(cid, True)
        code, body = self.get("/report/mine", self.worker)
        self.assertIn(f"/change?report_id={self.rid}", body,
                      "同步完了就不该再锁死")


class TestWorkerPicker(HTTPTestCase):
    """主管选人下拉：带姓名，且收起状态要显示当前选中的那个人。

    没有 selected 属性时浏览器一律高亮第一个 option，
    框里显示的是工号最小的那个，跟下面表格里查的人对不上 ——
    主管只能把下拉拉开看到打钩才知道现在看的是谁。
    """

    def setUp(self):
        super().setUp()
        self.add_employee("C001", "测试主管")
        self.add_employee("C002", "阿珍")
        self.add_employee("C003", "阿强")
        self.add_process("ZD1", 2, "四针六线裤", 0.77)
        self.add_bundle("ZD1", 1, 10, 100)
        for ygno, name in (("C002", "阿珍"), ("C003", "阿强")):
            self.db.create_report("S", "ZD1", 1, 10, 2, "四针六线裤",
                                  ygno, name, 20, 0.77)
        self.sup = self.login("C001")

    def _selected(self, body):
        import re
        m = re.findall(r'<option value="([^"]*)" selected>([^<]*)</option>', body)
        return m

    def test_当前看的人被选中(self):
        code, body = self.get("/report/mine?ygno=C003", self.sup)
        sel = self._selected(body)
        self.assertEqual(len(sel), 1, f"有且只有一个选中项，实际 {sel}")
        self.assertEqual(sel[0][0], "C003",
                         "收起状态必须显示正在看的那个人，不是列表第一个")

    def test_选项带姓名(self):
        code, body = self.get("/report/mine?ygno=C002", self.sup)
        self.assertIn("C002 阿珍", body, "光有工号认不出人")
        self.assertIn("C003 阿强", body)

    def test_默认看自己时也选中自己(self):
        code, body = self.get("/report/mine", self.sup)
        sel = self._selected(body)
        self.assertEqual(len(sel), 1)
        self.assertEqual(sel[0][0], "C001")

    def test_切换人选保留时间段(self):
        code, body = self.get(
            "/report/mine?ygno=C002&from=2026-10-01&to=2026-10-13", self.sup)
        self.assertIn('name="from" value="2026-10-01"', body)
        self.assertIn('name="to" value="2026-10-13"', body)

    def test_工人看不到这个下拉(self):
        code, body = self.get("/report/mine", self.login("C002"))
        self.assertNotIn("<select", body)

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


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class HTTPTestCase(ScanTestCase):
    def setUp(self):
        super().setUp()
        import app
        self.app = app
        # 服务端每请求打一行日志，20 个用例能把输出淹掉
        self._real_log = app.Handler.log_message
        app.Handler.log_message = lambda *a, **k: None
        self.port = _free_port()
        self.srv = start_local_server(app, self.port)
        self.base = f"http://127.0.0.1:{self.port}"

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
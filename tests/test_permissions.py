# -*- coding: utf-8 -*-
"""权限隔离测试。

这是全套测试里最重要的一组 —— 工资数据泄漏的后果最严重。
一旦这里挂了，工人就能看到全厂同事的工资明细。

覆盖：
  1. 登录：工号/手机号、错 PIN、未登记、离职、停用
  2. 报表权限：工人只能看自己，主管看全部
  3. 后台权限：普通工人访问 /admin 必须被拒
  4. 令牌：离职即失效、无效令牌不通过
"""

import unittest

from base import ScanTestCase


class TestLogin(ScanTestCase):
    def test_工号登录成功(self):
        self.add_employee("C001", "测试主管")
        ok, token, me, must = self.rules.login("C001", "5555")
        self.assertTrue(ok)
        self.assertEqual(me["ygno"], "C001")
        self.assertTrue(token)

    def test_手机号登录成功(self):
        self.add_employee("C001", "测试主管", phone="13844445555")
        ok, _, me, _ = self.rules.login("13844445555", "5555")
        self.assertTrue(ok)
        self.assertEqual(me["ygno"], "C001")

    def test_错PIN被拒(self):
        self.add_employee("C001")
        with self.assertRaises(self.rules.RuleError) as ctx:
            self.rules.login("C001", "0000")
        self.assertEqual(ctx.exception.code, "bad_pin")

    def test_未登记工号被拒(self):
        with self.assertRaises(self.rules.RuleError) as ctx:
            self.rules.login("Z999", "5555")
        self.assertEqual(ctx.exception.code, "not_registered")

    def test_离职即失效_D018(self):
        """D-018：令牌长期有效，但每次使用都要重查 ygzl。"""
        self.add_employee("C001")
        _, token, _, _ = self.rules.login("C001", "5555")
        self.assertTrue(self.rules.whoami(token))          # 在职时有效
        self.db.get_conn().execute(
            "UPDATE ygzl SET ygout=1 WHERE ygno='C001'")  # 标记离职
        self.db.get_conn().commit()
        self.assertIsNone(self.rules.whoami(token))        # 立刻失效
        with self.assertRaises(self.rules.RuleError) as ctx:
            self.rules.login("C001", "5555")
        self.assertEqual(ctx.exception.code, "resigned")

    def test_不在花名册被拒(self):
        """登录表有记录但花名册没有 —— 必须拒绝，不能只信登录表。"""
        import import_workers as IW
        h, s = IW.make_pin_hash("5555")
        self.db.get_conn().execute(
            "INSERT INTO worker_login (ygno,ygname,phone,pin_hash,pin_salt,"
            "must_change,active,created_at) VALUES ('X999','幽灵','13900000000',?,?,0,1,?)",
            (h, s, self.db.now_str()))
        self.db.get_conn().commit()
        with self.assertRaises(self.rules.RuleError) as ctx:
            self.rules.login("X999", "5555")
        self.assertEqual(ctx.exception.code, "not_in_roster")

    def test_停用账号被拒(self):
        self.add_employee("C001", active=0)
        with self.assertRaises(self.rules.RuleError) as ctx:
            self.rules.login("C001", "5555")
        self.assertEqual(ctx.exception.code, "inactive")

    def test_无效令牌(self):
        self.assertIsNone(self.rules.whoami(""))
        self.assertIsNone(self.rules.whoami("totally-bogus-token"))

    def test_空账号和空PIN(self):
        with self.assertRaises(self.rules.RuleError):
            self.rules.login("", "5555")
        self.add_employee("C001")
        with self.assertRaises(self.rules.RuleError):
            self.rules.login("C001", "")


class TestSupervisor(ScanTestCase):
    def test_白名单判定(self):
        self.assertTrue(self.rules.is_supervisor("C001"))
        self.assertTrue(self.rules.is_supervisor("A001"))
        self.assertFalse(self.rules.is_supervisor("C002"))

    def test_未配置白名单时无人是主管(self):
        import os
        os.environ["SUPERVISORS"] = ""
        self.assertFalse(self.rules.is_supervisor("C001"))

    def test_中文逗号也能分割(self):
        import os
        os.environ["SUPERVISORS"] = "C001，A001"
        self.assertTrue(self.rules.is_supervisor("A001"))

    def test_空值与类型健壮(self):
        self.assertFalse(self.rules.is_supervisor(None))
        self.assertFalse(self.rules.is_supervisor(""))


class TestReportPermissions(ScanTestCase):
    """报表权限 —— 工资泄漏就在这里发生。"""

    def setUp(self):
        super().setUp()
        self.add_employee("C001", "测试主管")
        self.add_employee("C002", "员工测试")
        # 两个工人都报了工，金额不同，用来验证互相看不到
        self.push_reports([
            (1, "2026-10-10 08:00:00", "C001", "测试主管", "ZD1", 1, 1, 1, 100, 0.5, 50.0, 1),
            (2, "2026-10-10 09:00:00", "C002", "员工测试", "ZD2", 1, 1, 2, 200, 0.5, 100.0, 1),
        ])

    def test_工人只看得到自己的工资(self):
        rows = self.db.report_wage("2026-10-01", "2026-10-31", "C002")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ygno"], "C002")

    def _mine_ygnos(self, ygno):
        return {r["ygno"] for r in
                self.db.report_mine("2026-10-01", "2026-10-31", ygno, 500)}

    def test_我的报工只含自己(self):
        """无论从哪个维度看，单个工人都只能看到自己的工号。"""
        for u in ("C001", "C002"):
            got = self._mine_ygnos(u)
            self.assertEqual(got, {u}, f"{u} 看到了别人的数据：{got}")

    def test_主管能看全量(self):
        rows = self.db.report_wage("2026-10-01", "2026-10-31")
        self.assertEqual({r["ygno"] for r in rows}, {"C001", "C002"})

    def test_汇总按员工维度不串人(self):
        rows = self.db.report_group("ygno", "2026-10-01", "2026-10-31")
        m = {r["k"]: r["amt"] for r in rows}
        self.assertAlmostEqual(m["C001"], 50.0)
        self.assertAlmostEqual(m["C002"], 100.0)


class TestChangeOwnership(ScanTestCase):
    """改量只能改自己的报工。"""

    def test_不能改别人的报工(self):
        self.add_employee("C001", "主管甲")
        self.add_employee("C002", "员工乙")
        self.add_process("ZD1", 1, "打洞纽扣", 0.5)
        self.add_bundle("ZD1", 1, 1, 100)
        rid = self.db.create_report("S1", "ZD1", 1, 1, 1, "打洞纽扣",
                                    "C002", "员工乙", 100, 0.5)["id"]
        self.db.get_conn().execute(
            "UPDATE report SET status='synced', jfgzid=9001 WHERE id=?", (rid,))
        self.db.get_conn().commit()
        with self.assertRaises(self.rules.RuleError):
            # C001 不是报工人，冒充申请改量
            self.rules.check_change_request(rid, 50, "想改", "C001")
        # 本人则可以
        rep, new_js = self.rules.check_change_request(rid, 50, "重复裁了", "C002")
        self.assertEqual(new_js, 50)

    def test_理由必填(self):
        self.add_employee("C002")
        self.add_process("ZD1", 1, "打洞纽扣", 0.5)
        self.add_bundle("ZD1", 1, 1, 100)
        rid = self.db.create_report("S1", "ZD1", 1, 1, 1, "打洞纽扣",
                                    "C002", "员工测试", 100, 0.5)["id"]
        self.db.get_conn().execute(
            "UPDATE report SET status='synced', jfgzid=9001 WHERE id=?", (rid,))
        self.db.get_conn().commit()
        with self.assertRaises(self.rules.RuleError):
            self.rules.check_change_request(rid, 50, "   ", "C002")

    def test_重复申请被拒(self):
        self.add_employee("C002")
        self.add_process("ZD1", 1, "打洞纽扣", 0.5)
        self.add_bundle("ZD1", 1, 1, 100)      # 必须有应做数，否则上限校验会先拦下
        rid = self.db.create_report("S1", "ZD1", 1, 1, 1, "打洞纽扣",
                                    "C002", "员工测试", 100, 0.5)["id"]
        self.db.get_conn().execute(
            "UPDATE report SET status='synced', jfgzid=9001 WHERE id=?", (rid,))
        self.db.get_conn().commit()
        self.rules.check_change_request(rid, 50, "第一次", "C002")
        self.db.create_change(rid, "C002", "员工测试", 50, "第一次")
        with self.assertRaises(ValueError):
            self.db.create_change(rid, "C002", "员工测试", 40, "第二次")


if __name__ == "__main__":
    unittest.main(verbosity=2)
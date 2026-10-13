# -*- coding: utf-8 -*-
"""报工与改量的业务规则测试。

这里守住的是**账**：报工数量对不对、上限拦不拦得住、
改量能不能改、改完金额算得对不对。

重点覆盖几个历史上真实出过的坑：
  - 今天报完工要能立刻看到（日期上界必须是闭区间）
  - 没报的工序不能被漏判成完工（最慢工序取 MIN 要 LEFT JOIN）
  - 张数/应做数不能被「扎×工序」交叉放大
  - 字符串 report_id 绑到 INTEGER 列会炸
"""

import datetime
import os
import unittest

from base import ScanTestCase


class TestReportRules(ScanTestCase):
    def setUp(self):
        super().setUp()
        self.add_employee("C002", "员工测试")
        self.add_process("ZD1", 1, "打洞纽扣", 0.5)
        self.add_process("ZD1", 2, "裤线头", 0.3)
        self.add_bundle("ZD1", 1, 1, 100)
        self.db.upsert_scan_map("SID", "ZD1", 1, 1)   # 二维码映射，缺了会报 bad_short_id

    def test_正常报工(self):
        rep = self.rules.check_and_build_report(
            "SID", "ZD1", 1, 1, 1, "C002", 50)
        self.assertEqual(rep["js"], 50)
        # 金额由 create_report 按 dj 现算，check_and_build_report 不返回 je
        self.assertAlmostEqual(rep["dj"], 0.5)
        rid = self.db.create_report(rep["short_id"], rep["zdno"], rep["cc"],
                                    rep["zh"], rep["gx"], rep["gxname"],
                                    "C002", "员工测试", rep["js"], rep["dj"])["id"]
        self.assertAlmostEqual(self.db.get_report(rid)["je"], 25.0)

    def test_超量被拒(self):
        self.db.create_report("SID", "ZD1", 1, 1, 1, "打洞纽扣",
                              "C002", "员工测试", 90, 0.5)
        with self.assertRaises(self.rules.RuleError) as ctx:
            self.rules.check_and_build_report("SID", "ZD1", 1, 1, 1, "C002", 20)
        self.assertEqual(ctx.exception.code, "over_limit")

    def test_报工数量必须正整数(self):
        for bad in (0, -1, "abc", None):
            with self.assertRaises(self.rules.RuleError):
                self.rules.check_and_build_report(
                    "SID", "ZD1", 1, 1, 1, "C002", bad)

    def test_假二维码被拒(self):
        with self.assertRaises(self.rules.RuleError) as ctx:
            self.rules.check_and_build_report(
                "NOTEXIST", "ZD1", 1, 1, 1, "C002", 10)
        self.assertEqual(ctx.exception.code, "bad_short_id")

    def test_标签与提交不一致被拒(self):
        self.db.upsert_scan_map("SID", "ZD1", 1, 1)
        with self.assertRaises(self.rules.RuleError) as ctx:
            self.rules.check_and_build_report(
                "SID", "ZD1", 9, 9, 1, "C002", 10)
        self.assertEqual(ctx.exception.code, "mismatch")

    def test_重复报工幂等(self):
        """同一个 short_id 重复提交不应重复计入。"""
        self.db.upsert_scan_map("SID", "ZD1", 1, 1)
        a = self.db.create_report("SID", "ZD1", 1, 1, 1, "打洞纽扣",
                                  "C002", "员工测试", 10, 0.5)
        b = self.db.create_report("SID", "ZD1", 1, 1, 1, "打洞纽扣",
                                  "C002", "员工测试", 10, 0.5)
        self.assertEqual(a["id"], b["id"])
        self.assertTrue(b["duplicated"])

    def test_连点两次跨秒也只算一条(self):
        """回归：数据库唯一索引的键里带 gzdate（秒级），
        两次提交只要隔一秒就能各插一条 —— 手机慢一点连点就翻倍。
        这里把第二条的时间戳改到 3 秒后，模拟跨秒，仍必须判重。
        """
        self.db.upsert_scan_map("SID", "ZD1", 1, 1)
        a = self.db.create_report("SID", "ZD1", 1, 1, 1, "打洞纽扣",
                                  "C002", "员工测试", 10, 0.5)
        later = (datetime.datetime.now() - datetime.timedelta(seconds=3)
                 ).strftime("%Y-%m-%d %H:%M:%S")
        self.db.get_conn().execute("UPDATE report SET gzdate=? WHERE id=?",
                                   (later, a["id"]))
        self.db.get_conn().commit()
        b = self.db.create_report("SID", "ZD1", 1, 1, 1, "打洞纽扣",
                                  "C002", "员工测试", 10, 0.5)
        self.assertEqual(a["id"], b["id"],
                         "时间窗内的重复提交必须被判为同一条")
        n = self.db.get_conn().execute(
            "SELECT COUNT(*) FROM report WHERE short_id='SID'").fetchone()[0]
        self.assertEqual(n, 1, "库里只能有一条")

    def test_超过时间窗才算新报工(self):
        """时间窗外（比如工人十分钟后又报一次）应当是新记录。"""
        self.db.upsert_scan_map("SID", "ZD1", 1, 1)
        a = self.db.create_report("SID", "ZD1", 1, 1, 1, "打洞纽扣",
                                  "C002", "员工测试", 10, 0.5)
        old = (datetime.datetime.now() - datetime.timedelta(
            seconds=self.db.IDEM_WINDOW_SEC + 60)
        ).strftime("%Y-%m-%d %H:%M:%S")
        self.db.get_conn().execute("UPDATE report SET gzdate=? WHERE id=?",
                                   (old, a["id"]))
        self.db.get_conn().commit()
        b = self.db.create_report("SID", "ZD1", 1, 1, 1, "打洞纽扣",
                                  "C002", "员工测试", 10, 0.5)
        self.assertNotEqual(a["id"], b["id"], "超出时间窗应视为新报工")


class TestChangeRules(ScanTestCase):
    def setUp(self):
        super().setUp()
        self.add_employee("C002", "员工测试")
        self.add_employee("C001", "测试主管")
        self.add_process("ZD1", 1, "打洞纽扣", 0.5)
        self.add_bundle("ZD1", 1, 1, 100)
        self.rid = self.db.create_report(
            "SID", "ZD1", 1, 1, 1, "打洞纽扣", "C002", "员工测试", 100, 0.5)["id"]
        # 改量要求报工已同步进 jfgz（有 jfgzid），否则改不动
        self.db.get_conn().execute(
            "UPDATE report SET status='synced', jfgzid=9001 WHERE id=?", (self.rid,))
        self.db.get_conn().commit()

    def test_正常改量(self):
        _, new_js = self.rules.check_change_request(self.rid, 92, "重复裁了8件", "C002")
        self.assertEqual(new_js, 92)

    def test_改量超量被拒(self):
        """改后合计不能超过应做数。

        报工同步后数量会落到 report_baseline（SQL Server 的历史基线），
        上限校验读的是 基线 + 未同步的本地报工，所以基线要先写好。
        """
        self.db.get_conn().execute(
            "INSERT OR REPLACE INTO report_baseline "
            "(zdno,cc,zh,gx,reported_qty,updated_at) VALUES (?,1,1,1,?,?)",
            ("ZD1", 30, self.db.now_str()))
        self.db.get_conn().commit()
        # 合计 = 基线30 - 原值100 + 新值 99 = 29 ... 仍不超；
        # 要触发超量必须让 基线 - 原值 + 新值 > 应做，
        # 所以把原报数量调小、基线调大，让改后合计明确超过 100
        self.db.get_conn().execute(
            "UPDATE report SET js=40 WHERE id=?", (self.rid,))
        self.db.get_conn().execute(
            "INSERT OR REPLACE INTO report_baseline VALUES (?,1,1,1,?,?)",
            ("ZD1", 80, self.db.now_str()))
        self.db.get_conn().commit()
        with self.assertRaises(self.rules.RuleError) as ctx:
            self.rules.check_change_request(self.rid, 90, "想改大", "C002")
        self.assertEqual(ctx.exception.code, "over_limit")

    def test_改量数量必须正(self):
        for bad in (0, -5, "abc", None):
            with self.assertRaises(self.rules.RuleError):
                self.rules.check_change_request(self.rid, bad, "理由", "C002")

    def test_审批只能做一次(self):
        cid = self.db.create_change(self.rid, "C002", "员工测试", 92, "重复了")
        ok, _ = self.db.decide_change(cid, True, "C001")
        self.assertTrue(ok)
        ok2, msg = self.db.decide_change(cid, True, "C001")
        self.assertFalse(ok2)
        self.assertIn("已处理", msg)

    def test_审批后不可驳回(self):
        cid = self.db.create_change(self.rid, "C002", "员工测试", 92, "重复了")
        self.db.decide_change(cid, True, "C001")
        ok, _ = self.db.decide_change(cid, False, "C001", "改主意了")
        self.assertFalse(ok)

    def test_驳回记录原因(self):
        cid = self.db.create_change(self.rid, "C002", "员工测试", 92, "重复了")
        self.db.decide_change(cid, False, "C001", "理由不充分")
        rows = self.db.list_changes(status=None, ygno="C002")
        mine = next(r for r in rows if r["id"] == cid)
        self.assertEqual(mine["status"], "rejected")
        self.assertEqual(mine["approve_note"], "理由不充分")


class TestTodayVisible(ScanTestCase):
    """回归：今天报完工要立刻能看到。

    曾经日期上界写成开区间 gzday < 今天，
    导致今天报的全被排掉，必须等到第二天才出现。
    """

    def setUp(self):
        super().setUp()
        self.add_employee("C002", "员工测试")
        self.today = datetime.date.today().isoformat()

    def test_今天的本地暂存立刻可见(self):
        self.db.create_report("S", "ZD1", 1, 1, 1, "打洞纽扣",
                              "C002", "员工测试", 7, 0.5)
        rows = self.db.report_mine("2020-01-01", self.today, "C002")
        self.assertEqual(len(rows), 1, "今天报的没显示出来")

    def test_今天的已同步数据可见(self):
        self.push_reports([(1, f"{self.today} 10:00:00", "C002", "员工测试",
                            "ZD1", 1, 1, 1, 9, 0.5, 4.5, 1)])
        rows = self.db.report_mine("2020-01-01", self.today, "C002")
        self.assertEqual(len(rows), 1)

    def test_已同步的不与本地暂存重复(self):
        self.push_reports([(1, f"{self.today} 10:00:00", "C002", "员工测试",
                            "ZD1", 1, 1, 1, 9, 0.5, 4.5, 1)])
        # 本地那一条已经回填了 jfgzid，说明已同步
        self.db.create_report("S", "ZD1", 1, 1, 1, "打洞纽扣",
                              "C002", "员工测试", 9, 0.5)
        self.db.get_conn().execute("UPDATE report SET jfgzid=1")
        self.db.get_conn().commit()
        rows = self.db.report_mine("2020-01-01", self.today, "C002")
        self.assertEqual(len(rows), 1, "同一笔被算了两次")

    def test_汇总包含未同步(self):
        self.db.create_report("S", "ZD1", 1, 1, 1, "打洞纽扣",
                              "C002", "员工测试", 20, 0.5)
        s = self.db.report_mine_summary("2020-01-01", self.today, "C002")
        self.assertEqual(s["pcs"], 20)
        self.assertEqual(s["pending"], 1)


class TestProgress(ScanTestCase):
    """进度计算 —— 完工判定必须包含「一道没报」的工序。"""

    def setUp(self):
        super().setUp()
        self.add_employee("C002", "员工测试")
        for gx in (1, 2):
            self.add_process("ZD1", gx, f"工序{gx}", 0.5)
        for zh in (1, 2):
            self.add_bundle("ZD1", 1, zh, 100, yn=f"色{zh}")

    def test_只报一道工序不算完工(self):
        """回归：原来 MIN 只在有报工的工序上取，
        2 道工序只报 1 道会被误判 100%。"""
        self.push_reports([(1, "2026-10-10 10:00:00", "C002", "员工测试",
                            "ZD1", 1, 1, 1, 100, 0.5, 50, 1)])
        d = self.db.report_progress("ZD1")
        fin = [b for b in d["bundles"] if b["done"] >= b["plan_qty"]]
        self.assertEqual(fin, [], "只报了一道工序就判定完工了")

    def test_两扎各自按最慢工序算(self):
        self.push_reports([
            (1, "2026-10-10 10:00:00", "C002", "员工测试", "ZD1", 1, 1, 1, 100, 0.5, 50, 1),
            (2, "2026-10-10 10:00:00", "C002", "员工测试", "ZD1", 2, 1, 1, 100, 0.5, 50, 1),
            (3, "2026-10-10 10:00:00", "C002", "员工测试", "ZD1", 1, 1, 2, 40, 0.5, 20, 1),
        ])
        d = self.db.report_progress("ZD1")
        byzh = {b["zh"]: b["done"] for b in d["bundles"]}
        self.assertEqual(byzh[1], 100)    # 两道都报满 → 最慢也是 100
        # 扎2 只有 gx1 报了 40，gx2 一件没报 → 最慢工序是 gx2，结果应为 0
        self.assertEqual(byzh[2], 0)

    def test_总览的扎数不被交叉放大(self):
        """回归：expect 是「扎×工序」行，直接 COUNT/SUM 会把
        2 扎 200 件放大成 4 扎 400 件。"""
        self.push_reports([(1, "2026-10-10 10:00:00", "C002", "员工测试",
                            "ZD1", 1, 1, 1, 100, 0.5, 50, 1)])
        rows = self.db.report_orders_overview(3650)
        r = next(x for x in rows if x["zdno"] == "ZD1")
        self.assertEqual(r["nb"], 2, "扎数被放大了")
        self.assertEqual(r["plan"], 200, "应做数被放大了")

    def test_总览未开工为0(self):
        self.push_reports([(1, "2026-10-10 10:00:00", "C002", "员工测试",
                            "ZD1", 1, 1, 1, 100, 0.5, 50, 1)])
        rows = self.db.report_orders_overview(3650)
        r = next(x for x in rows if x["zdno"] == "ZD1")
        self.assertEqual(r["rate"], 0.0)


class TestTypeCoercion(ScanTestCase):
    """回归：网页传来的都是字符串，绑 INTEGER 列会炸。"""

    def setUp(self):
        super().setUp()
        self.add_employee("C002", "员工测试")
        self.add_process("ZD1", 1, "打洞纽扣", 0.5)
        self.add_bundle("ZD1", 1, 1, 100)

    def test_字符串report_id可用(self):
        rid = self.db.create_report("S", "ZD1", 1, 1, 1, "打洞纽扣",
                                    "C002", "员工测试", 50, 0.5)["id"]
        self.assertIsNotNone(self.db.get_report(str(rid)))
        self.assertFalse(self.db.has_pending_change(str(rid)))
        cid = self.db.create_change(str(rid), "C002", "员工测试", "45", "理由")
        self.assertTrue(cid)

    def test_非法report_id优雅失败(self):
        self.assertIsNone(self.db.get_report("abc"))
        self.assertIsNone(self.db.get_report(None))
        self.assertFalse(self.db.has_pending_change("abc"))
        ok, _ = self.db.decide_change("abc", True, "C001")
        self.assertFalse(ok)


class TestMigration(ScanTestCase):
    """回归：旧库缺列导致改量页 500（no such column: ygno）。"""

    def test_阶段1旧表可自动升级(self):
        import sqlite3
        path = self.db_path
        c = sqlite3.connect(path)
        c.execute("DROP TABLE change_request")
        c.execute("""CREATE TABLE change_request (
            id INTEGER PRIMARY KEY AUTOINCREMENT, report_id INTEGER NOT NULL,
            new_num INTEGER NOT NULL, created_at TEXT)""")
        c.execute("INSERT INTO change_request (report_id,new_num,created_at)"
                  " VALUES (1,50,'2026-10-10')")
        c.commit()
        c.close()

        # init_db 走模块级的线程缓存连接，得先把缓存摘掉让它重开一个，
        # 否则会拿到刚才 sqlite3 直接 close 掉的连接，报
        # "Cannot operate on a closed database"
        conn = self.db.get_conn()
        conn.close()
        self.db._local.__dict__.pop("conn", None)

        os.environ.pop("HBC_QUIET_MIGRATE", None)   # 这次要看到迁移日志
        self.db.init_db()
        os.environ["HBC_QUIET_MIGRATE"] = "1"

        cols = {r[1] for r in self.db.get_conn().execute(
            "PRAGMA table_info(change_request)")}
        for need in ("ygno", "old_num", "reason", "status", "synced"):
            self.assertIn(need, cols, f"缺列 {need}")
        row = self.db.get_conn().execute(
            "SELECT report_id,new_num,status FROM change_request").fetchone()
        self.assertEqual(row[1], 50, "历史数据丢了")

    def test_迁移幂等(self):
        self.db.init_db()
        self.db.init_db()      # 再跑两次不能炸


if __name__ == "__main__":
    import os
    os.environ["HBC_QUIET_MIGRATE"] = "1"
    unittest.main(verbosity=2)

class TestDiscard(ScanTestCase):
    """作废未同步的报工 —— 失败报工唯一的出路。"""

    def setUp(self):
        super().setUp()
        self.add_employee("C001", "主管")
        self.add_employee("C002", "工人乙")
        self.add_process("ZD1", 1, "打洞纽扣", 0.5)
        self.add_bundle("ZD1", 1, 1, 100)
        self.rid = self.db.create_report(
            "S", "ZD1", 1, 1, 1, "打洞纽扣", "C002", "工人乙", 150, 0.5)["id"]
        self.db.get_conn().execute(
            "UPDATE report SET status='failed', last_err='超量' WHERE id=?",
            (self.rid,))
        self.db.get_conn().commit()

    def test_作废失败报工(self):
        ok, msg = self.db.discard_report(self.rid, "C002")
        self.assertTrue(ok, msg)
        r = self.db.get_report(self.rid)
        self.assertEqual(r["status"], "discarded")

    def test_已同步的不许作废(self):
        """铁律：已进 jfgz 的不能直接删，否则账实不符。"""
        self.db.get_conn().execute(
            "UPDATE report SET status='synced', jfgzid=999 WHERE id=?",
            (self.rid,))
        self.db.get_conn().commit()
        ok, msg = self.db.discard_report(self.rid, None)
        self.assertFalse(ok)
        self.assertIn("改量", msg)

    def test_不能作废别人的(self):
        ok, msg = self.db.discard_report(self.rid, "C001")
        self.assertFalse(ok)
        self.assertIn("自己的", msg)

    def test_主管可以作废任何人的(self):
        ok, msg = self.db.discard_report(self.rid, None)
        self.assertTrue(ok, msg)

    def test_不存在的记录(self):
        ok, msg = self.db.discard_report(99999, None)
        self.assertFalse(ok)

    def test_作废后不计入工人统计(self):
        import datetime
        today = datetime.date.today().isoformat()
        self.assertEqual(len(self.db.report_mine("2020-01-01", today, "C002")), 1)
        self.db.discard_report(self.rid, "C002")
        self.assertEqual(len(self.db.report_mine("2020-01-01", today, "C002")), 0)
        self.assertEqual(self.db.report_mine_summary(
            "2020-01-01", today, "C002")["pcs"], 0)

    def test_作废状态不计入待同步(self):
        rows = self.db.list_reports("pending", 50)
        self.assertEqual(len(rows), 0, "作废的不能再被同步器捡走")


class TestSupervisorEdit(ScanTestCase):
    """主管代改 —— 走同一条 jfzg_change 留痕链路，不开后门。"""

    def setUp(self):
        super().setUp()
        self.add_employee("C001", "测试主管")
        self.add_employee("C002", "工人乙")
        self.add_process("ZD1", 1, "打洞纽扣", 0.5)
        self.add_bundle("ZD1", 1, 11, 100)
        self.rid = self.db.create_report(
            "S", "ZD1", 1, 11, 1, "打洞纽扣", "C002", "工人乙", 100, 0.5)["id"]
        # 必须已同步才有 jfgzid，否则改量无处落地
        self.db.get_conn().execute(
            "UPDATE report SET status='synced', jfgzid=999 WHERE id=?", (self.rid,))
        self.db.get_conn().commit()

    def test_主管能改工人的报工(self):
        rep, new_js = self.rules.check_change_request(
            self.rid, 80, "点数有误", "C001", supervisor=True)
        self.assertEqual(new_js, 80)
        self.assertEqual(rep["ygno"], "C002")   # 归属仍是工人，不是主管

    def test_代改落成已通过(self):
        cid = self.db.create_change(self.rid, "C002", "工人乙", 80,
                                    "点数有误", approved_by="C001")
        r = next(x for x in self.db.list_changes(status=None, ygno="C002")
                 if x["id"] == cid)
        self.assertEqual(r["status"], "approved")
        self.assertEqual(r["approve_gh"], "C001")

    def test_代改同样要理由(self):
        with self.assertRaises(self.rules.RuleError) as ctx:
            self.rules.check_change_request(self.rid, 80, "  ", "C001",
                                            supervisor=True)
        self.assertEqual(ctx.exception.code, "no_reason")

    def test_其他工人仍然不能改(self):
        self.add_employee("C003", "工人丙")
        with self.assertRaises(self.rules.RuleError) as ctx:
            self.rules.check_change_request(self.rid, 80, "理由", "C003",
                                            supervisor=False)
        self.assertEqual(ctx.exception.code, "not_owner")

    def test_报工本人仍可申请(self):
        """C002 本人申请改自己的，权限不该被主管功能挤掉。"""
        rep, new_js = self.rules.check_change_request(
            self.rid, 80, "点数有误", "C002", supervisor=False)
        self.assertEqual(new_js, 80)

    def test_代改也要守上限(self):
        """主管也不能把量改到超过应做数。"""
        self.db.get_conn().execute(
            "INSERT OR REPLACE INTO report_baseline "
            "(zdno,cc,zh,gx,reported_qty,updated_at) VALUES (?,1,11,1,?,?)",
            ("ZD1", 60, self.db.now_str()))
        self.db.get_conn().commit()
        # 60 - 100 + 99 = 59，仍不超；把原值调到 40 才能构造出超量
        self.db.get_conn().execute(
            "UPDATE report SET js=40 WHERE id=?", (self.rid,))
        self.db.get_conn().commit()
        with self.assertRaises(self.rules.RuleError) as ctx:
            self.rules.check_change_request(self.rid, 95, "想改大", "C001",
                                            supervisor=True)
        self.assertEqual(ctx.exception.code, "over_limit")

    def test_未同步的改不了(self):
        """同步失败的报工没有 jfgzid，只能作废后重报。"""
        rid2 = self.db.create_report("S3", "ZD1", 1, 11, 1, "打洞纽扣",
                                     "C002", "工人乙", 150, 0.5)["id"]
        self.db.get_conn().execute(
            "UPDATE report SET status='failed', last_err='超量' WHERE id=?",
            (rid2,))
        self.db.get_conn().commit()
        with self.assertRaises(self.rules.RuleError) as ctx:
            self.rules.check_change_request(rid2, 10, "改小", "C001",
                                            supervisor=True)
        self.assertEqual(ctx.exception.code, "not_synced")
        self.assertIn("作废", ctx.exception.message)


class TestSchemaDrivenMigration(ScanTestCase):
    """回归：迁移必须由 schema.sql 驱动，不能手写列清单。

    手写清单会随表结构改动悄悄过期 —— 线上就因此漏了
    jfgzid/gx/gxname/zdno/cc/zh 六个列，主管代改时炸
    "table change_request has no column named gx"。
    已连踩两次，所以改成从 schema.sql 解析。
    """

    def test_解析出所有表的列(self):
        cols = self.db._schema_columns()
        self.assertIn("change_request", cols)
        self.assertIn("report", cols)
        names = [n for n, _ in cols["change_request"]]
        for need in ("id", "report_id", "jfgzid", "ygno", "gx", "gxname",
                     "zdno", "cc", "zh", "old_num", "new_num",
                     "reason", "status", "approve_gh", "created_at", "synced"):
            self.assertIn(need, names, f"没解析出 {need}")

    def test_旧表自动补齐到schema(self):
        import sqlite3
        conn = self.db.get_conn()
        self.db.get_conn().close()
        self.db._local.__dict__.pop("conn", None)
        raw = sqlite3.connect(self.db_path)
        raw.execute("DROP TABLE change_request")
        # 复刻线上那版：只有阶段1的四个字段
        raw.execute("""CREATE TABLE change_request (
            id INTEGER PRIMARY KEY AUTOINCREMENT, report_id INTEGER NOT NULL,
            new_num INTEGER NOT NULL, created_at TEXT)""")
        raw.execute("INSERT INTO change_request (report_id,new_num,created_at)"
                    " VALUES (999,50,'2026-10-10')")   # 用不存在的 report_id
        raw.commit()
        raw.close()

        self.db.init_db()
        have = {r[1] for r in self.db.get_conn().execute(
            "PRAGMA table_info(change_request)")}
        want = {n for n, _ in self.db._schema_columns()["change_request"]}
        self.assertEqual(want - have, set(), "仍有列没补上")
        # 历史数据必须还在
        row = self.db.get_conn().execute(
            "SELECT report_id,new_num,created_at FROM change_request").fetchone()
        self.assertEqual(tuple(row), (999, 50, "2026-10-10"))

    def test_迁移后能正常写入新列(self):
        """这才是真正要防的：迁移补齐后，代码的 INSERT 要能跑通。"""
        conn = self.db.get_conn()
        conn.execute("DROP TABLE change_request")
        conn.execute("""CREATE TABLE change_request (
            id INTEGER PRIMARY KEY AUTOINCREMENT, report_id INTEGER NOT NULL,
            new_num INTEGER NOT NULL, created_at TEXT)""")
        conn.execute("INSERT INTO change_request (report_id,new_num,created_at)"
                    " VALUES (999,50,'2026-10-10')")
        conn.commit()
        conn.close()
        self.db._local.__dict__.pop("conn", None)
        self.db.init_db()

        self.add_process("ZD1", 1, "打洞纽扣", 0.5)
        self.add_bundle("ZD1", 1, 11, 100)
        rid = self.db.create_report("S", "ZD1", 1, 11, 1, "打洞纽扣",
                                    "C002", "员工测试", 40, 0.5)["id"]
        self.db.get_conn().execute(
            "UPDATE report SET status='synced', jfgzid=1 WHERE id=?", (rid,))
        self.db.get_conn().commit()
        # 这里以前就是 "no such column named gx" 的现场
        cid = self.db.create_change(rid, "C002", "员工测试", 35, "少裁了")
        self.assertTrue(cid)

    def test_主管代改写入全部列(self):
        self.add_employee("C001", "主管")
        self.add_process("ZD1", 1, "打洞纽扣", 0.5)
        self.add_bundle("ZD1", 1, 11, 100)
        rid = self.db.create_report("S", "ZD1", 1, 11, 1, "打洞纽扣",
                                    "C002", "工人", 40, 0.5)["id"]
        self.db.get_conn().execute(
            "UPDATE report SET status='synced', jfgzid=7 WHERE id=?", (rid,))
        self.db.get_conn().commit()
        cid = self.db.create_change(rid, "C002", "工人", 30, "调整",
                                    approved_by="C001", note="调整")
        r = next(x for x in self.db.list_changes(status=None, ygno="C002")
                 if x["id"] == cid)
        self.assertEqual(r["gx"], 1)
        self.assertEqual(r["zdno"], "ZD1")
        self.assertEqual(r["status"], "approved")


class TestLegacyColumnMigration(ScanTestCase):
    """回归：历史遗留列 new_js 让主管代改一直报 NOT NULL 失败。

    change_request 最早建表用 new_js（NOT NULL 无默认值），
    后来 schema.sql 改成 new_num，迁移补上了 new_num，
    但 new_js 留在表里 —— 代码不写它，
    而它 NOT NULL 又没默认值，于是每次插入都炸。

    线上报错原文：
      NOT NULL constraint failed: change_request.new_js
    """

    def _make_legacy(self):
        conn = self.db.get_conn()
        conn.execute("DROP TABLE change_request")
        conn.execute("""CREATE TABLE change_request (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id   INTEGER NOT NULL,
            jfgzid      INTEGER,
            new_js      INTEGER NOT NULL,
            reason      TEXT    NOT NULL DEFAULT '',
            status      TEXT    NOT NULL DEFAULT 'pending',
            created_at  TEXT    NOT NULL,
            approve_at  TEXT,
            approve_gh  TEXT)""")
        conn.execute("INSERT INTO change_request (report_id,new_js,created_at,status)"
                     " VALUES (555,50,'2026-10-01','pending')")
        conn.commit()
        conn.close()
        self.db._local.__dict__.pop("conn", None)
        self.db.init_db()
        return {r[1]: (r[3], r[4]) for r in self.db.get_conn().execute(
            "PRAGMA table_info(change_request)")}

    def test_遗留列被放宽(self):
        info = self._make_legacy()
        self.assertEqual(info["new_js"][0], 0,
                         "new_js 必须放宽，否则 INSERT 永远失败")

    def test_正常列的约束不被削弱(self):
        info = self._make_legacy()
        for col in ("report_id", "created_at", "status", "reason"):
            self.assertEqual(info[col][0], 1,
                             f"{col} 的 NOT NULL 不该被改弱")
        self.assertEqual(info["status"][1], "'pending'")

    def test_历史数据不丢(self):
        self._make_legacy()
        row = self.db.get_conn().execute(
            "SELECT report_id,new_js,created_at FROM change_request").fetchone()
        self.assertEqual(tuple(row), (555, 50, "2026-10-01"))

    def test_有备份表可回退(self):
        self._make_legacy()
        names = [r[0] for r in self.db.get_conn().execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE '%legacy_backup%'")]
        self.assertIn("change_request_legacy_backup", names,
                      "重建前必须留备份，否则出问题无法回退")

    def test_只备份真正需要处理的表(self):
        """不该把所有表都重建一遍 —— 正常表的约束不能被动。"""
        self._make_legacy()
        names = [r[0] for r in self.db.get_conn().execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE '%legacy_backup%'")]
        self.assertEqual(names, ["change_request_legacy_backup"])

    def test_迁移后主管代改能写入(self):
        """这就是线上炸的那一步。"""
        self._make_legacy()
        self.add_process("ZD1", 1, "打洞纽扣", 0.5)
        self.add_bundle("ZD1", 1, 11, 100)
        rid = self.db.create_report("S", "ZD1", 1, 11, 1, "打洞纽扣",
                                    "C002", "工人", 40, 0.5)["id"]
        self.db.get_conn().execute(
            "UPDATE report SET status='synced', jfgzid=7 WHERE id=?", (rid,))
        self.db.get_conn().commit()
        cid = self.db.create_change(rid, "C002", "工人", 30, "调整",
                                    approved_by="C001", note="调整")
        self.assertTrue(cid)

    def test_迁移后工人申请也能写入(self):
        self._make_legacy()
        self.add_process("ZD1", 1, "打洞纽扣", 0.5)
        self.add_bundle("ZD1", 1, 11, 100)
        rid = self.db.create_report("S", "ZD1", 1, 11, 1, "打洞纽扣",
                                    "C002", "工人", 40, 0.5)["id"]
        self.db.get_conn().execute(
            "UPDATE report SET status='synced', jfgzid=7 WHERE id=?", (rid,))
        self.db.get_conn().commit()
        self.assertTrue(self.db.create_change(rid, "C002", "工人", 30, "申请"))

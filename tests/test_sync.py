# -*- coding: utf-8 -*-
"""改量回写逻辑测试（sync_reports.py）。

绝不连真实数据库 —— 用 FakeCursor 验证「发了什么 SQL、带什么参数」，
因为这条链路真的要往生产 jfgz 写数据，测试里连一下就出事。

守住的是账：审批留痕必须先于改数、金额要重算、必须幂等。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webapp"))
import _bootstrap  # noqa: F401,E402
import sync_reports as SR   # noqa: E402

from base import FakeCursor, ScanTestCase   # noqa: E402


# 这份夹具必须是 /api/changes **真实发出来的字段名**。
# 之前这里写的是 new_js，而 rules.change_view 发的是 new_num ——
# 字段名对不上，cur_change_id / apply_change 每次都抛 KeyError，
# 被 main() 的 except 吞成「回执 ok=False」，改量一条都没写进 jfgz。
# 单测用自己的夹具、绕开真实接口，所以一直没暴露。
CHANGE = {
    "id": 1, "jfgzid": 129416, "ygno": "C002", "old_num": 100, "new_num": 92,
    "reason": "这件重复裁了8件", "status": "approved", "approve_gh": "C001",
    "approve_note": "", "created_at": "2026-10-12 10:05:00",
    "approve_at": "2026-10-12 10:30:00", "short_id": "",
}


class TestCurChangeId(unittest.TestCase):
    """审批单留痕 —— 以前这个函数根本没定义，改量功能从上线起就没生效过。"""

    def test_函数存在(self):
        """回归：sync_reports 调用了 cur_change_id 但没定义，一执行就 NameError。"""
        self.assertTrue(hasattr(SR, "cur_change_id"))

    def test_首次写入审批单(self):
        # 键的顺序即匹配优先级：靠前的先命中
        cur = FakeCursor(rows_by_key={
            "FROM jfzg_change": [],                       # 没写过 → 要插
            "FROM jfgz WHERE": [["ZD1", 1, 1, 11]],
            "FROM jfdj": [["四针六线衣"]],
            "INSERT INTO jfzg_change": [],                # 插入本身不返回
        })
        cid = SR.cur_change_id(cur, CHANGE)
        self.assertEqual(cid, 999)
        ins = cur.executed_sql("INSERT INTO jfzg_change")
        self.assertEqual(len(ins), 1, "应该插入一条审批单")
        # 审批状态必须是「已通过」(status=1)，否则 apply_change 会拒绝
        # 参数顺序：0 short_id 1 jfgzid 2 ygno 3 gx 4 gxname 5 zdno
        #            6 cc 7 zh 8 old_num 9 new_num 10 reason 11 status
        #            12 approve_gh ...
        args = [a for s, a in cur.log if "INSERT INTO jfzg_change" in s][0]
        self.assertEqual(args[11], 1, "status 必须是 1（已通过）")
        self.assertEqual(args[2], "C002")
        self.assertEqual(args[9], 92)
        self.assertEqual(args[12], "C001", "要记下审批人")
        self.assertEqual(args[8], 100, "要留改前数量")

    def test_幂等_已存在则复用不重复插(self):
        """重跑同步器不能插出重复审批单。"""
        cur = FakeCursor(rows_by_key={
            "SELECT TOP 1 id FROM jfzg_change": [[42]],
        })
        cid = SR.cur_change_id(cur, CHANGE)
        self.assertEqual(cid, 42)
        self.assertEqual(cur.executed_sql("INSERT INTO jfzg_change"), [])

    def test_报工不存在时返回None(self):
        cur = FakeCursor(rows_by_key={
            "FROM jfzg_change": [],
            "FROM jfgz WHERE": [],                        # jfgz 里没有
        })
        self.assertIsNone(SR.cur_change_id(cur, CHANGE))


class TestApplyChange(unittest.TestCase):
    """改写 jfgz 本身。"""

    def setUp(self):
        self.ch = dict(CHANGE)

    def _cur(self, **kw):
        base = {
            "status FROM jfzg_change": [[7, "C002", 1]],   # 审批单存在且已通过
            "ISNULL(dj,0) FROM jfgz": [["ZD1", 1, 11, 1, 100, 0.5]],
        }
        base.update(kw)
        return FakeCursor(rows_by_key=base)

    def test_正常改量且重算金额(self):
        cur = self._cur(**{"SUM(js),0) FROM jfgz": [[100]],
                          "FROM jfzd2 WHERE": [[264]]})
        ok, msg, cid = SR.apply_change(cur, self.ch)
        self.assertTrue(ok, msg)
        upd = [a for s, a in cur.log if s.startswith("UPDATE jfgz SET js")]
        self.assertEqual(len(upd), 1)
        # UPDATE jfgz SET js=?, je=? WHERE jfgzid=? → (新数量, 金额, id)
        self.assertEqual(upd[0][0], 92, "新数量")
        self.assertAlmostEqual(upd[0][1], 46.0, msg="金额要按 92*0.5 重算")
        self.assertEqual(upd[0][2], 129416)

    def test_超量被拒且不改数(self):
        """回归：不能出现「审批都过了还写成超量」。"""
        cur = self._cur(**{"SUM(js),0) FROM jfgz": [[300]],
                          "FROM jfzd2 WHERE": [[264]]})
        ok, msg, _ = SR.apply_change(cur, self.ch)
        self.assertFalse(ok)
        self.assertIn("超量", msg)
        self.assertEqual(cur.executed_sql("UPDATE jfgz SET js"), [],
                         "被拒时绝不能改 jfgz")

    def test_数量相同不重复改(self):
        ch = dict(self.ch, new_num=100)
        cur = self._cur(**{"SUM(js),0) FROM jfgz": [[100]],
                          "FROM jfzd2 WHERE": [[264]]})
        ok, msg, _ = SR.apply_change(cur, ch)
        self.assertTrue(ok)
        self.assertEqual(cur.executed_sql("UPDATE jfgz SET js"), [],
                         "数量没变就不该发 UPDATE")

    def test_缺审批单被拒(self):
        """铁律：jfzg 里没有已通过的审批单，绝不允许改数。"""
        cur = self._cur(**{"status FROM jfzg_change": []})
        ok, msg, _ = SR.apply_change(cur, self.ch)
        self.assertFalse(ok)
        self.assertIn("审批单", msg)
        self.assertEqual(cur.executed_sql("UPDATE jfgz SET js"), [])

    def test_报工未同步不能改(self):
        ch = dict(self.ch, jfgzid=None)
        cur = FakeCursor()
        ok, msg, _ = SR.apply_change(cur, ch)
        self.assertFalse(ok)
        self.assertIn("还没同步", msg)


class TestInsertReport(unittest.TestCase):
    """报工写库语句本身。"""

    def test_插入报工带barcode1和完整时间(self):
        cur = FakeCursor(scalars={"@@IDENTITY": [[555]]})
        rec = {"gzdate": "2026-10-12 10:30:00", "ygno": "C002", "zdno": "ZD1",
               "gx": 1, "cc": 1, "zh": 11, "js": 40}
        jid = SR.insert_report(cur, rec, "四针六线衣", 0.5)
        self.assertEqual(jid, 555)
        sql, args = cur.log[0]
        self.assertIn("barcode", sql)
        self.assertIn("gzdate", sql)
        self.assertIn("2026-10-12 10:30:00", args)   # 完整时间，不是只有日期

    def test_金额按四舍五入三位(self):
        cur = FakeCursor(scalars={"@@IDENTITY": [[1]]})
        rec = {"gzdate": "2026-10-12 10:00:00", "ygno": "C002", "zdno": "ZD1",
               "gx": 1, "cc": 1, "zh": 1, "js": 33}
        SR.insert_report(cur, rec, "x", 0.1234567)
        # jfgz INSERT 参数：0 gzdate 1 ygno 2 zdno 3 gx 4 cc 5 zh 6 js 7 dj 8 je
        self.assertAlmostEqual(cur.log[0][1][8], 4.074, places=3)


class TestChangePayloadContract(ScanTestCase):
    """接口字段名与同步器之间的契约 —— 改量上线以来从没真正成功过的根因。

    线上表现：主管改量，页面一直显示「已改量（原 20），等同步」+「改量中」，
    后台同步任务跑过好几轮，jfgz 里的数量纹丝不动。

    链路：rules.change_view → /api/changes → sync_reports.cur_change_id /
    apply_change。两边字段名只要对不上就是 KeyError，被 main() 的
    except 吞掉当成「写回失败」，从日志上看还像那么回事。
    单测之前用手抄夹具 + 直接调函数，绕开了真实接口，两边对不上也测不出来。

    这里不用手抄：真的造一条 change_request，走真的 change_view，
    再喂给真的同步器函数。
    """

    def _real_payload(self):
        self.add_employee("C001", "主管")
        self.add_employee("C002", "工人")
        self.add_process("ZD1", 2, "四针六线裤", 0.77)
        self.add_bundle("ZD1", 1, 10, 100)
        rid = self.db.create_report("S", "ZD1", 1, 10, 2, "四针六线裤",
                                    "C002", "工人", 20, 0.77)["id"]
        self.db.get_conn().execute(
            "UPDATE report SET status='synced', jfgzid=555 WHERE id=?", (rid,))
        self.db.get_conn().commit()
        cid = self.db.create_change(rid, "C002", "工人", 9, "多扫了",
                                    approved_by="C001")
        ch = self.db.get_conn().execute(
            "SELECT * FROM change_request WHERE id=?", (cid,)).fetchone()
        import rules
        return rules.change_view(ch)

    def test_真实payload能走通留痕(self):
        payload = self._real_payload()
        self.assertEqual(payload["new_num"], 9, "接口发的是 new_num")
        cur = FakeCursor(rows_by_key={
            "FROM jfzg_change": [],                    # 没写过 → 要插
            "FROM jfgz": [[555, 2, 1, 10]],            # 报工本身在
            "FROM jfdj": [["四针六线裤"]],
            "@@IDENTITY": [[700]],
        })
        cid = SR.cur_change_id(cur, payload)          # KeyError 就是回归
        self.assertEqual(cid, 700)
        sql = " ".join(s for s, _ in cur.log)
        self.assertIn("INSERT INTO jfzg_change", sql)
        # 插入参数里必须带着改后数量 9
        params = [a for s, a in cur.log if "INSERT INTO jfzg_change" in s][0]
        self.assertIn(9, params, f"改后数量没带上：{params}")

    def test_真实payload能走通改数(self):
        payload = self._real_payload()
        cur = FakeCursor(rows_by_key={
            "status FROM jfzg_change": [[700, "C002", 1]],      # 审批单存在且通过
            # 报工当前值：zdno, cc, zh, gx, js, dj
            "ISNULL(dj,0) FROM jfgz": [["ZD1", 1, 10, 2, 20, 0.77]],
            "SUM(js),0) FROM jfgz": [[100]],
            "FROM jfzd2 WHERE": [[100]],
        })
        ok, msg, cid = SR.apply_change(cur, payload)
        self.assertTrue(ok, msg)
        self.assertEqual(cid, 700)
        upd = [a for s, a in cur.log if s.startswith("UPDATE jfgz SET js")]
        self.assertTrue(upd, "没有发出改数语句")
        self.assertEqual(upd[0][0], 9, "改成的新数量不对")

    def test_没有残留的new_js字面量(self):
        """静态兜底：sync_reports 里不该再直接索引 ch["new_js"]。"""
        import re
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "webapp", "sync_reports.py")
        src = open(path, encoding="utf-8").read()
        body = re.sub(r'"""[\s\S]*?"""', "", src)      # 注释里提一句不算
        for hit in re.findall(r'ch\[(?:"|\')new_js(?:"|\')\]', body):
            self.fail(f"还有直接索引 ch[{hit}] —— 接口字段名是 new_num")


class TestReportFetcher(unittest.TestCase):
    """推送端取数：字段顺序不能错，错一个报表就全乱。"""

    def test_默认参数(self):
        self.assertEqual(SR.MAX_BATCH, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)

class TestMainFlowOrder(unittest.TestCase):
    """回归：连接必须在改量处理**之前**建立。

    之前 conn/cur 是在改量循环之后才赋值的，
    改量分支里的 cur 是未赋值的局部变量，线上报：
      UnboundLocalError: cannot access local variable 'cur'
    于是改量一条都写不进去，日志里还留着成功假象
    （[2/2] 报表推送照常成功，掩盖了 [1/2] 的失败）。

    单测之前只用 FakeCursor 直接调函数，绕过了 main()，
    所以这个顺序问题一直没被发现。这里补一个真正跑 main() 的测试。
    """

    def test_改量分支执行前conn已建立(self):
        """静态检查：main() 里 connect() 必须早于第一次用 cur。"""
        import re
        src = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "webapp", "sync_reports.py"), encoding="utf-8").read()
        m = re.search(r"\ndef main\(\):(.*?)\nif __name__", src, re.S)
        self.assertTrue(m, "找不到 main()")
        body = m.group(1)
        first_use = body.find("cur_change_id(cur")
        self.assertNotEqual(first_use, -1, "main 里应该处理改量")
        connect_at = body.find("conn = connect(")
        cursor_at = body.find("cur = conn.cursor()")
        self.assertTrue(connect_at != -1 and cursor_at != -1)
        self.assertLess(connect_at, cursor_at, "cursor 要在 connect 之后")
        self.assertLess(cursor_at, first_use,
                        "cur 必须在第一次被使用**之前**就绪 —— "
                        "这是之前 UnboundLocalError 的根因")

    def test_空数据分支不会泄漏连接(self):
        import re
        src = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "webapp", "sync_reports.py"), encoding="utf-8").read()
        self.assertIn("conn.close()", src, "提前 open 连接后必须记得关")

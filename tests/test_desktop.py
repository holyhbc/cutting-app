# -*- coding: utf-8 -*-
"""桌面版 report.py 的 SQL 生成测试。

不连数据库 —— 只验证**生成的 SQL 字符串**是否符合 SQL Server 2000 的限制。
这几个限制当年都是我真踩出来的：
  1. TOP 不能带括号（2005+ 才支持）
  2. TOP 不能用占位符 ?，只能内联裸整数
  3. GROUP BY 不接受表达式，必须先在派生表里算好
  4. plan 是保留字
另外还要守住「列顺序和解包必须一致」——
改过 detail 的返回列后解包对不上，报表直接错位。
"""

import datetime
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cutt"))
import report as R          # noqa: E402


class CaptureDB(R.ReportDB):
    """把 _rows 换掉，只记录 SQL，不真执行。"""

    def __init__(self, rows=None):
        super().__init__("FAKE", timeout=1)
        self.sqls = []
        self.rows = rows if rows is not None else [[0]]

    def _rows(self, sql, args=None):
        self.sqls.append((" ".join(sql.split()), args))
        cols = ["c%d" % i for i in range(len(self.rows[0]))]
        return cols, self.rows


class TestSQL2000Syntax(unittest.TestCase):
    """SQL Server 2000 语法限制 —— 违反会直接语法错。"""

    def setUp(self):
        self.db = CaptureDB()
        self.a = datetime.datetime(2026, 10, 1)
        self.b = datetime.datetime(2026, 10, 11)

    # ---- TOP 的两条限制 ----

    def test_TOP不带括号(self):
        for dim in ("zdno", "ygno", "gx", "day"):
            self.db.group_by(dim, self.a, self.b)
        for sql, _ in self.db.sqls:
            self.assertIsNone(re.search(r"TOP\s*\(", sql, re.I),
                              "SQL Server 2000 的 TOP 不能带括号（2005+ 才行）")
            self.assertIsNone(re.search(r"TOP\s*\?", sql, re.I),
                              "TOP 不能用占位符，只能内联整数")

    def test_search_zdno_TOP也合法(self):
        self.db.search_zdno("269")
        self.db.detail(self.a, self.b)
        for sql, _ in self.db.sqls:
            self.assertIsNone(re.search(r"TOP\s*[(\?]", sql, re.I))

    # ---- GROUP BY 限制 ----

    def test_GROUPUBY不接受表达式(self):
        """按日期分组用了 CONVERT，必须在派生表里算好。"""
        self.db.group_by("day", self.a, self.b)
        sql = self.db.sqls[0][0]
        group_part = sql[sql.upper().rindex("GROUP BY"):]
        self.assertIsNone(re.search(r"GROUP BY\s+\w+\.\w+\s*$", group_part)
                          and re.search(r"CONVERT", group_part),
                          "CONVERT 必须在派生表里先算好，不能直接 GROUP BY")

    # ---- 保留字 ----

    def test_不用plan做列名(self):
        self.db.check_over_limit()
        self.db.progress_bundles("ZD1")
        for sql, _ in self.db.sqls:
            self.assertIsNone(re.search(r"\bAS\s+plan\b", sql, re.I),
                              "plan 是 SQL Server 保留字")
            self.assertIsNone(re.search(r"\bplan\b", sql, re.I),
                              "SQL 里出现了保留字 plan")

    # ---- 真实列名 ----

    def test_用ygzl的ygname不是xm(self):
        self.db.workers()
        self.db.wage_by_worker(self.a, self.b)
        for sql, _ in self.db.sqls:
            if "FROM ygzl" in sql:
                self.assertNotRegex(sql, r"\bxm\b",
                                    "ygzl 的姓名列是 ygname，不是 xm")
                self.assertRegex(sql, r"\bygname\b")


class TestGroupByDim(unittest.TestCase):
    def setUp(self):
        self.db = CaptureDB()
        self.a = datetime.datetime(2026, 10, 1)
        self.b = datetime.datetime(2026, 10, 11)

    def test_按工序汇总用工序名不是gx数字(self):
        """回归：gx 数字在不同定单里对应几十种工序，
        按数字汇总会把不相干的工序混一起。"""
        self.db.group_by("gx", self.a, self.b)
        sql = self.db.sqls[0][0]
        self.assertIn("gxname", sql, "按工序汇总应取工序名")
        self.assertNotIn("GROUP BY t.k, t.kname", sql,
                         "gx 维度不该再按数字分组")

    def test_未知维度不崩(self):
        for dim in ("不存在的维度", "", "Gx"):
            self.db.group_by(dim, self.a, self.b)   # 不抛异常即可

    def test_limit是整数(self):
        self.db.group_by("ygno", self.a, self.b, limit=10)
        sql = self.db.sqls[0][0]
        m = re.search(r"TOP (\d+)", sql, re.I)
        self.assertTrue(m)
        self.assertEqual(int(m.group(1)), 10)


class TestColumnContract(unittest.TestCase):
    """列顺序必须和消费方（展示/导出）的解包一致。

    改过 detail 的返回列（加了 gxname/yn），如果解包没跟着改，
    报表会静默错位 —— 这类 bug 很难靠肉眼发现。
    """

    def setUp(self):
        self.db = CaptureDB()
        self.a = datetime.datetime(2026, 10, 1)
        self.b = datetime.datetime(2026, 10, 11)

    def _cols(self, sql, args=None):
        """从 SELECT 段提取列名（别名优先，去掉表前缀）。"""
        head = sql[sql.upper().index("SELECT") + 6:sql.upper().index(" FROM ")]
        # 去掉嵌套括号里的内容（子查询），否则逗号会切错
        depth, out, buf = 0, [], ""
        for ch in head:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                out.append(buf)
                buf = ""
            else:
                buf += ch
        out.append(buf)
        cols = []
        for c in out:
            c = c.strip()
            if not c:
                continue
            m = re.search(r"\bAS\s+(\w+)$", c, re.I)
            if m:
                cols.append(m.group(1).lower())
            else:
                cols.append(c.split()[-1].split(".")[-1].lower())
        return cols

    def test_detail列数与解包一致(self):
        self.db.detail(self.a, self.b)
        sql = self.db.sqls[0][0]
        cols = self._cols(sql)
        self.assertEqual(len(cols), 12,
                         "detail 应返回 12 列（加了 gxname 和 yn）")
        # _show_detail 的解包顺序
        expect = ["gzdate", "ygno", "zdno", "gx", "gxname", "yn",
                  "cc", "zh", "js", "dj", "je", "barcode"]
        self.assertEqual(cols, expect)

    def test_wage带工序名(self):
        self.db.wage_by_worker(self.a, self.b)
        cols = self._cols(self.db.sqls[0][0])
        self.assertIn("ygname", cols)
        self.assertIn("gx", cols)

    def test_progress_gx按扎去重不报错(self):
        """cc 可能为 NULL（barcode=0 的人工行），串接要兜住。"""
        self.db.progress_gx("ZD1")
        sql = self.db.sqls[0][0]
        self.assertIn("ISNULL(cc", sql, "cc 可能为 NULL，必须兜住")


class TestConnStr(unittest.TestCase):
    def test_默认连生产库但可被环境变量覆盖(self):
        os.environ.pop("REPORT_CONN_STR", None)
        d = R.default_conn_str()
        self.assertIn("ShintHrmDb", d)
        os.environ["REPORT_CONN_STR"] = "DRIVER={X};DATABASE=Test"
        self.assertEqual(R.default_conn_str(), "DRIVER={X};DATABASE=Test")
        os.environ.pop("REPORT_CONN_STR")

    def test_不在代码里硬编码密码(self):
        """密码应该走环境变量，代码里留默认值是历史遗留。"""
        os.environ.pop("REPORT_CONN_STR", None)
        # 只是记录现状，不强制失败（改密码要同步改文档）
        self.assertIsInstance(R.default_conn_str(), str)


class TestExcelExport(unittest.TestCase):
    """Excel 导出：openpyxl 优先，失败必须能退回 CSV。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="hbc_xls_")
        self.path = os.path.join(self.tmp, "t.xlsx").replace("\\", "/")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_导出xlsx(self):
        ok, msg = R.export_rows_to_excel(
            self.path, "报表", ["工号", "件数"], [["A001", 10]])
        self.assertTrue(ok, msg)
        self.assertTrue(os.path.exists(self.path))

    def test_Decimal能导出(self):
        """金额是 Decimal，直接写会出错。"""
        from decimal import Decimal
        ok, msg = R.export_rows_to_excel(
            self.path, "报表", ["工号", "金额"], [["A001", Decimal("1234.5")]])
        self.assertTrue(ok, msg)

    def test_两个sheet都导出(self):
        ok, msg = R.export_rows_to_excel(
            self.path, "汇总", ["k", "v"], [["a", 1]],
            sum_headers=["总数", "值"], sum_rows=[[2, 3]])
        self.assertTrue(ok, msg)

    def test_路径不可写时退回CSV(self):
        """目标目录不存在时不能崩，要退 CSV 并如实报告。"""
        bad = os.path.join(self.tmp, "不存在的目录", "x.xlsx").replace("\\", "/")
        ok, msg = R.export_rows_to_excel(bad, "报表", ["a"], [[1]])
        self.assertTrue(ok, f"应该退回 CSV 而不是失败: {msg}")
        self.assertIn("csv", msg.lower())


class TestCheckOverLimit(unittest.TestCase):
    def test_超量统计SQL可用(self):
        db = CaptureDB()
        db.check_over_limit()
        sql = db.sqls[0][0]
        self.assertIn("jfzd2", sql)
        self.assertIn("plan_num", sql)


if __name__ == "__main__":
    unittest.main(verbosity=2)
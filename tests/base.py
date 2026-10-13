# -*- coding: utf-8 -*-
"""测试基座：每个用例独立的临时库，不碰真实数据、不联网。

三条铁律：
  1. 不连 SQL Server —— 需要写库的逻辑一律用假游标（FakeCursor）验证
  2. 不打真实 VPS    —— HTTP 测试只打本地临时端口
  3. 不碰真实 scan.db —— 每个用例建自己的临时库，用完删掉

跑法：
    python -m unittest discover -s tests -v
    python tests/run_all.py            # 带统计
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest

import _bootstrap  # noqa: F401  先打桩/设环境，再引入被测代码


class ScanTestCase(unittest.TestCase):
    """所有 webapp 测试的基类：自动准备干净数据库。"""

    # 改成 True 可以调 --keep 保留临时库，排查失败现场用
    KEEP = False

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="hbc_test_")
        self.db_path = os.path.join(self.tmpdir, "scan.db").replace("\\", "/")
        os.environ["SCAN_DB"] = self.db_path
        os.environ["SYNC_TOKEN"] = "test-token-for-unit-test"
        os.environ["SUPERVISORS"] = "C001,A001"
        os.environ["HBC_QUIET_MIGRATE"] = "1"   # 别让 migrate 日志刷满输出
        os.environ.pop("HBC_DB", None)
        os.environ.pop("HBC_SERVER", None)
        # 测试里不该有 ResourceWarning 噪音
        import warnings
        warnings.simplefilter("ignore", ResourceWarning)

        # db 模块按路径缓存连接，必须在设好环境变量之后再 import
        for mod in ("db", "rules", "app"):
            sys.modules.pop(mod, None)
        import db
        self.db = db
        self.rules = sys.modules["rules"] if "rules" in sys.modules else __import__("rules")
        db.init_db()

    def tearDown(self):
        try:
            self.db.get_conn().close()
        except Exception:
            pass
        if not self.KEEP:
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ---------- 造数据助手 ----------

    def add_employee(self, ygno, ygname="测试", active=1, phone=None, pin="5555"):
        """造一个可登录的工人（进花名册 + 登录表）。"""
        import import_workers as IW
        self.db.get_conn().execute(
            "INSERT OR REPLACE INTO ygzl (ygno,ygname,ygout) VALUES (?,?,0)",
            (ygno, ygname))
        h, salt = IW.make_pin_hash(pin)
        if not phone:
            # 注意括号：% 的优先级高于 +，写成 "1380000%04d" % n + x 会炸
            phone = "1380000" + ("%04d" % (abs(hash(ygno)) % 10000))
        self.db.get_conn().execute(
            "INSERT OR REPLACE INTO worker_login"
            " (ygno,ygname,phone,pin_hash,pin_salt,must_change,active,created_at)"
            " VALUES (?,?,?,?,?,0,?,?)",
            (ygno, ygname, phone, h, salt, active, self.db.now_str()))
        self.db.get_conn().commit()
        return ygno

    def add_process(self, zdno, gx, gxname, dj=0.5):
        self.db.get_conn().execute(
            "INSERT OR REPLACE INTO process (zdno,gx,gxname,dj) VALUES (?,?,?,?)",
            (zdno, gx, gxname, dj))
        self.db.get_conn().commit()

    def add_bundle(self, zdno, cc, zh, plan, yn="黑色", cm="均码"):
        self.db.get_conn().execute(
            "INSERT OR REPLACE INTO bundle (zdno,cc,zh,yn,cm,plan_qty)"
            " VALUES (?,?,?,?,?,?)", (zdno, cc, zh, yn, cm, plan))
        self.db.get_conn().commit()

    def push_reports(self, rows):
        """灌入历史报表数据（模拟从 SQL Server 推送）。"""
        return self.db.apply_report_snapshot(rows, len(rows),
                                             max(r[0] for r in rows))


class FakeCursor:
    """假 pyodbc 游标，用来验证 sync_reports 写库逻辑而不真连数据库。

    rows_by_key / scalars 都是「SQL 片段 → 返回值」的映射。
    匹配按**语句顺序**依次尝试，取第一个命中的，
    而不是全局搜 —— 否则 INSERT 语句里的 WHERE 子句
    会被当成 SELECT 的返回配置，测出来的东西是假的。
    """

    def __init__(self, rows_by_key=None, scalars=None):
        self.rows_by_key = list((rows_by_key or {}).items())
        self.scalars = list((scalars or {}).items())
        self.log = []
        self._last = None

    def execute(self, sql, *args):
        flat = " ".join(sql.split())
        low = flat.lower()
        # pyodbc 允许把参数打包成一个序列传（execute(sql, (a,b,c))），
        # 这里摊平，测试断言就能直接按位置取值，不用关心调用写法。
        if len(args) == 1 and isinstance(args[0], (tuple, list)):
            args = tuple(args[0])
        self.log.append((flat[:120], args))
        # 用户配置的优先，这样测试才能指定 IDENTITY 的返回值
        for key, val in self.scalars + self.rows_by_key:
            if key.lower() in low:
                self._last = val
                return self
        if "@@identity" in low:
            self._last = [[999]]
        elif low.startswith("select count"):
            self._last = [[0]]
        else:
            self._last = []
        return self

    def fetchone(self):
        return self._last[0] if self._last else None

    def fetchall(self):
        return list(self._last or [])

    def executed_sql(self, keyword):
        # 关键字也要转大写，否则传小写关键字永远匹配不上
        kw = keyword.upper()
        return [s for s, _ in self.log if kw in s.upper()]


def start_local_server(app_module, port=None, retries=8):
    """起一个本地 HTTP 服务。

    port=None 时让内核分配一个空闲端口。
    注意：先探测再关闭、再让服务器绑定，中间存在竞态 ——
    那个端口可能被别的进程抢走，导致偶发失败（CI 上尤其明显）。
    这里绑定失败就重试，直到成功为止。
    """
    last = None
    for _ in range(retries):
        if port is None:
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            p = s.getsockname()[1]
            s.close()
        else:
            p = port
        try:
            srv = app_module.ThreadingHTTPServer(("127.0.0.1", p),
                                               app_module.Handler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            return srv
        except OSError as e:
            last = e
            port = None if port is not None else None
    raise RuntimeError(f"本地测试服务器起不来: {last}")
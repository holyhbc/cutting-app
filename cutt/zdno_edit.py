# -*- coding: utf-8 -*-
"""ZDNO 数据维护 V2

一个可独立运行、也可被 hbc-print 主程序导入调用的 ZDNO 维护窗口。

支持修改
    1. 工序名称  jfdj.GXNAME  nvarchar(30)
    2. 单价      jfdj.DJ      decimal(9,4)
    3. 颜色      jfzd2.YS     nvarchar(10)
    4. 尺码      jfzd2.CM     nvarchar(8)
    5. 数量      jfzd2.JS     smallint

安全设计（重要）
    * 本模块不提供任何“删除数据”的界面入口；唯一会删除行的操作是
      「重建颜色矩阵」，必须手动触发、二次确认，并可一键撤销。
    * 每次写入都用完整主键定位单行（jfdj: zdno+gx；jfzd2: zdno+cc+zh），
      并校验 rowcount == 1，否则回滚并报错。
    * 每次写入前把旧值写入快照文件，写入后记入修改日志，可「撤销上次修改」。
    * jfzd3 是历史遗留的派生表，与 jfzd2 在现存数据中已有 208 组不一致，
      因此绝不自动同步，仅提供只读查看 + 手动重建。

用法
    独立运行：  python zdno_edit.py
    主程序调用：  from zdno_edit import open_zdno_editor
                  open_zdno_editor(parent_tk, conn_str)

数据库版本：SQL Server 2000（不使用 CTE / MERGE / IIF / sys.indexes）
"""

from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import traceback
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, ttk

import pyodbc


APP_TITLE = "ZDNO 数据维护 V2"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_FILE = os.path.join(BASE_DIR, "ZDNO修改快照.json")
AUDIT_FILE = os.path.join(BASE_DIR, "ZDNO修改日志.txt")

MAX_SL = 50
HEADER_GH = "缸号↓"
HEADER_YS = "颜色↓ 尺码→"

# 列宽上限来自表结构，写入前必须校验，否则 SQL Server 会直接报错截断
LIMITS = {
    "gxname": 30,   # jfdj.GXNAME nvarchar(30)
    "dj_max": 99999,  # jfdj.DJ decimal(9,4) -> 整数部分上限
    "dj_scale": 4,  # decimal(9,4) -> 4 位小数
    "ys": 10,   # jfzd2.YS nvarchar(10)
    "cm": 8,   # jfzd2.CM nvarchar(8)
    "gh": 10,   # jfzd2.GH nvarchar(10)
    "js_max": 32767,  # jfzd2.JS smallint
}

PALETTE = {
    "bg": "#eef1f6",
    "card": "#ffffff",
    "head": "#2c3e50",
    "head_fg": "#ffffff",
    "accent": "#1a73a8",
    "accent_soft": "#d6e6f2",
    "ok": "#1e8e3e",
    "warn": "#b26a00",
    "danger": "#c0392b",
    "row_alt": "#f6f8fb",
    "muted": "#7a8699",
}


# ============================================================
# 校验
# ============================================================

def _as_text(value) -> str:
    return "" if value is None else str(value).strip()


def friendly_error(exc: Exception) -> str:
    """把 pyodbc 的原始报错整理成人能看懂的一句话。

    pyodbc 的 str(exc) 是一串带 [Microsoft][ODBC ...] 前缀和 (17) (64) 错误码的
    长文本，直接弹给操作员看没有意义，这里只保留数据库给出的说明部分。
    """
    text = ""
    if isinstance(exc, pyodbc.Error):
        # 取最长的字符串参数：有时 args = (详细说明, 'HY000')，取 args[1] 会丢掉真正原因
        candidates = [a for a in exc.args if isinstance(a, str) and a.strip()]
        if candidates:
            text = max(candidates, key=len)
        text = re.sub(r"^\(\s*'[0-9A-Za-z]{5}'\s*,\s*", "", text.strip())
        if text.endswith(")"):
            text = text[:-1]
    if not text:
        text = str(exc)

    # 逐段去掉 [Microsoft][ODBC SQL Server Driver][...] 这类前缀
    while True:
        start = text.find("[")
        if start == -1:
            break
        depth = 0
        end = -1
        for i in range(start, len(text)):
            if text[i] == "[":
                depth += 1
            elif text[i] == "]":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            text = text[start + 1:]
            break
        text = text[:start] + text[end + 1:]

    # 去掉 (17) (64) 这类错误码和多余分号
    text = re.sub(r"\(\s*\d+\s*\)", " ", text)
    text = re.sub(r"\s*;\s*", "；", text)
    text = re.sub(r"\s+", " ", text).strip(" ；'\"")
    return text or exc.__class__.__name__


def check_gxname(value: str):
    text = _as_text(value)
    if not text:
        return None, "工序名称不能为空"
    if len(text) > LIMITS["gxname"]:
        return None, f"工序名称最多 {LIMITS['gxname']} 个字符，当前 {len(text)} 个"
    return text, None


def check_dj(value):
    """校验 jfdj.DJ decimal(9,4)。返回 (Decimal, None) 或 (None, 错误信息)。"""
    text = _as_text(value).replace(",", "")
    if not text:
        return None, "单价不能为空"
    try:
        num = Decimal(text)
    except (InvalidOperation, ValueError):
        return None, f"单价不是有效数字：{value}"
    if num < 0:
        return None, "单价不能为负数"
    if num > Decimal(LIMITS["dj_max"]):
        return None, f"单价超出范围（最大 {LIMITS['dj_max']}）"
    if -num.as_tuple().exponent > LIMITS["dj_scale"]:
        return None, f"单价最多 {LIMITS['dj_scale']} 位小数"
    return num.quantize(Decimal("0.0001")), None


def check_ys(value: str):
    text = _as_text(value)
    if not text:
        return None, "颜色不能为空"
    if len(text) > LIMITS["ys"]:
        return None, f"颜色最多 {LIMITS['ys']} 个字符（nvarchar(10)），当前 {len(text)} 个"
    return text, None


def check_cm(value: str):
    text = _as_text(value)
    if not text:
        return None, "尺码不能为空"
    if len(text) > LIMITS["cm"]:
        return None, f"尺码最多 {LIMITS['cm']} 个字符（nvarchar(8)），当前 {len(text)} 个"
    return text, None


def check_gh(value: str):
    text = _as_text(value)
    if len(text) > LIMITS["gh"]:
        return None, f"缸号最多 {LIMITS['gh']} 个字符"
    return text, None


def check_js(value):
    text = _as_text(value)
    if not text:
        return None, "数量不能为空"
    try:
        num = int(text)
    except ValueError:
        return None, f"数量必须是整数：{value}"
    if num < 0:
        return None, "数量不能为负数"
    if num > LIMITS["js_max"]:
        return None, f"数量超出 smallint 上限 {LIMITS['js_max']}"
    return num, None


# ============================================================
# 修改日志 / 快照 / 撤销
# ============================================================

def _write_audit(action: str, detail: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(AUDIT_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} | {action} | {detail}\n")
    except OSError:
        pass


def _save_snapshot(payload: dict) -> None:
    try:
        with open(SNAPSHOT_FILE, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def load_snapshot():
    try:
        if os.path.exists(SNAPSHOT_FILE):
            with open(SNAPSHOT_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
    except (OSError, ValueError):
        pass
    return None


def clear_snapshot() -> None:
    try:
        if os.path.exists(SNAPSHOT_FILE):
            os.remove(SNAPSHOT_FILE)
    except OSError:
        pass


# ============================================================
# 数据层
# ============================================================

SL_COLS = ", ".join(f"SL{i}" for i in range(1, MAX_SL + 1))
SL_PLACEHOLDERS = ", ".join(["?"] * MAX_SL)


class ZDNODatabase:
    """ZDNO 维护的数据访问层。

    连接按线程缓存（pyodbc 连接非线程安全，不能跨线程共享），
    每个工作线程各自持有一个连接，窗口关闭时统一释放。
    """

    def __init__(self, conn_str: str, timeout: int = 10):
        self.conn_str = conn_str
        self.timeout = timeout
        self._local = threading.local()
        self._all_conns = []
        self._lock = threading.Lock()

    # ---------- 连接管理 ----------
    @property
    def conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = pyodbc.connect(self.conn_str, timeout=self.timeout, autocommit=True)
            self._local.conn = conn
            with self._lock:
                self._all_conns.append(conn)
        return conn

    def close(self) -> None:
        with self._lock:
            conns, self._all_conns = self._all_conns, []
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass

    # ---------- 只读查询 ----------
    def ping(self) -> bool:
        cur = self.conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        return True

    def search_zdno(self, keyword: str, limit: int = 500):
        """模糊查询 ZDNO：同时在 jfdj / jfzd2 / jfzd3 中查找并去重。

        返回 [(zdno, gx_count, detail_count, color_count, mdate), ...]
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return self.recent_zdno(limit)

        like = f"%{keyword}%"
        sql = f"""
            SELECT z.zdno,
                   ISNULL(j.g_cnt, 0)  AS gx_cnt,
                   ISNULL(d.r_cnt, 0)  AS r_cnt,
                   ISNULL(d.k_cnt, 0)  AS color_cnt,
                   ISNULL(j.mdate, '') AS mdate
            FROM (
                SELECT zdno FROM jfdj  WHERE zdno LIKE ?
                UNION
                SELECT zdno FROM jfzd2 WHERE zdno LIKE ?
                UNION
                SELECT zdno FROM jfzd3 WHERE zdno LIKE ?
            ) z
            LEFT JOIN (SELECT zdno, COUNT(1) g_cnt, MAX(mdate) mdate
                       FROM jfdj GROUP BY zdno) j ON j.zdno = z.zdno
            LEFT JOIN (SELECT zdno, COUNT(1) r_cnt, COUNT(DISTINCT YS) k_cnt
                       FROM jfzd2 GROUP BY zdno) d ON d.zdno = z.zdno
            ORDER BY mdate DESC, z.zdno
        """
        cur = self.conn.cursor()
        cur.execute(sql, like, like, like)
        rows = cur.fetchmany(int(limit))
        return [tuple(r) for r in rows]

    def recent_zdno(self, limit: int = 100):
        """最近修改的工单（关键字为空时的默认列表），统计值同样是真实数据。"""
        cur = self.conn.cursor()
        cur.execute(f"""
            SELECT TOP {int(limit)} j.zdno, j.g_cnt, ISNULL(d.r_cnt, 0),
                   ISNULL(d.k_cnt, 0), j.mdate
            FROM (SELECT zdno, COUNT(1) g_cnt, MAX(mdate) mdate
                  FROM jfdj GROUP BY zdno) j
            LEFT JOIN (SELECT zdno, COUNT(1) r_cnt, COUNT(DISTINCT YS) k_cnt
                       FROM jfzd2 GROUP BY zdno) d ON d.zdno = j.zdno
            ORDER BY j.mdate DESC
        """)
        return [tuple(r) for r in cur.fetchall()]

    def exact_zdno(self, zdno: str):
        """精确匹配一个 ZDNO，找不到返回 None。"""
        zdno = (zdno or "").strip()
        if not zdno:
            return None
        cur = self.conn.cursor()
        cur.execute("SELECT ZDNO FROM jfdj WHERE zdno = ?", zdno)
        row = cur.fetchone()
        return str(row[0]) if row else None

    def get_jfdj(self, zdno: str):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT gx, gxname, dj, mdate, oksl
            FROM jfdj
            WHERE zdno = ?
            ORDER BY gx
        """, zdno)
        return [tuple(r) for r in cur.fetchall()]

    def get_jfzd2(self, zdno: str):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT CC, ZH, GH, YS, CM, JS, MDATE
            FROM jfzd2
            WHERE zdno = ?
            ORDER BY CC, ZH
        """, zdno)
        return [tuple(r) for r in cur.fetchall()]

    def get_jfzd3(self, zdno: str):
        """读取颜色矩阵全部 50 个 SL 列（旧版本只读前 25 列，会丢数据）。"""
        cur = self.conn.cursor()
        cur.execute(f"""
            SELECT CC, ITEM, ZHBEGIN, GH, YS, {SL_COLS}
            FROM jfzd3
            WHERE zdno = ?
            ORDER BY CC, ITEM
        """, zdno)
        return [tuple(r) for r in cur.fetchall()]

    def get_counts(self, zdno: str):
        """选中工单的汇总数据：工序数、明细行数、总数量、颜色数、矩阵行数、按床次分布。"""
        cur = self.conn.cursor()
        cur.execute("""
            SELECT
              (SELECT COUNT(1) FROM jfdj  WHERE zdno = ?),
              (SELECT COUNT(1) FROM jfzd2 WHERE zdno = ?),
              (SELECT COUNT(1) FROM jfzd3 WHERE zdno = ?),
              (SELECT COUNT(1) FROM jfzd3 WHERE zdno = ? AND ITEM = 0),
              (SELECT ISNULL(SUM(JS), 0) FROM jfzd2 WHERE zdno = ?),
              (SELECT COUNT(DISTINCT YS) FROM jfzd2 WHERE zdno = ?)
        """, zdno, zdno, zdno, zdno, zdno, zdno)
        row = cur.fetchone()

        cur.execute("""
            SELECT CC, COUNT(1), ISNULL(SUM(JS), 0)
            FROM jfzd2 WHERE zdno = ? GROUP BY CC ORDER BY CC
        """, zdno)
        by_cc = [(int(r[0]), int(r[1]), int(r[2])) for r in cur.fetchall()]

        return {
            "jfdj": int(row[0]),
            "jfzd2": int(row[1]),
            "jfzd3": int(row[2]),
            "jfzd3_header": int(row[3]),
            "total_qty": int(row[4] or 0),
            "color_cnt": int(row[5] or 0),
            "by_cc": by_cc,
        }

    def get_distinct(self, table: str, column: str):
        """取某列的去重值，用于输入联想。table/column 仅接受白名单内的值。"""
        if (table, column) not in (("jfzd2", "YS"), ("jfzd2", "CM"),
                                   ("jfzd2", "GH"), ("jfdj", "GXNAME")):
            raise ValueError("非法的列名")
        cur = self.conn.cursor()
        cur.execute(f"""
            SELECT DISTINCT {column}
            FROM {table}
            WHERE {column} IS NOT NULL AND LTRIM(RTRIM({column})) <> ''
            ORDER BY {column}
        """)
        return [str(r[0]) for r in cur.fetchall()]

    # ---------- 裁剪明细统计（jfzd2_detail） ----------
    DETAIL_TABLE = "jfzd2_detail"

    def detail_table_exists(self) -> bool:
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(1) FROM sysobjects WHERE name = ? AND type = 'U'",
                    self.DETAIL_TABLE)
        return bool(cur.fetchone()[0])

    def get_detail_types(self):
        """明细表里出现过的类型（用于筛选下拉）。"""
        if not self.detail_table_exists():
            return []
        cur = self.conn.cursor()
        cur.execute(f"""
            SELECT DISTINCT TYPE_NAME FROM {self.DETAIL_TABLE}
            WHERE TYPE_NAME IS NOT NULL AND LTRIM(RTRIM(TYPE_NAME)) <> ''
            ORDER BY TYPE_NAME
        """)
        return [str(r[0]) for r in cur.fetchall()]

    def query_detail_summary(self, date_from, date_to, type_name=""):
        """按时间段 + 类型汇总：每个类型的件数、涉及顺序号数、明细行数。"""
        if not self.detail_table_exists():
            return []
        where = ["CREATED_AT >= ?", "CREATED_AT <= ?"]
        params = [date_from, date_to]
        if type_name and type_name != "全部":
            where.append("TYPE_NAME = ?")
            params.append(type_name)
        sql = f"""
            SELECT TYPE_NAME,
                   SUM(ISNULL(JS, 0))            AS total_qty,
                   COUNT(1)                      AS row_count,
                   COUNT(DISTINCT ORDER_NO)      AS order_cnt,
                   COUNT(DISTINCT ZDNO)          AS zdno_cnt,
                   MIN(CREATED_AT)               AS first_at,
                   MAX(CREATED_AT)               AS last_at
            FROM {self.DETAIL_TABLE}
            WHERE {' AND '.join(where)}
            GROUP BY TYPE_NAME
            ORDER BY total_qty DESC, TYPE_NAME
        """
        cur = self.conn.cursor()
        cur.execute(sql, *params)
        return [tuple(r) for r in cur.fetchall()]

    def query_detail_rows(self, date_from, date_to, type_name="", limit=20000):
        """按时间段 + 类型取明细（用于预览和导出）。"""
        if not self.detail_table_exists():
            return []
        where = ["CREATED_AT >= ?", "CREATED_AT <= ?"]
        params = [date_from, date_to]
        if type_name and type_name != "全部":
            where.append("TYPE_NAME = ?")
            params.append(type_name)
        # limit 用 TOP 内联（SQL Server 2000 的 TOP 不接受参数），不能再进 params
        sql = f"""
            SELECT TOP {int(limit)}
                   CREATED_AT, TYPE_NAME, ZDNO, TEMPLATE_ZDNO,
                   ORDER_NO, YS, CM, JS, MODE
            FROM {self.DETAIL_TABLE}
            WHERE {' AND '.join(where)}
            ORDER BY CREATED_AT DESC, TYPE_NAME, ZDNO, ORDER_NO
        """
        cur = self.conn.cursor()
        cur.execute(sql, *params)
        return [tuple(r) for r in cur.fetchall()]

    def query_detail_by_zdno(self, zdno):
        """按制单号或原始工单号查明细。"""
        if not self.detail_table_exists():
            return []
        cur = self.conn.cursor()
        cur.execute(f"""
            SELECT CREATED_AT, TYPE_NAME, ZDNO, TEMPLATE_ZDNO,
                   ORDER_NO, YS, CM, JS, MODE
            FROM {self.DETAIL_TABLE}
            WHERE ZDNO = ? OR TEMPLATE_ZDNO = ?
            ORDER BY CREATED_AT DESC, ORDER_NO
        """, zdno, zdno)
        return [tuple(r) for r in cur.fetchall()]

    # ---------- 写入：全部带主键定位 + rowcount 校验 ----------
    def update_process(self, zdno: str, gx: int, new_name, new_dj):
        """修改 jfdj 的工序名称与单价。返回 (ok, message, before_dict)。"""
        name, err = check_gxname(new_name)
        if err:
            return False, err, None
        price, err = check_dj(new_dj)
        if err:
            return False, err, None

        conn = self.conn
        conn.autocommit = False
        cur = conn.cursor()
        try:
            cur.execute("SELECT gxname, dj FROM jfdj WHERE zdno = ? AND gx = ?",
                        zdno, int(gx))
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                return False, f"未找到工序 {zdno} / {gx}，可能已被其他人删除", None
            before = {"gxname": str(row[0] or ""), "dj": str(row[1])}

            cur.execute("""
                UPDATE jfdj SET gxname = ?, dj = ?
                WHERE zdno = ? AND gx = ?
            """, name, price, zdno, int(gx))
            if cur.rowcount != 1:
                conn.rollback()
                return False, f"更新行数为 {cur.rowcount}，已回滚（预期 1 行）", None

            conn.commit()
            after = {"gxname": name, "dj": str(price)}
            _save_snapshot({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "table": "jfdj",
                "key": {"zdno": zdno, "gx": int(gx)},
                "before": before,
                "after": after,
            })
            _write_audit("修改工序", f"{zdno} gx={gx} {before} -> {after}")
            return True, f"工序 {gx} 已更新", before
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            return False, f"更新失败：{friendly_error(exc)}", None
        finally:
            conn.autocommit = True

    def update_detail(self, zdno: str, cc: int, zh: int, new_ys, new_cm, new_js):
        """修改 jfzd2 的颜色 / 尺码 / 数量。返回 (ok, message, before_dict)。"""
        ys, err = check_ys(new_ys)
        if err:
            return False, err, None
        cm, err = check_cm(new_cm)
        if err:
            return False, err, None
        js, err = check_js(new_js)
        if err:
            return False, err, None

        conn = self.conn
        conn.autocommit = False
        cur = conn.cursor()
        try:
            cur.execute("SELECT YS, CM, JS FROM jfzd2 WHERE zdno = ? AND CC = ? AND ZH = ?",
                        zdno, int(cc), int(zh))
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                return False, f"未找到明细 {zdno} / 床次{cc} / 扎号{zh}", None
            before = {"ys": str(row[0] or ""), "cm": str(row[1] or ""), "js": str(row[2])}

            cur.execute("""
                UPDATE jfzd2 SET YS = ?, CM = ?, JS = ?
                WHERE zdno = ? AND CC = ? AND ZH = ?
            """, ys, cm, int(js), zdno, int(cc), int(zh))
            if cur.rowcount != 1:
                conn.rollback()
                return False, f"更新行数为 {cur.rowcount}，已回滚（预期 1 行）", None

            conn.commit()
            after = {"ys": ys, "cm": cm, "js": str(js)}
            _save_snapshot({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "table": "jfzd2",
                "key": {"zdno": zdno, "cc": int(cc), "zh": int(zh)},
                "before": before,
                "after": after,
            })
            _write_audit("修改明细", f"{zdno} cc={cc} zh={zh} {before} -> {after}")
            return True, f"扎号 {zh} 已更新", before
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            return False, f"更新失败：{friendly_error(exc)}", None
        finally:
            conn.autocommit = True

    def undo_last(self):
        """按快照撤销最后一次修改。"""
        snap = load_snapshot()
        if not snap:
            return False, "没有可撤销的修改记录", None

        table = snap.get("table")
        key = snap.get("key") or {}
        before = snap.get("before") or {}
        zdno = key.get("zdno", "")

        conn = self.conn
        conn.autocommit = False
        cur = conn.cursor()
        try:
            if table == "jfdj":
                cur.execute("SELECT gxname, dj FROM jfdj WHERE zdno = ? AND gx = ?",
                            zdno, int(key.get("gx", 0)))
                if cur.fetchone() is None:
                    conn.rollback()
                    return False, "目标行已不存在，无法撤销", None
                cur.execute("""
                    UPDATE jfdj SET gxname = ?, dj = ? WHERE zdno = ? AND gx = ?
                """, before.get("gxname", ""), Decimal(str(before.get("dj", "0"))),
                    zdno, int(key.get("gx", 0)))
                if cur.rowcount != 1:
                    conn.rollback()
                    return False, "撤销失败：影响行数异常", None
                label = f"jfdj 工序 {key.get('gx')}"

            elif table == "jfzd2":
                cur.execute("SELECT YS, CM, JS FROM jfzd2 WHERE zdno = ? AND CC = ? AND ZH = ?",
                            zdno, int(key.get("cc", 0)), int(key.get("zh", 0)))
                if cur.fetchone() is None:
                    conn.rollback()
                    return False, "目标行已不存在，无法撤销", None
                cur.execute("""
                    UPDATE jfzd2 SET YS = ?, CM = ?, JS = ?
                    WHERE zdno = ? AND CC = ? AND ZH = ?
                """, before.get("ys", ""), before.get("cm", ""),
                    int(before.get("js", 0)), zdno,
                    int(key.get("cc", 0)), int(key.get("zh", 0)))
                if cur.rowcount != 1:
                    conn.rollback()
                    return False, "撤销失败：影响行数异常", None
                label = f"jfzd2 扎号 {key.get('zh')}"

            elif table == "jfzd3":
                rows = before.get("rows") or []
                cc_filter = key.get("cc")
                if cc_filter is None:
                    cur.execute("DELETE FROM jfzd3 WHERE zdno = ?", zdno)
                else:
                    cur.execute("DELETE FROM jfzd3 WHERE zdno = ? AND CC = ?",
                                zdno, int(cc_filter))
                for row in rows:
                    cols = [f"SL{i}" for i in range(1, MAX_SL + 1)]
                    cur.execute(f"""
                        INSERT INTO jfzd3 (ZDNO, CC, ITEM, ZHBEGIN, GH, YS, {SL_COLS})
                        VALUES (?, ?, ?, ?, ?, ?, {SL_PLACEHOLDERS})
                    """, zdno, row[0], row[1], row[2], row[3], row[4], *row[5])
                label = f"jfzd3 颜色矩阵（{len(rows)} 行）"

            else:
                conn.rollback()
                return False, f"未知的快照类型：{table}", None

            conn.commit()
            clear_snapshot()
            _write_audit("撤销修改", f"{zdno} {label}")
            return True, f"已撤销：{label}", snap
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            return False, f"撤销失败：{friendly_error(exc)}", None
        finally:
            conn.autocommit = True

    def plan_rebuild_jfzd3(self, zdno: str):
        """生成 jfzd3 重建方案（只读，不写库）。

        返回 (ok, message, plan)。plan = {'cc':…, 'before':[…], 'after':[…], 'drop':…}
        """
        details = self.get_jfzd2(zdno)
        if not details:
            return False, "jfzd2 中没有明细数据，无法重建颜色矩阵", None

        before_rows = self.get_jfzd3(zdno)
        existing_ccs = sorted({int(r[0]) for r in before_rows})

        by_cc = {}
        for cc, zh, _gh, ys, cm, js, _md in details:
            by_cc.setdefault(int(cc), []).append((int(zh), str(ys or ""), str(cm or ""), int(js or 0)))

        after_rows = []
        for cc in sorted(by_cc):
            items = sorted(by_cc[cc], key=lambda x: x[0])
            if len(items) > MAX_SL:
                return False, (
                    f"床次 {cc} 有 {len(items)} 条明细，超过 SL1~SL{MAX_SL} 的容量，"
                    f"无法用颜色矩阵表达。已保持原数据不变。"
                ), None

            zh_begin = items[0][0]
            # INSERT 语句固定 56 个占位符（6 + 50），SL 数组必须补齐到 50 列
            header_sl = [(cm or "") for _zh, _ys, cm, _js in items]
            header_sl += [""] * (MAX_SL - len(header_sl))
            after_rows.append((cc, 0, zh_begin, HEADER_GH, HEADER_YS, header_sl))

            order = []
            for _zh, ys, _cm, _js in items:
                if ys not in order:
                    order.append(ys)
            for idx, color in enumerate(order, start=1):
                row_sl = ["" if ys != color else str(js) for _zh, ys, _cm, js in items]
                row_sl += [""] * (MAX_SL - len(row_sl))
                after_rows.append((cc, idx, zh_begin, "", color, row_sl))

        if len(after_rows) > 32767:
            return False, "颜色行数超出 smallint 范围，已保持原数据不变", None

        return True, "", {
            "cc": sorted(by_cc),
            "before": before_rows,
            "after": after_rows,
            "drop_ccs": existing_ccs,
        }

    def rebuild_jfzd3(self, zdno: str, plan: dict):
        """按 plan 重建 jfzd3。会先删掉该 ZDNO 的旧矩阵行再写入新行。"""
        conn = self.conn
        conn.autocommit = False
        cur = conn.cursor()
        try:
            for cc in plan.get("drop_ccs") or []:
                cur.execute("DELETE FROM jfzd3 WHERE zdno = ? AND CC = ?", zdno, int(cc))
            for cc, item, zh_begin, gh, ys, sl in plan["after"]:
                cur.execute(f"""
                    INSERT INTO jfzd3 (ZDNO, CC, ITEM, ZHBEGIN, GH, YS, {SL_COLS})
                    VALUES (?, ?, ?, ?, ?, ?, {SL_PLACEHOLDERS})
                """, zdno, cc, item, zh_begin, gh, ys, *[str(v) if v != "" else "" for v in sl])

            written = cur.execute("SELECT COUNT(1) FROM jfzd3 WHERE zdno = ?", zdno).fetchone()[0]
            expected = len(plan["after"])
            if int(written) != expected:
                conn.rollback()
                return False, f"写入行数校验失败（期望 {expected}，实际 {written}），已回滚", None

            conn.commit()
            _save_snapshot({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "table": "jfzd3",
                "key": {"zdno": zdno, "cc": None},
                "before": {"rows": [list(r) for r in plan["before"]]},
                "after": {"rows": len(plan["after"])},
            })
            _write_audit("重建颜色矩阵", f"{zdno} 共 {expected} 行")
            return True, f"颜色矩阵已重建，共 {expected} 行", None
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            return False, f"重建失败：{friendly_error(exc)}", None
        finally:
            conn.autocommit = True


# ============================================================
# UI
# ============================================================

def _pick_theme() -> str:
    available = set(ttk.Style().theme_names())
    for name in ("vista", "xpnative", "winnative", "clam"):
        if name in available:
            return name
    return "default"


class ZDNOEditDialog(tk.Toplevel):
    """ZDNO 维护主窗口。

    参数与旧版保持兼容：ZDNOEditDialog(parent, conn_str)
    """

    def __init__(self, parent, conn_str, on_log=None):
        super().__init__(parent)
        self.title(APP_TITLE)
        self.geometry("1320x820")
        self.minsize(1000, 640)
        self.configure(bg=PALETTE["bg"])
        # 独立运行时 master 是被 withdraw 的空窗口，而 Tk 的 wm transient 会
        # 跟着把本窗口一起隐藏（表现为“双击没反应”）。只在 master 真正可见时
        # 才设置 transient，从 hbc-print 打开时仍保持父子联动。
        try:
            if parent.winfo_exists() and parent.winfo_viewable():
                self.transient(parent)
        except tk.TclError:
            pass

        self.db = ZDNODatabase(conn_str)
        self.on_log = on_log

        self.current_zdno = None
        self.current_plan = None
        self._busy = False
        self._closing = False
        self._queue = []
        self._results = queue.Queue()
        self._timer_on = False
        self._flash_pending = False
        self._pending_status = None
        self._projected_total = None
        self._detail_rows = []
        self._suggest_cache = {}

        self._init_style()
        self._build_ui()
        self._bind_keys()
        self.after(80, self._initial_load)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- 样式 ----------
    def _init_style(self):
        style = ttk.Style(self)
        style.theme_use(_pick_theme())

        families = set()
        try:
            families = set(tkfont.families(self))
        except tk.TclError:
            pass
        if "Microsoft YaHei UI" in families:
            base = ("Microsoft YaHei UI", 9)
        elif "Microsoft YaHei" in families:
            base = ("Microsoft YaHei", 9)
        else:
            base = ("TkDefaultFont", 9)
        self._font = base
        bold = (base[0], base[1], "bold")

        style.configure(".", font=base, background=PALETTE["bg"], foreground="#1f2933")
        style.configure("Card.TFrame", background=PALETTE["card"])
        style.configure("Card.TLabel", background=PALETTE["card"])
        style.configure("Muted.TLabel", foreground=PALETTE["muted"], background=PALETTE["bg"])
        style.configure("CardTitle.TLabel", font=bold,
                        background=PALETTE["card"], foreground=PALETTE["head"])
        style.configure("Ok.TLabel", foreground=PALETTE["ok"], background=PALETTE["bg"],
                        font=bold)
        style.configure("Warn.TLabel", foreground=PALETTE["warn"], background=PALETTE["bg"],
                        font=bold)
        style.configure("Danger.TLabel", foreground=PALETTE["danger"],
                        background=PALETTE["bg"], font=bold)
        style.configure("Status.TLabel", background="#dfe6ee", foreground="#33475b",
                        padding=(8, 4), relief="flat")
        # 底部汇总条：总数量最醒目，颜色数量变化时高亮一下
        style.configure("Summary.TLabel", background="#dfe6ee", foreground="#0b5c8a",
                        font=bold, padding=(12, 4), relief="flat")
        style.configure("SummaryHot.TLabel", background="#cfe8d8", foreground="#0d6b32",
                        font=bold, padding=(12, 4), relief="flat")
        style.configure("Accent.TButton", font=bold, padding=(12, 5))
        style.configure("Danger.TButton", font=bold,
                        foreground=PALETTE["danger"], padding=(12, 5))
        style.configure("TNotebook.Tab", padding=(16, 7), font=bold)
        style.configure("Treeview", rowheight=25, fieldbackground="#ffffff", borderwidth=0)
        style.configure("Treeview.Heading", font=bold, relief="flat", padding=(4, 5))
        style.map("Treeview",
                  background=[("selected", PALETTE["accent"])],
                  foreground=[("selected", "#ffffff")])

    # ---------- 界面 ----------
    def _build_ui(self):
        self._build_header()
        self._build_toolbar()
        # 状态栏必须先建好：下面的页签在构建时就会发起后台查询（如读取类型列表），
        # 而 _run -> _set_busy 会写 status_var，顺序反了会抛 AttributeError。
        self._build_status()

        body = ttk.Frame(self, padding=(10, 6, 10, 0))
        body.pack(fill="both", expand=True)

        self.paned = ttk.PanedWindow(body, orient="horizontal")
        self.paned.pack(fill="both", expand=True)

        self._build_result_panel(self.paned)
        self._build_detail_panel(self.paned)

        self._set_busy(False)

    def _build_header(self):
        head = tk.Frame(self, bg=PALETTE["head"])
        head.pack(fill="x")
        inner = tk.Frame(head, bg=PALETTE["head"])
        inner.pack(fill="x", padx=16, pady=10)

        left = tk.Frame(inner, bg=PALETTE["head"])
        left.pack(side="left")
        tk.Label(left, text="🧵  ZDNO 数据维护", bg=PALETTE["head"],
                 fg=PALETTE["head_fg"],
                 font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
        tk.Label(left, text="修改单价 / 工序名称 / 颜色 / 数量 · 每次改动都会记录快照并可撤销",
                 bg=PALETTE["head"], fg="#b9c7d6",
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(2, 0))

        right = tk.Frame(inner, bg=PALETTE["head"])
        right.pack(side="right")
        tk.Label(right, text=f"{datetime.now():%Y-%m-%d}", bg=PALETTE["head"],
                 fg="#b9c7d6", font=("Microsoft YaHei UI", 9)).pack(anchor="e")

    def _build_toolbar(self):
        bar = ttk.Frame(self, padding=(10, 8, 10, 4))
        bar.pack(fill="x")

        ttk.Label(bar, text="🔍 ZDNO", font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(bar, textvariable=self.search_var, width=30)
        self.search_entry.pack(side="left", padx=6, ipady=2)
        self.search_entry.insert(0, "")
        self.search_entry.focus_set()

        ttk.Button(bar, text="查询", width=8, style="Accent.TButton",
                   command=self.do_search).pack(side="left")
        ttk.Button(bar, text="最近工单", width=10,
                   command=self.show_recent).pack(side="left", padx=4)
        self.btn_undo = ttk.Button(bar, text="↩ 撤销上次修改", width=14,
                                   command=self.do_undo)
        self.btn_undo.pack(side="left", padx=4)
        ttk.Button(bar, text="⟳ 刷新当前", width=11,
                   command=self.reload_current).pack(side="left", padx=4)

        self.search_info = tk.StringVar(value="输入内容后按回车即可模糊查询")
        ttk.Label(bar, textvariable=self.search_info, style="Muted.TLabel").pack(
            side="left", padx=12)

    def _build_result_panel(self, parent):
        card = ttk.Frame(parent, style="Card.TFrame", padding=1)
        parent.add(card, weight=0)

        inner = tk.Frame(card, bg=PALETTE["card"])
        inner.pack(fill="both", expand=True)

        tk.Label(inner, text="查询结果", bg=PALETTE["card"], fg=PALETTE["head"],
                 font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=10, pady=(8, 2))
        tk.Label(inner, text="双击或按回车打开工单", bg=PALETTE["card"],
                 fg=PALETTE["muted"], font=("Microsoft YaHei UI", 8)).pack(anchor="w", padx=10)

        cols = ("zdno", "gx_cnt", "r_cnt", "k_cnt", "mdate")
        self.tree_list = ttk.Treeview(inner, columns=cols, show="headings",
                                      selectmode="browse")
        heads = {"zdno": "ZDNO", "gx_cnt": "工序", "r_cnt": "明细",
                 "k_cnt": "颜色", "mdate": "最近修改"}
        widths = {"zdno": 185, "gx_cnt": 46, "r_cnt": 46, "k_cnt": 46, "mdate": 132}
        for col in cols:
            self.tree_list.heading(col, text=heads[col])
            self.tree_list.column(col, width=widths[col],
                                  anchor="center" if col != "zdno" else "w")
        sb = ttk.Scrollbar(inner, orient="vertical", command=self.tree_list.yview)
        self.tree_list.configure(yscrollcommand=sb.set)
        self.tree_list.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=6)
        sb.pack(side="right", fill="y", padx=(0, 4), pady=6)
        self._stripe(self.tree_list)
        self.tree_list.bind("<<TreeviewSelect>>", self._on_pick)
        self.tree_list.bind("<Double-1>", lambda e: self._open_selected())
        self.tree_list.bind("<Return>", lambda e: self._open_selected())

    def _build_detail_panel(self, parent):
        card = ttk.Frame(parent, style="Card.TFrame", padding=1)
        parent.add(card, weight=1)

        inner = tk.Frame(card, bg=PALETTE["card"])
        inner.pack(fill="both", expand=True)

        top = tk.Frame(inner, bg=PALETTE["card"])
        top.pack(fill="x", padx=12, pady=(10, 4))
        self.title_var = tk.StringVar(value="未选择工单")
        tk.Label(top, textvariable=self.title_var, bg=PALETTE["card"], fg=PALETTE["head"],
                 font=("Microsoft YaHei UI", 12, "bold")).pack(side="left")
        self.subtitle_var = tk.StringVar(value="请先在左侧选择或查询一个 ZDNO")
        tk.Label(top, textvariable=self.subtitle_var, bg=PALETTE["card"],
                 fg=PALETTE["muted"], font=("Microsoft YaHei UI", 9)).pack(side="left", padx=10)

        ttk.Button(top, text="🔍 一致性校验", width=12,
                   command=self.check_consistency).pack(side="right")

        self.notebook = ttk.Notebook(inner)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=(0, 10))
        self._build_process_tab()
        self._build_detail_tab()
        self._build_matrix_tab()
        self._build_stats_tab()

    def _build_process_tab(self):
        page = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(page, text="  ① 工序（单价 / 名称）  ")

        hint = ttk.Frame(page)
        hint.pack(fill="x", pady=(0, 4))
        ttk.Label(hint, text="双击「工序名称」或「单价」单元格修改，回车确认，Esc 取消",
                  style="Muted.TLabel").pack(side="left")
        self.gx_hint = tk.StringVar(value="")
        ttk.Label(hint, textvariable=self.gx_hint, style="Warn.TLabel").pack(side="right")

        wrap = ttk.Frame(page)
        wrap.pack(fill="both", expand=True)
        self.tree_gx = ttk.Treeview(wrap, columns=("gx", "gxname", "dj", "mdate", "oksl"),
                                    show="headings")
        heads = {"gx": "工序", "gxname": "工序名称", "dj": "单价",
                 "mdate": "日期", "oksl": "完成数量"}
        widths = {"gx": 60, "gxname": 220, "dj": 100, "mdate": 140, "oksl": 90}
        for col in self.tree_gx["columns"]:
            self.tree_gx.heading(col, text=heads[col])
            self.tree_gx.column(col, width=widths[col],
                                anchor="center" if col in ("gx", "dj", "oksl") else "w")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree_gx.yview)
        self.tree_gx.configure(yscrollcommand=sb.set)
        self.tree_gx.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self._stripe(self.tree_gx)
        self.tree_gx.tag_configure("editable", background="#fffbe6")
        self.tree_gx.bind("<Double-1>", lambda e: self._begin_edit(self.tree_gx, e))

    def _build_detail_tab(self):
        page = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(page, text="  ② 明细（颜色 / 尺码 / 数量）  ")

        hint = ttk.Frame(page)
        hint.pack(fill="x", pady=(0, 4))
        ttk.Label(hint, text="双击「颜色」「尺码」「缸号」「数量」修改；床次与扎号是主键，不可修改",
                  style="Muted.TLabel").pack(side="left")
        ttk.Button(hint, text="🧱 重建颜色矩阵…", width=16,
                   command=self.ask_rebuild).pack(side="right")

        wrap = ttk.Frame(page)
        wrap.pack(fill="both", expand=True)
        cols = ("cc", "zh", "gh", "ys", "cm", "js", "mdate")
        self.tree_dt = ttk.Treeview(wrap, columns=cols, show="headings")
        heads = {"cc": "床次", "zh": "扎号", "gh": "缸号", "ys": "颜色",
                 "cm": "尺码", "js": "数量", "mdate": "日期"}
        widths = {"cc": 60, "zh": 70, "gh": 90, "ys": 140, "cm": 110,
                  "js": 80, "mdate": 140}
        for col in cols:
            self.tree_dt.heading(col, text=heads[col])
            self.tree_dt.column(col, width=widths[col],
                                anchor="center" if col in ("cc", "zh", "js") else "w")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree_dt.yview)
        self.tree_dt.configure(yscrollcommand=sb.set)
        self.tree_dt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self._stripe(self.tree_dt)
        self.tree_dt.tag_configure("editable", background="#fffbe6")
        self.tree_dt.bind("<Double-1>", lambda e: self._begin_edit(self.tree_dt, e))

    def _build_matrix_tab(self):
        page = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(page, text="  ③ 颜色矩阵（只读）  ")

        hint = ttk.Frame(page)
        hint.pack(fill="x", pady=(0, 4))
        ttk.Label(hint,
                  text="jfzd3 为历史派生表，仅供查看；不会随明细修改自动变动",
                  style="Muted.TLabel").pack(side="left")
        self.matrix_hint = tk.StringVar(value="")
        ttk.Label(hint, textvariable=self.matrix_hint, style="Warn.TLabel").pack(side="right")

        wrap = ttk.Frame(page)
        wrap.pack(fill="both", expand=True)
        cols = ("cc", "item", "zhbegin", "gh", "ys") + tuple(
            f"SL{i}" for i in range(1, MAX_SL + 1))
        self.tree_mx = ttk.Treeview(wrap, columns=cols, show="headings")
        self.tree_mx.heading("cc", text="床次")
        self.tree_mx.heading("item", text="行")
        self.tree_mx.heading("zhbegin", text="起始扎号")
        self.tree_mx.heading("gh", text="缸号")
        self.tree_mx.heading("ys", text="颜色")
        for col, w in (("cc", 55), ("item", 45), ("zhbegin", 80), ("gh", 90), ("ys", 150)):
            self.tree_mx.column(col, width=w,
                                anchor="center" if col in ("cc", "item", "zhbegin") else "w")
        for i in range(1, MAX_SL + 1):
            col = f"SL{i}"
            self.tree_mx.heading(col, text=str(i))
            self.tree_mx.column(col, width=52, anchor="center")
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree_mx.yview)
        hsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.tree_mx.xview)
        self.tree_mx.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree_mx.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.tree_mx.tag_configure("header", background="#e8eef6", foreground=PALETTE["head"])
        self._stripe(self.tree_mx)

    def _build_stats_tab(self):
        """裁剪统计：按时间段 + 类型查各类款式裁剪了多少件，并导出 Excel。"""
        page = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(page, text="  ④ 裁剪统计 / 导出  ")

        # ---- 查询条件 ----
        cond = ttk.LabelFrame(page, text="查询条件", padding=8)
        cond.pack(fill="x", pady=(0, 6))

        line1 = ttk.Frame(cond)
        line1.pack(fill="x", pady=2)
        ttk.Label(line1, text="开始日期：").pack(side="left")
        self.stat_from_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))
        ttk.Entry(line1, textvariable=self.stat_from_var, width=13).pack(side="left", padx=(0, 4))
        ttk.Label(line1, text="结束日期：").pack(side="left", padx=(10, 0))
        self.stat_to_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))
        ttk.Entry(line1, textvariable=self.stat_to_var, width=13).pack(side="left", padx=(0, 4))

        for label, days, cmd in (("今天", 0, None), ("近7天", 6, None),
                                 ("近30天", 29, None), ("本月", None, "month"),
                                 ("上月", None, "prev")):
            btn = ttk.Button(line1, text=label, width=6)
            if cmd == "month":
                btn.config(command=self._stat_range_this_month)
            elif cmd == "prev":
                btn.config(command=self._stat_range_last_month)
            else:
                btn.config(command=lambda d=days: self._stat_range_days(d))
            btn.pack(side="left", padx=2)

        line2 = ttk.Frame(cond)
        line2.pack(fill="x", pady=4)
        ttk.Label(line2, text="类型：").pack(side="left")
        self.stat_type_var = tk.StringVar(value="全部")
        self.stat_type_combo = ttk.Combobox(line2, textvariable=self.stat_type_var,
                                            values=["全部"], width=12, state="readonly")
        self.stat_type_combo.pack(side="left", padx=(0, 8))
        ttk.Button(line2, text="🔄 刷新类型", width=10,
                   command=self._load_stat_types).pack(side="left", padx=2)
        ttk.Button(line2, text="🔍 查询", width=10, style="Accent.TButton",
                   command=self._do_stat_query).pack(side="left", padx=2)
        ttk.Button(line2, text="📊 导出 Excel", width=14,
                   command=self._do_stat_export).pack(side="left", padx=2)
        self.stat_hint = tk.StringVar(value="")
        ttk.Label(line2, textvariable=self.stat_hint, style="Muted.TLabel").pack(side="left", padx=8)

        # ---- 汇总 ----
        ttk.Label(page, text="按类型汇总", style="CardTitle.TLabel").pack(anchor="w")
        sum_wrap = ttk.Frame(page)
        sum_wrap.pack(fill="x", pady=(2, 8))
        cols = ("type", "qty", "rows", "orders", "zdnos", "first", "last")
        self.tree_stat = ttk.Treeview(sum_wrap, columns=cols, show="headings", height=6)
        heads = {"type": "类型", "qty": "总件数", "rows": "明细行数", "orders": "顺序号个数",
                 "zdnos": "制单号个数", "first": "最早时间", "last": "最晚时间"}
        widths = {"type": 110, "qty": 100, "rows": 90, "orders": 100, "zdnos": 110,
                  "first": 145, "last": 145}
        for c in cols:
            self.tree_stat.heading(c, text=heads[c])
            self.tree_stat.column(c, width=widths[c],
                                  anchor="e" if c in ("qty", "rows", "orders", "zdnos") else "center")
        sb = ttk.Scrollbar(sum_wrap, orient="vertical", command=self.tree_stat.yview)
        self.tree_stat.configure(yscrollcommand=sb.set)
        self.tree_stat.pack(side="left", fill="x", expand=True)
        sb.pack(side="right", fill="y")
        self._stripe(self.tree_stat)
        self.tree_stat.tag_configure("total", background="#e8eef6", foreground="#0b5c8a")

        # ---- 明细 ----
        ttk.Label(page, text="明细（导出内容与此一致）", style="CardTitle.TLabel").pack(anchor="w")
        det_wrap = ttk.Frame(page)
        det_wrap.pack(fill="both", expand=True, pady=(2, 0))
        dcols = ("time", "type", "zdno", "template", "order_no", "ys", "cm", "js", "mode")
        self.tree_stat_det = ttk.Treeview(det_wrap, columns=dcols, show="headings")
        dheads = {"time": "时间", "type": "类型", "zdno": "制单号", "template": "原始工单号",
                  "order_no": "顺序号", "ys": "颜色", "cm": "尺码", "js": "数量", "mode": "模式"}
        dwidths = {"time": 140, "type": 80, "zdno": 175, "template": 175, "order_no": 80,
                   "ys": 95, "cm": 80, "js": 70, "mode": 70}
        for c in dcols:
            self.tree_stat_det.heading(c, text=dheads[c])
            self.tree_stat_det.column(c, width=dwidths[c],
                                      anchor="center" if c in ("order_no", "js", "mode") else "w")
        dsb = ttk.Scrollbar(det_wrap, orient="vertical", command=self.tree_stat_det.yview)
        self.tree_stat_det.configure(yscrollcommand=dsb.set)
        self.tree_stat_det.pack(side="left", fill="both", expand=True)
        dsb.pack(side="right", fill="y")
        self._stripe(self.tree_stat_det)

        self._stat_summary = []
        self._stat_detail = []
        self._stat_range = ("", "")
        self._load_stat_types()

    # ---------- 统计页：时间范围快捷按钮 ----------
    def _stat_range_days(self, days):
        end = datetime.now()
        start = end - timedelta(days=int(days))
        self.stat_from_var.set(start.strftime("%Y-%m-%d"))
        self.stat_to_var.set(end.strftime("%Y-%m-%d"))
        self._do_stat_query()

    def _stat_range_this_month(self):
        now = datetime.now()
        self.stat_from_var.set(now.replace(day=1).strftime("%Y-%m-%d"))
        self.stat_to_var.set(now.strftime("%Y-%m-%d"))
        self._do_stat_query()

    def _stat_range_last_month(self):
        now = datetime.now()
        first_this = now.replace(day=1)
        last_month_end = first_this - timedelta(days=1)
        last_month_start = last_month_end.replace(day=1)
        self.stat_from_var.set(last_month_start.strftime("%Y-%m-%d"))
        self.stat_to_var.set(last_month_end.strftime("%Y-%m-%d"))
        self._do_stat_query()

    def _load_stat_types(self):
        def job():
            return (self.db.get_detail_types(),)
        self._run(job, self._fill_stat_types, "正在读取类型…")

    def _fill_stat_types(self, types):
        values = ["全部"] + list(types)
        self.stat_type_combo["values"] = values
        if self.stat_type_var.get() not in values:
            self.stat_type_var.set("全部")

    # ---------- 统计页：查询 ----------
    def _stat_bounds(self):
        """把界面上的日期解析成 smalldatetime 区间（含当天 23:59:59）。"""
        f_txt = (self.stat_from_var.get() or "").strip()
        t_txt = (self.stat_to_var.get() or "").strip()
        try:
            start = datetime.strptime(f_txt, "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"开始日期格式不对：{f_txt}（应为 YYYY-MM-DD）")
        try:
            end = datetime.strptime(t_txt, "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"结束日期格式不对：{t_txt}（应为 YYYY-MM-DD）")
        if start > end:
            raise ValueError("开始日期不能晚于结束日期")
        return start, end.replace(hour=23, minute=59, second=59)

    def _do_stat_query(self):
        try:
            start, end = self._stat_bounds()
        except ValueError as e:
            messagebox.showerror("日期有误", str(e), parent=self)
            return
        tname = (self.stat_type_var.get() or "全部").strip()

        def job():
            return (self.db.query_detail_summary(start, end, tname),
                    self.db.query_detail_rows(start, end, tname))

        self._run(job, self._fill_stat, f"正在统计 {start:%Y-%m-%d} ~ {end:%Y-%m-%d} …")

    def _fill_stat(self, summary, detail):
        self._stat_summary = summary
        self._stat_detail = detail
        self._stat_range = (self.stat_from_var.get().strip(), self.stat_to_var.get().strip())

        self.tree_stat.delete(*self.tree_stat.get_children())
        for idx, row in enumerate(summary):
            tname, qty, rows, orders, zdnos, first, last = row
            self.tree_stat.insert("", "end", values=(
                tname, f"{int(qty or 0):,}", int(rows or 0), int(orders or 0), int(zdnos or 0),
                first.strftime("%Y-%m-%d %H:%M") if first else "",
                last.strftime("%Y-%m-%d %H:%M") if last else "",
            ), tags=("odd",) if idx % 2 else ())
        total_qty = sum(int(r[1] or 0) for r in summary)
        if summary:
            self.tree_stat.insert("", "end", values=(
                "合计", f"{total_qty:,}", len(detail),
                sum(int(r[3] or 0) for r in summary),
                sum(int(r[4] or 0) for r in summary), "", "",
            ), tags=("total",))

        self.tree_stat_det.delete(*self.tree_stat_det.get_children())
        for idx, row in enumerate(detail):
            at, tname, zdno, template, order_no, ys, cm, js, mode = row
            self.tree_stat_det.insert("", "end", values=(
                at.strftime("%Y-%m-%d %H:%M") if at else "", tname, zdno, template or "",
                order_no, ys or "", cm or "", int(js or 0), mode or "",
            ), tags=("odd",) if idx % 2 else ())

        if not summary:
            self.stat_hint.set("该时间段没有明细数据")
            self.summary_var.set("裁剪统计：该时间段没有数据")
        else:
            self.stat_hint.set(f"{len(summary)} 个类型，合计 {total_qty:,} 件，明细 {len(detail)} 行")
            self.summary_var.set(f"裁剪统计：合计 {total_qty:,} 件（{len(summary)} 个类型）")
        self.status_var.set(f"✅ 统计完成：{total_qty:,} 件")

    # ---------- 统计页：导出 ----------
    def _do_stat_export(self):
        if not self._stat_detail:
            messagebox.showinfo("没有数据", "当前没有可导出的明细，请先点「🔍 查询」。", parent=self)
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        tname = (self.stat_type_var.get() or "全部").strip()
        safe = "".join(ch for ch in tname if ch.isalnum() or ch in "-_") or "全部"
        path = os.path.join(BASE_DIR, f"裁剪统计_{safe}_{stamp}.xlsx")
        ok, msg = export_detail_to_excel(path, self._stat_summary, self._stat_detail)
        if ok:
            self.status_var.set(f"✅ {msg}")
            self.stat_hint.set("已导出")
            messagebox.showinfo("导出完成", msg, parent=self)
            if ask := messagebox.askyesno("导出完成", msg + "\n\n现在打开所在文件夹？", parent=self):
                try:
                    if sys.platform.startswith("win"):
                        os.startfile(os.path.dirname(path))
                    else:
                        open_folder = None  # 保持跨平台不报错
                except Exception:
                    pass
        else:
            messagebox.showerror("导出失败", msg, parent=self)
            self.status_var.set(f"❌ {msg}")

    def _build_status(self):
        bar = ttk.Frame(self, padding=(10, 4))
        bar.pack(fill="x")
        self.status_var = tk.StringVar(value="✅ 就绪")
        ttk.Label(bar, textvariable=self.status_var, style="Status.TLabel").pack(
            side="left", fill="x", expand=True)
        self.summary_var = tk.StringVar(value="")
        self.summary_label = ttk.Label(bar, textvariable=self.summary_var,
                                       style="Summary.TLabel", anchor="e")
        self.summary_label.pack(side="right", padx=(8, 0))

    @staticmethod
    def _stripe(tree: ttk.Treeview):
        tree.tag_configure("odd", background=PALETTE["row_alt"])

    # ---------- 快捷键 ----------
    def _bind_keys(self):
        self.search_entry.bind("<Return>", lambda e: self.do_search())
        self.search_entry.bind("<KeyRelease>", self._on_search_key)
        self.bind("<Control-f>", lambda e: self.search_entry.focus_set())
        self.bind("<Escape>", lambda e: self._cancel_edit())
        self.bind("<F5>", lambda e: self.reload_current())
        self.bind("<Control-z>", lambda e: self.do_undo())

    def _on_search_key(self, _event=None):
        if self._editor is not None:
            self._finish_edit(commit=True)
        self.search_info.set("按回车查询")

    # ---------- 异步执行 ----------
    def _prewarm(self):
        """后台预取颜色/尺码/缸号/工序名称的候选值，避免编辑时卡顿。"""
        for table, column in (("jfzd2", "YS"), ("jfzd2", "CM"),
                              ("jfzd2", "GH"), ("jfdj", "GXNAME")):
            try:
                self._suggest_cache[f"{table}.{column}"] = self.db.get_distinct(table, column)
            except Exception:
                self._suggest_cache[f"{table}.{column}"] = []

    def _set_busy(self, busy: bool, text: str = ""):
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.btn_undo.config(state=state)
        if busy and text:
            self.status_var.set(f"⏳ {text}")

    def _run(self, func, on_done, busy_text="处理中…"):
        """把一个数据库任务放进队列，在后台线程执行，结果回到主线程渲染。

        func 必须返回 tuple，回调按 tuple 的元素展开为参数。
        用队列而不是“忙就直接跳过”，是为了避免连续操作被静默丢弃。
        """
        if self._closing:
            return
        self._queue.append((func, on_done))
        self._pump(busy_text)

    def _pump(self, busy_text="处理中…"):
        if self._closing or self._busy or not self._queue:
            return
        func, on_done = self._queue.pop(0)
        self._set_busy(True, busy_text)

        def worker():
            # 工作线程只往队列里塞结果，绝不碰 Tk（跨线程调用 after 会失败，
            # 一旦失败界面就会永远停在“处理中”）。
            try:
                value = func()
                if not isinstance(value, tuple):
                    value = (value,)
                self._results.put((on_done, True, value, None, None))
            except Exception as exc:
                self._results.put((on_done, False, None, exc, traceback.format_exc()))

        threading.Thread(target=worker, daemon=True).start()
        self._ensure_timer()

    def _ensure_timer(self):
        """由主线程调度轮询定时器，把后台结果取出来刷新界面。"""
        if self._timer_on or self._closing:
            return
        self._timer_on = True
        try:
            self.after(80, self._drain)
        except (tk.TclError, RuntimeError):
            self._timer_on = False

    def _drain(self):
        self._timer_on = False
        if self._closing:
            return
        got = False
        while True:
            try:
                on_done, ok, value, exc, tb = self._results.get_nowait()
            except queue.Empty:
                break
            got = True
            self._set_busy(False)
            if ok:
                try:
                    on_done(*value)
                except Exception as e:
                    self.status_var.set(f"❌ 界面刷新失败：{e}")
            else:
                self.status_var.set(f"❌ 出错：{friendly_error(exc)}")
                try:
                    messagebox.showerror("数据库错误", f"{exc}\n\n{tb[-800:]}", parent=self)
                except tk.TclError:
                    pass
        if self._queue and not self._busy:
            self._pump()
        # 还有任务在跑或队列里还有活，就继续轮询；完全空闲才停
        if self._queue or self._busy:
            self._ensure_timer()

    # ---------- 查询 ----------
    def _initial_load(self):
        threading.Thread(target=self._prewarm, daemon=True).start()
        # 被主程序带着 ZDNO 打开时，不要再用“最近工单”把搜索框清空
        if self.current_zdno:
            zdno = self.current_zdno
            self._run(lambda: (self.db.search_zdno(zdno),),
                      self._fill_results, f"正在加载 {zdno} …")
            return
        self.show_recent()

    def do_search(self):
        keyword = self.search_var.get().strip()
        if not keyword:
            self.show_recent()
            return
        self._run(lambda: (self.db.search_zdno(keyword),),
                  self._fill_results, f"正在查询「{keyword}」…")

    def show_recent(self):
        self.search_var.set("")
        self._run(lambda: (self.db.recent_zdno(200),),
                  self._fill_results, "正在加载最近工单…")

    def _fill_results(self, rows):
        self.tree_list.delete(*self.tree_list.get_children())
        for idx, row in enumerate(rows):
            zdno, gx_cnt, r_cnt, k_cnt, mdate = row
            mdate_s = ""
            if mdate:
                try:
                    mdate_s = mdate.strftime("%Y-%m-%d %H:%M")
                except AttributeError:
                    mdate_s = str(mdate)
            self.tree_list.insert(
                "", "end", iid=str(idx),
                values=(zdno, gx_cnt, r_cnt, k_cnt, mdate_s),
                tags=("odd",) if idx % 2 else ())
        self.search_info.set(f"共 {len(rows)} 条")
        self.status_var.set(f"✅ 查询到 {len(rows)} 个 ZDNO")
        if rows:
            first = self.tree_list.get_children()[0]
            self.tree_list.selection_set(first)
            self.tree_list.focus(first)

    def _on_pick(self, _event=None):
        if self._editor is not None:
            self._finish_edit(commit=True)

    def _open_selected(self):
        sel = self.tree_list.selection()
        if not sel:
            return
        values = self.tree_list.item(sel[0], "values")
        zdno = str(values[0]).strip()
        if zdno:
            self.load_zdno(zdno)

    def load_zdno(self, zdno: str):
        if self._editor is not None:
            self._finish_edit(commit=True)
        self.current_zdno = zdno

        def job():
            return (self.db.get_jfdj(zdno), self.db.get_jfzd2(zdno),
                    self.db.get_jfzd3(zdno), self.db.get_counts(zdno))

        self._run(job, self._fill_detail, f"正在加载 {zdno} …")

    def reload_current(self):
        if self.current_zdno:
            self.load_zdno(self.current_zdno)
        else:
            self.status_var.set("请先选择一个 ZDNO")

    def _fill_detail(self, gx_rows, dt_rows, mx_rows, counts):
        self.title_var.set(self.current_zdno or "未选择工单")

        warn = ""
        if mx_rows and not any(int(r[1]) == 0 for r in mx_rows):
            warn = " · 缺少表头行"
        cc_detail = ""
        if len(counts["by_cc"]) > 1:
            cc_detail = " · " + " ".join(
                f"床次{cc}:{n:,}件" for cc, _rows, n in counts["by_cc"])
        self.subtitle_var.set(
            f"工序 {counts['jfdj']} 条 · 明细 {counts['jfzd2']} 条 · "
            f"总数量 {counts['total_qty']:,} 件{cc_detail}{warn}"
        )
        self._update_summary(counts, flash=self._flash_pending)
        self._flash_pending = False

        self.tree_gx.delete(*self.tree_gx.get_children())
        for idx, (gx, gxname, dj, mdate, oksl) in enumerate(gx_rows):
            self.tree_gx.insert("", "end", iid=str(idx), values=(
                gx, gxname or "",
                (f"{Decimal(str(dj)):.4f}" if dj is not None else ""),
                mdate.strftime("%Y-%m-%d %H:%M") if mdate else "",
                oksl if oksl is not None else 0,
            ), tags=("odd",) if idx % 2 else ())
        self.gx_hint.set("")

        self._detail_rows = dt_rows
        self.tree_dt.delete(*self.tree_dt.get_children())
        for idx, (cc, zh, gh, ys, cm, js, mdate) in enumerate(dt_rows):
            self.tree_dt.insert("", "end", iid=str(idx), values=(
                cc, zh, gh or "", ys or "", cm or "",
                js if js is not None else 0,
                mdate.strftime("%Y-%m-%d %H:%M") if mdate else "",
            ), tags=("odd",) if idx % 2 else ())
        self.matrix_hint.set("")

        self.tree_mx.delete(*self.tree_mx.get_children())
        for idx, row in enumerate(mx_rows):
            cc, item, zhbegin, gh, ys = row[0], row[1], row[2], row[3], row[4]
            sls = ["" if v is None else str(v) for v in row[5:5 + MAX_SL]]
            tags = ["header"] if int(item) == 0 else (["odd"] if idx % 2 else [])
            self.tree_mx.insert("", "end", iid=str(idx),
                                values=(cc, item, zhbegin, gh or "", ys or "", *sls),
                                tags=tags)

        if self._pending_status:
            self.status_var.set(self._pending_status)
            self._pending_status = None
        else:
            self.status_var.set(f"✅ 已加载 {self.current_zdno}")
        self._projected_total = None

    # ---------- 单元格编辑 ----------
    _editor = None

    def _begin_edit(self, tree: ttk.Treeview, event):
        if self._busy or not self.current_zdno:
            return
        row_id = tree.identify_row(event.y)
        col_id = tree.identify_column(event.x)
        if not row_id or not col_id:
            return
        col = int(col_id[1:]) - 1
        cols = tree["columns"]
        if col >= len(cols):
            return
        name = cols[col]
        editable = {"gxname", "dj"} if tree is self.tree_gx else {"gh", "ys", "cm", "js"}
        if name not in editable:
            return

        self._finish_edit(commit=False)
        cell = tree.bbox(row_id, col_id)
        if not cell:
            return
        x, y, w, h = cell
        current = tree.set(row_id, name)
        entry = ttk.Entry(tree, width=max(6, int(w / 8)))
        entry.insert(0, current)
        entry.select_range(0, "end")
        entry.place(x=x, y=y, width=w, height=h)
        entry.focus_set()
        self._editor = {"tree": tree, "iid": row_id, "col": name, "entry": entry}

        entry.bind("<Return>", lambda e: self._finish_edit(commit=True))
        entry.bind("<FocusOut>", lambda e: self.after(120, self._finish_edit, True))
        entry.bind("<Escape>", lambda e: self._cancel_edit())

        if name in ("ys", "cm", "gh", "gxname"):
            entry.bind("<KeyRelease>", lambda e: self._autocomplete(entry, name))

    def _autocomplete(self, entry, name):
        table = "jfzd2" if name in ("ys", "cm", "gh") else "jfdj"
        column = {"ys": "YS", "cm": "CM", "gh": "GH", "gxname": "GXNAME"}[name]
        key = f"{table}.{column}"
        if key not in self._suggest_cache:
            try:
                self._suggest_cache[key] = self.db.get_distinct(table, column)
            except Exception:
                self._suggest_cache[key] = []
        text = entry.get().strip()
        if not text:
            return
        for value in self._suggest_cache[key]:
            if value.startswith(text) and value != text:
                entry.delete(0, "end")
                entry.insert(0, value)
                entry.icursor("end")
                entry.selection_range(0, "end")
                entry.xview("end")
                break

    def _cancel_edit(self):
        self._finish_edit(commit=False)

    def _finish_edit(self, commit=True):
        ed = self._editor
        if ed is None:
            return
        self._editor = None
        tree, iid, name = ed["tree"], ed["iid"], ed["col"]
        entry = ed["entry"]
        new_text = entry.get()
        old_text = tree.set(iid, name)
        try:
            entry.destroy()
        except tk.TclError:
            pass
        if not commit or new_text.strip() == old_text.strip():
            return

        if tree is self.tree_gx:
            self._commit_process(tree, iid, name, new_text)
        else:
            self._commit_detail(tree, iid, name, new_text)

    def _commit_process(self, tree, iid, name, new_text):
        gx = int(tree.set(iid, "gx"))
        old_name = tree.set(iid, "gxname")
        old_dj = tree.set(iid, "dj")

        if name == "gxname":
            new_name, new_dj = new_text.strip(), old_dj
        else:
            new_name, new_dj = old_name, new_text.strip()

        valid_name, err = check_gxname(new_name)
        if err:
            messagebox.showerror("输入无效", err, parent=self)
            return
        price, err = check_dj(new_dj)
        if err:
            messagebox.showerror("输入无效", err, parent=self)
            return

        info = (f"ZDNO：{self.current_zdno}\n工序：{gx}\n\n"
                f"工序名称：{old_name or '(空)'}  →  {valid_name}\n"
                f"单价    ：{old_dj or '(空)'}  →  {price}")
        if not messagebox.askyesno("确认修改", info + "\n\n确认写入数据库？", parent=self):
            return

        self._projected_total = None   # 改单价/名称不影响总数量

        def job():
            return self.db.update_process(self.current_zdno, gx, valid_name, price)

        self._run(job, self._after_write, "正在写入…")

    def _commit_detail(self, tree, iid, name, new_text):
        idx = int(iid)
        cc, zh, gh, ys, cm, js = self._detail_rows[idx][0:6]

        old_vals = {
            "gh": str(gh or ""),
            "ys": str(ys or ""),
            "cm": str(cm or ""),
            "js": str(js if js is not None else 0),
        }
        new_vals = dict(old_vals)
        new_vals[name] = new_text.strip()
        if new_vals[name] == old_vals[name]:
            return

        v_gh, err = check_gh(new_vals["gh"])
        if err:
            messagebox.showerror("输入无效", err, parent=self)
            return
        v_ys, err = check_ys(new_vals["ys"])
        if err:
            messagebox.showerror("输入无效", err, parent=self)
            return
        v_cm, err = check_cm(new_vals["cm"])
        if err:
            messagebox.showerror("输入无效", err, parent=self)
            return
        v_js, err = check_js(new_vals["js"])
        if err:
            messagebox.showerror("输入无效", err, parent=self)
            return

        labels = {"gh": "缸号", "ys": "颜色", "cm": "尺码", "js": "数量"}
        changed = [k for k in ("gh", "ys", "cm", "js") if new_vals[k] != old_vals[k]]
        lines = [f"ZDNO：{self.current_zdno}　床次 {cc}　扎号 {zh}", ""]
        for k in changed:
            lines.append(f"{labels[k]}：{old_vals[k] or '(空)'}  →  {new_vals[k] or '(空)'}")
        lines += ["", "只修改 jfzd2 这一行，颜色矩阵不会被自动改动。", "确认写入数据库？"]
        if not messagebox.askyesno("确认修改", "\n".join(lines), parent=self):
            return

        # 用本地明细算出改完之后的总数量，写入成功后直接显示在状态栏
        try:
            current_total = sum(int(r[5] or 0) for r in self._detail_rows)
            self._projected_total = current_total - int(js or 0) + v_js
        except (TypeError, ValueError, IndexError):
            self._projected_total = None

        def job():
            return self.db.update_detail(self.current_zdno, int(cc), int(zh),
                                         v_ys, v_cm, v_js)

        self._run(job, self._after_write, "正在写入…")

    def _after_write(self, ok, msg, _before):
        if ok:
            # 数量类改动：先按本地数据算出改完的总数量，让状态栏立刻能报出来
            extra = ""
            if self._projected_total is not None:
                extra = f"，总数量现为 {self._projected_total:,} 件"
            self._pending_status = f"✅ {msg}{extra}"
            self._flash_pending = True
            self.matrix_hint.set("jfzd3 未自动同步，如需要请手动重建")
            self.reload_current()
        else:
            self._projected_total = None
            self._pending_status = None
            self.status_var.set(f"❌ {msg}")
            messagebox.showerror("修改失败", msg, parent=self)
        if self.on_log:
            try:
                self.on_log(f"[ZDNO维护] {self.current_zdno} {msg}{extra}")
            except Exception:
                pass

    # ---------- 底部汇总条 ----------
    def _format_summary(self, counts) -> str:
        """把汇总数据拼成底部状态栏的一行文字（总数量放在最前面）。"""
        if not counts:
            return ""
        qty = counts["total_qty"]
        parts = [f"总数量 {qty:,} 件"]
        parts.append(f"明细 {counts['jfzd2']:,} 行")
        parts.append(f"工序 {counts['jfdj']:,} 条")
        if counts["color_cnt"]:
            parts.append(f"颜色 {counts['color_cnt']} 种")
        parts.append(f"矩阵 {counts['jfzd3']:,} 行")

        text = " ｜ ".join(parts)
        if len(counts["by_cc"]) > 1:
            detail = "，".join(f"床次{cc} {n:,}件" for cc, _rows, n in counts["by_cc"])
            text += f" ｜ {detail}"
        return text

    def _update_summary(self, counts, flash=False):
        """刷新底部汇总条；flash=True 时短暂高亮，方便一眼看到总数量变了。"""
        text = self._format_summary(counts)
        if not text:
            self.summary_var.set("")
            return
        self.summary_var.set(text)
        if not flash:
            return
        try:
            self.summary_label.configure(style="SummaryHot.TLabel")
            self.after(2200, self._summary_flash_off)
        except tk.TclError:
            pass

    def _summary_flash_off(self):
        try:
            if not self._closing:
                self.summary_label.configure(style="Summary.TLabel")
        except tk.TclError:
            pass

    # ---------- 一致性校验（只读） ----------
    def check_consistency(self):
        if not self.current_zdno:
            messagebox.showinfo("提示", "请先选择一个 ZDNO", parent=self)
            return
        zdno = self.current_zdno

        def job():
            details = self.db.get_jfzd2(zdno)
            matrix = self.db.get_jfzd3(zdno)
            report = {"detail_rows": len(details), "matrix_rows": len(matrix)}
            header = [r for r in matrix if int(r[1]) == 0]
            report["has_header"] = bool(header)
            by_cc = {}
            for cc, zh, _gh, ys, _cm, _js, _md in details:
                by_cc.setdefault(int(cc), []).append((int(zh), str(ys or "")))
            report["over_limit"] = {cc: len(v) for cc, v in by_cc.items() if len(v) > MAX_SL}
            report["matrix_ccs"] = sorted({int(r[0]) for r in matrix})
            report["detail_ccs"] = sorted(by_cc)
            names2 = sorted({ys for rows_ in by_cc.values() for _zh, ys in rows_})
            names3 = sorted({str(r[4] or "") for r in matrix if int(r[1]) > 0})
            report["color_diff"] = sorted(set(names2) ^ set(names3))
            return (report,)

        self._run(job, self._show_consistency, "正在比对 jfzd2 与 jfzd3…")

    def _show_consistency(self, report):
        lines = [f"ZDNO：{self.current_zdno}", ""]
        lines.append(f"jfzd2 明细行数：{report['detail_rows']}")
        lines.append(f"jfzd3 矩阵行数：{report['matrix_rows']}")
        lines.append(f"jfzd3 表头行：{'有' if report['has_header'] else '缺失'}")
        lines.append(f"jfzd2 床次：{report['detail_ccs']}")
        lines.append(f"jfzd3 床次：{report['matrix_ccs']}")
        if report["over_limit"]:
            lines += ["", "以下床次的明细超过 50 条，矩阵无法完整表达："]
            for cc, n in sorted(report["over_limit"].items()):
                lines.append(f"  床次 {cc}：{n} 条")
        if report["color_diff"]:
            lines += ["", "jfzd2 与 jfzd3 的颜色名称不一致（仅出现在其中一侧）："]
            lines += [f"  {name}" for name in report["color_diff"][:20]]
        else:
            lines += ["", "两侧颜色名称一致。"]
        lines += ["", "以上为只读比对，未修改任何数据。"]
        messagebox.showinfo("一致性校验", "\n".join(lines), parent=self)

    # ---------- 重建颜色矩阵（唯一会删行的操作，需二次确认） ----------
    def ask_rebuild(self):
        if not self.current_zdno:
            messagebox.showinfo("提示", "请先选择一个 ZDNO", parent=self)
            return
        zdno = self.current_zdno

        warn = (
            f"即将重建 ZDNO「{zdno}」的颜色矩阵（jfzd3）。\n\n"
            f"· 会先删除该工单原有的 jfzd3 行，再按 jfzd2 重新生成。\n"
            f"· 只影响这一个工单，不会动其他工单。\n"
            f"· 重建前会自动保存完整快照，可点「撤销上次修改」还原。\n\n"
            f"如果你只是改了颜色或数量，通常不需要重建。\n\n确认继续？"
        )
        if not messagebox.askyesno("重建颜色矩阵", warn, parent=self):
            return

        self._run(lambda: self.db.plan_rebuild_jfzd3(zdno), self._show_plan, "正在生成重建方案…")

    def _show_plan(self, ok, msg, plan):
        if not ok:
            messagebox.showwarning("无法重建", msg + "\n\n数据库未做任何改动。", parent=self)
            self.status_var.set(f"⚠ {msg}")
            return
        self.current_plan = plan
        before_n = len(plan["before"])
        after_n = len(plan["after"])
        text = [f"ZDNO：{_as_text(self.current_zdno)}", ""]
        text.append(f"当前 jfzd3 行数：{before_n}")
        text.append(f"重建后行数：{after_n}")
        text.append(f"涉及床次：{plan['cc']}")
        text += ["", "重建后的内容：", ""]
        for cc, item, zh_begin, gh, ys, sl in plan["after"][:40]:
            filled = [f"{i+1}:{v}" for i, v in enumerate(sl) if v not in ("", None)]
            head = "表头" if int(item) == 0 else f"ITEM {item}"
            text.append(f"  床次{cc} {head} {ys or ''} 起始扎号{zh_begin}")
            if int(item) == 0:
                text.append(f"      槽位尺码：{', '.join(str(v) for v in sl)}")
            else:
                text.append(f"      数量：{', '.join(filled) if filled else '(空)'}")
        if len(plan["after"]) > 40:
            text.append(f"  ...（共 {len(plan['after'])} 行）")
        text += ["", "确认执行重建？"]
        if not messagebox.askyesno("确认重建", "\n".join(text), parent=self):
            self.current_plan = None
            return

        zdno = self.current_zdno
        self._run(lambda: self.db.rebuild_jfzd3(zdno, plan),
                  self._after_rebuild, "正在重建颜色矩阵…")

    def _after_rebuild(self, ok, msg, _x):
        if ok:
            self.status_var.set(f"✅ {msg}")
            messagebox.showinfo("完成", msg + "\n\n如需还原，请点「撤销上次修改」。", parent=self)
            self.load_zdno(self.current_zdno)
        else:
            self.status_var.set(f"❌ {msg}")
            messagebox.showerror("重建失败", msg, parent=self)

    # ---------- 撤销 ----------
    def do_undo(self):
        snap = load_snapshot()
        if not snap:
            messagebox.showinfo("无可撤销", "没有找到修改快照。", parent=self)
            return
        table = snap.get("table")
        key = snap.get("key") or {}
        before = snap.get("before") or {}
        after = snap.get("after") or {}
        detail = f"时间：{snap.get('time')}\n表：{table}\n主键：{key}\n\n修改前：{before}\n修改后：{after}"
        if not messagebox.askyesno("撤销上次修改",
                                   detail + "\n\n确认把数据还原成“修改前”的状态？",
                                   parent=self):
            return

        def job():
            return self.db.undo_last()

        self._run(job, self._after_undo, "正在撤销…")

    def _after_undo(self, ok, msg, snap):
        if ok:
            messagebox.showinfo("已撤销", msg, parent=self)
            self.status_var.set(f"✅ {msg}")
            zdno = (snap or {}).get("key", {}).get("zdno")
            if zdno:
                self.current_zdno = zdno
                self.load_zdno(zdno)
        else:
            messagebox.showerror("撤销失败", msg, parent=self)
            self.status_var.set(f"❌ {msg}")

    # ---------- 关闭 ----------
    def _on_close(self):
        self._closing = True
        self._queue = []
        self._timer_on = False
        try:
            if self._editor is not None:
                self._finish_edit(commit=False)
        except Exception:
            pass
        try:
            self.db.close()
        finally:
            self.destroy()


# ==================== 裁剪统计：Excel 导出 ====================

def export_detail_to_excel(path, summary_rows, detail_rows):
    """把裁剪统计写成 xlsx。

    优先用 openpyxl；没装或出错时退回 CSV（Excel 能直接打开），保证一定导出得出来。
    返回 (True, 提示信息) 或 (False, 错误信息)。
    """
    headers = ["时间", "类型", "制单号", "原始工单号", "顺序号", "颜色", "尺码", "数量", "模式"]
    sum_headers = ["类型", "总件数", "明细行数", "顺序号个数", "制单号个数", "最早时间", "最晚时间"]

    def _fmt(v):
        if v is None:
            return ""
        if hasattr(v, "strftime"):
            return v.strftime("%Y-%m-%d %H:%M:%S")
        return v

    def _to_csv(reason):
        import csv
        try:
            csv_path = os.path.splitext(path)[0] + ".csv"
            with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(headers)
                for row in detail_rows:
                    w.writerow([_fmt(c) for c in row])
            return True, f"{reason}：{csv_path}\n共 {len(detail_rows)} 行"
        except Exception as exc:
            return False, f"导出失败：{friendly_error(exc)}"

    try:
        import openpyxl
        from openpyxl.styles import Font, Alignment, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        return _to_csv("未安装 openpyxl，已改为导出 CSV（Excel 可直接打开）")

    try:
        wb = openpyxl.Workbook()

        ws1 = wb.active
        ws1.title = "汇总"
        ws1.append(sum_headers)
        for row in summary_rows:
            ws1.append([_fmt(row[0]), int(row[1] or 0), int(row[2] or 0),
                        int(row[3] or 0), int(row[4] or 0), _fmt(row[5]), _fmt(row[6])])

        ws2 = wb.create_sheet("明细")
        ws2.append(headers)
        for row in detail_rows:
            ws2.append([_fmt(c) for c in row])

        head_font = Font(bold=True, color="FFFFFF")
        head_fill = PatternFill("solid", fgColor="2C3E50")
        for ws, cols in ((ws1, len(sum_headers)), (ws2, len(headers))):
            for i in range(1, cols + 1):
                cell = ws.cell(row=1, column=i)
                cell.font = head_font
                cell.fill = head_fill
                cell.alignment = Alignment(horizontal="center", vertical="center")
            ws.freeze_panes = "A2"
            for i in range(1, cols + 1):
                ws.column_dimensions[get_column_letter(i)].width = 22 if i in (3, 4) else 16

        wb.save(path)
        return True, (f"已导出 Excel：{path}\n"
                      f"汇总 {len(summary_rows)} 行，明细 {len(detail_rows)} 行")
    except Exception:
        return _to_csv("生成 xlsx 时出错，已改为导出 CSV")


def open_zdno_editor(parent, conn_str, on_log=None):
    """供 hbc-print 主程序调用：打开 ZDNO 维护窗口。

    已存在窗口时直接前置，避免重复开窗。
    """
    for child in parent.winfo_children():
        if isinstance(child, ZDNOEditDialog) and child.winfo_exists():
            try:
                child.deiconify()
                child.lift()
                child.focus_force()
            except tk.TclError:
                pass
            return child
    return ZDNOEditDialog(parent, conn_str, on_log=on_log)


# ============================================================
# 独立运行
# ============================================================

def _default_conn_str():
    return (
        "DRIVER={SQL Server};"
        "SERVER=192.168.0.73;"
        "DATABASE=ShintHrmDb;"
        "UID=sa;PWD=1"
    )


def _run_standalone():
    root = tk.Tk()
    # root 只作为 Tk 父窗口存在并保持隐藏，真正的界面是下面的 Toplevel。
    # 隐藏 master 不会影响子窗口显示（transient 已在构造函数里按可见性跳过）。
    root.withdraw()

    # 先把窗口显示出来，再由后台线程去连数据库。
    # 这样即使数据库连不上，用户也能立刻看到界面和报错，而不是对着黑屏等超时。
    win = ZDNOEditDialog(root, _default_conn_str())

    def shutdown():
        try:
            win._on_close()
        finally:
            try:
                root.destroy()
            except tk.TclError:
                pass

    # 独立运行时关闭维护窗口必须退出程序，否则 mainloop 会一直空转
    win.protocol("WM_DELETE_WINDOW", shutdown)
    win.deiconify()
    win.lift()
    win.focus_force()
    root.mainloop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(_run_standalone())
    except Exception:
        # 界面起不来时，至少把原因留在控制台，不要静默退出
        import traceback

        traceback.print_exc()
        print("[zdno_edit] 启动失败，请把上面的报错信息发出来。", flush=True)
        sys.exit(1)
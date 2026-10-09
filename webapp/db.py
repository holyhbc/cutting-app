# -*- coding: utf-8 -*-
"""扫码报工 · 本地暂存库访问层（SQLite）。

设计原则（见 docs/DECISIONS.md）：
- D-006 上限：SUM(jfgz.js)[zdno,cc,zh,gx] <= SUM(jfzd2.JS)[zdno,cc,zh]
  本地没有 jfgz 实时数据，所以用「历史基线 + 本地待同步」两部分相加。
- D-003 barcode=1 表示扫码录入
- D-004 gzdate 存完整日期时间
"""
import os
import sqlite3
import threading
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("SCAN_DB", os.path.join(BASE_DIR, "scan.db"))
SCHEMA = os.path.join(BASE_DIR, "schema.sql")

_local = threading.local()
_write_lock = threading.Lock()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_conn():
    """每线程一个连接。SQLite 连接不能跨线程共享。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")     # 允许读并发
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def init_db():
    conn = get_conn()
    with open(SCHEMA, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()


def log_audit(action, detail="", ip=""):
    conn = get_conn()
    conn.execute("INSERT INTO audit (at, action, detail, ip) VALUES (?,?,?,?)",
                 (now_str(), action, detail[:2000], ip))
    conn.commit()


# ---------------------------------------------------------------- 扫码映射

def upsert_scan_map(short_id, zdno, cc, zh, gxname=""):
    conn = get_conn()
    conn.execute(
        "INSERT INTO scan_map (short_id, zdno, cc, zh, gxname, created_at) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(short_id) DO UPDATE SET zdno=excluded.zdno, cc=excluded.cc, "
        "zh=excluded.zh, gxname=excluded.gxname",
        (short_id, zdno, int(cc), int(zh), gxname, now_str()))
    conn.commit()


def get_scan_map(short_id):
    row = get_conn().execute(
        "SELECT * FROM scan_map WHERE short_id=?", (short_id,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------- 数量查询

def get_plan_qty(zdno, cc, zh):
    """该扎应做数 = SUM(jfzd2.JS)，按颜色×尺码求和。

    注意不能取单行 JS —— 一扎可能有多行（不同颜色/尺码）。
    """
    row = get_conn().execute(
        "SELECT COALESCE(SUM(plan_qty),0) FROM bundle WHERE zdno=? AND cc=? AND zh=?",
        (zdno, int(cc), int(zh))).fetchone()
    return int(row[0]) if row else 0


def get_reported_qty(zdno, cc, zh, gx):
    """已报数 = SQL Server 历史基线 + 本地尚未同步的报工。

    同步成功后 baseline 会被同步器刷新，那时 local_pending 归零，
    两者自动衔接，不会重复计算。
    """
    conn = get_conn()
    base = conn.execute(
        "SELECT reported_qty FROM report_baseline WHERE zdno=? AND cc=? AND zh=? AND gx=?",
        (zdno, int(cc), int(zh), int(gx))).fetchone()
    base_qty = int(base[0]) if base else 0
    pending = conn.execute(
        "SELECT COALESCE(SUM(js),0) FROM report "
        "WHERE zdno=? AND cc=? AND zh=? AND gx=? AND status IN ('pending','failed')",
        (zdno, int(cc), int(zh), int(gx))).fetchone()
    return base_qty + int(pending[0])


def get_reported_by_employee(zdno, cc, zh, gx, ygno):
    """某员工在该扎该工序的已报量（用于改量上限：应做 - 他人已报 + 自己已报）"""
    conn = get_conn()
    base = conn.execute(
        "SELECT COALESCE(SUM(js),0) FROM report "
        "WHERE zdno=? AND cc=? AND zh=? AND gx=? AND ygno=? AND status IN ('pending','failed')",
        (zdno, int(cc), int(zh), int(gx), ygno)).fetchone()
    return int(base[0])


def get_processes(zdno):
    """该定单可做的工序列表（gx 已在同步时限定 1~18）"""
    rows = get_conn().execute(
        "SELECT gx, gxname, dj FROM process WHERE zdno=? ORDER BY gx", (zdno,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 报工

def create_report(short_id, zdno, cc, zh, gx, gxname, ygno, ygname, js, dj, ip=""):
    """新增报工。幂等：唯一索引冲突时返回已有记录的 id，不重复计入。"""
    je = round(float(js) * float(dj), 4)
    stamp = now_str()
    with _write_lock:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO report (short_id, zdno, cc, zh, gx, gxname, ygno, ygname, "
                "js, dj, je, gzdate, barcode, status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,'pending',?)",
                (short_id, zdno, int(cc), int(zh), int(gx), gxname, ygno, ygname,
                 int(js), float(dj), je, stamp, stamp))
            conn.commit()
            new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("UPDATE scan_map SET used_count=used_count+1 WHERE short_id=?",
                         (short_id,))
            conn.commit()
            log_audit("report", f"#{new_id} {zdno} cc{cc} zh{zh} gx{gx} {ygno} x{js}", ip)
            return {"id": new_id, "created": True, "duplicated": False}
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT id FROM report WHERE short_id=? AND zdno=? AND cc=? AND zh=? "
                "AND gx=? AND ygno=? AND js=? AND gzdate=?",
                (short_id, zdno, int(cc), int(zh), int(gx), ygno, int(js), stamp)).fetchone()
            conn.rollback()
            return {"id": row[0] if row else None, "created": False, "duplicated": True}


def list_pending(limit=200):
    rows = get_conn().execute(
        "SELECT * FROM report WHERE status='pending' ORDER BY id LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def mark_report(id_, status, jfgzid=None, err=None):
    conn = get_conn()
    if status == "synced":
        conn.execute(
            "UPDATE report SET status='synced', synced_at=?, jfgzid=?, last_err=NULL WHERE id=?",
            (now_str(), jfgzid, id_))
    else:
        conn.execute(
            "UPDATE report SET status=?, last_err=?, retry=retry+1 WHERE id=?",
            (status, (err or "")[:500], id_))
    conn.commit()


def stats():
    conn = get_conn()
    one = lambda q: conn.execute(q).fetchone()[0]
    return {
        "scan_map": one("SELECT COUNT(*) FROM scan_map"),
        "bundle": one("SELECT COUNT(*) FROM bundle"),
        "process": one("SELECT COUNT(*) FROM process"),
        "pending": one("SELECT COUNT(*) FROM report WHERE status='pending'"),
        "synced": one("SELECT COUNT(*) FROM report WHERE status='synced'"),
        "failed": one("SELECT COUNT(*) FROM report WHERE status='failed'"),
    }

# -*- coding: utf-8 -*-
"""扫码报工 · 本地暂存库访问层（SQLite）。

设计原则（见 docs/DECISIONS.md）：
- D-006 上限：SUM(jfgz.js)[zdno,cc,zh,gx] <= SUM(jfzd2.JS)[zdno,cc,zh]
  本地没有 jfgz 实时数据，所以用「历史基线 + 本地待同步」两部分相加。
- D-003 barcode=1 表示扫码录入
- D-004 gzdate 存完整日期时间
"""
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta

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


def _migrate_quiet():
    """测试环境不刷 migrate 日志（每建一个库都打一行太吵）。"""
    return os.environ.get("HBC_QUIET_MIGRATE") == "1"


def _schema_columns():
    """从 schema.sql 里解析出「表 -> [(列名, 列定义)]」。

    迁移必须由 schema.sql 驱动，不能手写列清单 ——
    手写的清单会随表结构改动而悄悄过期，
    结果就是线上又报 "no such column: xxx"。踩过两次了。
    """
    import re
    try:
        with open(SCHEMA, "r", encoding="utf-8") as f:
            sql = f.read()
    except Exception:
        return {}
    out = {}
    for m in re.finditer(
            r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\);", sql, re.S):
        table, body = m.group(1), m.group(2)
        cols = []
        for line in body.splitlines():
            # 先去掉行尾注释（schema.sql 里列定义后面常跟说明），
            # 再去尾逗号，否则 DEFAULT 'pending', -- xxx 会把注释
            # 一起当成列定义带进 ALTER TABLE，直接语法错。
            line = line.split("--")[0].strip().rstrip(",").strip()
            if not line:
                continue
            name = line.split()[0]
            if not re.match(r"^[A-Za-z_]\w*$", name):
                continue
            if name.upper() in ("PRIMARY", "UNIQUE", "FOREIGN", "CHECK",
                                "CONSTRAINT"):
                continue
            cols.append((name, line))
        if cols:
            out[table] = cols
    return out


def _migrate(conn):
    """把历史库补齐到 schema.sql 声明的结构。

    schema.sql 全是 CREATE TABLE IF NOT EXISTS —— 对已存在的旧表
    它一个列都不会补。旧库缺列会直接抛 "no such column: xxx"（500）。

    做法：以 schema.sql 为准，缺的列一律补上。已存在的跳过，幂等。
    """
    def cols(table):
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}

    for table, decls in _schema_columns().items():
        have = cols(table)
        if not have:
            continue                      # 表不存在，交给 schema.sql 建
        for name, ddl in decls:
            if name in have:
                continue
            # ALTER TABLE ADD COLUMN 的限制比 CREATE TABLE 严：
            #  · 带 PRIMARY KEY / UNIQUE 的列定义带过去必然语法错
            #  · NOT NULL 且没有 DEFAULT 时，SQLite 会因为存量行
            #    没有值而直接拒绝（报错文案还不一定指向 ALTER）
            # 所以这里剥掉键约束，并给无默认值的 NOT NULL 补一个默认值。
            d = re.sub(r"\s+CONSTRAINT\s+\S+\s+PRIMARY\s+KEY[^,]*", "", ddl,
                       flags=re.I)
            d = re.sub(r"\s+PRIMARY\s+KEY[^,]*", "", d, flags=re.I)
            d = re.sub(r"\s+UNIQUE[^,]*", "", d, flags=re.I)
            has_default = re.search(r"\bDEFAULT\b", d, re.I) is not None
            if re.search(r"\bNOT\s+NULL\b", d, re.I) and not has_default:
                # 按类型给一个安全的空值
                if re.match(r"^[\w]+\s+(TEXT|REAL|FLOAT|DOUBLE)", d, re.I):
                    d = d[:len("NOT NULL")] + "DEFAULT ''"
                elif re.search(r"\bINTEGER\b", d, re.I):
                    d = d[:len("NOT NULL")] + "DEFAULT 0"
                else:
                    d = d[:len("NOT NULL")] + "DEFAULT NULL"
            d = re.sub(r"\bNOT\s+NULL\b", "", d, flags=re.I)
            d = d.replace("  ", " ").strip()
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {d}")
                if not _migrate_quiet():
                    print(f"[migrate] {table} + {name}", flush=True)
            except Exception as e:
                if not _migrate_quiet():
                    print(f"[migrate] {table}.{name} 失败: {e}", flush=True)
    conn.commit()


def init_db():
    conn = get_conn()
    # 必须先迁移再执行 schema.sql：
    # schema.sql 里有 CREATE INDEX ... ON change_request(status)，
    # 旧表缺 status 列时，建索引会直接报错，ALTER 根本没机会跑。
    _migrate(conn)
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


def get_scan_map_by_zdno(zdno):
    """查某个定单已同步到本地的扎。打印前用它判断要不要重新推送。"""
    rows = get_conn().execute(
        "SELECT short_id, cc, zh FROM scan_map WHERE zdno=? ORDER BY cc, zh", (zdno,)).fetchall()
    return [dict(r) for r in rows]


def get_bundle_colors(zdno, cc, zh):
    """某一扎的颜色/尺码/数量明细。

    一扎可能有多行（不同颜色×尺码），实例如：
    扎1 火山灰 均码 264 / 扎2 奶茶棕 均码 406 / 扎3 棕咖 均码 42
    """
    rows = get_conn().execute(
        "SELECT yn, cm, plan_qty FROM bundle WHERE zdno=? AND cc=? AND zh=? "
        "ORDER BY yn, cm", (zdno, int(cc), int(zh))).fetchall()
    return [{"yn": r[0] or "", "cm": r[1] or "", "qty": int(r[2] or 0)} for r in rows]


def get_zdno_summary(zdno, cc=None):
    """定单总览：共几扎、共多少件、有哪些颜色。

    按 cc（车间层）统计 —— 同一定单可能跨层生产（实库 52/10295 个定单跨层），
    工人在第几层就看到第几层的总量。
    """
    conn = get_conn()
    if cc is None:
        row = conn.execute("SELECT MIN(cc) FROM bundle WHERE zdno=?", (zdno,)).fetchone()
        cc = row[0] if row else None
    if cc is None:
        return {"cc": None, "zh_count": 0, "qty_total": 0, "colors": []}
    n = conn.execute("SELECT COUNT(DISTINCT zh) FROM bundle WHERE zdno=? AND cc=?",
                     (zdno, int(cc))).fetchone()[0]
    qty = conn.execute("SELECT COALESCE(SUM(plan_qty),0) FROM bundle WHERE zdno=? AND cc=?",
                       (zdno, int(cc))).fetchone()[0]
    colors = [r[0] for r in conn.execute(
        "SELECT DISTINCT yn FROM bundle WHERE zdno=? AND cc=? AND yn<>'' ORDER BY yn",
        (zdno, int(cc)))]
    # 哪些扎已经报满，方便工人/班组长看进度
    return {"cc": int(cc), "zh_count": int(n or 0), "qty_total": int(qty or 0),
            "colors": colors}


def get_processes(zdno):
    """该定单可做的工序列表（gx 已在同步时限定 1~18）"""
    rows = get_conn().execute(
        "SELECT gx, gxname, dj FROM process WHERE zdno=? ORDER BY gx", (zdno,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 报工

IDEM_WINDOW_SEC = 120   # 同一笔报工的判重时间窗（秒）


def create_report(short_id, zdno, cc, zh, gx, gxname, ygno, ygname, js, dj, ip=""):
    """新增报工。

    幂等：同一标签+扎+工序+员工+数量，在 IDEM_WINDOW_SEC 秒内只算一条。

    为什么不能只靠数据库唯一索引：
    ux_report_idem 的键里带 gzdate，而 gzdate 只精确到秒 ——
    手机慢一点、工人连点两下「提交」，两笔间隔一两秒就会各插一条，
    数量直接翻倍。所以判重必须在应用层按时间窗做。
    数据库索引只作为「同一秒内」的兜底。
    """
    je = round(float(js) * float(dj), 4)
    stamp = now_str()
    with _write_lock:
        conn = get_conn()
        # 先按时间窗查重
        since = (datetime.now() - timedelta(
            seconds=IDEM_WINDOW_SEC)).strftime("%Y-%m-%d %H:%M:%S")
        dup = conn.execute(
            "SELECT id FROM report WHERE short_id=? AND zdno=? AND cc=? AND zh=? "
            "AND gx=? AND ygno=? AND js=? AND gzdate>=? ORDER BY id DESC LIMIT 1",
            (short_id, zdno, int(cc), int(zh), int(gx), ygno, int(js), since)
        ).fetchone()
        if dup:
            return {"id": dup[0], "created": False, "duplicated": True}
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


def admin_overview():
    """主管后台概览：待同步/失败/待审批/待回写，各多少条、最早卡了多久。"""
    conn = get_conn()
    out = {}
    for k, sql in (
        ("pending", "SELECT COUNT(*) FROM report WHERE status='pending'"),
        ("failed", "SELECT COUNT(*) FROM report WHERE status='failed'"),
        ("synced", "SELECT COUNT(*) FROM report WHERE status='synced'"),
        ("change_pending", "SELECT COUNT(*) FROM change_request WHERE status='pending'"),
        ("change_unsynced",
         "SELECT COUNT(*) FROM change_request WHERE status='approved' AND synced=0"),
        ("workers", "SELECT COUNT(*) FROM worker_login WHERE active=1"),
        ("sessions", "SELECT COUNT(*) FROM worker_session"),
    ):
        out[k] = int(conn.execute(sql).fetchone()[0] or 0)
    r = conn.execute(
        "SELECT MIN(gzdate) FROM report WHERE status IN ('pending','failed')").fetchone()
    out["oldest"] = r[0] if r and r[0] else ""
    r = conn.execute(
        "SELECT COALESCE(SUM(js),0) FROM report WHERE status IN ('pending','failed')"
    ).fetchone()
    out["waiting_pcs"] = int(r[0] or 0)
    return out


def list_failed(limit=100):
    rows = get_conn().execute(
        "SELECT * FROM report WHERE status='failed' ORDER BY id LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def discard_report(id_, ygno=None):
    """作废一条尚未同步的报工。

    只允许作废 pending / failed —— 这两种都**还没进 jfgz**，
    删掉不会造成账实不符。已同步的不行：那条已经写进 jfgz 了，
    要撤销必须走 jfzg_change 改量审批，不能直接删（铁律 D-005）。

    ygno 传入时表示「只能作废自己的」，主管传 None 表示不限。
    返回 (成功, 说明)
    """
    rep = get_report(id_)
    if not rep:
        return False, "报工记录不存在"
    if rep["status"] == "synced" or rep.get("jfgzid"):
        return False, "这条已经同步到数据库了，不能直接作废；请走改量审批"
    if ygno and rep["ygno"] != ygno:
        return False, "只能作废自己的报工"
    with _write_lock:
        conn = get_conn()
        conn.execute(
            "UPDATE report SET status='discarded', synced_at=?, last_err=? WHERE id=?",
            (now_str(), "已作废", int(id_)))
        conn.commit()
    return True, f"已作废 #{id_}（{rep['zdno']} 扎{rep['zh']} {rep['js']}件）"


def list_reports(status="synced", limit=200, ygno=None):
    """按状态列报工。主管看全部，工人只能看自己。"""
    sql = "SELECT * FROM report WHERE 1=1"
    args = []
    if status and status != "all":
        sql += " AND status=?"
        args.append(status)
    if ygno:
        sql += " AND ygno=?"
        args.append(ygno)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in get_conn().execute(sql, args).fetchall()]


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


def apply_snapshot(data, replace=False):
    """接收局域网推送的快照并落库。

    data 结构：
        {"ygzl":[{ygno,ygname,ygout}],
         "bundle":[{zdno,cc,zh,yn,cm,plan_qty}],
         "process":[{zdno,gx,gxname,dj}],
         "baseline":[{zdno,cc,zh,gx,reported_qty}]}

    replace=True 时先清空再灌入（用于全量重灌，保证没有陈旧残留）。
    默认只做 upsert（用于增量/按定单推送）。
    返回写入行数。
    """
    stamp = now_str()
    n = {}
    with _write_lock:
        conn = get_conn()
        try:
            if replace:
                for t in ("ygzl", "bundle", "process", "report_baseline"):
                    conn.execute(f"DELETE FROM {t}")
                conn.commit()

            rows = data.get("ygzl") or []
            conn.executemany(
                "INSERT INTO ygzl (ygno, ygname, ygout) VALUES (?,?,?) "
                "ON CONFLICT(ygno) DO UPDATE SET ygname=excluded.ygname, "
                "ygout=excluded.ygout",
                [(r["ygno"], r.get("ygname", ""), int(r.get("ygout") or 0)) for r in rows])
            n["ygzl"] = len(rows)

            rows = data.get("bundle") or []
            conn.executemany(
                "INSERT INTO bundle (zdno, cc, zh, yn, cm, plan_qty) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(zdno, cc, zh, yn, cm) DO UPDATE SET plan_qty=excluded.plan_qty",
                [(r["zdno"], int(r["cc"]), int(r["zh"]), r.get("yn", ""),
                  r.get("cm", ""), int(r.get("plan_qty") or 0)) for r in rows])
            n["bundle"] = len(rows)

            # 顺带登记扫码映射，保证标签上的短ID在服务端一定能查到
            conn.executemany(
                "INSERT INTO scan_map (short_id, zdno, cc, zh, created_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(short_id) DO UPDATE SET zdno=excluded.zdno, "
                "cc=excluded.cc, zh=excluded.zh",
                [(r["short_id"], r["zdno"], int(r["cc"]), int(r["zh"]), stamp)
                 for r in (data.get("scan_map") or [])])
            n["scan_map"] = len(data.get("scan_map") or [])

            rows = data.get("process") or []
            conn.executemany(
                "INSERT INTO process (zdno, gx, gxname, dj) VALUES (?,?,?,?) "
                "ON CONFLICT(zdno, gx) DO UPDATE SET gxname=excluded.gxname, dj=excluded.dj",
                [(r["zdno"], int(r["gx"]), r.get("gxname", ""), float(r.get("dj") or 0))
                 for r in rows])
            n["process"] = len(rows)

            rows = data.get("baseline") or []
            conn.executemany(
                "INSERT INTO report_baseline (zdno, cc, zh, gx, reported_qty, updated_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(zdno, cc, zh, gx) DO UPDATE SET "
                "reported_qty=excluded.reported_qty, updated_at=excluded.updated_at",
                [(r["zdno"], int(r["cc"]), int(r["zh"]), int(r["gx"]),
                  int(r.get("reported_qty") or 0), stamp) for r in rows])
            n["baseline"] = len(rows)

            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return n


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
        "ygzl": one("SELECT COUNT(*) FROM ygzl"),
        "worker_login": one("SELECT COUNT(*) FROM worker_login"),
        "pending_change": one("SELECT COUNT(*) FROM change_request WHERE status='pending'"),
        "jfgz_report": one("SELECT COUNT(*) FROM jfgz_report"),
    }


# ---------------------------------------------------------------- 改量审批

def get_report(id_):
    # 表单/查询串传进来的是字符串，统一转 int，避免绑到 INTEGER 列时出错
    try:
        id_ = int(id_)
    except (TypeError, ValueError):
        return None
    r = get_conn().execute("SELECT * FROM report WHERE id=?", (id_,)).fetchone()
    return dict(r) if r else None


def has_pending_change(report_id):
    """同一报工是否已有待审申请（有就不能再提）。"""
    try:
        report_id = int(report_id)
    except (TypeError, ValueError):
        return False
    r = get_conn().execute(
        "SELECT COUNT(*) FROM change_request WHERE report_id=? AND status='pending'",
        (report_id,)).fetchone()
    return int(r[0]) > 0


def create_change(report_id, ygno, ygname, new_js, reason,
                  approved_by="", note=""):
    """工人申请改量。返回 (id, 说明)。

    approved_by 非空 = 主管代改，直接落成 approved 状态。
    仍然写 change_request、走同一条 jfzg_change 留痕链路，
    不是开后门直改 jfgz（铁律 D-005）。
    """
    # 表单传来的都是字符串，SQLite 不接受 str 绑到 INTEGER 列。
    # 必须在入口统一转成 int，否则报
    # "Error binding parameter 0 - probably unsupported type"。
    try:
        report_id = int(report_id)
    except (TypeError, ValueError):
        raise ValueError("报工记录编号无效")
    rep = get_report(report_id)
    if not rep:
        raise ValueError("报工记录不存在")
    ts = now_str()
    with _write_lock:
        conn = get_conn()
        if has_pending_change(report_id):
            raise ValueError("这条报工已有一条待审批的改量申请，请等主管处理完")
        if approved_by:
            conn.execute(
                "INSERT INTO change_request (report_id, jfgzid, ygno, ygname, gx, "
                "gxname, zdno, cc, zh, old_num, new_num, reason, status, "
                "created_at, approve_gh, approve_at, approve_note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'approved',?,?,?,?)",
                (report_id, rep["jfgzid"], ygno, ygname, rep["gx"], rep["gxname"],
                 rep["zdno"], rep["cc"], rep["zh"], rep["js"], int(new_js),
                 reason, ts, approved_by, ts, (note or "")[:200]))
        else:
            conn.execute(
                "INSERT INTO change_request (report_id, jfgzid, ygno, ygname, gx, "
                "gxname, zdno, cc, zh, old_num, new_num, reason, status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)",
                (report_id, rep["jfgzid"], ygno, ygname, rep["gx"], rep["gxname"],
                 rep["zdno"], rep["cc"], rep["zh"], rep["js"], int(new_js),
                 reason, ts))
        conn.commit()
        new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    log_audit("change_apply",
              f"#{new_id} {rep['zdno']} gx{rep['gx']} {rep['js']}->{new_js} by {ygno}")
    return new_id


def list_changes(status="pending", ygno=None, limit=200):
    """列改量申请。主管看全部（可按状态筛），工人只看自己的。"""
    sql = "SELECT * FROM change_request WHERE 1=1"
    args = []
    if status:
        sql += " AND status=?"
        args.append(status)
    if ygno:
        sql += " AND ygno=?"
        args.append(ygno)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    # SQLite 的 execute 只接受 (sql, 序列)，不能用 *args 展开
    return [dict(r) for r in get_conn().execute(sql, args).fetchall()]


def decide_change(change_id, approve, approve_gh, note=""):
    """主管审批。用条件更新保证并发下只有一条能成功。

    返回 (成功?, 说明)
    """
    try:
        change_id = int(change_id)
    except (TypeError, ValueError):
        return False, "申请编号无效"
    with _write_lock:
        conn = get_conn()
        row = conn.execute(
            "SELECT id, status FROM change_request WHERE id=?", (change_id,)).fetchone()
        if not row:
            return False, "申请不存在"
        if row[1] != "pending":
            return False, f"该申请已处理（{row[1]}），不能重复审批"
        cur = conn.execute(
            "UPDATE change_request SET status=?, approve_gh=?, approve_note=?, approve_at=? "
            "WHERE id=? AND status='pending'",
            ("approved" if approve else "rejected", approve_gh, note or "", now_str(),
             change_id))
        changed = cur.rowcount
        conn.commit()
    if changed != 1:
        return False, "该申请已被其他人处理"
    log_audit("change_decide",
              f"#{change_id} {'通过' if approve else '驳回'} by {approve_gh} {note or ''}")
    return True, ("已通过" if approve else "已驳回")


def count_pending_changes():
    return int(get_conn().execute(
        "SELECT COUNT(*) FROM change_request WHERE status='pending'").fetchone()[0])


def list_unsynced_changes(limit=100):
    rows = get_conn().execute(
        "SELECT * FROM change_request WHERE status='approved' AND synced=0 "
        "ORDER BY id LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def mark_change_synced(change_id, ok, err=""):
    conn = get_conn()
    if ok:
        conn.execute("UPDATE change_request SET synced=1 WHERE id=?", (change_id,))
    else:
        conn.execute("UPDATE change_request SET synced=-1, approve_note=? WHERE id=?",
                     ((err or "")[:500], change_id))
    conn.commit()


# ============================================================
# 阶段4 · 报工统计报表
# ============================================================

def apply_report_snapshot(rows, total, max_id):
    """接收局域网推送的 jfgz 报工明细并落库（全量覆盖）。

    rows: [(jfgzid, gzdate, ygno, ygname, zdno, gx, cc, zh, js, dj, je, barcode)]
    """
    conn = get_conn()
    conn.execute("DELETE FROM jfgz_report")
    conn.executemany(
        "INSERT OR REPLACE INTO jfgz_report"
        " (jfgzid,gzdate,gzday,ygno,ygname,zdno,gx,cc,zh,js,dj,je,barcode)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(r[0], str(r[1]), str(r[1])[:10], r[2], r[3], r[4], r[5],
          r[6], r[7], r[8], float(r[9] or 0), float(r[10] or 0), int(r[11] or 1))
         for r in rows])
    for k, v in (("jfgz_rows", str(total)), ("jfgz_max_id", str(max_id)),
                 ("jfgz_synced_at", now_str())):
        conn.execute("INSERT OR REPLACE INTO sync_meta (key,value) VALUES (?,?)", (k, v))
    conn.commit()
    return {"rows": len(rows), "max_id": max_id}


def get_sync_meta(key, default=""):
    r = get_conn().execute("SELECT value FROM sync_meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def report_meta():
    r = get_conn().execute(
        "SELECT COUNT(*) n, MIN(gzday) d0, MAX(gzday) d1,"
        " COUNT(DISTINCT zdno) nzd, COUNT(DISTINCT ygno) nyg FROM jfgz_report").fetchone()
    return dict(r)


def report_group(dim, dfrom, dto, limit=20000):
    """多维汇总：dim in (zdno, ygno, gx, day)。

    ygno 维度带员工姓名。
    gx 维度按**工序名**汇总，而不是按 gx 数字：
      实测同一个 gx 数字在不同定单里对应几十种工序
      （gx=1 有 67 种：领子绷针 / 平机踩领 / 包肩 / 上胶…），
      按数字汇总会把毫不相干的工序混成一堆。
      反过来同一个工序名会跨几千个定单（如「裤线头」4272 个），
      按名字汇总才有业务意义。
      name 为空的（2004 年老定单无工序记录）单独归到「(未命名)」。
    """
    cols = {"zdno": "zdno", "ygno": "ygno", "gx": "gx", "day": "gzday"}
    col = cols.get(dim, "zdno")
    limit = int(limit)
    if dim == "gx":
        rows = get_conn().execute(
            "SELECT COALESCE(NULLIF(p.gxname,''),'(未命名)') k, "
            "       '' kname, COUNT(*) recs, SUM(r.js) pcs, SUM(r.je) amt "
            "FROM jfgz_report r "
            "LEFT JOIN process p ON p.zdno=r.zdno AND p.gx=r.gx "
            "WHERE r.gzday>=? AND r.gzday<=? "
            f"GROUP BY COALESCE(NULLIF(p.gxname,''),'(未命名)') "
            f"ORDER BY pcs DESC LIMIT {limit}",
            (dfrom, dto)).fetchall()
        return [dict(r) for r in rows]
    kname = "MAX(ygname)" if dim == "ygno" else "''"
    rows = get_conn().execute(
        f"SELECT {col} k, {kname} kname, COUNT(*) recs, SUM(js) pcs, SUM(je) amt"
        f" FROM jfgz_report WHERE gzday>=? AND gzday<=?"
        f" GROUP BY {col} ORDER BY pcs DESC LIMIT {limit}",
        (dfrom, dto)).fetchall()
    return [dict(r) for r in rows]


def report_wage(dfrom, dto, ygno=None):
    """计件工资（口径 A：直接 SUM(je)，和入库金额一致）。

    带上工序名称，财务核对时不用再对着 gx 编号猜是哪道工序。
    """
    sql = ("SELECT r.ygno, MAX(r.ygname) ygname, r.gx, "
           "       COALESCE(MAX(p.gxname),'') gxname, "
           "       COUNT(*) recs, SUM(r.js) pcs, SUM(r.je) amt "
           "FROM jfgz_report r "
           "LEFT JOIN process p ON p.zdno=r.zdno AND p.gx=r.gx "
           "WHERE r.gzday>=? AND r.gzday<=?")
    args = [dfrom, dto]
    if ygno:
        sql += " AND r.ygno=?"
        args.append(ygno)
    sql += " GROUP BY r.ygno, r.gx ORDER BY r.ygno, r.gx"
    return [dict(r) for r in get_conn().execute(sql, args).fetchall()]


def report_detail(dfrom, dto, zdno=None, ygno=None, gx=None, limit=300):
    """报工明细（手机上只取最近 limit 条）。"""
    sql = ("SELECT gzdate, ygno, ygname, zdno, gx, cc, zh, js, je"
           " FROM jfgz_report WHERE gzday>=? AND gzday<=?")
    args = [dfrom, dto]
    if zdno:
        sql += " AND zdno=?"
        args.append(zdno)
    if ygno:
        sql += " AND ygno=?"
        args.append(ygno)
    if gx is not None:
        sql += " AND gx=?"
        args.append(gx)
    sql += " ORDER BY gzdate DESC, jfgzid DESC LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in get_conn().execute(sql, args).fetchall()]


def report_orders_overview(days=7, limit=100):
    """在手定单总览：每个定单做到哪了。

    只看最近 days 天有报工的定单（=主管手上在做的），
    200 多个定单全部铺开手机上一屏放不下，必须有时间窗。

    完工扎数按「最慢工序」判：一扎要所有工序都报满才算完工。
    未同步的本地暂存也算进去，否则工人报完工这里纹丝不动。
    """
    since = _today_minus(int(days))
    rows = get_conn().execute(
        # 只保留最近有报工的定单（先缩小范围，后面几次 join 才不会太慢）
        "WITH active AS (SELECT zdno, MAX(gzday) last_day, COUNT(*) recs, SUM(js) pcs"
        "            FROM jfgz_report WHERE gzday>=? GROUP BY zdno),"
        # 该定单「应有的」每扎×每道工序
        " expect AS (SELECT b.zdno,b.cc,b.zh,b.plan_qty,p.gx"
        "            FROM bundle b JOIN process p ON p.zdno=b.zdno"
        "             JOIN active a ON a.zdno=b.zdno"
        "            GROUP BY b.zdno,b.cc,b.zh,b.plan_qty,p.gx),"
        # 每道工序的已报 = 已同步 + 未同步本地暂存
        " per_gx AS ("
        "  SELECT zdno,cc,zh,gx,SUM(js) d FROM jfgz_report"
        "   WHERE gzday>=? GROUP BY zdno,cc,zh,gx"
        "  UNION ALL"
        "  SELECT zdno,cc,zh,gx,SUM(js) FROM report"
        "   WHERE jfgzid IS NULL GROUP BY zdno,cc,zh,gx),"
        # 每扎取「最慢那道工序」，先聚合到扎级。
        # 必须 LEFT JOIN 到 expect 上：一道工序完全没报过时，
        # 若只在有报工的工序里取 MIN，等于漏掉它，这扎会被误判成完工。
        # 也必须先聚成扎级再统计，否则 expect 是「扎×工序」行数，
        # 直接 COUNT/SUM 会把扎数和应做数放大好几倍。
        " bundle_agg AS ("
        "  SELECT e.zdno,e.cc,e.zh,MAX(e.plan_qty) plan_qty,"
        "         MIN(COALESCE(g.d,0)) mindone"
        "  FROM expect e LEFT JOIN per_gx g"
        "    ON g.zdno=e.zdno AND g.cc=e.cc AND g.zh=e.zh AND g.gx=e.gx"
        "  GROUP BY e.zdno,e.cc,e.zh)"
        "SELECT a.zdno, a.last_day, a.recs, a.pcs,"
        "       COUNT(b.zh) nb, COALESCE(SUM(b.plan_qty),0) plan,"
        "       COALESCE(SUM(CASE WHEN b.mindone>=b.plan_qty THEN 1 ELSE 0 END),0) fin "
        "FROM active a "
        "LEFT JOIN bundle_agg b ON b.zdno=a.zdno "
        "GROUP BY a.zdno, a.last_day, a.recs, a.pcs "
        "ORDER BY a.last_day DESC, a.pcs DESC LIMIT ?",
        (since, since, int(limit))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["rate"] = (d["fin"] / d["nb"] * 100) if d["nb"] else 0
        out.append(d)
    return out


def _today_minus(n):
    import datetime
    return (datetime.date.today() - datetime.timedelta(days=int(n))).isoformat()


def report_progress(zdno):
    """定单进度：每道工序的应做/已报/完成率。

    应做数来自 jfzd2（按扎汇总，定单所有扎的件数之和）；
    每道工序都要做全这个量，所以各工序应做数相同。
    已报数来自 jfgz_report（含还没同步的本地暂存）。
    """
    conn = get_conn()
    plan = conn.execute(
        "SELECT COALESCE(SUM(plan_qty),0) FROM bundle WHERE zdno=?", (zdno,)).fetchone()[0]
    plan = int(plan or 0)
# 每扎的应做与「最慢工序」已报。
    # 必须以「应有的 扎×工序」为准再 LEFT JOIN 实际报工：
    # 若只在有报工的工序里取 MIN，一道工序完全没报过时会被漏掉，
    # 这扎就被误判成完工了（实测踩过：2道工序只报1道，显示100%）。
    bundles = [dict(r) for r in conn.execute(
        "SELECT e.zdno, e.cc, e.zh, COALESCE(MAX(b.yn),'') yn, e.plan_qty, "
        "       MIN(COALESCE(g.d,0)) done "
        "FROM (SELECT b2.zdno,b2.cc,b2.zh,b2.plan_qty,p.gx "
        "      FROM bundle b2 JOIN process p ON p.zdno=b2.zdno "
        "      WHERE b2.zdno=? GROUP BY b2.zdno,b2.cc,b2.zh,b2.plan_qty,p.gx) e "
        "LEFT JOIN bundle b ON b.zdno=e.zdno AND b.cc=e.cc AND b.zh=e.zh "
        "LEFT JOIN ("
        "   SELECT zdno,cc,zh,gx,SUM(js) d FROM jfgz_report "
        "    WHERE zdno=? GROUP BY zdno,cc,zh,gx "
        "   UNION ALL "
        "   SELECT zdno,cc,zh,gx,SUM(js) FROM report "
        "    WHERE zdno=? AND jfgzid IS NULL GROUP BY zdno,cc,zh,gx) g "
        "  ON g.zdno=e.zdno AND g.cc=e.cc AND g.zh=e.zh AND g.gx=e.gx "
        "GROUP BY e.zdno,e.cc,e.zh,e.plan_qty ORDER BY e.cc, e.zh",
        (zdno, zdno, zdno)).fetchall()]
    # 每道工序（用 process 拿工序名，按名分组避免 gx 歧义）
    procs = [dict(r) for r in conn.execute(
        "SELECT p.gx, p.gxname, p.dj, "
        "       COALESCE((SELECT SUM(r.js) FROM jfgz_report r "
        "                 WHERE r.zdno=p.zdno AND r.gx=p.gx),0) done, "
        "       COALESCE((SELECT SUM(r.js) FROM report r "
        "                 WHERE r.zdno=p.zdno AND r.gx=p.gx AND r.jfgzid IS NULL),0) wait "
        "FROM process p WHERE p.zdno=? ORDER BY p.gx", (zdno,)).fetchall()]
    return {"zdno": zdno, "plan": plan, "bundles": bundles, "procs": procs}


def report_search_orders(keyword, limit=30):
    """按定单号模糊搜报过工的定单，进度页好让主管挑。"""
    kw = (keyword or "").strip()
    if not kw:
        return []
    return [dict(r) for r in get_conn().execute(
        "SELECT r.zdno, COUNT(*) recs, SUM(r.js) pcs, MAX(r.gzday) last_day "
        "FROM jfgz_report r WHERE r.zdno LIKE ? "
        "GROUP BY r.zdno ORDER BY MAX(r.gzdate) DESC LIMIT ?",
        (f"%{kw}%", int(limit))).fetchall()]


def report_mine(dfrom, dto, ygno, limit=100):
    """某工人的报工明细（自助查询）。

    必须合并两个来源，否则工人报完工来看会「什么都没有」：
      1. jfgz_report —— 从 SQL Server 推回来的历史（已同步的）
      2. report      —— 本地暂存，还没写回 jfgz 的（jfgzid 为空）
    第 2 类才是刚报的那几条，缺了它工人要等一天才看得到自己报了什么。
    """
    # 工序名从本地 process 表关联拿（它就是 jfdj 推下来的，主键 zdno+gx）。
    # 实测能覆盖 99.9% 的历史记录；剩下 0.1% 是 2004 年老定单，
    # jfdj 里压根没有对应工序，显示"—"是如实反映。
    # 颜色从 bundle 表关联（同一定单一扎只有一个颜色，bundle 主键含 yn）。
    hist = [dict(r) for r in get_conn().execute(
        # id 必须是**本地 report 表的 id**，不是 jfgzid。
    # /change?report_id= 查的是本地 report 表，
    # 传 jfgzid 进去会 get_report 返回 None → 404。
    # 本地没有对应行（别处报的历史工）就返回 NULL，前端不给「申请改量」入口 ——
    # 那条报工不是本系统提交的，本来也改不了。
    "SELECT lr.id, r.gzdate, r.ygno, r.zdno, r.gx, r.cc, r.zh, r.js, r.je, "
        "       COALESCE(p.gxname,'') gxname, "
        "       COALESCE(b.yn,'') yn, 'synced' src "
        "FROM jfgz_report r "
        "LEFT JOIN report lr ON lr.jfgzid = r.jfgzid "
        "LEFT JOIN process p ON p.zdno=r.zdno AND p.gx=r.gx "
        "LEFT JOIN (SELECT zdno,cc,zh,MIN(yn) yn FROM bundle GROUP BY zdno,cc,zh) b "
        "       ON b.zdno=r.zdno AND b.cc=r.cc AND b.zh=r.zh "
        "WHERE r.ygno=? AND r.gzday>=? AND r.gzday<=? "
        "ORDER BY r.gzdate DESC, r.jfgzid DESC LIMIT ?",
        (ygno, dfrom, dto, int(limit))).fetchall()]
    local = [dict(r) for r in get_conn().execute(
        "SELECT r.id, r.gzdate, r.ygno, r.zdno, r.gx, r.cc, r.zh, r.js, r.je, "
        "       COALESCE(NULLIF(r.gxname,''),(SELECT p.gxname FROM process p "
        "                    WHERE p.zdno=r.zdno AND p.gx=r.gx)) gxname, "
        "       COALESCE((SELECT MIN(yn) FROM bundle b "
        "                 WHERE b.zdno=r.zdno AND b.cc=r.cc AND b.zh=r.zh),'') yn, "
        "       r.status src FROM report r"
        " WHERE r.ygno=? AND date(r.gzdate)>=? AND date(r.gzdate)<=?"
        "   AND r.jfgzid IS NULL AND r.status<>'discarded'"
        " ORDER BY r.gzdate DESC, r.id DESC LIMIT ?",
        (ygno, dfrom, dto, int(limit))).fetchall()]
    # 本地暂存的时间戳带时分秒，历史只有日期，统一截断到分钟便于排序
    def _key(r):
        return (str(r["gzdate"]), r["zdno"])
    merged = sorted(hist + local, key=_key, reverse=True)
    return merged[:int(limit)]


def report_mine_summary(dfrom, dto, ygno):
    """某工人在指定范围的合计（件数/金额/笔数），含未同步的本地暂存。"""
    rows = report_mine(dfrom, dto, ygno, limit=100000)
    return {
        "cnt": len(rows),
        "pcs": sum(int(r["js"] or 0) for r in rows),
        "amt": sum(float(r["je"] or 0) for r in rows),
        "pending": sum(1 for r in rows if r.get("src") == "pending"),
        "failed": sum(1 for r in rows if r.get("src") == "failed"),
    }


def list_report_workers(limit=60):
    """有报工记录的工号列表，供主管选择代改谁的报工。"""
    return [r[0] for r in get_conn().execute(
        "SELECT DISTINCT ygno FROM report WHERE ygno IS NOT NULL AND ygno<>'' "
        "ORDER BY ygno LIMIT ?", (int(limit),)).fetchall()]

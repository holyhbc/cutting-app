# -*- coding: utf-8 -*-
"""快照同步器：SQL Server(只读) -> 本地 SQLite。

⚠️ 部署位置：按 DECISIONS.md D-009，这个脚本跑在**局域网内的电脑上**，
   由它主动连 SQL Server 并把只读快照推给 VPS。
   VPS 上的 Web 服务**从不**连 SQL Server。

用法：
  python sync_snapshot.py --zdno 9951背心2024-1-19     # 同步一个定单
  python sync_snapshot.py --recent 500                  # 同步最近 500 个定单
  python sync_snapshot.py --full                        # 全量（较慢）
  python sync_snapshot.py --zdno XXX --print-qr         # 顺带打印短ID

环境变量：
  HBC_SERVER / HBC_DB(默认 ShintHrmDb-test) / HBC_USER / HBC_PWD
  SCAN_DB  SQLite 路径
"""
import argparse
import hashlib
import json
import os
import sys
import io
import sqlite3
import urllib.request
import urllib.error

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as D

import pyodbc

_A32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def gen_short_id(zdno, cc, zh):
    """与 hbc-print 中完全一致的实现，两边必须能算出同一个值。"""
    raw = f"{str(zdno).strip()}|{int(cc)}|{int(zh)}"
    digest = hashlib.sha1(raw.encode("utf-8")).digest()[:5]
    value = int.from_bytes(digest, "big")
    out = []
    for _ in range(8):
        out.append(_A32[value & 31])
        value >>= 5
    return "".join(reversed(out))


def connect():
    return pyodbc.connect(
        "DRIVER={SQL Server};SERVER=%s;DATABASE=%s;UID=%s;PWD=%s" % (
            os.environ.get("HBC_SERVER", "192.168.0.73"),
            os.environ.get("HBC_DB", "ShintHrmDb-test"),
            os.environ.get("HBC_USER", "sa"),
            os.environ.get("HBC_PWD", "1")),
        timeout=30, readonly=True)


# ---------------------------------------------------------------- 各表同步

def sync_employees(cur, conn):
    rows = cur.execute("SELECT ygno, ygname, ISNULL(ygout,0) FROM ygzl").fetchall()
    conn.executemany(
        "INSERT INTO ygzl (ygno, ygname, ygout) VALUES (?,?,?) "
        "ON CONFLICT(ygno) DO UPDATE SET ygname=excluded.ygname, ygout=excluded.ygout",
        [(r[0], r[1], int(r[2] or 0)) for r in rows])
    return len(rows)


def sync_bundles(cur, conn, zdno=None, recent=None, full=False):
    """扎快照：应做数按颜色×尺码拆行。"""
    if zdno:
        where = "WHERE zdno = ?"
        args = [zdno]
    elif recent:
        where = ("WHERE zdno IN (SELECT TOP %d zdno FROM jfzd2 ORDER BY zdno DESC)" % int(recent))
        args = []
    elif full:
        where = ""
        args = []
    else:
        return 0
    rows = cur.execute(
        "SELECT zdno, cc, zh, ISNULL(YS,''), ISNULL(CM,''), ISNULL(SUM(JS),0) "
        f"FROM jfzd2 {where} GROUP BY zdno, cc, zh, YS, CM", *args).fetchall()
    conn.executemany(
        "INSERT INTO bundle (zdno, cc, zh, yn, cm, plan_qty) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(zdno, cc, zh, yn, cm) DO UPDATE SET plan_qty=excluded.plan_qty",
        [(r[0], int(r[1]), int(r[2]), r[3], r[4], int(r[5] or 0)) for r in rows])

    # 顺带建扫码映射（三元组 -> 短ID）
    triples = {(r[0], int(r[1]), int(r[2])) for r in rows}
    conn.executemany(
        "INSERT INTO scan_map (short_id, zdno, cc, zh, created_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(short_id) DO UPDATE SET zdno=excluded.zdno, cc=excluded.cc, zh=excluded.zh",
        [(gen_short_id(z, c, h), z, c, h, D.now_str()) for z, c, h in triples])
    return len(rows)


def sync_processes(cur, conn, zdno=None, recent=None, full=False):
    """工序快照。必须限定 gx BETWEEN 1 AND 18 —— 实库有 5 个定单存在 gx>18 脏数据。"""
    if zdno:
        where = "WHERE zdno = ?"
        args = [zdno]
    elif recent:
        where = ("WHERE gx BETWEEN 1 AND 18 AND zdno IN "
                 "(SELECT TOP %d zdno FROM jfzd2 ORDER BY zdno DESC)" % int(recent))
        args = []
    elif full:
        where = "WHERE gx BETWEEN 1 AND 18"
        args = []
    else:
        return 0
    rows = cur.execute(
        f"SELECT zdno, gx, ISNULL(gxname,''), ISNULL(dj,0) FROM jfdj {where}", *args).fetchall()
    conn.executemany(
        "INSERT INTO process (zdno, gx, gxname, dj) VALUES (?,?,?,?) "
        "ON CONFLICT(zdno, gx) DO UPDATE SET gxname=excluded.gxname, dj=excluded.dj",
        [(r[0], int(r[1]), r[2], float(r[3] or 0)) for r in rows])
    return len(rows)


def sync_baseline(cur, conn, zdno=None, recent=None, full=False):
    """历史已报基线：SUM(jfgz.js) 按 (zdno,cc,zh,gx)。

    上限校验要把它加上本地待同步量，所以必须同步（决策 D-006）。
    """
    # 基础过滤：cc/zh 为 NULL 的历史行无法定位到具体扎，必须排除
    conds = ["cc IS NOT NULL", "zh IS NOT NULL"]
    if zdno:
        conds.append("zdno = ?")
    if recent:
        conds.append("zdno IN (SELECT TOP %d zdno FROM jfzd2 ORDER BY zdno DESC)" % int(recent))
    where = "WHERE " + " AND ".join(conds)
    if not (zdno or recent or full):
        return 0
    rows = cur.execute(
        "SELECT zdno, cc, zh, gx, ISNULL(SUM(js),0) FROM jfgz "
        f"{where} GROUP BY zdno, cc, zh, gx", *([zdno] if zdno else [])).fetchall()
    conn.executemany(
        "INSERT INTO report_baseline (zdno, cc, zh, gx, reported_qty, updated_at) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(zdno, cc, zh, gx) DO UPDATE SET reported_qty=excluded.reported_qty, "
        "updated_at=excluded.updated_at",
        [(r[0], int(r[1]), int(r[2]), int(r[3]), int(r[4] or 0), D.now_str()) for r in rows])
    return len(rows)


def push_to_server(target, token, zdno=None, replace=False, timeout=600):
    """把快照 POST 到 VPS 的 /api/snapshot。

    这是「局域网主动出站」的那一端（决策 D-009）：
    VPS 从不连 SQL Server，由这台局域网电脑把只读快照推过去。
    """
    if not token:
        raise SystemExit("推送必须提供 --token（或设置环境变量 SYNC_TOKEN）")
    src = connect()
    cur = src.cursor()

    print(f"读取快照（源库 {os.environ.get('HBC_DB','ShintHrmDb-test')}，只读）…")
    data = {
        "replace": replace,
        "ygzl": [{"ygno": r[0], "ygname": r[1], "ygout": int(r[2] or 0)}
                 for r in cur.execute("SELECT ygno, ygname, ISNULL(ygout,0) FROM ygzl")],
    }
    n = build_scoped(cur, "bundle", zdno)
    data["bundle"] = [r for r in n]
    print(f"  扎快照 {len(n)} 行")

    n = build_scoped(cur, "process", zdno)
    data["process"] = [r for r in n]
    print(f"  工序快照 {len(n)} 行")

    n = build_scoped(cur, "baseline", zdno)
    data["baseline"] = [r for r in n]
    print(f"  历史基线 {len(n)} 行")

    src.close()

    # 短ID必须与服务端算法一致，两边独立实现互为校验
    data["scan_map"] = [
        {"short_id": gen_short_id(r["zdno"], r["cc"], r["zh"]),
         "zdno": r["zdno"], "cc": r["cc"], "zh": r["zh"]}
        for r in data["bundle"]]

    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    print(f"\n推送到 {target}  （{len(body)/1024/1024:.2f} MB，replace={replace}）")
    req = urllib.request.Request(
        target.rstrip("/") + "/api/snapshot", data=body,
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Authorization": f"Bearer {token}"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            print("服务端返回:", r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"推送失败 HTTP {e.code}: {e.read().decode('utf-8', 'replace')}", file=sys.stderr)
        raise SystemExit(2)


def build_scoped(cur, kind, zdno=None):
    """按 --zdno 范围读取三类快照，返回 dict 列表。"""
    out = []
    if kind == "bundle":
        if zdno:
            rows = cur.execute(
                "SELECT zdno, cc, zh, ISNULL(YS,''), ISNULL(CM,''), ISNULL(SUM(JS),0) "
                "FROM jfzd2 WHERE zdno=? GROUP BY zdno, cc, zh, YS, CM", zdno).fetchall()
        else:
            rows = cur.execute(
                "SELECT zdno, cc, zh, ISNULL(YS,''), ISNULL(CM,''), ISNULL(SUM(JS),0) "
                "FROM jfzd2 GROUP BY zdno, cc, zh, YS, CM").fetchall()
        out = [{"zdno": r[0], "cc": int(r[1]), "zh": int(r[2]), "yn": r[3],
                "cm": r[4], "plan_qty": int(r[5] or 0)} for r in rows]
    elif kind == "process":
        if zdno:
            rows = cur.execute(
                "SELECT zdno, gx, ISNULL(gxname,''), ISNULL(dj,0) FROM jfdj "
                "WHERE gx BETWEEN 1 AND 18 AND zdno=?", zdno).fetchall()
        else:
            rows = cur.execute(
                "SELECT zdno, gx, ISNULL(gxname,''), ISNULL(dj,0) FROM jfdj "
                "WHERE gx BETWEEN 1 AND 18").fetchall()
        out = [{"zdno": r[0], "gx": int(r[1]), "gxname": r[2], "dj": float(r[3] or 0)}
               for r in rows]
    elif kind == "baseline":
        if zdno:
            rows = cur.execute(
                "SELECT zdno, cc, zh, gx, ISNULL(SUM(js),0) FROM jfgz "
                "WHERE cc IS NOT NULL AND zh IS NOT NULL AND zdno=? "
                "GROUP BY zdno, cc, zh, gx", zdno).fetchall()
        else:
            rows = cur.execute(
                "SELECT zdno, cc, zh, gx, ISNULL(SUM(js),0) FROM jfgz "
                "WHERE cc IS NOT NULL AND zh IS NOT NULL "
                "GROUP BY zdno, cc, zh, gx").fetchall()
        out = [{"zdno": r[0], "cc": int(r[1]), "zh": int(r[2]), "gx": int(r[3]),
                "reported_qty": int(r[4] or 0)} for r in rows]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zdno")
    ap.add_argument("--recent", type=int)
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--print-qr", action="store_true", help="打印可用的二维码短ID")
    ap.add_argument("--push", metavar="URL", help="把快照推送到 VPS，如 https://cut.holyhbc.eu.org")
    ap.add_argument("--token", default=os.environ.get("SYNC_TOKEN", ""), help="推送令牌")
    ap.add_argument("--replace", action="store_true", help="推送时先清空目标库（配合全量）")
    args = ap.parse_args()

    if args.push:
        # 推送模式：全量或按定单
        push_to_server(args.push, args.token, zdno=args.zdno, replace=args.replace)
        return

    if not (args.zdno or args.recent or args.full):
        ap.error("必须指定 --zdno / --recent / --full / --push 之一")

    D.init_db()
    src = connect()
    cur = src.cursor()
    conn = D.get_conn()
    stamp = D.now_str()
    scope = f"zdno={args.zdno}" if args.zdno else (f"recent={args.recent}" if args.recent else "全量")

    print(f"同步范围: {scope}")
    print(f"源库    : {os.environ.get('HBC_DB','ShintHrmDb-test')}  (只读)")
    print(f"目标库  : {D.DB_PATH}")
    print("-" * 52)

    n_emp = sync_employees(cur, conn)
    print(f"  员工花名册        {n_emp:>7} 人")
    n_bun = sync_bundles(cur, conn, args.zdno, args.recent, args.full)
    print(f"  扎快照(颜色×尺码) {n_bun:>7} 行")
    n_pro = sync_processes(cur, conn, args.zdno, args.recent, args.full)
    print(f"  工序快照          {n_pro:>7} 行")
    n_bas = sync_baseline(cur, conn, args.zdno, args.recent, args.full)
    print(f"  历史已报基线      {n_bas:>7} 行")

    conn.commit()
    D.log_audit("sync", f"{scope} 员工{n_emp} 扎{n_bun} 工序{n_pro} 基线{n_bas}")
    src.close()

    st = D.stats()
    print("-" * 52)
    print(f"本地库: 映射{st['scan_map']} 扎{st['bundle']} 工序{st['process']} "
          f"待同步{st['pending']} 已同步{st['synced']}")

    if args.print_qr:
        print("\n可用的二维码短ID（前 12 条）:")
        for r in conn.execute(
                "SELECT sm.short_id, sm.zdno, sm.cc, sm.zh, b.plan_qty "
                "FROM scan_map sm JOIN bundle b "
                "  ON b.zdno=sm.zdno AND b.cc=sm.cc AND b.zh=sm.zh "
                "GROUP BY sm.short_id, sm.zdno, sm.cc, sm.zh, b.plan_qty "
                "ORDER BY sm.zdno, sm.zh LIMIT 12"):
            print(f"  {r[0]}  {r[1]:<24} cc={r[2]} zh={r[3]:<4} 应做={r[4]}")


if __name__ == "__main__":
    main()

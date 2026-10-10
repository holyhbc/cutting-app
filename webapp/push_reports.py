# -*- coding: utf-8 -*-
"""把 SQL Server 的 jfgz（报工表）推送到 VPS，供手机报表查询。

为什么需要这个：VPS 不连 SQL Server（D-009，SQL Server 不暴露公网），
所以报表数据也走「局域网电脑主动出站推送」，和快照/回写同一套路。

推的是全量（约 13 万行 / 12 MB，实测 7 秒左右），每天跑一次即可。
VPS 收到后整表覆盖，不做增量——增量要处理 jfgzid 空洞，不值得。

用法：
    python push_reports.py --push https://cut.holyhbc.eu.org
    python push_reports.py --push http://127.0.0.1:10080 --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

try:
    import pyodbc
except ImportError:
    print("需要 pyodbc：pip install pyodbc", file=sys.stderr)
    raise

DEFAULT_CONN = (
    "DRIVER={SQL Server};"
    "SERVER=192.168.0.73;"
    "DATABASE=ShintHrmDb;"
    "UID=sa;PWD=1"
)


def default_conn_str():
    return os.environ.get("REPORT_CONN_STR", DEFAULT_CONN)


def fetch_reports(cur, limit=0, since_id=0):
    """读 jfgz 报工明细，附带 ygzl 姓名。"""
    sql = """
    SELECT r.jfgzid, r.gzdate, r.ygno, ISNULL(y.ygname,''), r.zdno, r.gx,
           r.cc, r.zh, r.js, ISNULL(r.dj,0), ISNULL(r.je,0), ISNULL(r.barcode,1)
    FROM jfgz r LEFT JOIN ygzl y ON y.ygno = r.ygno
    WHERE r.jfgzid > %d
    """ % int(since_id)
    if limit:
        sql += "\n   AND r.jfgzid <= %d" % int(limit)
    sql += "\nORDER BY r.jfgzid"

    cur.execute(sql)
    rows = cur.fetchall()
    meta = cur.execute(
        "SELECT COUNT(*), MAX(jfgzid) FROM jfgz WHERE jfgzid > %d" % int(since_id)
    ).fetchone()
    return rows, int(meta[0] or 0), int(meta[1] or 0)


def push_to_server(target, token, rows, total, max_id, timeout=900, dry_run=False):
    """把报工明细推给 VPS。"""
    # 行转成 list，因为 pyodbc 的行对象不能直接 JSON 序列化
    payload_rows = [
        [r[0], str(r[1]), r[2], r[3], r[4], r[5], r[6], r[7], r[8],
         float(r[9] or 0), float(r[10] or 0), int(r[11] or 1)]
        for r in rows
    ]
    body = json.dumps({"rows": payload_rows, "total": total, "max_id": max_id},
                      ensure_ascii=False).encode("utf-8")
    print(f"\n共 {total:,} 行，最大 jfgzid={max_id}")
    print(f"推送到 {target}  （{len(body)/1024/1024:.2f} MB）")

    if dry_run:
        print("dry-run：未真正发送")
        return

    req = urllib.request.Request(
        target.rstrip("/") + "/api/reports/push", data=body,
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Authorization": f"Bearer {token}"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            print("服务端返回:", r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"推送失败 HTTP {e.code}: {e.read().decode('utf-8', 'replace')}",
              file=sys.stderr)
        raise SystemExit(2)


def main():
    ap = argparse.ArgumentParser(description="推送 jfgz 报工明细到 VPS（阶段4报表）")
    ap.add_argument("--push", default="https://cut.holyhbc.eu.org", help="VPS 地址")
    ap.add_argument("--conn", default=None, help="SQL Server 连接串（默认生产库）")
    ap.add_argument("--token", default=os.environ.get("SYNC_TOKEN", ""),
                    help="推送令牌（或设环境变量 SYNC_TOKEN）")
    ap.add_argument("--since-id", type=int, default=0, help="只推 jfgzid 大于此值的（默认全量）")
    ap.add_argument("--dry-run", action="store_true", help="只读不推")
    args = ap.parse_args()

    if not args.dry_run and not args.token:
        raise SystemExit("推送必须提供 --token（或设置环境变量 SYNC_TOKEN）")

    conn_str = args.conn or default_conn_str()
    print(f"连接 {conn_str.split('DATABASE=')[1].split(';')[0]} …")
    conn = pyodbc.connect(conn_str, timeout=30)
    try:
        cur = conn.cursor()
        print("读取 jfgz …")
        rows, total, max_id = fetch_reports(cur, since_id=args.since_id)
        print(f"读到 {len(rows):,} 行")
        push_to_server(args.push, args.token, rows, total, max_id, dry_run=args.dry_run)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
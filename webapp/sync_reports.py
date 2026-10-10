# -*- coding: utf-8 -*-
"""阶段 2：把 VPS 上的扫码报工回写到 SQL Server 的 jfgz 表。

部署位置（决策 D-009）：**跑在局域网电脑上**，由它主动连 SQL Server。
VPS 只存暂存数据，从不直连数据库。

流程：
  1. 从 VPS 拉取 status='pending' 的报工
  2. 逐条在 SQL Server 上**重新校验**（不信任 VPS 的判断）：
     - 扎是否存在、应做数
     - 工序是否存在、单价
     - 上限：已报 + 本次 <= 应做数
     - 幂等：完全相同的记录是否已存在
  3. 事务写入 jfgz（barcode=1, gzdate 完整时间）
  4. 回执给 VPS：成功写 jfgzid / 失败写原因
  5. 可选：把刷新后的基线推回 VPS，避免上限校验把已同步的算两遍

用法：
  python sync_reports.py --server https://cut.holyhbc.eu.org
  python sync_reports.py --server https://... --dry-run
  python sync_reports.py --server https://... --push-baseline

安全：
  - 默认连测试库 ShintHrmDb-test，写测试库
  - 要写生产库必须显式 --target prod
"""
import argparse
import json
import os
import sys
import io
import hashlib
import urllib.request
import urllib.error
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)
import pyodbc

A32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
MAX_BATCH = 200


# ---------------------------------------------------------------- VPS 侧接口

def vps_get(server, path, token, timeout=60):
    req = urllib.request.Request(server.rstrip("/") + path,
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def vps_post(server, path, payload, token, timeout=60):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        server.rstrip("/") + path, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------- SQL Server

def connect(target):
    db = "ShintHrmDb" if target == "prod" else "ShintHrmDb-test"
    return pyodbc.connect(
        "DRIVER={SQL Server};SERVER=%s;DATABASE=%s;UID=%s;PWD=%s" % (
            os.environ.get("HBC_SERVER", "192.168.0.73"), db,
            os.environ.get("HBC_USER", "sa"), os.environ.get("HBC_PWD", "1")),
        timeout=30, autocommit=False)


def plan_qty(cur, zdno, cc, zh):
    """该扎应做数，按颜色×尺码求和（jfzd2 一扎可能多行）。"""
    r = cur.execute("SELECT ISNULL(SUM(JS),0) FROM jfzd2 WHERE zdno=? AND cc=? AND zh=?",
                    zdno, cc, zh).fetchone()
    return int(r[0] or 0)


def reported_qty(cur, zdno, cc, zh, gx):
    r = cur.execute("SELECT ISNULL(SUM(js),0) FROM jfgz WHERE zdno=? AND cc=? AND zh=? AND gx=?",
                    zdno, cc, zh, gx).fetchone()
    return int(r[0] or 0)


def find_process(cur, zdno, gx):
    r = cur.execute("SELECT TOP 1 gxname, ISNULL(dj,0) FROM jfdj "
                    "WHERE zdno=? AND gx=? AND gx BETWEEN 1 AND 18", zdno, gx).fetchone()
    return (r[0], float(r[1])) if r else (None, None)


def already_exists(cur, rec):
    """幂等：完全相同的报工是否已写入过。

    靠 (zdno,cc,zh,gx,ygno,js,gzdate) 判断 —— gzdate 含秒级时间，
    配合 VPS 侧的唯一索引，不会误判。
    """
    r = cur.execute(
        "SELECT TOP 1 jfgzid FROM jfgz WHERE zdno=? AND cc=? AND zh=? AND gx=? "
        "AND ygno=? AND js=? AND gzdate=?",
        rec["zdno"], rec["cc"], rec["zh"], rec["gx"], rec["ygno"], rec["js"],
        rec["gzdate"]).fetchone()
    return r[0] if r else None


def apply_change(cur, ch):
    """把一条已审批的改量写回 SQL Server。

    铁律：jfgz 只能经 jfzg_change 留痕后改写，且要校验审批单确实存在且通过。
    返回 (成功, 说明, jfzg_change_id)
    """
    jfgzid = ch.get("jfgzid")
    if not jfgzid:
        return False, "该报工还没同步到 jfgz，无法改量（先等它同步）", None

    # 审批单必须存在且 status=1（已通过）
    row = cur.execute(
        "SELECT TOP 1 id, ygno, status FROM jfzg_change "
        "WHERE jfgzid=? AND ygno=? AND status=1 ORDER BY id DESC",
        jfgzid, ch["ygno"]).fetchone()
    if not row:
        return False, f"jfgzid={jfgzid} 在 jfzg_change 里找不到已通过的审批单", None
    chg_id = int(row[0])

    # 读当前值（事务内读最新，避免用陈旧数据算上限）
    cur.execute("SELECT zdno, cc, zh, gx, js, ISNULL(dj,0) FROM jfgz WHERE jfgzid=?",
                jfgzid)
    cur_rec = cur.fetchone()
    if not cur_rec:
        return False, f"jfgzid={jfgzid} 在 jfgz 里不存在", None
    zdno, cc, zh, gx, old_js, dj = cur_rec

    new_js = int(ch["new_js"])
    if int(old_js) == new_js:
        return True, f"数量已是 {new_js}，无需改", chg_id

    # 幂等：同一审批单已处理过就不再改
    cur.execute("SELECT COUNT(*) FROM jfzg_change WHERE id=? AND approve_at IS NOT NULL",
                chg_id)
    if cur.fetchone()[0] and ch.get("synced"):
        return True, f"审批单 {chg_id} 已处理", chg_id

    # 上限重算：改后合计必须 <= 应做数
    plan = plan_qty(cur, zdno, cc, zh)
    cur.execute("SELECT ISNULL(SUM(js),0) FROM jfgz WHERE zdno=? AND cc=? AND zh=? AND gx=?",
                zdno, cc, zh, gx)
    reported = int(cur.fetchone()[0] or 0)
    after = reported - int(old_js) + new_js
    if after > plan:
        return False, (f"改后超量：应做{plan}，改后合计{after}（已报{reported}，"
                       f"原值{old_js}→新值{new_js}）"), None

    je = round(new_js * float(dj or 0), 3)
    cur.execute("UPDATE jfgz SET js=?, je=? WHERE jfgzid=?", (new_js, je, jfgzid))
    cur.execute("UPDATE jfzg_change SET approve_at=ISNULL(approve_at, GETDATE()) WHERE id=?",
                chg_id)
    return True, f"jfgzid={jfgzid} 数量 {old_js}→{new_js}（审批单 {chg_id}）", chg_id


def insert_report(cur, rec, gxname, dj):
    """写入 jfgz。barcode=1 表示扫码录入（D-003），gzdate 完整时间（D-004）。"""
    je = round(rec["js"] * dj, 3)
    cur.execute(
        "INSERT INTO jfgz (gzdate, ygno, zdno, gx, cc, zh, js, dj, je, barcode) "
        "VALUES (?,?,?,?,?,?,?,?,?,1)",
        rec["gzdate"], rec["ygno"], rec["zdno"], rec["gx"], rec["cc"], rec["zh"],
        rec["js"], dj, je)
    cur.execute("SELECT @@IDENTITY")
    return int(cur.fetchone()[0])


# ---------------------------------------------------------------- 主流程

def validate(cur, rec):
    """返回 (ok, info_or_error)。逐项在 SQL Server 上重新校验。"""
    zdno, cc, zh, gx = rec["zdno"], rec["cc"], rec["zh"], rec["gx"]

    if not zdno or len(str(zdno)) > 15:
        return False, f"定单号非法或超长({len(str(zdno))}>15)"

    gxname, dj = find_process(cur, zdno, gx)
    if gxname is None:
        return False, f"定单 {zdno} 没有工序 {gx}（可能标签印错）"

    plan = plan_qty(cur, zdno, cc, zh)
    if plan <= 0:
        return False, f"扎 {zdno}/{cc}/{zh} 没有应做数"

    done = reported_qty(cur, zdno, cc, zh, gx)
    if done + rec["js"] > plan:
        return False, (f"超量：应做{plan}，已报{done}，本次{rec['js']}，"
                       f"超出 {done + rec['js'] - plan}")

    return True, {"gxname": gxname, "dj": dj, "plan": plan, "done": done}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=os.environ.get("SCAN_SERVER", ""),
                    help="VPS 地址，如 https://cut.holyhbc.eu.org")
    ap.add_argument("--token", default=os.environ.get("SYNC_TOKEN", ""))
    ap.add_argument("--target", choices=["test", "prod"], default="test",
                    help="写哪个库，默认测试库；写生产必须显式 --target prod")
    ap.add_argument("--dry-run", action="store_true", help="只校验不写库")
    ap.add_argument("--push-baseline", action="store_true",
                    help="同步后把最新基线推回 VPS")
    ap.add_argument("--limit", type=int, default=MAX_BATCH)
    args = ap.parse_args()

    if not args.server or not args.token:
        ap.error("必须提供 --server 和 --token（或设置 SCAN_SERVER / SYNC_TOKEN）")

    print("=" * 60)
    print(" 阶段2  报工回写  VPS -> SQL Server")
    print("=" * 60)
    print(f" VPS     : {args.server}")
    print(f" 目标库  : {'生产 ShintHrmDb' if args.target=='prod' else '测试 ShintHrmDb-test'}")
    print(f" 模式    : {'DRY-RUN 只校验不写' if args.dry_run else '实际写入'}")
    print("=" * 60)

    if args.target == "prod" and not args.dry_run:
        print("\n⚠️  将写入**生产库** ShintHrmDb 的 jfgz 表。")
        print("   确认无误后加 --target prod 执行。\n")
        if os.environ.get("CONFIRM_PROD") != "1":
            print("如需执行，请先设置环境变量 CONFIRM_PROD=1 再运行。\n")
            return 2

    # 1. 拉取待同步
    try:
        data = vps_get(args.server, "/api/pending?limit=%d" % args.limit, args.token)
    except urllib.error.HTTPError as e:
        print(f"拉取失败 HTTP {e.code}: {e.read().decode('utf-8','replace')}")
        return 1
    except Exception as e:
        print(f"拉取失败: {e}")
        return 1

    items = data.get("reports") or []

    # ---- 改量审批：先处理已通过但未同步的 ----
    try:
        chg = vps_get(args.server, "/api/changes?status=approved", args.token)
        changes = chg.get("changes") or []
    except Exception:
        changes = []

    if changes:
        print(f"\n已审批待写回的改量 {len(changes)} 条")
        for ch in changes:
            try:
                exist = cur_change_id(cur, ch)
                ok, msg, cid = apply_change(cur, ch)
                if ok:
                    conn.commit()
                    results_chg.append({"id": ch["id"], "ok": True, "msg": msg})
                    print(f"  [改量] {msg}")
                else:
                    conn.rollback()
                    results_chg.append({"id": ch["id"], "ok": False, "msg": msg})
                    print(f"  [改量] 失败：{msg}")
            except Exception as e:
                conn.rollback()
                results_chg.append({"id": ch["id"], "ok": False, "msg": str(e)})
                print(f"  [改量] 异常：{e}")
        if not args.dry_run and results_chg:
            try:
                vps_post(args.server, "/api/changes/ack",
                         {"results": results_chg}, args.token)
                print(f"  改量回执已发送")
            except Exception as e:
                print(f"  改量回执失败（下次幂等重试）：{e}")

    print(f"\n待同步报工 {len(items)} 条")
    if not items:
        if not changes:
            print("没有待同步的报工。")
        return 0

    conn = connect(args.target)
    cur = conn.cursor()
    results = []
    results_chg = []
    ok_n = fail_n = dup_n = 0

    for rec in items:
        rid = rec["id"]
        try:
            exist = already_exists(cur, rec)
            if exist:
                print(f"  [{rid}] 已存在 jfgzid={exist}，跳过（幂等）")
                results.append({"id": rid, "status": "synced", "jfgzid": exist})
                dup_n += 1
                continue

            good, info = validate(cur, rec)
            if not good:
                print(f"  [{rid}] 拒绝：{info}")
                print(f"        {rec['zdno']} cc{rec['cc']} zh{rec['zh']} "
                      f"gx{rec['gx']} {rec['ygno']} x{rec['js']}")
                results.append({"id": rid, "status": "failed", "err": info})
                fail_n += 1
                continue

            if args.dry_run:
                print(f"  [{rid}] 校验通过：{rec['zdno']} 扎{rec['zh']} "
                      f"{info['gxname']} x{rec['js']} "
                      f"(应做{info['plan']} 已报{info['done']} 单价{info['dj']})")
                results.append({"id": rid, "status": "synced", "jfgzid": 0})
                ok_n += 1
                continue

            jfgzid = insert_report(cur, rec, info["gxname"], info["dj"])
            conn.commit()
            print(f"  [{rid}] 已写入 jfgzid={jfgzid}  {rec['zdno']} 扎{rec['zh']} "
                  f"{info['gxname']} x{rec['js']} 单价{info['dj']} "
                  f"金额{round(rec['js']*info['dj'],2)}")
            results.append({"id": rid, "status": "synced", "jfgzid": jfgzid})
            ok_n += 1
        except Exception as e:
            conn.rollback()
            print(f"  [{rid}] 异常：{e}")
            results.append({"id": rid, "status": "failed", "err": str(e)})
            fail_n += 1

    conn.close()

    # 2. 回执给 VPS
    if not args.dry_run:
        try:
            ack = vps_post(args.server, "/api/ack", {"results": results}, args.token)
            print(f"\n回执完成: {json.dumps(ack, ensure_ascii=False)}")
        except Exception as e:
            print(f"\n回执失败（数据已写入，下次会靠幂等跳过）: {e}")

    # 3. 刷新基线
    if args.push_baseline and not args.dry_run:
        print("\n刷新基线…")
        os.system(f'"{sys.executable}" "{os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync_snapshot.py")}" '
                  f'--push "{args.server}" --token {args.token}')
    elif args.push_baseline:
        print("\n（dry-run 模式不推送基线）")

    print("=" * 60)
    print(f" 成功 {ok_n}  幂等跳过 {dup_n}  拒绝/失败 {fail_n}  共 {len(items)}")
    print("=" * 60)
    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

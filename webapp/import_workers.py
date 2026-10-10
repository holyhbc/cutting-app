# -*- coding: utf-8 -*-
"""员工登录信息导入：Excel/CSV -> worker_login 表。

为什么需要这个脚本
------------------
`ygzl`（人事主数据）里可用于第二因子的字段几乎全是空的：
  con_tel  仅 1/116 有值
  ygsend1~6 全空（且是 decimal，是金额不是号码）
  id_no / cardid 全空
所以手机号必须另行收集。本脚本负责：

  读 Excel/CSV（工号 / 姓名 / 手机号）
  → 校验：工号在 ygzl 存在、在职、手机号合法、不与他人重复
  → 手机号归一化（只留数字、剥 +86）
  → 生成初始 PIN（手机号后 4 位）并算哈希+盐
  → 批量 upsert 到 worker_login
  → 输出汇总报告

用法
----
  # 1. 生成模板给班组长填
  python import_workers.py --template

  # 2. 先预览，不写库（强烈建议先跑这步）
  python import_workers.py --file 员工手机号.xlsx --dry-run

  # 3. 正式导入
  python import_workers.py --file 员工手机号.xlsx

参数
----
  --db-path PATH   指定 SQLite（默认 webapp/scan.db）
  --only-active    只导入在 ygzl 中在职的
  --pin-from-phone 初始 PIN 用手机号后 4 位（默认开）
  --pin XXXX       指定统一初始 PIN（覆盖 --pin-from-phone）
"""
import argparse
import csv
import hashlib
import io
import os
import re
import secrets
import sqlite3
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.environ.get("SCAN_DB", os.path.join(BASE_DIR, "scan.db"))

HBC_SERVER = os.environ.get("HBC_SERVER", "192.168.0.73")
HBC_DB = os.environ.get("HBC_DB", "ShintHrmDb-test")
HBC_USER = os.environ.get("HBC_USER", "sa")
HBC_PWD = os.environ.get("HBC_PWD", "1")


# ---------------------------------------------------------------- 工具

def norm_phone(raw):
    """手机号归一化：只留数字，剥掉 +86 前缀。

    '138 1234-5678' -> '13812345678'
    '+8613812345678' -> '13812345678'
    """
    d = re.sub(r"\D", "", str(raw or ""))
    if d.startswith("0086") and len(d) == 15:
        d = d[4:]
    elif d.startswith("86") and len(d) == 13:
        d = d[2:]
    return d


def valid_phone(p):
    """中国大陆手机号：1 开头，第二位 3-9，共 11 位。"""
    return bool(re.fullmatch(r"1[3-9]\d{9}", p or ""))


def make_pin_hash(pin, salt=None):
    """PIN 哈希 + 盐。绝不明文落库（D-017）。"""
    salt = salt or secrets.token_hex(8)
    h = hashlib.sha256((salt + ":" + str(pin)).encode("utf-8")).hexdigest()
    return h, salt


def now():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 读文件

TEMPLATE_ROWS = [
    ["工号", "姓名", "手机号"],
    ["A001", "张三", "13812345678"],
    ["A002", "李四", "13912345678"],
]


def write_template(path):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        for r in TEMPLATE_ROWS:
            w.writerow(r)
    print(f"模板已生成: {path}")
    print("填好后用 --file 导入。先跑 --dry-run 预览。")


def read_rows(path):
    """读 Excel 或 CSV，返回 [dict(工号,姓名,手机号), ...]"""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        try:
            from openpyxl import load_workbook
        except ImportError:
            raise SystemExit("读 .xlsx 需要 openpyxl：pip install openpyxl\n"
                             "（或另存为 .csv 用 Excel 打开另存为 CSV 格式）")
        wb = load_workbook(path, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
    else:
        for enc in ("utf-8-sig", "gbk", "utf-8"):
            try:
                with open(path, "r", encoding=enc, newline="") as f:
                    rows = list(csv.reader(f))
                break
            except UnicodeDecodeError:
                continue
        else:
            raise SystemExit(f"无法解码文件 {path}，请另存为 UTF-8 或 CSV")

    if not rows:
        return []
    head = [str(c or "").strip() for c in rows[0]]
    # 容错：允许列顺序不同，按表头名找列
    def col(*names):
        for n in names:
            if n in head:
                return head.index(n)
        return None

    i_no = col("工号", "ygno", "员工工号", "编号")
    i_nm = col("姓名", "ygname", "名字")
    i_ph = col("手机号", "电话", "phone", "手机")
    if i_no is None or i_ph is None:
        raise SystemExit(f"表头缺少「工号」或「手机号」列，实际表头: {head}")

    out = []
    for r in rows[1:]:
        if not r or not any(r):
            continue
        out.append({
            "ygno": str(r[i_no] or "").strip(),
            "ygname": str(r[i_nm] or "").strip() if i_nm is not None else "",
            "phone": norm_phone(r[i_ph]),
        })
    return out


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="Excel/CSV 文件路径")
    ap.add_argument("--db-path", default=DEFAULT_DB)
    ap.add_argument("--template", action="store_true", help="生成导入模板")
    ap.add_argument("--dry-run", action="store_true", help="只校验不写库")
    ap.add_argument("--pin-from-phone", dest="pfp", action="store_true", default=True,
                    help="初始 PIN 用手机号后4位（默认）")
    ap.add_argument("--pin", help="统一指定初始 PIN（覆盖 --pin-from-phone）")
    ap.add_argument("--no-pin-from-phone", dest="pfp", action="store_false")
    args = ap.parse_args()

    if args.template:
        write_template(os.path.join(BASE_DIR, "员工手机号模板.csv"))
        return 0
    if not args.file:
        ap.error("需要 --file 或 --template")

    rows = read_rows(args.file)
    print(f"读到 {len(rows)} 行")
    if not rows:
        print("没有有效数据行")
        return 1

    # ---- 校验 ----
    import pyodbc
    conn = pyodbc.connect(
        f"DRIVER={{SQL Server}};SERVER={HBC_SERVER};DATABASE={HBC_DB};"
        f"UID={HBC_USER};PWD={HBC_PWD}", timeout=30, readonly=True)
    cur = conn.cursor()
    staff = {}
    for r in cur.execute("SELECT ygno, ygname, ISNULL(ygout,0) FROM ygzl"):
        staff[str(r[0]).strip()] = (r[1] or "", int(r[2] or 0))
    conn.close()
    print(f"ygzl 花名册 {len(staff)} 人（源库 {HBC_DB}）\n")

    ok_rows, errs = [], []
    seen_ygno, seen_phone = {}, {}
    for r in rows:
        ygno = r["ygno"].strip()
        ph = r["phone"]
        if not ygno:
            errs.append((ygno or "?", ph, "工号为空"))
            continue
        if ygno in seen_ygno:
            errs.append((ygno, ph, f"工号重复（前面还有一行 {seen_ygno[ygno]}）"))
            continue
        seen_ygno[ygno] = r["ygname"]
        if ygno not in staff:
            errs.append((ygno, ph, "工号不在 ygzl 花名册中"))
            continue
        if staff[ygno][1] == 1:
            errs.append((ygno, ph, "该员工已离职(ygout=1)，不导入"))
            continue
        if not valid_phone(ph):
            errs.append((ygno, ph, "手机号格式不对（应为11位，1[3-9]开头）"))
            continue
        if ph in seen_phone:
            errs.append((ygno, ph, f"手机号与工号 {seen_phone[ph]} 重复"))
            continue
        seen_phone[ph] = ygno
        ok_rows.append({**r, "ygname": r["ygname"] or staff[ygno][0],
                        "resigned": staff[ygno][1]})

    print(f"校验通过 {len(ok_rows)} 条，问题 {len(errs)} 条\n")
    if errs:
        print("【问题明细】")
        for ygno, ph, why in errs:
            print(f"  ✗ 工号 {ygno:<8} 手机 {ph or '(空)':<14} {why}")
        print()

    print("【待导入清单】")
    for i, r in enumerate(ok_rows, 1):
        pin = args.pin or (r["phone"][-4:] if args.pfp else "0000")
        print(f"  {i:>3}. {r['ygno']:<8} {r['ygname']:<10} {r['phone']}  初始PIN={pin}")
    print()

    if args.dry_run:
        print("DRY-RUN：未写库。确认无误后去掉 --dry-run 再执行。")
        return 0
    if not ok_rows:
        print("没有可导入的数据")
        return 1

    # ---- 写库 ----
    db = sqlite3.connect(args.db_path, timeout=20)
    db.execute("""CREATE TABLE IF NOT EXISTS worker_login (
        ygno TEXT PRIMARY KEY, ygname TEXT NOT NULL DEFAULT '', phone TEXT,
        pin_hash TEXT NOT NULL, pin_salt TEXT NOT NULL,
        must_change INTEGER NOT NULL DEFAULT 1,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL, updated_at TEXT)""")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_worker_login_phone ON worker_login(phone)")
    db.commit()

    ts = now()
    n_ins = n_upd = 0
    for r in ok_rows:
        pin = args.pin or (r["phone"][-4:] if args.pfp else "0000")
        h, salt = make_pin_hash(pin)
        exists = db.execute("SELECT 1 FROM worker_login WHERE ygno=?", (r["ygno"],)).fetchone()
        if exists:
            db.execute("""UPDATE worker_login SET ygname=?, phone=?, pin_hash=?, pin_salt=?,
                          must_change=1, updated_at=? WHERE ygno=?""",
                       (r["ygname"], r["phone"], h, salt, ts, r["ygno"]))
            n_upd += 1
        else:
            db.execute("""INSERT INTO worker_login
                          (ygno,ygname,phone,pin_hash,pin_salt,must_change,active,created_at)
                          VALUES (?,?,?,?,?,1,1,?)""",
                       (r["ygno"], r["ygname"], r["phone"], h, salt, ts))
            n_ins += 1
    db.commit()

    total = db.execute("SELECT COUNT(*) FROM worker_login").fetchone()[0]
    act = db.execute("SELECT COUNT(*) FROM worker_login WHERE active=1").fetchone()[0]
    db.close()

    print("=" * 56)
    print(f"导入完成：新增 {n_ins}，更新 {n_upd}")
    print(f"worker_login 共 {total} 条，其中启用 {act} 人")
    print("=" * 56)
    print("\n下一步：")
    print("  1. 通知工人：登录时填「工号 或 手机号」+ PIN（初始PIN=手机号后4位）")
    print("  2. 建议让工人首次登录后改成自己好记的4位")
    print("  3. 未录入此表的工号无法登录（active=0）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
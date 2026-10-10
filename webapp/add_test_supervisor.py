#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 VPS 上建立/更新一个登录账号（C001 测试主管）。

用途：工号 C001 已加入**测试库**花名册，本脚本在 VPS 本地建对应的
worker_login 记录（手机号 + PIN），使其能登录并行使主管权限。

安全说明：
  - 只写本地 SQLite 的 worker_login 表，不碰 SQL Server
  - PIN 只存哈希+盐（复用 import_workers 的算法），不存明文
  - 重复执行是幂等的（INSERT OR REPLACE）

用法（在 VPS 上执行）：
    python3 add_test_supervisor.py --ygno C001 --name 测试主管 \
        --phone 13844445555 --pin 5555
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import os
import secrets
import sqlite3
import sys

DEFAULT_DB = os.environ.get("SCAN_DB", "/opt/scanapp/scan.db")


def make_pin_hash(pin, salt=None):
    """和 import_workers.py / rules.py 完全一致，保证三边哈希能互相验证。

    注意分隔符是冒号 ":"，写成竖线会导致登录时 PIN 校验失败。
    """
    if salt is None:
        salt = secrets.token_hex(8)
    h = hashlib.sha256((salt + ":" + str(pin)).encode("utf-8")).hexdigest()
    return h, salt


def main():
    ap = argparse.ArgumentParser(description="建立/更新 VPS 登录账号")
    ap.add_argument("--ygno", required=True, help="工号，如 C001")
    ap.add_argument("--name", required=True, help="姓名")
    ap.add_argument("--phone", required=True, help="手机号")
    ap.add_argument("--pin", required=True, help="初始 PIN（明文传入，只在本次用一次）")
    ap.add_argument("--db-path", default=DEFAULT_DB)
    args = ap.parse_args()

    if not os.path.exists(args.db_path):
        raise SystemExit(f"找不到数据库：{args.db_path}")

    conn = sqlite3.connect(args.db_path)
    conn.row_factory = sqlite3.Row
    h, salt = make_pin_hash(args.pin)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        # 先确认花名册里有这个人（登录时每次都会校验，D-018）
        emp = conn.execute(
            "SELECT ygno, ygname, ygout FROM ygzl WHERE ygno=?", (args.ygno,)
        ).fetchone()
        if not emp:
            print(f"⚠️  警告：花名册(ygzl)里没有 {args.ygno}，登录会被拒（'该工号已不在员工花名册中'）")
            print(f"    请先从 SQL Server 推送花名册快照，再执行本脚本。")
        elif int(emp["ygout"] or 0) == 1:
            print(f"⚠️  警告：{args.ygno} 在花名册里标记为已离职，登录会被拒")

        conn.execute(
            "INSERT OR REPLACE INTO worker_login"
            " (ygno,ygname,phone,pin_hash,pin_salt,must_change,active,created_at,updated_at)"
            " VALUES (?,?,?,?,?,0,1,?,?)",
            (args.ygno, args.name, args.phone, h, salt, now, now),
        )
        conn.commit()

        r = conn.execute(
            "SELECT ygno,ygname,phone,active,must_change FROM worker_login WHERE ygno=?",
            (args.ygno,),
        ).fetchone()
        print(f"\n✅ 账号已建立：")
        print(f"   工号  ：{r['ygno']}")
        print(f"   姓名  ：{r['ygname']}")
        print(f"   手机号：{r['phone']}")
        print(f"   状态  ：{'启用' if r['active'] else '停用'}")
        print(f"   PIN   ：{args.pin}（只存哈希，不存明文）")
        print(f"\n⚠️  还没生效！还需要两步：")
        print(f"   1) 把 C001 加进主管白名单：")
        print(f"      echo 'SUPERVISORS=C001,A001,A003' >> /opt/scanapp/.env")
        print(f"      systemctl restart cutapp.service")
        print(f"   2) 从测试库推送花名册快照（让 ygzl 里有 C001）：")
        print(f"      python G:\\hbc\\opencode\\webapp\\sync_snapshot.py \\")
        print(f"        --conn <测试库连接串> --push https://cut.holyhbc.eu.org")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
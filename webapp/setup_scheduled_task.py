# -*- coding: utf-8 -*-
"""把每日同步注册成 Windows 计划任务。

扫码报工要写回 jfgz，必须有台局域网电脑每天跑一次同步器。
VPS 不连数据库（D-009），所以这个任务只能装在能连 SQL Server 的机器上。

用法：
    python setup_scheduled_task.py          # 只打印命令，不执行
    python setup_scheduled_task.py --install # 真正注册（需管理员）
"""

from __future__ import annotations

import argparse
import subprocess
import sys

TASK_NAME = "扫码报工每日同步"
CMD = r"G:\hbc\opencode\webapp\run_daily_sync.cmd"
LOG_DIR = r"G:\hbc\sync-logs"


def main():
    ap = argparse.ArgumentParser(description="注册每日同步计划任务")
    ap.add_argument("--install", action="store_true", help="真正注册任务")
    ap.add_argument("--time", default="08:07", help="每天运行时间，默认 08:07")
    ap.add_argument("--remove", action="store_true", help="删除已注册的任务")
    args = ap.parse_args()

    if args.remove:
        subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
        print("已删除计划任务")
        return 0

    # 08:07 而不是 08:00：错开整点，避免和别的定时任务抢带宽
    hh, mm = args.time.split(":")
    cmd = (f'schtasks /Create /TN "{TASK_NAME}" /TR \'{CMD}\' '
           f'/SC DAILY /ST {hh}:{mm} /RL HIGHEST /F')
    print("将执行：")
    print("  " + cmd)
    print()
    print("注意：")
    print(f"  · 需要管理员权限运行本命令")
    print(f"  · 请先编辑 {CMD}，把 SYNC_TOKEN 填上")
    print(f"  · 日志会写到 {LOG_DIR}")

    if not args.install:
        print("\n加 --install 才会真正注册。")
        return 0

    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    print(r.stdout or r.stderr)
    if r.returncode == 0:
        print(f"\n✅ 已注册。每天 {args.time} 自动运行。")
        print(f"   手动立刻跑一次：{CMD}")
        print(f"   查看运行记录：schtasks /Query /TN \"{TASK_NAME}\" /V /FO LIST")
    else:
        print(f"\n❌ 注册失败（退出码 {r.returncode}），多半是没管理员权限。"
              f"请以管理员身份运行命令行再试。")
    return 0 if r.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
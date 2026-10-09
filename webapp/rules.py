# -*- coding: utf-8 -*-
"""扫码报工 · 业务规则（Web 服务与同步器共用）。

集中放在这里，避免两边算出不一样的结果。
"""
from db import get_conn, get_plan_qty, get_reported_qty, get_reported_by_employee

# 一次报工的数量上限，防止误填把手数当件数
MAX_SINGLE_REPORT = 100000


class RuleError(Exception):
    """业务规则不通过，附带给工人看的提示。"""

    def __init__(self, message, code=""):
        super().__init__(message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------- 员工

def find_employee(ygno):
    """按工号查花名册。

    员工表随快照一起推送到本地（表名 ygzl 镜像），不连 SQL Server。
    离职(ygout)或查不到都不给报工。
    """
    ygno = str(ygno or "").strip()
    if not ygno:
        raise RuleError("请填写工号", "no_ygno")
    row = get_conn().execute(
        "SELECT ygno, ygname, ygout FROM ygzl WHERE ygno=?", (ygno,)).fetchone()
    if not row:
        raise RuleError(f"工号 {ygno} 不在员工花名册中", "not_found")
    if int(row[2] or 0) == 1:
        raise RuleError(f"工号 {ygno} 已离职，不能报工", "resigned")
    return {"ygno": row[0], "ygname": row[1]}


# ---------------------------------------------------------------- 上限校验

def check_and_build_report(short_id, zdno, cc, zh, gx, ygno, js):
    """校验报工请求并组装待写入记录。任一不通过则抛 RuleError。

    上限规则（DECISIONS.md D-006，实测倍率 1.00）：
        已报(该扎该工序全部人合计) <= 该扎应做数
    """
    try:
        js = int(js)
    except (TypeError, ValueError):
        raise RuleError("数量必须是整数", "bad_qty")
    if js <= 0:
        raise RuleError("数量必须大于 0", "bad_qty")
    if js > MAX_SINGLE_REPORT:
        raise RuleError(f"单次报工不能超过 {MAX_SINGLE_REPORT} 件", "bad_qty")

    gx = int(gx)
    cc, zh = int(cc), int(zh)

    smap = get_conn().execute(
        "SELECT * FROM scan_map WHERE short_id=?", (short_id,)).fetchone()
    if not smap:
        raise RuleError("二维码无效或已过期，请重新打印标签", "bad_short_id")

    # 以映射表里的三元组为准，不信任前端传来的 zdno/cc/zh，防止改参数越权
    _, m_zdno, m_cc, m_zh = smap[0], smap[1], smap[2], smap[3]
    if (str(m_zdno), m_cc, m_zh) != (str(zdno), cc, zh):
        raise RuleError("标签与提交内容不一致，请重新扫码", "mismatch")

    proc = get_conn().execute(
        "SELECT gx, gxname, dj FROM process WHERE zdno=? AND gx=?", (m_zdno, gx)).fetchone()
    if not proc:
        raise RuleError("该定单没有这道工序，可能标签印错了", "bad_gx")
    _, gxname, dj = proc[0], proc[1], float(proc[2] or 0)

    plan = get_plan_qty(m_zdno, m_cc, m_zh)
    if plan <= 0:
        raise RuleError("这扎没有应做数，可能标签印错了", "no_plan")

    reported = get_reported_qty(m_zdno, m_cc, m_zh, gx)
    remain = plan - reported
    if js > remain:
        raise RuleError(
            f"超出剩余数量：这扎应做 {plan} 件，"
            f"「{gxname}」已报 {reported} 件，剩余 {max(remain,0)} 件",
            "over_limit")

    return {
        "short_id": short_id, "zdno": m_zdno, "cc": m_cc, "zh": m_zh,
        "gx": gx, "gxname": gxname, "dj": dj, "js": js,
    }


def employee_change_limit(zdno, cc, zh, gx, ygno):
    """个人改量上限 = 应做数 - 他人已报 + 自己已报。"""
    plan = get_plan_qty(zdno, cc, zh)
    reported = get_reported_qty(zdno, cc, zh, gx)
    mine = get_reported_by_employee(zdno, cc, zh, gx, ygno)
    return max(0, plan - (reported - mine))


def bundle_view(zdno, cc, zh):
    """一扎的完整视图：应做数 + 每道工序的已报/剩余/单价。"""
    plan = get_plan_qty(zdno, cc, zh)
    procs = []
    for p in get_conn().execute(
            "SELECT gx, gxname, dj FROM process WHERE zdno=? ORDER BY gx", (zdno,)):
        gx = p[0]
        reported = get_reported_qty(zdno, cc, zh, gx)
        procs.append({
            "gx": gx, "gxname": p[1], "dj": p[2],
            "reported": reported,
            "remain": max(0, plan - reported),
            "done": reported >= plan and plan > 0,
        })
    return {"plan_qty": plan, "processes": procs}

# -*- coding: utf-8 -*-
"""扫码报工 · 业务规则（Web 服务与同步器共用）。

集中放在这里，避免两边算出不一样的结果。
"""
import re

from db import (get_conn, get_plan_qty, get_reported_qty, get_reported_by_employee,
               get_bundle_colors, get_zdno_summary, now_str)

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


# ---------------------------------------------------------------- 登录

def norm_phone(raw):
    """手机号归一化：只留数字、剥 +86。与 import_workers.py 保持一致。"""
    d = re.sub(r"\D", "", str(raw or ""))
    if d.startswith("0086") and len(d) == 15:
        d = d[4:]
    elif d.startswith("86") and len(d) == 13:
        d = d[2:]
    return d


def _pin_ok(stored_hash, salt, pin):
    import hashlib
    h = hashlib.sha256((salt + ":" + str(pin)).encode("utf-8")).hexdigest()
    return h == stored_hash


def login(account, pin):
    """用工号**或**手机号 + PIN 登录，返回 (ok, 令牌, 员工信息, 提示)。

    D-016：两个标识都能用 —— 有些工人记不住工号但记得住手机号。
    D-017：PIN 只比对哈希，不留明文。
    D-018：令牌长期有效，但每次使用都要重新查 ygzl，离职即失效。
    """
    account = str(account or "").strip()
    pin = str(pin or "").strip()
    if not account:
        raise RuleError("请填写工号或手机号", "no_account")
    if not pin:
        raise RuleError("请填写 PIN", "no_pin")

    conn = get_conn()
    phone = norm_phone(account)
    row = None
    if phone:
        row = conn.execute(
            "SELECT * FROM worker_login WHERE phone=?", (phone,)).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM worker_login WHERE ygno=?", (account,)).fetchone()
    if row is None:
        raise RuleError("工号或手机号未登记，请找班组长录入", "not_registered")
    if int(row["active"] or 0) == 0:
        raise RuleError(f"{row['ygname']} 暂时不能登录（已停用）", "inactive")
    if not _pin_ok(row["pin_hash"], row["pin_salt"], pin):
        raise RuleError("PIN 不对", "bad_pin")

    # 关键：每次登录都重新查 ygzl，确认在职（D-018）
    emp = get_conn().execute(
        "SELECT ygno, ygname, ygout FROM ygzl WHERE ygno=?", (row["ygno"],)).fetchone()
    if not emp:
        raise RuleError("该工号已不在员工花名册中", "not_in_roster")
    if int(emp[2] or 0) == 1:
        raise RuleError(f"{emp[1]} 已离职，不能登录", "resigned")

    import secrets
    token = secrets.token_urlsafe(32)
    import datetime
    ts = datetime.datetime.now()
    far = (ts + datetime.timedelta(days=3650)).strftime("%Y-%m-%d %H:%M:%S")
    get_conn().execute(
        "INSERT INTO worker_session (token, ygno, created_at, expires_at, last_seen, device) "
        "VALUES (?,?,?,?,?,?)",
        (token, emp[0], ts.strftime("%Y-%m-%d %H:%M:%S"), far,
         ts.strftime("%Y-%m-%d %H:%M:%S"), ""))
    get_conn().commit()
    return True, token, {"ygno": emp[0], "ygname": emp[1]}, bool(row["must_change"])


def whoami(token):
    """凭令牌取员工。每次都查 ygzl —— 离职即失效（D-018）。"""
    if not token:
        return None
    conn = get_conn()
    row = conn.execute("SELECT * FROM worker_session WHERE token=?", (token,)).fetchone()
    if not row:
        return None
    emp = conn.execute("SELECT ygno, ygname, ygout FROM ygzl WHERE ygno=?",
                       (row["ygno"],)).fetchone()
    if not emp or int(emp[2] or 0) == 1:
        logout(token)      # 离职/不在册 → 立即作废令牌
        return None
    return {"ygno": emp[0], "ygname": emp[1]}


def logout(token):
    if not token:
        return
    conn = get_conn()
    conn.execute("DELETE FROM worker_session WHERE token=?", (token,))
    conn.commit()


# ---------------------------------------------------------------- R2 工序记忆

def remember_pref(ygno, zdno, cc, zh, gx):
    """记住该工人在这扎/这单做的工序（覆盖式，只留最新）。"""
    if not ygno:
        return
    ts = now_str()
    conn = get_conn()
    for scope in ("bundle", "zdno"):
        conn.execute(
            "INSERT INTO worker_pref (ygno,scope,zdno,cc,zh,gx,updated_at) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(ygno,scope,zdno,cc,zh) DO UPDATE SET gx=excluded.gx, "
            "updated_at=excluded.updated_at",
            (ygno, scope, zdno, int(cc), int(zh), int(gx), ts))
    conn.commit()


def recall_pref(ygno, zdno, cc, zh):
    """两级回退：先查(工号+扎)，再查(工号+定单)。返回 gx 或 None。"""
    if not ygno:
        return None
    conn = get_conn()
    r = conn.execute(
        "SELECT gx FROM worker_pref WHERE ygno=? AND scope='bundle' AND zdno=? AND cc=? AND zh=?",
        (ygno, zdno, int(cc), int(zh))).fetchone()
    if r:
        return int(r[0])
    r = conn.execute(
        "SELECT gx FROM worker_pref WHERE ygno=? AND scope='zdno' AND zdno=?",
        (ygno, zdno)).fetchone()
    return int(r[0]) if r else None


def bundle_view(zdno, cc, zh):
    """一扎的完整视图：颜色/本扎应做 + 定单总览 + 每道工序的已报/剩余/单价。"""
    plan = get_plan_qty(zdno, cc, zh)
    colors = get_bundle_colors(zdno, cc, zh)
    summary = get_zdno_summary(zdno, cc)
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
    return {
        "plan_qty": plan,
        "colors": colors,
        "summary": summary,
        "processes": procs,
    }

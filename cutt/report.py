# -*- coding: utf-8 -*-
"""报工统计报表工具（桌面端，独立运行）。

直连 SQL Server 查 `jfgz`（报工表），做三类报表：
    A. 生产进度看板  —— 按定单/扎/工序看完成率
    B. 计件工资报表  —— 按实报 SUM(je) 口径
    C. 明细汇总导出  —— 任意维度组合，导出 Excel

设计要点（与 zdno_edit.py 保持一致）：
  - pyodbc 连接非线程安全 → 每线程各自持有连接，关闭时统一释放
  - Excel 导出优先 openpyxl，失败退回 CSV，保证一定导得出来
  - 工资口径固定为 A：直接 SUM(jfgz.je)，和入库时算好的金额一致

用法：
    python report.py                # 独立运行（默认连 192.168.0.73 生产库）
"""

from __future__ import annotations

import csv
import json  # noqa: F401
import os
import queue
import sys
import threading
import traceback
from datetime import datetime, timedelta
from decimal import Decimal

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

try:
    import pyodbc
except ImportError:
    print("需要 pyodbc，请先：pip install pyodbc", flush=True)
    raise


PALETTE = {
    "bg": "#eef1f6",
    "card": "#ffffff",
    "head": "#2c3e50",
    "head_fg": "#ffffff",
    "accent": "#1a73a8",
    "accent_soft": "#d6e6f2",
    "ok": "#1e8e3e",
    "warn": "#b26a00",
    "danger": "#c0392b",
    "row_alt": "#f6f8fb",
    "muted": "#7a8699",
}

DEFAULT_CONN = (
    "DRIVER={SQL Server};"
    "SERVER=192.168.0.73;"
    "DATABASE=ShintHrmDb;"
    "UID=sa;PWD=1"
)


def default_conn_str():
    """默认连生产库；可用环境变量 REPORT_CONN_STR 覆盖（测试库用）。"""
    return os.environ.get("REPORT_CONN_STR", DEFAULT_CONN)


# ============================================================
# Excel 导出（openpyxl 优先，失败退 CSV）
# ============================================================

def export_rows_to_excel(path, sheet_name, headers, rows, sum_headers=None, sum_rows=None):
    """把报表写成 xlsx。返回 (True, 提示) 或 (False, 错误信息)。

    优先 openpyxl；没装或出错时退回 CSV（Excel 能直接打开），保证一定导出得出来。
    """
    def _fmt(v):
        if v is None:
            return ""
        if isinstance(v, Decimal):
            return float(v)
        if hasattr(v, "strftime"):
            return v.strftime("%Y-%m-%d %H:%M:%S")
        return v

    def _write_csv(sheets, fallback_note):
        base = os.path.splitext(path)[0] + ".csv"
        with open(base, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            for i, (hs, rs) in enumerate(sheets):
                if i:
                    w.writerow([])
                w.writerow(hs)
                for r in rs:
                    w.writerow([_fmt(c) for c in r])
        return True, f"未使用 openpyxl，已改为导出 CSV：{os.path.basename(base)}（{fallback_note}）"

    try:
        import openpyxl
        from openpyxl.styles import Font, Alignment, PatternFill
        from openpyxl.utils import get_column_letter
    except Exception:
        sheets = [(headers, rows)]
        if sum_headers:
            sheets.append((sum_headers, sum_rows or []))
        return _write_csv(sheets, "未安装 openpyxl")

    try:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = sheet_name
        bold = Font(bold=True)
        head_fill = PatternFill("solid", fgColor="D6E6F2")
        center = Alignment(horizontal="center", vertical="center")

        def fill(ws_, hs, rs, start=1):
            ws_.append(hs)
            for c in range(1, len(hs) + 1):
                cell = ws_.cell(row=start, column=c)
                cell.font = bold
                cell.fill = head_fill
                cell.alignment = center
            for r in rs:
                ws_.append([_fmt(c) for c in r])
            # 列宽按内容估算
            for ci in range(1, len(hs) + 1):
                letter = get_column_letter(ci)
                width = len(str(hs[ci - 1])) + 4
                for r in rs[:500]:
                    v = _fmt(r[ci - 1]) if ci - 1 < len(r) else ""
                    width = max(width, min(len(str(v)) + 2, 40))
                ws_.column_dimensions[letter].width = width
            ws_.freeze_panes = ws_.cell(row=start + 1, column=1)

        fill(ws, headers, rows)
        if sum_headers:
            ws2 = wb.create_sheet("汇总")
            fill(ws2, sum_headers, sum_rows or [])
        wb.save(path)
        return True, f"已导出：{os.path.basename(path)}（{len(rows)} 行）"
    except Exception as exc:
        sheets = [(headers, rows)]
        if sum_headers:
            sheets.append((sum_headers, sum_rows or []))
        return _write_csv(sheets, f"openpyxl 失败：{exc}")


# ============================================================
# 数据库层
# ============================================================

class ReportDB:
    """只读报表查询。连接按线程缓存，窗口关闭统一释放。"""

    def __init__(self, conn_str: str, timeout: int = 15):
        self.conn_str = conn_str
        self.timeout = timeout
        self._local = threading.local()
        self._all_conns = []
        self._lock = threading.Lock()

    @property
    def conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = pyodbc.connect(self.conn_str, timeout=self.timeout, autocommit=True)
            self._local.conn = conn
            with self._lock:
                self._all_conns.append(conn)
        return conn

    def close(self):
        with self._lock:
            conns, self._all_conns = self._all_conns, []
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass

    def _rows(self, sql, args=None):
        cur = self.conn.cursor()
        cur.execute(sql, *(args or []))
        cols = [c[0] for c in cur.description]
        return cols, [list(r) for r in cur.fetchall()]

    # ---- 元信息 ----

    def meta(self):
        """返回 (总行数, 最大 jfgzid, 最早, 最晚, 定单数, 人数)。"""
        sql = (
            "SELECT COUNT(*), MAX(jfgzid), MIN(gzdate), MAX(gzdate), "
            "       COUNT(DISTINCT zdno), COUNT(DISTINCT ygno) FROM jfgz"
        )
        _, rs = self._rows(sql)
        return rs[0]

    def check_over_limit(self):
        """统计有多少 (zdno,cc,zh,gx) 超量（报 > 计划）。"""
        # 注意：plan 是 SQL Server 保留字，列名改用 plan_num
        sql = """
        SELECT COUNT(*) FROM (
            SELECT r.zdno, r.cc, r.zh, r.gx, SUM(r.js) done, p.plan_num
            FROM jfgz r
            JOIN (SELECT zdno, cc, zh, SUM(JS) plan_num FROM jfzd2
                  GROUP BY zdno, cc, zh) p
              ON p.zdno=r.zdno AND p.cc=r.cc AND p.zh=r.zh
            GROUP BY r.zdno, r.cc, r.zh, r.gx, p.plan_num
        ) t WHERE t.done > t.plan_num
        """
        _, rs = self._rows(sql)
        return rs[0][0]

    # ---- 员工表（取姓名） ----

    def workers(self):
        _, rs = self._rows(
            "SELECT ygno, ygname FROM ygzl WHERE ygout=0 ORDER BY ygno")
        return rs

    def _name_join(self):
        """JOIN ygzl 取姓名（ygzl.ygname 是真实列，不是 xm）。"""
        return "LEFT JOIN ygzl y ON y.ygno = r.ygno"

    # ---- 报表 A：生产进度 ----

    def progress_bundles(self, zdno):
        """某定单每扎的应做/已报/完成率。"""
        sql = """
        SELECT p.zdno, p.cc, p.zh, p.plan_num,
               ISNULL(d.done, 0) AS done
        FROM (SELECT zdno, cc, zh, SUM(JS) plan_num FROM jfzd2
              WHERE zdno=? GROUP BY zdno, cc, zh) p
        LEFT JOIN (SELECT zdno, cc, zh, SUM(js) done FROM jfgz
                   WHERE zdno=? GROUP BY zdno, cc, zh) d
          ON d.zdno=p.zdno AND d.cc=p.cc AND d.zh=p.zh
        ORDER BY p.cc, p.zh
        """
        return self._rows(sql, (zdno, zdno))[1]

    def progress_gx(self, zdno):
        """某定单每道工序的已报数 / 涉及扎数。"""
        # cc 可能为 NULL（barcode=0 的人工行），用 ISNULL 兜住，避免串接报错
        sql = """
        SELECT gx,
               COUNT(DISTINCT zdno+'_'+CAST(ISNULL(cc,0) AS varchar)+'_'+CAST(zh AS varchar)) AS bundles,
               SUM(js) AS done
        FROM jfgz WHERE zdno=? GROUP BY gx ORDER BY gx
        """
        return self._rows(sql, (zdno,))[1]

    def search_zdno(self, keyword, limit=100):
        """按定单号模糊搜（有报工的）。"""
        kw = (keyword or "").strip()
        if not kw:
            return []
        limit = int(limit)
        sql = f"""
        SELECT TOP {limit} zdno, COUNT(*) recs, SUM(js) done, MAX(gzdate) last_dt
        FROM jfgz WHERE zdno LIKE ? GROUP BY zdno
        ORDER BY MAX(gzdate) DESC
        """
        return self._rows(sql, (f"%{kw}%",))[1]

    # ---- 报表 B：计件工资（口径 A = SUM(je)） ----

    def wage_by_worker(self, dfrom, dto, ygno=None):
        """按工号×工序汇总（带姓名）。ygno=None 则汇总全部（仅主管）。"""
        where = "WHERE r.gzdate >= ? AND r.gzdate < ?"
        args = [dfrom, dto]
        if ygno:
            where += " AND r.ygno = ?"
            args.append(ygno)
        sql = f"""
        SELECT r.ygno, ISNULL(y.ygname, '') AS ygname, r.gx,
               COUNT(*) AS recs, SUM(r.js) AS pcs, SUM(r.je) AS amt
        FROM jfgz r {self._name_join()}
        {where} GROUP BY r.ygno, y.ygname, r.gx ORDER BY r.ygno, r.gx
        """
        return self._rows(sql, args)[1]

    def wage_daily(self, dfrom, dto, ygno=None):
        """按日期×工号汇总（趋势图用）。"""
        where = "WHERE gzdate >= ? AND gzdate < ?"
        args = [dfrom, dto]
        if ygno:
            where += " AND ygno = ?"
            args.append(ygno)
        sql = f"""
        SELECT gzdate, ygno, SUM(js) AS pcs, SUM(je) AS amt
        FROM jfgz {where} GROUP BY gzdate, ygno ORDER BY gzdate, ygno
        """
        return self._rows(sql, args)[1]

    # ---- 报表 C：明细汇总 ----

    def group_by(self, dim, dfrom, dto, limit=20000):
        """按指定维度汇总：zdno / ygno / gx / day。ygno 维度带姓名。

        两个 SQL Server 2000 的限制必须绕开：
          1. TOP 不能用占位符 ?，也不能带括号（那是 2005+），只能内联裸整数
          2. GROUP BY 不接受表达式（如 CONVERT(...)），必须先在派生表里算好
        """
        limit = int(limit)
        if dim == "ygno":
            key = "r.ygno"
            kname = "ISNULL(y.ygname,'')"
            src = "FROM jfgz r LEFT JOIN ygzl y ON y.ygno=r.ygno"
        elif dim == "day":
            key = "CONVERT(varchar(10), r.gzdate, 120)"
            kname = "''"
            src = "FROM jfgz r"
        else:
            key = f"r.{dim}"
            kname = "''"
            src = "FROM jfgz r"
        sql = f"""
        SELECT TOP {limit} t.k, t.kname,
               COUNT(*) AS recs, SUM(t.js) AS pcs, SUM(t.je) AS amt
        FROM (SELECT {key} AS k, {kname} AS kname, r.js AS js, r.je AS je
              {src}
              WHERE r.gzdate >= ? AND r.gzdate < ?) t
        GROUP BY t.k, t.kname ORDER BY pcs DESC
        """
        return self._rows(sql, (dfrom, dto))[1]

    def detail(self, dfrom, dto, zdno=None, ygno=None, gx=None, limit=50000):
        """报工明细（可多维过滤）。"""
        where = "WHERE gzdate >= ? AND gzdate < ?"
        args = [dfrom, dto]
        if zdno:
            where += " AND zdno = ?"
            args.append(zdno)
        if ygno:
            where += " AND ygno = ?"
            args.append(ygno)
        if gx is not None:
            where += " AND gx = ?"
            args.append(gx)
        limit = int(limit)
        sql = f"""
        SELECT TOP {limit} gzdate, ygno, zdno, gx, cc, zh, js, dj, je, barcode
        FROM jfgz {where} ORDER BY gzdate
        """
        return self._rows(sql, args)[1]


# ============================================================
# GUI
# ============================================================

class ReportWindow:
    TITLE = "  报工统计报表 "

    def __init__(self, root, conn_str):
        self.root = root
        self.db = ReportDB(conn_str)
        self.q = queue.Queue()
        self._closed = False

        root.title(self.TITLE)
        root.geometry("1180x760")
        root.configure(bg=PALETTE["bg"])
        self._init_style()

        # 顶部：信息条 + 数据截至
        top = tk.Frame(root, bg=PALETTE["head"])
        top.pack(fill="x")
        tk.Label(top, text="📊 报工统计报表", bg=PALETTE["head"], fg="white",
                 font=("Microsoft YaHei", 13, "bold")).pack(side="left", padx=14, pady=10)
        self.lbl_meta = tk.Label(top, text="正在读取…", bg=PALETTE["head"], fg="#cfe8f8",
                                 font=("Microsoft YaHei", 9))
        self.lbl_meta.pack(side="right", padx=14)

        # 主体：左侧标签页 + 右侧筛选
        main = tk.Frame(root, bg=PALETTE["bg"])
        main.pack(fill="both", expand=True, padx=10, pady=10)

        self.nb = ttk.Notebook(main)
        self.nb.pack(side="top", fill="both", expand=True)

        self._build_progress_tab()
        self._build_wage_tab()
        self._build_group_tab()
        self._build_detail_tab()

        self._build_statusbar(main)

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll()
        self._load_meta_async()

    def _init_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        base = ("Microsoft YaHei", 9)
        bold = ("Microsoft YaHei", 9, "bold")
        style.configure(".", font=base, background=PALETTE["bg"], foreground="#1f2933")
        style.configure("Card.TFrame", background=PALETTE["card"])
        style.configure("Card.TLabel", background=PALETTE["card"])
        style.configure("Muted.TLabel", foreground=PALETTE["muted"], background=PALETTE["bg"])
        style.configure("CardTitle.TLabel", font=bold, background=PALETTE["card"],
                        foreground=PALETTE["head"])
        style.configure("Danger.TButton", font=bold, foreground="white", background=PALETTE["danger"])
        style.configure("Accent.TButton", font=bold, foreground="white", background=PALETTE["accent"])
        style.configure("Ok.TButton", font=bold, foreground="white", background=PALETTE["ok"])
        style.configure("TNotebook.Tab", padding=(16, 7), font=bold)
        style.configure("Treeview", rowheight=24, fieldbackground="white", borderwidth=0)
        style.configure("Treeview.Heading", font=bold, relief="flat", padding=(4, 5))

    def _card(self, parent, title=None):
        f = ttk.Frame(parent, style="Card.TFrame", padding=10)
        if title:
            ttk.Label(f, text=title, style="CardTitle.TLabel").pack(anchor="w", pady=(0, 6))
        return f

    def _mktree(self, parent, headers, widths):
        wrap = ttk.Frame(parent, style="Card.TFrame")
        tree = ttk.Treeview(wrap, columns=[f"c{i}" for i in range(len(headers))],
                            show="headings", selectmode="browse")
        for i, (h, w) in enumerate(zip(headers, widths)):
            tree.heading(f"c{i}", text=h)
            tree.column(f"c{i}", width=w, anchor="e" if i else "w")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        return wrap, tree

    def _statusbar(self, parent):
        bar = tk.Frame(parent, bg="#dfe6ee", height=26)
        bar.pack(fill="x", side="bottom")
        self.lbl_status = tk.Label(bar, text="就绪", bg="#dfe6ee", fg="#33475b",
                                   font=("Microsoft YaHei", 8), anchor="w")
        self.lbl_status.pack(side="left", padx=8)

    def _status(self, msg):
        self.lbl_status.config(text=msg)
        self.root.update_idletasks()

    # ---------- 异步调度 ----------

    def _run(self, fn, done=None):
        """后台线程执行 fn（DB 查询），结果经队列回主线程。"""
        def worker():
            try:
                res = fn()
                self.q.put(("ok", res, done))
            except Exception as exc:
                self.q.put(("err", exc, done))
        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, payload, done = self.q.get_nowait()
                if kind == "ok":
                    if done:
                        done(payload)
                else:
                    self._status(f"出错：{payload}")
                    messagebox.showerror("查询失败", str(payload))
        except queue.Empty:
            pass
        if not self._closed:
            self.root.after(80, self._poll)

    def _load_meta_async(self):
        def work():
            meta = self.db.meta()
            over = self.db.check_over_limit()
            return meta, over
        self._run(work, self._show_meta)

    def _show_meta(self, payload):
        meta, over = payload
        total, maxid, dmin, dmax, nzd, nyg = meta
        self.lbl_meta.config(
            text=f"报工 {total:,} 行 · {nzd:,} 定单 · {nyg} 人 · {str(dmin)[:10]} ~ {str(dmax)[:10]}"
        )
        self.n_total = total
        self.n_over = over

    # ---------- 日期工具 ----------

    def _dstr(self, dt):
        return dt.strftime("%Y-%m-%d")

    def _ask_dates(self):
        """弹出日期范围对话框，返回 (from, to) 或 None。"""
        win = tk.Toplevel(self.root)
        win.title("选择日期范围")
        win.transient(self.root)
        win.resizable(False, False)
        today = datetime.now()
        frame = ttk.Frame(win, padding=14)
        frame.pack(fill="both", expand=True)

        dfrom = tk.StringVar(value=self._dstr(today.replace(day=1) - timedelta(days=1)) if False else self._dstr((today.replace(day=1) - timedelta(days=1))))
        dto = tk.StringVar(value=self._dstr(today))
        result = {}

        quick = [("今天", today, today), ("近7天", today - timedelta(days=6), today),
                 ("近30天", today - timedelta(days=29), today), ("本月", today.replace(day=1), today)]

        ttk.Label(frame, text="开始日期：").grid(row=0, column=0, sticky="e", pady=4)
        ttk.Entry(frame, textvariable=dfrom, width=14).grid(row=0, column=1, pady=4)
        ttk.Label(frame, text="结束日期：").grid(row=1, column=0, sticky="e", pady=4)
        ttk.Entry(frame, textvariable=dto, width=14).grid(row=1, column=1, pady=4)

        qrow = ttk.Frame(frame)
        qrow.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 4))
        for i, (label, a, b) in enumerate(quick):
            ttk.Button(qrow, text=label, width=8,
                       command=lambda a=a, b=b: (dfrom.set(self._dstr(a)), dto.set(self._dstr(b)))
            ).grid(row=0, column=i, padx=2)

        def ok():
            try:
                a = datetime.strptime(dfrom.get(), "%Y-%m-%d")
                b = datetime.strptime(dto.get(), "%Y-%m-%d") + timedelta(days=1)
                if b <= a:
                    raise ValueError
            except Exception:
                messagebox.showerror("日期无效", "请用 YYYY-MM-DD 格式，且结束日期不早于开始日期", parent=win)
                return
            result["range"] = (a, b)
            win.destroy()

        ttk.Button(frame, text="确定", command=ok, style="Accent.TButton").grid(
            row=3, column=0, columnspan=2, sticky="e", pady=(10, 0))
        win.grab_set()
        self.root.wait_window(win)
        return result.get("range")

    # ---------- 报表 A：生产进度 ----------

    def _build_progress_tab(self):
        page = ttk.Frame(self.nb, padding=12)
        self.nb.add(page, text="  ① 生产进度  ")

        top = ttk.Frame(page)
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="定单号：", font=("Microsoft YaHei", 10)).pack(side="left")
        self.ent_zdno = tk.Entry(top, width=22, font=("Consolas", 10))
        self.ent_zdno.pack(side="left", padx=4)
        ttk.Button(top, text="🔍 查进度", style="Accent.TButton",
                   command=self._do_progress).pack(side="left", padx=4)
        ttk.Button(top, text="📊 导出", command=self._export_progress).pack(side="left", padx=4)

        body = ttk.Frame(page)
        body.pack(fill="both", expand=True)
        self._progress_bundles_tree = None
        wrap1, self.tr_bundles = self._mktree(body, ["扎 (cc,zh)", "应做", "已报", "完成率"], [160, 100, 100, 100])
        wrap1.pack(fill="both", expand=True, side="top")
        ttk.Label(body, text="工序进度", style="CardTitle.TLabel").pack(anchor="w", pady=(10, 4))
        wrap2, self.tr_gx = self._mktree(body, ["工序 gx", "涉及扎数", "已报件数"], [100, 120, 120])
        wrap2.pack(fill="both", expand=True, side="top")

    def _do_progress(self):
        zdno = self.ent_zdno.get().strip()
        if not zdno:
            messagebox.showinfo("提示", "请先输入定单号")
            return
        self._progress_zdno = zdno
        self._status(f"查询 {zdno} …")
        self._run(lambda: (self.db.progress_bundles(zdno), self.db.progress_gx(zdno)),
                  self._show_progress)

    def _show_progress(self, payload):
        bundles, gxs = payload
        for t in (self.tr_bundles, self.tr_gx):
            t.delete(*t.get_children())
        total_plan = total_done = 0
        for cc, zh, plan, done in bundles:
            total_plan += plan or 0
            total_done += done or 0
            rate = (done / plan * 100) if plan else 0
            self.tr_bundles.insert("", "end", values=(f"{cc}-{zh}", plan, done, f"{rate:.0f}%"))
        # 合计行
        if total_plan:
            self.tr_bundles.insert("", "end", values=("合计", total_plan, total_done,
                                                     f"{total_done/total_plan*100:.0f}%"))
        for gx, bundles_n, done in gxs:
            self.tr_gx.insert("", "end", values=(gx, bundles_n, done))
        self._status(f"{self._progress_zdno}：应做 {total_plan}，已报 {total_done}")

    def _export_progress(self):
        if not hasattr(self, "_progress_zdno"):
            messagebox.showinfo("提示", "先查一个定单再导出")
            return
        zdno = self._progress_zdno
        rows = [list(r) for r in self.tr_bundles.get_children()]  # 显示值
        # 拉真实数据导出
        self._run(lambda: self.db.progress_bundles(zdno),
                  lambda d: self._save_progress(zdno, d))

    def _save_progress(self, zdno, data):
        headers = ["扎CC", "扎号", "应做", "已报", "完成率%"]
        rows = []
        for cc, zh, plan, done in data:
            rate = (done / plan * 100) if plan else 0
            rows.append([cc, zh, plan, done, round(rate, 1)])
        self._save_excel(f"进度_{zdno}", "进度", headers, rows)

    def _save_excel(self, default_name, sheet, headers, rows, sum_headers=None, sum_rows=None):
        path = filedialog.asksaveasfilename(
            parent=self.root,
            initialfile=f"{default_name}.xlsx",
            defaultextension=".xlsx",
            filetypes=[("Excel", "*.xlsx"), ("CSV", "*.csv"), ("全部", "*.*")],
        )
        if not path:
            return
        ok, msg = export_rows_to_excel(path, sheet, headers, rows, sum_headers, sum_rows)
        (messagebox.showinfo if ok else messagebox.showerror)("导出结果", msg)
        self._status(msg)

    # ---------- 报表 B：计件工资 ----------

    def _build_wage_tab(self):
        page = ttk.Frame(self.nb, padding=12)
        self.nb.add(page, text="  ② 计件工资  ")

        bar = ttk.Frame(page)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="工号：", font=("Microsoft YaHei", 10)).pack(side="left")
        self.ent_wage_ygno = tk.Entry(bar, width=12, font=("Consolas", 10))
        self.ent_wage_ygno.pack(side="left", padx=4)
        ttk.Label(bar, text="（留空=全部，主管用）", foreground=PALETTE["muted"]).pack(side="left")
        ttk.Button(bar, text="📊 查工资", style="Ok.TButton",
                   command=self._do_wage).pack(side="left", padx=6)
        ttk.Button(bar, text="📈 导出", command=self._export_wage).pack(side="left")

        wrap, self.tr_wage = self._mktree(page, ["工号", "姓名", "工序", "报工次数", "件数", "金额"],
                                  [90, 90, 70, 100, 100, 120])
        wrap.pack(fill="both", expand=True)

    def _do_wage(self):
        rng = self._ask_dates()
        if not rng:
            return
        dfrom, dto = rng
        ygno = self.ent_wage_ygno.get().strip() or None
        self._wage_rng, self._wage_ygno = rng, ygno
        self._status("统计工资…")
        self._run(lambda: self.db.wage_by_worker(dfrom, dto, ygno), self._show_wage)

    def _show_wage(self, data):
        self.tr_wage.delete(*self.tr_wage.get_children())
        tot_recs = tot_pcs = tot_amt = 0
        for ygno, ygname, gx, recs, pcs, amt in data:
            self.tr_wage.insert("", "end", values=(ygno, ygname, gx, recs, pcs, f"{amt:,.2f}"))
            tot_recs += recs; tot_pcs += pcs; tot_amt += amt
        if data:
            self.tr_wage.insert("", "end", values=("合计", "", "", tot_recs, tot_pcs, f"{tot_amt:,.2f}"))

    def _export_wage(self):
        if not hasattr(self, "_wage_rng"):
            messagebox.showinfo("提示", "先查一次工资再导出")
            return
        dfrom, dto = self._wage_rng
        ygno = self._wage_ygno
        self._run(lambda: self.db.wage_by_worker(dfrom, dto, ygno),
                  lambda d: self._save_wage(dfrom, dto, ygno, d))

    def _save_wage(self, dfrom, dto, ygno, data):
        headers = ["工号", "姓名", "工序gx", "报工次数", "件数", "金额"]
        rows = [[a, b, c, d, e, float(f)] for a, b, c, d, e, f in data]
        label = ygno or "全部"
        self._save_excel(f"工资_{label}_{self._dstr(dfrom)}_{self._dstr(dto - timedelta(days=1))}",
                         "工资", headers, rows)

    # ---------- 报表 C：多维汇总 ----------

    def _build_group_tab(self):
        page = ttk.Frame(self.nb, padding=12)
        self.nb.add(page, text="  ③ 多维汇总  ")

        bar = ttk.Frame(page)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="汇总维度：", font=("Microsoft YaHei", 10)).pack(side="left")
        self.cmb_dim = ttk.Combobox(bar, values=["定单", "员工", "工序", "日期"],
                                    state="readonly", width=10)
        self.cmb_dim.current(0)
        self.cmb_dim.pack(side="left", padx=4)
        ttk.Button(bar, text="📊 汇总", style="Accent.TButton",
                   command=self._do_group).pack(side="left", padx=6)
        ttk.Button(bar, text="📈 导出", command=self._export_group).pack(side="left")

        wrap, self.tr_group = self._mktree(page, ["分组", "姓名", "报工次数", "件数", "金额"],
                                   [200, 90, 110, 110, 130])
        wrap.pack(fill="both", expand=True)

    def _do_group(self):
        rng = self._ask_dates()
        if not rng:
            return
        dfrom, dto = rng
        dim = {"定单": "zdno", "员工": "ygno", "工序": "gx", "日期": "day"}[self.cmb_dim.get()]
        self._group_ctx = (dfrom, dto, dim)
        self._status("汇总中…")
        self._run(lambda: self.db.group_by(dim, dfrom, dto), self._show_group)

    def _show_group(self, data):
        self.tr_group.delete(*self.tr_group.get_children())
        tr_r = tr_p = tr_a = 0
        for k, kname, recs, pcs, amt in data:
            self.tr_group.insert("", "end", values=(k, kname, recs, pcs, f"{amt:,.2f}"))
            tr_r += recs; tr_p += pcs; tr_a += amt
        if data:
            self.tr_group.insert("", "end", values=("合计", "", tr_r, tr_p, f"{tr_a:,.2f}"))

    def _export_group(self):
        if not hasattr(self, "_group_ctx"):
            messagebox.showinfo("提示", "先汇总再导出")
            return
        dfrom, dto, dim = self._group_ctx
        self._run(lambda: self.db.group_by(dim, dfrom, dto),
                  lambda d: self._save_group(dfrom, dto, dim, d))

    def _save_group(self, dfrom, dto, dim, data):
        dimname = {"zdno": "定单", "ygno": "员工", "gx": "工序", "day": "日期"}[dim]
        headers = [dimname, "姓名", "报工次数", "件数", "金额"]
        rows = [[k, kname, recs, pcs, float(amt)] for k, kname, recs, pcs, amt in data]
        self._save_excel(f"汇总_{dimname}_{self._dstr(dfrom)}_{self._dstr(dto - timedelta(days=1))}",
                         dimname, headers, rows)

    # ---------- 报表 D：报工明细 ----------

    def _build_detail_tab(self):
        page = ttk.Frame(self.nb, padding=12)
        self.nb.add(page, text="  ④ 报工明细  ")

        bar = ttk.Frame(page)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="定单：", font=("Microsoft YaHei", 9)).pack(side="left")
        self.ent_d_zdno = tk.Entry(bar, width=16, font=("Consolas", 9))
        self.ent_d_zdno.pack(side="left", padx=3)
        ttk.Label(bar, text="工号：", font=("Microsoft YaHei", 9)).pack(side="left", padx=(8, 0))
        self.ent_d_ygno = tk.Entry(bar, width=9, font=("Consolas", 9))
        self.ent_d_ygno.pack(side="left", padx=3)
        ttk.Label(bar, text="工序：", font=("Microsoft YaHei", 9)).pack(side="left", padx=(8, 0))
        self.ent_d_gx = tk.Entry(bar, width=5, font=("Consolas", 9))
        self.ent_d_gx.pack(side="left", padx=3)
        ttk.Button(bar, text="🔍 查询", style="Accent.TButton",
                   command=self._do_detail).pack(side="left", padx=6)
        ttk.Button(bar, text="📈 导出", command=self._export_detail).pack(side="left")

        wrap, self.tr_detail = self._mktree(page, ["时间", "工号", "定单", "工序", "CC", "扎", "件数", "单价", "金额"],
                                            [150, 70, 150, 60, 50, 50, 80, 80, 90])
        wrap.pack(fill="both", expand=True)

    def _do_detail(self):
        rng = self._ask_dates()
        if not rng:
            return
        dfrom, dto = rng
        zdno = self.ent_d_zdno.get().strip() or None
        ygno = self.ent_d_ygno.get().strip() or None
        gx = self.ent_d_gx.get().strip()
        gx = int(gx) if gx.isdigit() else None
        self._detail_ctx = (dfrom, dto, zdno, ygno, gx)
        self._status("查询明细…")
        self._run(lambda: self.db.detail(dfrom, dto, zdno, ygno, gx), self._show_detail)

    def _show_detail(self, data):
        self.tr_detail.delete(*self.tr_detail.get_children())
        for gzdate, ygno, zdno, gx, cc, zh, js, dj, je, barcode in data:
            self.tr_detail.insert("", "end", values=(
                str(gzdate)[:16], ygno, zdno, gx, cc, zh, js,
                float(dj), float(je), barcode))
        self._status(f"明细 {len(data)} 行")

    def _export_detail(self):
        if not hasattr(self, "_detail_ctx"):
            messagebox.showinfo("提示", "先查询再导出")
            return
        dfrom, dto, zdno, ygno, gx = self._detail_ctx
        self._run(lambda: self.db.detail(dfrom, dto, zdno, ygno, gx),
                  lambda d: self._save_detail(dfrom, dto, d))

    def _save_detail(self, dfrom, dto, data):
        headers = ["时间", "工号", "定单", "工序", "CC", "扎", "件数", "单价", "金额", "条码"]
        rows = [[str(a)[:16], b, c, d, e, f, g, float(h), float(i), j]
                for a, b, c, d, e, f, g, h, i, j in data]
        self._save_excel(f"明细_{self._dstr(dfrom)}_{self._dstr(dto - timedelta(days=1))}",
                         "明细", headers, rows)

    # ---------- 关闭 ----------

    def _on_close(self):
        self._closed = True
        try:
            self.db.close()
        except Exception:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def _run_standalone():
    root = tk.Tk()
    win = ReportWindow(root, default_conn_str())

    def shutdown():
        try:
            win._on_close()
        finally:
            try:
                root.destroy()
            except tk.TclError:
                pass

    root.protocol("WM_DELETE_WINDOW", shutdown)
    root.mainloop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(_run_standalone())
    except Exception:
        traceback.print_exc()
        print("[report] 启动失败，请把上面的报错信息发出来。", flush=True)
        sys.exit(1)
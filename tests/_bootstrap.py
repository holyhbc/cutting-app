# -*- coding: utf-8 -*-
"""测试启动引导 —— 所有 test_*.py 必须先 import 这个。

解决三件事：
  1. 把 webapp / cutt 加进 sys.path
  2. 桩掉 CI 上没有、测试又用不到的模块
     pyodbc   —— 需要真实 ODBC 驱动，Linux 上装它要编译，慢且脆。
                测试里一律用 FakeCursor，根本不连库。
     tkinter  —— 桌面 GUI，CI 是无头环境。
     openpyxl —— 可选依赖，缺失时导出应退回 CSV。
  3. 固定环境变量，防止误连真实数据库

为什么用桩而不是 pip install：
  pyodbc 在 Linux 上装要 unixODBC 头文件 + 编译器，
  GitHub Actions 每次跑都编译一遍，慢且容易因为系统包版本翻车。
  而测试**从不真正连数据库**，装它纯属浪费。
"""

from __future__ import annotations

import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEBAPP = os.path.join(ROOT, "webapp")
CUTT = os.path.join(ROOT, "cutt")
for p in (WEBAPP, CUTT, os.path.dirname(os.path.abspath(__file__))):
    if p not in sys.path:
        sys.path.insert(0, p)

# ---- 安全兜底：绝不能连到真实数据库/真实服务 ----
os.environ.setdefault("HBC_QUIET_MIGRATE", "1")
os.environ.pop("SCAN_DB", None)
os.environ.pop("REPORT_CONN_STR", None)
os.environ.pop("SYNC_TOKEN", None)
os.environ.pop("SUPERVISORS", None)


def _stub(name, attrs):
    """装一个假的模块（如果真的没有）。"""
    try:
        __import__(name)
        return False
    except ImportError:
        pass
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    m.__hbc_stub__ = True          # 方便测试断言「这是桩」
    sys.modules[name] = m
    return True


def _no_connect(*a, **k):
    raise RuntimeError(
        "测试里不允许真正连接数据库。"
        "需要验证 SQL 的用例请用 tests/base.py 的 FakeCursor。")


# pyodbc：接口留够用，真调用就报错
stubbed = []
if _stub("pyodbc", {
        "connect": _no_connect,
        "ProgrammingError": type("ProgrammingError", (Exception,), {}),
        "DataError": type("DataError", (Exception,), {}),
        "IntegrityError": type("IntegrityError", (Exception,), {}),
        "IntegrityWarning": type("IntegrityWarning", (Exception,), {}),
        "Date": type("Date", (Exception,), {}),
        "Time": type("Time", (Exception,), {}),
        "Timestamp": type("Timestamp", (Exception,), {}),
        "Binary": type("Binary", (Exception,), {}),
        "DateFromTicks": type("DateFromTicks", (Exception,), {}),
        "TimeFromTicks": type("TimeFromTicks", (Exception,), {}),
        "TimestampFromTicks": type("TimestampFromTicks", (Exception,), {}),
        "STRING": "STRING", "BINARY": "BINARY", "NUMBER": "NUMBER",
        "DATETIME": "DATETIME", "ROWID": "ROWID"}):
    stubbed.append("pyodbc")

# tkinter：桌面 GUI，CI 无头。
# 注意不能往 sys.modules 里塞 None —— `import tkinter.font` 会因此报
# "import of tkinter.font halted: None in sys.modules"。
# 每个子模块都得是一个真正的 ModuleType。
if _stub("tkinter", {}):
    stubbed.append("tkinter")
    _stub("tkinter.font", {"Font": type("Font", (), {})})
    _stub("tkinter.ttk", {
        "Frame": type("Frame", (), {}), "Label": type("Label", (), {}),
        "Button": type("Button", (), {}), "Entry": type("Entry", (), {}),
        "Notebook": type("Notebook", (), {}), "Treeview": type("Treeview", (), {}),
        "Combobox": type("Combobox", (), {}), "Scrollbar": type("Scrollbar", (), {}),
        "Style": type("Style", (), {}), "LabelFrame": type("LabelFrame", (), {}),
    })
    _stub("tkinter.messagebox", {
        "showinfo": lambda *a, **k: None, "showerror": lambda *a, **k: None,
        "showwarning": lambda *a, **k: None, "askyesno": lambda *a, **k: False,
    })
    _stub("tkinter.filedialog", {
        "asksaveasfilename": lambda *a, **k: "",
        "askopenfilename": lambda *a, **k: "",
    })
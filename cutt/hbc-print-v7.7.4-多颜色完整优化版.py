# ccimport-plus-V2-with-stockin.py
import tkinter as tk

try:
    import pymupdf
except ImportError:
    pymupdf = None
from tkinter import ttk, messagebox, simpledialog
import pyodbc
import re
from datetime import datetime
import os
import threading
import json
import subprocess
import sys
import ctypes
import hashlib
import socket
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ZDNO 数据维护模块（单价 / 工序名称 / 颜色 / 数量 修改）
# 缺失时不影响主程序运行，只是菜单里的入口会提示不可用。
try:
    import zdno_edit as _zdno_edit
except Exception as _zdno_edit_err:
    _zdno_edit = None
    _zdno_edit_import_error = _zdno_edit_err

# ================================================================
# ReportLab / OpenSSL MD5 兼容补丁
# 某些 Windows Python / OpenSSL 环境中的 md5 实现不接受
# usedforsecurity=False，而新版 ReportLab 会传入该参数。
# 在导入 ReportLab 之前统一兼容处理。
# ================================================================
_original_md5 = hashlib.md5

def _compat_md5(data=b"", *args, **kwargs):
    kwargs.pop("usedforsecurity", None)
    return _original_md5(data, *args, **kwargs)

hashlib.md5 = _compat_md5

# 条码/PDF依赖（预览和打印功能）
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.graphics.barcode import code128
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.graphics import renderPDF

# ==================== 数据库连接配置 ====================
# 连接信息支持用环境变量覆盖（HBC_SERVER / HBC_DB / HBC_USER / HBC_PWD 等），
# 便于换服务器时不用改代码；没有设置环境变量时保持原来的默认值，开箱即用不变。
DB_CONFIG = {
    'server': os.environ.get('HBC_SERVER', '192.168.0.73'),
    'database': os.environ.get('HBC_DB', 'ShintHrmDb-test'),
    'username': os.environ.get('HBC_USER', 'sa'),
    'password': os.environ.get('HBC_PWD', '1')
}

conn_str = (
    f"DRIVER={{SQL Server}};"
    f"SERVER={DB_CONFIG['server']};"
    f"DATABASE={DB_CONFIG['database']};"
    f"UID={DB_CONFIG['username']};"
    f"PWD={DB_CONFIG['password']}"
)

# ==================== 库存数据库（第二个数据库）配置 OS-201703151649====================
INVENTORY_DB_CONFIG = {
    'server': os.environ.get('HBC_SERVER', '192.168.0.73'),
    'database': os.environ.get('HBC_INV_DB', 'adsfz2021'),
    'username': os.environ.get('HBC_USER', 'sa'),
    'password': os.environ.get('HBC_PWD', '1')
}
INVENTORY_CONN_STR = (
    f"DRIVER={{SQL Server}};"
    f"SERVER={INVENTORY_DB_CONFIG['server']};"
    f"DATABASE={INVENTORY_DB_CONFIG['database']};"
    f"UID={INVENTORY_DB_CONFIG['username']};"
    f"PWD={INVENTORY_DB_CONFIG['password']}"
    #f"Trusted_Connection=yes;"
)
# ==================== 日志文件配置 ====================
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "插入历史记录.txt")

# ==================== 数据安全基础设施 ====================
# 本程序会往 jfdj / jfzd / jfzd2 / jfzd3 写数据。为了避免误操作破坏历史工单，
# 这里做三件事：
#   1. 所有"删除/覆盖"类操作在落库前把旧数据完整导出成快照文件；
#   2. 界面提供「↩ 撒销上次写库」按钮，一键还原；
#   3. 关键字段长度按表结构预先校验，不让 SQL Server 报错中断事务。
SNAPSHOT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "写库撒销快照.json")
AUDIT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "写库操作日志.txt")

# 只允许对这些表做拼接过渡，其他一律拒绝（防误写 / 防注入）
SAFE_TABLES = ('jfdj', 'jfzd', 'jfzd2', 'jfzd3')

# 各表列宽限制，取自 INFORMATION_SCHEMA，写入前先拦一道
COLUMN_LIMITS = {
    'zdno': 15,     # nvarchar(15)
    'gxname': 30,   # nvarchar(30)
    'ys': 10,       # jfzd2.YS nvarchar(10)
    'cm': 8,        # jfzd2.CM nvarchar(8)
    'gh': 10,       # jfzd2.GH nvarchar(10)
    'type_name': 20,   # jfzd2_detail.TYPE_NAME nvarchar(20)
}

APP_INSTANCE = None

# ---- 报错信息整理 ----
def friendly_error(exc):
    """把 pyodbc 的原始报错整理成人能看懂的一句话。

    pyodbc 的 str(exc) 是一长串带 [Microsoft][ODBC ...] 前缀和 (17) (64) 错误码的文本，
    直接弹给操作员看没有意义，这里只保留数据库给出的说明部分。
    """
    text = ""
    if isinstance(exc, pyodbc.Error):
        # 取最长的字符串参数：有时 args = (详细说明, 'HY000')，取 args[1] 会丢掉真正原因
        candidates = [a for a in exc.args if isinstance(a, str) and a.strip()]
        if candidates:
            text = max(candidates, key=len)
        text = re.sub(r"^\(\s*'[0-9A-Za-z]{5}'\s*,\s*", "", text.strip())
        if text.endswith(")"):
            text = text[:-1]
    if not text:
        text = str(exc)

    while True:  # 逐段去掉 [Microsoft][ODBC SQL Server Driver][...] 前缀
        start = text.find("[")
        if start == -1:
            break
        depth = 0
        end = -1
        for i in range(start, len(text)):
            if text[i] == "[":
                depth += 1
            elif text[i] == "]":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            text = text[start + 1:]
            break
        text = text[:start] + text[end + 1:]

    text = re.sub(r"\(\s*\d+\s*\)", " ", text)
    text = re.sub(r"\s*;\s*", "；", text)
    text = re.sub(r"\s+", " ", text).strip(" ；'\"")
    return text or exc.__class__.__name__


# ---- 连接复用 ----
_conn_local = threading.local()

def get_conn(conn_string, timeout=10, autocommit=True):
    """按线程复用数据库连接。

    pyodbc 连接非线程安全，不能跨线程共享，所以按 thread-local 缓存。
    桌面程序的操作都是人在点击，连接握手成本不低，复用后每次操作省掉一次 TCP+认证。
    """
    key = (conn_string, bool(autocommit))
    cache = getattr(_conn_local, "cache", None)
    if cache is None:
        cache = {}
        _conn_local.cache = cache
    conn = cache.get(key)
    if conn is not None:
        # 连接可能被别处关闭过，先确认还活着再复用，避免把死连接一直传下去
        try:
            conn.cursor().execute("SELECT 1")
            _conn_local.last_used = datetime.now()
            return conn
        except Exception:
            cache.pop(key, None)
            try:
                conn.close()
            except Exception:
                pass
    conn = pyodbc.connect(conn_string, timeout=timeout, autocommit=autocommit)
    cache[key] = conn
    _conn_local.last_used = datetime.now()
    return conn


def close_connections():
    """退出时关闭本线程持有的连接。"""
    cache = getattr(_conn_local, "cache", None) or {}
    for conn in list(cache.values()):
        try:
            conn.close()
        except Exception:
            pass
    cache.clear()


# ---- 写库快照 / 撒销 ----
def save_write_snapshot(payload):
    """保存一次写库操作的快照，供「撒销上次写库」还原。"""
    try:
        with open(SNAPSHOT_FILE, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        return True
    except Exception as e:
        print(f"保存撒销快照失败：{e}")
        return False


def load_write_snapshot():
    try:
        if os.path.exists(SNAPSHOT_FILE):
            with open(SNAPSHOT_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return None


def clear_write_snapshot():
    try:
        if os.path.exists(SNAPSHOT_FILE):
            os.remove(SNAPSHOT_FILE)
    except Exception:
        pass


def write_audit(action, detail):
    """写库审计日志，只追加不覆盖。"""
    stamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        with open(AUDIT_FILE, 'a', encoding='utf-8') as f:
            f.write(f"{stamp} | {action} | {detail}\n")
    except Exception:
        pass


def check_len(field, value, label=None):
    """按表结构校验 nvarchar 长度，超长直接给出中文提示而不是让数据库报错。"""
    limit = COLUMN_LIMITS.get(field)
    text = '' if value is None else str(value).strip()
    if limit and len(text) > limit:
        raise ValueError(
            f"{label or field} 内容过长：{len(text)} 个字符，"
            f"数据库字段只允许 {limit} 个字符。\n请缩短后再试。"
        )
    return text


# ==================== 明细统计表（jfzd2_detail） ====================
# 用途：按时间段统计各类款式裁剪了多少件，并支持导出 Excel。
# 制单号规则：原 ZDNO 去掉中间汉字后的前 6 位 + 类型 + 结尾数字编号
#   268810套装7-12  + 类型=背心  ->  268810背心7-12
#   1055-1背心185-202 + 类型=背心 ->  1055-1背心185-202（原样）
#   261018花边66-76  + 类型=套装  ->  261018套装66-76
# 结尾数字段兼容 7-12 / 171-176A / 424-475. 等写法；
# 遇到纯汉字前缀（如「补裤子385-390」）时保持原 ZDNO 不变，避免丢信息。
TYPE_PRESETS = ('背心', '打底裤', '套装', '单衣', '单裤')
DEFAULT_TYPE = '套装'
DETAIL_TABLE = 'jfzd2_detail'
MAX_SL = 50            # jfzd3 槽位上限 SL1~SL50，明细表每个顺序号一行
_CJK_PATTERN = re.compile(r'[\u4e00-\u9fff]')
_TRAILING_NUM = re.compile(r'(\d[\d\-]*[A-Za-z]?\.?)$')


def build_detail_zdno(zdno, type_name=DEFAULT_TYPE):
    """按规则生成明细表用的制单号。返回 (制单号, 前段, 数字编号段)。"""
    zdno = (zdno or '').strip()
    type_name = str(type_name or '').strip() or DEFAULT_TYPE

    match = _TRAILING_NUM.search(zdno)
    number_part = match.group(1) if match else ''
    head = zdno[:len(zdno) - len(number_part)] if number_part else zdno
    prefix = _CJK_PATTERN.sub('', head)[:6]

    # 纯汉字前缀（去掉汉字就没了）时不做替换，直接沿用原 ZDNO，避免信息丢失
    if not prefix:
        return zdno, '', number_part
    return f"{prefix}{type_name}{number_part}", prefix, number_part


def ensure_detail_table():
    """确保 jfzd2_detail 表存在（幂等）。别的电脑上没建表时自动补上。"""
    ddl = """
    IF OBJECT_ID('jfzd2_detail') IS NULL
    BEGIN
        CREATE TABLE jfzd2_detail (
            ID            int IDENTITY(1,1) NOT NULL,
            ZDNO          nvarchar(40)   NOT NULL,
            TEMPLATE_ZDNO nvarchar(40)   NULL,
            ORDER_NO      int            NOT NULL,
            ORDER_BEGIN   int            NULL,
            ORDER_END     int            NULL,
            YS            nvarchar(10)   NULL,
            CM            nvarchar(8)    NULL,
            JS            int            NULL,
            TYPE_NAME     nvarchar(20)   NOT NULL,
            MODE          nvarchar(10)   NULL,
            CREATED_AT    smalldatetime  NOT NULL DEFAULT GETDATE(),
            CREATED_BY    nvarchar(20)   NULL,
            REMARK        nvarchar(200)  NULL,
            CONSTRAINT PK_jfzd2_detail PRIMARY KEY (ID)
        )
    END
    """
    idx = [
        ("IX_jfzd2_detail_created", "CREATE INDEX IX_jfzd2_detail_created ON jfzd2_detail (CREATED_AT)"),
        ("IX_jfzd2_detail_type", "CREATE INDEX IX_jfzd2_detail_type ON jfzd2_detail (TYPE_NAME)"),
        ("IX_jfzd2_detail_zdno", "CREATE INDEX IX_jfzd2_detail_zdno ON jfzd2_detail (ZDNO)"),
        ("IX_jfzd2_detail_type_time", "CREATE INDEX IX_jfzd2_detail_type_time ON jfzd2_detail (TYPE_NAME, CREATED_AT)"),
    ]
    conn = get_conn(conn_str)
    cursor = conn.cursor()
    cursor.execute(ddl)
    for name, ddl_one in idx:
        cursor.execute("SELECT COUNT(1) FROM sysindexes WHERE name = ?", name)
        if not cursor.fetchone()[0]:
            cursor.execute(ddl_one)


def write_log(record):
    """同时写入TXT日志和当前程序界面日志。"""
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(record + '\n')
    except Exception as e:
        print(f"写入日志失败：{e}")
    app = APP_INSTANCE
    if app is not None:
        try:
            app.append_log(record)
        except Exception as e:
            print(f"界面日志写入失败：{e}")


# ==================== 库存管理函数 ====================

def get_inventory_users(keyword=''):
    """获取库存系统中的货号列表"""
    keyword = (keyword or '').strip()
    conn = get_conn(INVENTORY_CONN_STR, timeout=5)
    try:
        cursor = conn.cursor()
        if keyword:
            cursor.execute("""
                SELECT DISTINCT usercode 
                FROM ptype 
                WHERE deleted = 0 AND usercode LIKE ?
                ORDER BY usercode
            """, f"%{keyword}%")
        else:
            cursor.execute("""
                SELECT DISTINCT usercode 
                FROM ptype 
                WHERE deleted = 0
                ORDER BY usercode
            """)
        return [str(row[0]) for row in cursor.fetchall() if row[0] is not None]
    finally:
        pass


def call_stock_in_procedure(target_usercode, color_data, price=52.00, operator='系统', remark=''):
    """
    调用入库存储过程 hbc_StockInFromCutting
    """
    if not target_usercode:
        return False, "目标货号不能为空", None
    
    if not color_data:
        return False, "入库数据不能为空", None
    
    # ===== 构建 color_data_str =====
    parts = []
    for item in color_data:
        # 兼容多种字段名
        color = item.get('color') or item.get('颜色', '')
        size = item.get('size') or item.get('尺码', '')
        qty = item.get('qty') or item.get('数量', 0)
        
        # 确保是字符串并去除空格
        color = str(color).strip()
        size = str(size).strip()
        
        try:
            qty = int(qty)
        except (ValueError, TypeError):
            qty = 0
        
        print(f"【调试】处理后: color='{color}', size='{size}', qty={qty}")
        
        if color and size and qty > 0:
            parts.append(f"{color}|{size}|{qty}")
    
    color_data_str = ','.join(parts)
    
    print(f"【调试】最终 color_data_str: '{color_data_str}' targetusercode:'{target_usercode}'")
    
    if not color_data_str:
        return False, "没有有效的入库数据（颜色、尺码、数量不能为空）", None
    
    # ===== 调用存储过程 =====
    conn = pyodbc.connect(INVENTORY_CONN_STR, timeout=30)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            EXEC hbc_StockInFromCutting 
                @targetUsercode = ?, 
                @colorData = ?, 
                @price = ?, 
                @operator = ?, 
                @remark = ?
        """, target_usercode, color_data_str, price, operator, remark)
        
        # 获取结果
        results = []
        while True:
            try:
                rows = cursor.fetchall()
                if rows:
                    for row in rows:
                        results.append(row)
                if not cursor.nextset():
                    break
            except pyodbc.ProgrammingError:
                break
        
        # 解析结果
        status = '失败'
        message = ''
        in_number = ''
        total_qty = 0
        total_amount = 0
        details = []
        
        for row in results:
            if hasattr(row, '状态'):
                status = row.状态
                if hasattr(row, '入库单号'):
                    in_number = row.入库单号
                if hasattr(row, '入库总数量'):
                    total_qty = row.入库总数量
                if hasattr(row, '入库总金额'):
                    total_amount = row.入库总金额
                if hasattr(row, '备注'):
                    message = row.备注
                if hasattr(row, '错误信息'):
                    message = row.错误信息
            elif hasattr(row, '颜色') and hasattr(row, '尺码') and hasattr(row, '数量'):
                details.append({
                    'color': row.颜色,
                    'size': row.尺码,
                    'qty': row.数量,
                    'color_id': getattr(row, '颜色ID', 0),
                    'size_id': getattr(row, '尺码ID', 0)
                })
        
        print(f"【调试】最终结果: status={status}, message={message}")
        
        # ===== ✅ 关键修复：如果存储过程执行成功，提交事务 =====
        if status == '成功':
            conn.commit()  # <--- 必须显示提交，数据库才会真正保存修改
            return True, f"入库成功！单号：{in_number}，总数量：{total_qty}", {
                'in_number': in_number,
                'total_qty': total_qty,
                'total_amount': total_amount,
                'details': details
            }
        else:
            conn.rollback() # <--- 失败时显式回滚
            return False, message or '入库失败', None
            
    except Exception as e:
        conn.rollback()
        print(f"【调试】异常: {e}")
        return False, str(e), None
    finally:
        conn.close()




def check_stock_in_names(color_data):
    """入库前校验颜色/尺码是否在库存库里存在。

    存储过程 hbc_StockInFromCutting 写明细时用的是
        LEFT JOIN size  s ON s.name = d.sizeName
        LEFT JOIN color c ON c.name = d.colorName
        ... ISNULL(s.sizeid, 0) / ISNULL(c.colorid, 0)
    也就是说颜色或尺码一旦对不上，存储过程不会报错，而是把数量记到
    colorid=0 / sizeid=0 上——单据看起来入库成功，明细却是无效的，
    下游报表按颜色/尺码统计时这批数量就等于消失了。

    所以这里在调用存储过程之前先把名字核对一遍，对不上就中止，
    宁可不入库也不要写出一堆 id=0 的明细。
    """
    names_colors = []
    names_sizes = []
    for item in color_data or []:
        c = str(item.get('color') or '').strip()
        s = str(item.get('size') or '').strip()
        if c and c not in names_colors:
            names_colors.append(c)
        if s and s not in names_sizes:
            names_sizes.append(s)
    if not names_colors and not names_sizes:
        return True, "没有需要校验的颜色/尺码"

    try:
        cursor = get_conn(INVENTORY_CONN_STR, timeout=8).cursor()
    except Exception as exc:
        # 连不上库存库时不阻断，但要让用户知道没校验成
        return True, f"⚠ 未校验颜色/尺码（库存库连接失败：{friendly_error(exc)}）"

    cursor.execute("SELECT name FROM size")
    db_sizes = {str(r[0]) for r in cursor.fetchall()}
    cursor.execute("SELECT name FROM color")
    db_colors = {str(r[0]) for r in cursor.fetchall()}

    missing_sizes = [s for s in names_sizes if s not in db_sizes]
    missing_colors = [c for c in names_colors if c not in db_colors]

    if not missing_sizes and not missing_colors:
        return True, (f"已核对 {len(names_colors)} 个颜色、{len(names_sizes)} 个尺码，"
                      f"库存库中均存在")

    lines = []
    if missing_colors:
        lines.append("库存库 color 表里没有这些颜色：" + "、".join(missing_colors))
    if missing_sizes:
        lines.append("库存库 size 表里没有这些尺码：" + "、".join(missing_sizes))
    lines.append("")
    lines.append("如果不处理，存储过程会把数量记到 colorid=0 / sizeid=0，")
    lines.append("单据显示入库成功，但按颜色/尺码统计时这批数量会消失。")
    lines.append("")
    lines.append("请先在库存系统里补上这些颜色/尺码，或在界面上改成库存库里已有的名称。")
    lines.append("")
    lines.append(f"库存库现有尺码：{'、'.join(sorted(db_sizes))}")
    return False, "\n".join(lines)


def handle_direct_stock_in(record_data):
    """
    直接入库，跳过 StockInDialog 弹窗
    
    :param record_data: 包含当前行/记录数据的字典或对象
    """
    # 1. 获取【目标货号】（优先使用关联货号）
    target_usercode = (
        record_data.get('target_usercode') or 
        record_data.get('关联货号') or 
        record_data.get('货号', '')
    ).strip()
    
    if not target_usercode:
        print("【错误】未找到关联货号或目标货号")
        return False, "无法入库：未找到关联货号"

    # 2. 提取基本属性
    item_code = str(record_data.get('item_usercode') or record_data.get('货号') or '').strip()
    start_no = str(record_data.get('start_no') or record_data.get('开始编号') or record_data.get('start_sn') or '').strip()
    end_no = str(record_data.get('end_no') or record_data.get('结束编号') or record_data.get('end_sn') or '').strip()
    color_data = record_data.get('color_data') or record_data.get('details') or []

    # ========== ✅ 尺码映射函数 ==========
    def map_size(size):
        """将前端尺码映射为数据库尺码"""
        size_map = {
            '3XL': 'XXXL',
            '4XL': 'XXXXL',
            '5XL': 'XXXXXL',
            # 可以继续添加其他映射
            # '2XL': 'XXL',  # 如果需要的话
        }
        # 去除空格并转换为大写
        size_clean = str(size).strip().upper()
        # 返回映射后的值，如果没有映射则返回原值
        return size_map.get(size_clean, size_clean)

    # ========== 合并相同颜色+尺码的数量 ==========
    merged_data = {}
    for item in color_data:
        color = str(item.get('color') or item.get('颜色') or '').strip()
        size = map_size(item.get('size') or item.get('尺码') or '')  # ← 应用尺码映射
        try:
            qty = int(item.get('qty') or item.get('数量', 0))
        except (ValueError, TypeError):
            qty = 0
        
        if color and size and qty > 0:
            key = f"{color}|{size}"
            merged_data[key] = merged_data.get(key, 0) + qty
    
    # 转换为合并后的列表
    merged_color_data = []
    for key, total_qty in merged_data.items():
        color, size = key.split('|', 1)
        merged_color_data.append({
            'color': color,
            'size': size,
            'qty': total_qty
        })
    
    print(f"【调试】合并前: {len(color_data)} 条")
    print(f"【调试】合并后: {len(merged_color_data)} 条")
    for item in merged_color_data:
        print(f"【调试】合并后: color='{item['color']}', size='{item['size']}', qty={item['qty']}")

    # 3. 按颜色统计【总数】（使用合并后的数据）
    color_totals = {}
    for item in merged_color_data:
        c = item.get('color', '').strip()
        q = item.get('qty', 0)
        if c and q > 0:
            color_totals[c] = color_totals.get(c, 0) + q

    # 格式化颜色总数
    color_summary_list = [f"{color}-{total_qty}" for color, total_qty in color_totals.items()]
    color_str = " ".join(color_summary_list)

    # 4. 拼接完整【备注】
    remark_parts = []
    if item_code:
        remark_parts.append(item_code)
    if color_str:
        remark_parts.append(color_str)
    remark = " ".join(remark_parts)
    print(f"【调试】自动生成的备注: '{remark}'")

    # 5. 固定【操作人】
    operator = "hbc"
    
    # 6. 默认单价
    price = float(record_data.get('price', 52.00))

    # 6.5 入库前核对颜色/尺码，避免存储过程把数量写到 colorid=0 / sizeid=0
    ok_names, names_msg = check_stock_in_names(merged_color_data)
    print(f"【入库前校验】{names_msg}")
    if not ok_names:
        return False, names_msg, None

    # 7. 直接调用存储过程入库（使用合并后的数据）
    # 注意：operator / remark 的位置曾经写反过，导致 dlyndx.summary 里存的是备注、
    # Comment 里存的是操作人。这里保持 operator=操作人，remark=备注。
    success, message, result_data = call_stock_in_procedure(
        target_usercode=target_usercode,
        color_data=merged_color_data,  # ← 使用合并后的数据
        price=price,
        operator=remark,
        remark=operator
    )
    
    return success, message, result_data


# ==================== 原有函数（保持不变） ====================

def get_b_product_list(keyword):
    """B数据库模糊查询货号"""
    keyword = (keyword or '').strip()
    if not keyword:
        return []
    conn = get_conn(INVENTORY_CONN_STR, timeout=5)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT a.usercode
            FROM ptype a
            WHERE a.deleted = 0
              AND a.usercode LIKE ?
            ORDER BY a.usercode
        """, f"%{keyword}%")
        return [str(row[0]) for row in cursor.fetchall() if row[0] is not None]
    finally:
        pass


def get_b_colors(usercode):
    """根据货号获取可用颜色"""
    usercode = (usercode or '').strip()
    if not usercode:
        return []
    conn = get_conn(INVENTORY_CONN_STR, timeout=5)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT d.name AS color
            FROM ptype a
            inner join color_group b on b.colorgroupid=a.colorgroupid
            inner join color_group_relation c on b.colorgroupid=c.colorgroupid
            inner join color d on c.colorid=d.colorid
            WHERE a.deleted = 0
              AND a.usercode = ?
              AND d.name IS NOT NULL
            ORDER BY d.name
        """, usercode)
        return [str(row[0]) for row in cursor.fetchall() if row[0] is not None]
    finally:
        pass


def get_b_sizes_qty(usercode, color):
    """根据货号+颜色获取尺码和数量"""
    usercode = (usercode or '').strip()
    color = (color or '').strip()
    if not usercode or not color:
        return []
    conn = get_conn(INVENTORY_CONN_STR, timeout=5)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT s1.name AS size, SUM(ISNULL(c.qty, 0)) AS qty
            FROM ptype a
            INNER JOIN goodsstocks b ON a.typeid = b.ptypeid
            INNER JOIN goodsstockdetail c ON b.orderid = c.orderid
            INNER JOIN color c1 ON c.colorid = c1.colorid
            INNER JOIN size s1 ON c.sizeid = s1.sizeid
            WHERE a.deleted = 0
              AND a.usercode = ?
              AND c1.name = ?
            GROUP BY s1.name
            ORDER BY s1.name
        """, usercode, color)
        return [(str(row[0]), int(row[1] or 0)) for row in cursor.fetchall()]
    finally:
        pass


def copy_template_jfdj(source_zdno, new_zdno):
    """只复制jfdj模板工序到新的ZDNO"""
    source_zdno = (source_zdno or '').strip()
    new_zdno = (new_zdno or '').strip()
    if not source_zdno or not new_zdno:
        return False, '原模板ZDNO和新ZDNO不能为空', 0
    if source_zdno == new_zdno:
        return False, '原模板ZDNO与新ZDNO不能相同', 0
    try:
        check_len('zdno', new_zdno, '新 ZDNO')
    except ValueError as e:
        return False, str(e), 0

    conn = get_conn(conn_str, autocommit=False)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT gx, gxname, dj, mdate, oksl, remark FROM jfdj WHERE zdno = ? ORDER BY gx", source_zdno)
        rows = cursor.fetchall()
        if not rows:
            return False, f"模板 {source_zdno} 不存在或没有jfdj工序", 0
        cursor.execute("SELECT COUNT(1) FROM jfdj WHERE zdno = ?", new_zdno)
        if cursor.fetchone()[0] > 0:
            return False, f"新ZDNO {new_zdno} 已存在，请换一个名称", 0

        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        for row in rows:
            check_len('gxname', row.gxname or '', f'工序 {row.gx} 的名称')
            cursor.execute("""
                INSERT INTO jfdj (zdno, gx, gxname, dj, mdate, oksl, remark)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, new_zdno, row.gx, row.gxname, row.dj, now, 0, row.remark)

        cursor.execute("SELECT COUNT(1) FROM jfdj WHERE zdno = ?", new_zdno)
        written = cursor.fetchone()[0]
        if int(written) != len(rows):
            conn.rollback()
            return False, (f"复制行数校验失败（期望 {len(rows)}，实际 {written}），"
                           f"已回滚，数据库未改动。"), 0
        conn.commit()
        write_audit("复制模板", f"{source_zdno} → {new_zdno} | 仅jfdj | {len(rows)}条")
        return True, f"复制成功：{source_zdno} → {new_zdno}，仅复制jfdj，共{len(rows)}条工序", len(rows)
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return False, str(e), 0
    finally:
        try:
            conn.autocommit = True
        except Exception:
            pass


def get_zdno_list(zdno):
    """获取 jfdj 表中所有 zdno 作为模板列表"""
    cursor = get_conn(conn_str).cursor()
    cursor.execute("SELECT DISTINCT zdno,mdate  FROM jfdj where zdno like ? ORDER BY mdate desc, zdno ", f'%{zdno}%')
    rows = cursor.fetchall()
    return [row[0] for row in rows]


def get_jfdj_data(zdno):
    """获取指定 zdno 的 jfdj 表数据"""
    cursor = get_conn(conn_str).cursor()
    cursor.execute("""
        SELECT gx, gxname, dj, mdate, oksl 
        FROM jfdj 
        WHERE zdno = ?
        ORDER BY gx
    """, zdno)
    rows = cursor.fetchall()
    return rows


def parse_mdate(value):
    """将界面上的日期字符串转为 datetime"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"日期格式无效：{value}（请使用 YYYY-MM-DD 或 YYYY-MM-DD HH:MM）")


def format_dj(value):
    """格式化单价显示"""
    if value is None or value == '':
        return '0'
    try:
        return str(float(value)).rstrip('0').rstrip('.')
    except (TypeError, ValueError):
        return str(value)


def save_jfdj_data(zdno, data_rows):
    """保存 jfdj 表数据（整组覆盖）。

    安全要点：
      * 写库前把该工单原有的 jfdj 行完整快照到 写库撒销快照.json，可一键还原；
      * 保留 remark 列。原实现 DELETE+INSERT 时不写 remark，会把该列重置成 NULL，
        等于每次保存模板都悄悄抹掉备注；
      * 写完核对行数，不一致就回滚。
    """
    zdno = (zdno or '').strip()
    if not zdno:
        return False, "ZDNO 不能为空"

    # 长度预校验：超长直接给中文提示，不让数据库报错打断事务
    try:
        check_len('zdno', zdno, 'ZDNO')
        for gx, gxname, _dj, _mdate, _oksl in data_rows:
            check_len('gxname', gxname or '', f'工序 {gx} 的名称')
    except ValueError as e:
        return False, str(e)

    conn = get_conn(conn_str, autocommit=False)
    cursor = conn.cursor()
    try:
        # 先读出旧数据（含 remark）用于快照和还原
        cursor.execute("SELECT gx, gxname, dj, mdate, oksl, remark FROM jfdj WHERE zdno = ?", zdno)
        before_rows = [list(r) for r in cursor.fetchall()]

        cursor.execute("DELETE FROM jfdj WHERE zdno = ?", zdno)
        # 旧 remark 按工序号带过来，避免保存模板时丢失备注
        remark_by_gx = {}
        for row in before_rows:
            remark_by_gx[row[0]] = row[5]

        for gx, gxname, dj, mdate, oksl in data_rows:
            cursor.execute("""
                INSERT INTO jfdj (zdno, gx, gxname, dj, mdate, oksl, remark)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, zdno, gx, gxname or '', dj, mdate, oksl, remark_by_gx.get(gx))

        cursor.execute("SELECT COUNT(1) FROM jfdj WHERE zdno = ?", zdno)
        written = cursor.fetchone()[0]
        if int(written) != len(data_rows):
            conn.rollback()
            return False, (f"写入行数校验失败（期望 {len(data_rows)} 条，实际 {written} 条），"
                           f"已回滚，数据库未改动。")

        conn.commit()
        save_write_snapshot({
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'action': '保存模板工序',
            'zdno': zdno,
            'table': 'jfdj',
            'before': before_rows,
            'after': [[r[0], r[1], str(r[2]), r[3].isoformat() if r[3] else None,
                       r[4], r[5]] for r in _rows_for_snapshot(cursor, zdno)],
        })
        write_audit("保存模板工序", f"{zdno} | 原{len(before_rows)}条 -> 现{len(data_rows)}条")
        return True, f"保存成功！共 {len(data_rows)} 条工序数据（可点「↩ 撤销上次写库」还原）"
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return False, f"保存失败：{friendly_error(e)}"
    finally:
        try:
            conn.autocommit = True
        except Exception:
            pass


def _rows_for_snapshot(cursor, zdno):
    """读取刚写入的 jfdj 行用于快照（在同一事务内调用）。"""
    cursor.execute("SELECT gx, gxname, dj, mdate, oksl, remark FROM jfdj WHERE zdno = ? ORDER BY gx", zdno)
    return cursor.fetchall()


def get_template_record_counts(zdno):
    """统计指定 ZDNO 在四张表中的记录数"""
    tables = SAFE_TABLES
    conn = get_conn(conn_str)
    cursor = conn.cursor()
    counts = {}
    try:
        for table in tables:
            cursor.execute(f"SELECT COUNT(1) FROM {table} WHERE ZDNO = ?", zdno)
            counts[table] = cursor.fetchone()[0]
        return counts
    finally:
        pass


SNAPSHOT_QUERIES = {
    'jfdj': "SELECT zdno, gx, gxname, dj, mdate, oksl, remark FROM jfdj WHERE zdno = ? ORDER BY gx",
    'jfzd': "SELECT ZDNO, GX, ZDID, DDSL FROM jfzd WHERE zdno = ? ORDER BY ZDID",
    'jfzd2': "SELECT ZDNO, CC, ZH, GH, YS, CM, JS, MDATE FROM jfzd2 WHERE zdno = ? ORDER BY CC, ZH",
    'jfzd3': ("SELECT ZDNO, CC, ITEM, ZHBEGIN, GH, YS, " +
              ", ".join(f"SL{i}" for i in range(1, 51)) +
              " FROM jfzd3 WHERE zdno = ? ORDER BY CC, ITEM"),
}

RESTORE_QUERIES = {
    'jfdj': ("INSERT INTO jfdj (zdno, gx, gxname, dj, mdate, oksl, remark) "
             "VALUES (?, ?, ?, ?, ?, ?, ?)"),
    'jfzd': "INSERT INTO jfzd (ZDNO, GX, ZDID, DDSL) VALUES (?, ?, ?, ?)",
    'jfzd2': "INSERT INTO jfzd2 (ZDNO, CC, ZH, GH, YS, CM, JS, MDATE) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    'jfzd3': ("INSERT INTO jfzd3 (ZDNO, CC, ITEM, ZHBEGIN, GH, YS, " +
              ", ".join(f"SL{i}" for i in range(1, 51)) + ") VALUES (" +
              ", ".join(["?"] * 56) + ")"),
}


def export_zdno_snapshot(zdno):
    """把一个工单在四张表中的数据完整导出（用于删除前留底 / 撒销还原）。"""
    zdno = (zdno or '').strip()
    conn = get_conn(conn_str)
    cursor = conn.cursor()
    data = {}
    for table in SAFE_TABLES:
        cursor.execute(SNAPSHOT_QUERIES[table], zdno)
        data[table] = [list(r) for r in cursor.fetchall()]
    return data


def restore_zdno_snapshot(zdno, data):
    """按快照还原一个工单在四张表中的数据。"""
    zdno = (zdno or '').strip()
    conn = get_conn(conn_str, autocommit=False)
    cursor = conn.cursor()
    try:
        for table in SAFE_TABLES:
            cursor.execute(f"DELETE FROM {table} WHERE ZDNO = ?", zdno)
        restored = 0
        for table in SAFE_TABLES:
            rows = data.get(table) or []
            for row in rows:
                cursor.execute(RESTORE_QUERIES[table], zdno, *row[1:])
                restored += 1
        conn.commit()
        write_audit("撒销还原", f"{zdno} | 还原 {restored} 行")
        return True, f"已还原 {zdno}，共恢复 {restored} 条记录"
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return False, f"还原失败：{friendly_error(e)}"
    finally:
        try:
            conn.autocommit = True
        except Exception:
            pass


def delete_template_by_zdno(zdno):
    """根据 ZDNO 删除四张表中的对应记录。

    删除前会把该工单在四张表中的全部数据导出到 写库撒销快照.json，
    删除后仍可通过「↩ 撒销上次写库」一键还原。
    """
    zdno = (zdno or '').strip()
    if not zdno:
        return False, "ZDNO 不能为空", {}

    conn = get_conn(conn_str, autocommit=False)
    cursor = conn.cursor()
    try:
        # 先在事务内取快照，保证和删除看到的是同一份数据
        backup = {}
        for table in SAFE_TABLES:
            cursor.execute(SNAPSHOT_QUERIES[table], zdno)
            backup[table] = [list(r) for r in cursor.fetchall()]

        deleted_counts = {}
        for table in SAFE_TABLES:
            cursor.execute(f"DELETE FROM {table} WHERE ZDNO = ?", zdno)
            deleted_counts[table] = cursor.rowcount

        total = sum(deleted_counts.values())
        conn.commit()

        save_write_snapshot({
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'action': '删除模板',
            'zdno': zdno,
            'table': 'ALL',
            'before': backup,
            'deleted_counts': deleted_counts,
        })
        detail = '，'.join(f"{table}:{count}" for table, count in deleted_counts.items())
        write_audit("删除模板", f"{zdno} | {detail} | 共{total}条")
        return True, (f"已删除模板 {zdno}（{detail}，共 {total} 条）。\n"
                      f"删除前的数据已备份，可点「↩ 撤销上次写库」还原。"), deleted_counts
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return False, f"删除失败：{friendly_error(e)}", {}
    finally:
        try:
            conn.autocommit = True
        except Exception:
            pass


def get_next_available_zdid(conn):
    """获取下一个可用的 ZDID（1~9999 循环使用）"""
    cursor = conn.cursor()
    cursor.execute("SELECT ZDID FROM jfzd WHERE ZDID BETWEEN 1 AND 9999")
    used_zdids = {row[0] for row in cursor.fetchall()}
    for zdid in range(1, 10000):
        if zdid not in used_zdids:
            return zdid
    raise ValueError("ZDID 池已满（1~9999 全部被占用），请清理数据库")


def build_detail_rows_from_ui(color_data, start_num, end_num, zdno_display, type_name, mode):
    """按界面上的尺码数据生成明细表待写入行。

    只收「明细」：每一手（pieces）展开成一个顺序号，颜色/尺码/数量一一对应。
    返回 (ok, message, 待写入行列表)
    """
    color_data = color_data or []
    if not color_data:
        return False, "界面上没有有效的尺码数据（颜色/尺码/件数/数量都要大于0）", []
    try:
        type_name = check_len('type_name', type_name, '类型') or DEFAULT_TYPE
    except ValueError as e:
        return False, str(e), []

    detail_zdno, _prefix, _num = build_detail_zdno(zdno_display, type_name)
    try:
        check_len('zdno', detail_zdno, '明细制单号')
    except ValueError as e:
        return False, str(e), []

    expanded = []
    for item in color_data:
        color = str(item.get('color', '') or '').strip()
        size = str(item.get('size', '') or '').strip()
        try:
            pieces = int(item.get('pieces', 0) or 0)
            qty = int(item.get('quantity', 0) or 0)
        except (TypeError, ValueError):
            continue
        if not color or not size or pieces <= 0 or qty <= 0:
            continue
        for _ in range(pieces):
            expanded.append((color, size, qty))

    total_count = int(end_num) - int(start_num) + 1
    if len(expanded) != total_count:
        return False, (
            f"件数与编号范围对不上：编号 {start_num}~{end_num} 共 {total_count} 个，"
            f"但尺码数据展开后是 {len(expanded)} 个。\n"
            f"请调整「件数」，让每行件数之和等于编号个数。"), []

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    rows = []
    for idx, (color, size, qty) in enumerate(expanded):
        rows.append((detail_zdno, zdno_display, int(start_num) + idx,
                     int(start_num), int(end_num),
                     color, size, int(qty), type_name, mode, now, 'hbc', ''))

    summary = {}
    for color, size, qty in expanded:
        summary[(color, size)] = summary.get((color, size), 0) + qty
    detail_txt = "；".join(f"{c}/{s}={q}件" for (c, s), q in summary.items())
    return True, (f"制单号 {detail_zdno}，顺序号 {start_num}~{end_num}，"
                  f"共 {len(rows)} 行，合计 {sum(r[7] for r in rows)} 件\n{detail_txt}"), rows


def count_detail_in_range(conn, detail_zdno, zdno_display, start_num, end_num):
    """该制单号 + 编号范围内已经写了多少行（用于提示重复写入）。"""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COUNT(1), ISNULL(SUM(JS), 0)
        FROM jfzd2_detail
        WHERE ZDNO = ? AND ORDER_NO >= ? AND ORDER_NO <= ?
    """, detail_zdno, int(start_num), int(end_num))
    row = cursor.fetchone()
    return int(row[0] or 0), int(row[1] or 0)


def guess_type_from_zdno(zdno):
    """从 ZDNO 里的汉字猜款式类型。

    现有工单里约 40% 的 ZDNO 直接带了类型词（1055-1背心185-202、002单衣108-131…），
    能自动认出来就不用手工选；认不出返回 None，交给用户选。
    """
    zdno = str(zdno or '')
    # 打底裤 3 个字，放最前面优先匹配，避免被两字类型抢先
    for name in ('打底裤',) + tuple(n for n in TYPE_PRESETS if len(n) == 2):
        if name in zdno:
            return name
    return None


def _detail_header_slots(conn, zdno):
    """jfzd3 表头行里非空槽位的个数 = 生成该工单时的顺序号个数。

    insert_data 写 jfzd3 表头的规则：
      * 明细模式 → 每个顺序号一个槽位（槽位数 = 顺序号个数）
      * 汇总模式 → 只写 SL1 一个槽位
    所以槽位数能可靠区分明细工单和汇总工单，比看颜色名是否带 '/' 更准
    （单颜色汇总的颜色名是「宝兰」而不是「宝兰/黑色」）。
    """
    cols = ", ".join(f"SL{i}" for i in range(1, 51))
    cursor = conn.cursor()
    cursor.execute(f"SELECT {cols} FROM jfzd3 WHERE zdno = ? AND ITEM = 0", zdno)
    slots = 0
    for row in cursor.fetchall():
        slots += sum(1 for v in row if v not in (None, ''))
    return slots


def build_detail_rows(conn, zdno, type_name, mode=""):
    """从 jfzd2 读取一个 ZDNO 的明细，生成 jfzd2_detail 的待写入行。

    jfzd2 本身就是「每个顺序号一行」，所以直接一对一映射：
        ORDER_NO  = jfzd2.ZH（顺序号）
        ORDER_BEGIN / ORDER_END = 该 ZDNO 的最小 / 最大扎号
    返回 (ok, message, 待写入行列表)
    """
    zdno = (zdno or '').strip()
    if not zdno:
        return False, "请先填写 ZDNO", []
    try:
        type_name = check_len('type_name', type_name, '类型') or DEFAULT_TYPE
    except ValueError as e:
        return False, str(e), []

    cursor = conn.cursor()
    cursor.execute("""
        SELECT CC, ZH, GH, YS, CM, JS
        FROM jfzd2 WHERE zdno = ? ORDER BY CC, ZH
    """, zdno)
    src = cursor.fetchall()
    if not src:
        return False, f"工单 {zdno} 在 jfzd2 里没有明细数据，无法补录", []

    # 同一 ZDNO 可能有多条摘要记录，全部按各自顺序号写入
    detail_zdno, _prefix, _num = build_detail_zdno(zdno, type_name)
    order_begin = min(int(r[1]) for r in src)
    order_end = max(int(r[1]) for r in src)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 只收明细。判断是不是汇总工单：
    #   1) jfzd3 表头槽位 <= 1 且 jfzd2 只有 1 行  -> insert_data 的汇总模式
    #   2) 颜色名里带 '/'                            -> 多颜色汇总时颜色用 '/' 连接
    try:
        header_slots = _detail_header_slots(conn, zdno)
    except Exception:
        header_slots = -1
    looks_summary = (header_slots <= 1 and len(src) == 1)

    detail_src = []
    summary_rows = []
    for r in src:
        ys_text = str(r[3] or '')
        if looks_summary or '/' in ys_text:
            summary_rows.append(r)
        else:
            detail_src.append(r)

    rows = []
    for _cc, zh, _gh, ys, cm, js in detail_src:
        rows.append((detail_zdno, zdno, int(zh), order_begin, order_end,
                     str(ys or ''), str(cm or ''), int(js or 0),
                     type_name, mode or '明细', now, 'hbc', ''))

    skip_note = ""
    if summary_rows:
        skip_note = (f"（已跳过 {len(summary_rows)} 条汇总记录，"
                     f"汇总数据不入明细表）")
    if not rows:
        return False, (f"工单 {zdno} 在 jfzd2 里只有汇总记录、没有逐个顺序号的明细，"
                       f"无法补录明细。{skip_note}"), []

    return True, (f"工单 {zdno}：制单号 {detail_zdno}，"
                  f"顺序号 {order_begin}~{order_end}，"
                  f"共 {len(rows)} 行，合计 {sum(r[7] for r in rows)} 件{skip_note}"), rows


def count_detail_existing(conn, zdno):
    """该工单在明细表里已有多少行（用于提示重复补录）。

    只按 TEMPLATE_ZDNO 统计：不同工单可能因为"汉字替换成类型"而得到相同的制单号
    （例如 268810套装7-12 和 268810包缝7-12 在类型=套装 时都是 268810套装7-12），
    若再按 ZDNO 匹配会把别的工单也算进来。
    """
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(1) FROM jfzd2_detail WHERE TEMPLATE_ZDNO = ?", zdno)
    return int(cursor.fetchone()[0])


def write_detail_rows(conn, all_rows):
    """把预览确认过的行写入 jfzd2_detail。返回 (ok, message)。"""
    if not all_rows:
        return False, "没有需要写入的数据"
    # 明细表 13 列；行长度对不上时直接报清楚，别让 SQL 报"参数个数不符"
    bad = [i for i, r in enumerate(all_rows) if len(r) != 13]
    if bad:
        return False, (f"内部错误：第 {bad[0] + 1} 行有 {len(all_rows[bad[0]])} 个字段，"
                       f"应为 13 个（制单号/原始工单号/顺序号/开始/结束/颜色/尺码/数量/"
                       f"类型/模式/时间/操作人/备注）。已中止，未写入。")
    try:
        ensure_detail_table()
        cursor = conn.cursor()
        for row in all_rows:
            cursor.execute("""
                INSERT INTO jfzd2_detail
                    (ZDNO, TEMPLATE_ZDNO, ORDER_NO, ORDER_BEGIN, ORDER_END,
                     YS, CM, JS, TYPE_NAME, MODE, CREATED_AT, CREATED_BY, REMARK)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, *row)
        cursor.execute("SELECT COUNT(1) FROM jfzd2_detail WHERE CREATED_AT = ?",
                       all_rows[0][10])
        return True, f"已补录 {len(all_rows)} 行到明细表"
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        return False, f"补录失败：{friendly_error(exc)}"


def backfill_detail(zdnos, type_name, mode=""):
    """补录一个或多个 ZDNO 到明细表。

    zdnos: 字符串，逗号 / 空格 / 换行分隔
    返回 (ok, message, 明细行列表)
    """
    parts = [p for p in re.split(r'[,\s，、]+', str(zdnos or '')) if p]
    if not parts:
        return False, "请填写要补录的 ZDNO（多个可用逗号分隔）", []

    conn = get_conn(conn_str, autocommit=False)
    try:
        all_rows = []
        notes = []
        for z in parts:
            ok, msg, rows = build_detail_rows(conn, z, type_name, mode)
            if not ok:
                conn.rollback()
                return False, msg, []
            already = count_detail_existing(conn, z)
            note = f"　· {msg}"
            if already:
                note += f"（明细表里已有 {already} 行，将重复补录）"
            notes.append(note)
            all_rows.extend(rows)
        return True, "\n".join(notes), all_rows
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        return False, f"读取明细失败：{friendly_error(exc)}", []
    finally:
        try:
            conn.autocommit = True
        except Exception:
            pass


def insert_data(template_zdno, new_zdno_prefix, start_num, end_num, color_data, mode="明细",
                  type_name=DEFAULT_TYPE):
    """插入数据到四张表：jfdj, jfzd, jfzd3, jfzd2

    安全要点：
      * 插入前先检查新 ZDNO 是否已经存在，避免撞主键后弹出一堆数据库原始报错；
      * ZDNO / 颜色 / 尺码的长度按表结构预校验（这三列超长会直接让事务失败）；
      * 保留 remark 列不被复制成 NULL；
      * 整组写入同一事务，任何一步失败全部回滚，并在完成后留下可撤销的快照。
    """
    conn = get_conn(conn_str, autocommit=False)
    cursor = conn.cursor()

    try:
        # 明细模式也会走到函数末尾，因此必须先初始化，避免
        # return 成功消息时出现 local variable 'total_qty' referenced before assignment。
        total_qty = sum(
            int(item.get('pieces', 0) or 0) * int(item.get('quantity', 0) or 0)
            for item in color_data
        )
        new_zdno = f"{new_zdno_prefix}{start_num}-{end_num}"

        # ---- 预校验：长度 ----
        check_len('zdno', new_zdno, '新 ZDNO')
        type_name = check_len('type_name', type_name, '类型') or DEFAULT_TYPE
        for item in color_data:
            check_len('ys', item.get('color', ''), f"颜色「{item.get('color', '')}」")
            check_len('cm', item.get('size', ''), f"尺码「{item.get('size', '')}」")

        total_count = end_num - start_num + 1
        if total_count > 50:
            raise ValueError(f"总个数 {total_count} 超过 50，无法存入 SL1~SL50")

        # ---- 预校验：新 ZDNO 不能已存在 ----
        # 原实现靠 jfzd 的单列主键冲突来拦重复提交，用户看到的是数据库原始报错。
        # 这里提前查出来，直接给出可读提示，并且不改动任何数据。
        existing = {}
        for table in SAFE_TABLES:
            cursor.execute(f"SELECT COUNT(1) FROM {table} WHERE ZDNO = ?", new_zdno)
            existing[table] = cursor.fetchone()[0]
        hit = {t: c for t, c in existing.items() if c}
        if hit:
            detail = '，'.join(f"{t}:{c}条" for t, c in hit.items())
            raise ValueError(
                f"新 ZDNO「{new_zdno}」在数据库中已经存在（{detail}）。\n"
                f"为避免覆盖历史数据，本次操作已停止。\n"
                f"请换一个 ZDNO，或先在「🧵 ZDNO维护」窗口里查看该工单。")

        # 1. 插入 jfdj
        # V7.7.1 修复：多颜色时不要因为重复提交或历史残留导致 PKjfdj 冲突
        # jfdj 与颜色无关，只需要每个新ZDNO保存一次工序
        cursor.execute("SELECT gx, gxname, dj, mdate, oksl, remark FROM jfdj WHERE zdno = ?", template_zdno)
        template_rows = cursor.fetchall()
        if not template_rows:
            raise ValueError(f"模板 ZDNO '{template_zdno}' 不存在")

        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        created_by = 'hbc'

        # remark 存「类型」，这样从 jfdj 就能看出这个工单属于哪一类款式
        for row in template_rows:
            cursor.execute("""
                INSERT INTO jfdj (zdno, gx, gxname, dj, mdate, oksl, remark)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, new_zdno, row.gx, row.gxname, row.dj, now, 0, type_name)

        detail_zdno, _prefix, _num = build_detail_zdno(new_zdno, type_name)
        check_len('zdno', detail_zdno, '明细制单号')
        
        # 2. 插入 jfzd（修改：循环使用 ZDID）
        gx_count = len(template_rows)
        new_zdid = get_next_available_zdid(conn)
        cursor.execute("""
            INSERT INTO jfzd (ZDNO, GX, ZDID, DDSL)
            VALUES (?, ?, ?, ?)
        """, new_zdno, gx_count, new_zdid, 0)
        
        # 3. 插入 jfzd3
        cursor.execute("SELECT ISNULL(MAX(CC), 0) + 1 FROM jfzd3 WHERE ZDNO = ?", new_zdno)
        new_cc = cursor.fetchone()[0]

        placeholders = ', '.join(['?'] * (6 + 50))
        sql_jfzd3 = f"""
            INSERT INTO jfzd3 (ZDNO, CC, ITEM, ZHBEGIN, GH, YS,
                SL1, SL2, SL3, SL4, SL5, SL6, SL7, SL8, SL9, SL10,
                SL11, SL12, SL13, SL14, SL15, SL16, SL17, SL18, SL19, SL20,
                SL21, SL22, SL23, SL24, SL25, SL26, SL27, SL28, SL29, SL30,
                SL31, SL32, SL33, SL34, SL35, SL36, SL37, SL38, SL39, SL40,
                SL41, SL42, SL43, SL44, SL45, SL46, SL47, SL48, SL49, SL50)
            VALUES ({placeholders})
        """

        # V7.7.4 核心修复：先把 UI 尺码行展开成“每一手/每一个缸号”的槽位。
        # 例如：
        # 红 M 2手×43、红 L 1手×50、黑 M 2手×71、黑 XL 1手×80
        # 会展开为：
        # [红/M/43, 红/M/43, 红/L/50, 黑/M/71, 黑/M/71, 黑/XL/80]
        # jfzd3 的 SL1~SL50 必须按照这个全局顺序对齐，不能每个颜色都从 SL1 重新开始。
        expanded_slots = []
        for item in color_data:
            color = str(item.get('color', '') or '').strip()
            size = str(item.get('size', '') or '').strip()
            try:
                pieces = int(item.get('pieces', 0) or 0)
                qty = int(item.get('quantity', 0) or 0)
            except (TypeError, ValueError):
                pieces, qty = 0, 0
            if not color or not size or pieces <= 0 or qty <= 0:
                continue
            for _ in range(pieces):
                expanded_slots.append({
                    'color': color,
                    'size': size,
                    'quantity': qty,
                })

        if mode == "明细":
            if len(expanded_slots) != total_count:
                raise ValueError(
                    f"尺码数据与编号数量不一致：开始/结束编号共 {total_count} 个，"
                    f"而颜色/尺码/件数展开后为 {len(expanded_slots)} 个。"
                    f"请检查右侧尺码数据中的件数。"
                )

            # jfzd3 第0行：尺码表头。
            sl_header = [''] * 50
            for i, slot in enumerate(expanded_slots[:50]):
                sl_header[i] = slot['size']
            params_header = [new_zdno, new_cc, 0, start_num, '缸号↓', '颜色↓ 尺码→'] + sl_header
            cursor.execute(sql_jfzd3, params_header)

            # 按颜色第一次出现的顺序建立颜色行。
            # 关键点：每个颜色的数据写回“全局槽位”，不能从 SL1 重新开始。
            color_order = []
            for slot in expanded_slots:
                if slot['color'] not in color_order:
                    color_order.append(slot['color'])

            for color_index, color in enumerate(color_order, start=1):
                sl_data = [''] * 50
                for i, slot in enumerate(expanded_slots[:50]):
                    if slot['color'] == color:
                        sl_data[i] = str(slot['quantity'])

                params_data = [new_zdno, new_cc, color_index, start_num, '', color] + sl_data
                cursor.execute(sql_jfzd3, params_data)

            # jfzd2：每个缸号/尺码使用“同一槽位”的准确颜色和数量。
            # 不再用 size -> quantity 字典，避免同尺码不同颜色/不同数量时被覆盖。
            cursor.execute("SELECT ISNULL(MAX(CC), 0) + 1 FROM jfzd2 WHERE ZDNO = ?", new_zdno)
            jfzd2_cc = cursor.fetchone()[0]
            for i, slot in enumerate(expanded_slots):
                zh_num = start_num + i
                cursor.execute("""
                    INSERT INTO jfzd2 (ZDNO, CC, ZH, GH, YS, CM, JS, MDATE)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, new_zdno, jfzd2_cc, zh_num, '', slot['color'], slot['size'], slot['quantity'], now)

        else:
            # 汇总模式：jfzd3 保持“均码”表头，但多颜色分别成行。
            # 每种颜色的 SL1 保存该颜色的整批数量，避免按 pieces 重复铺到多个 SL 列。
            sl_header = [''] * 50
            sl_header[0] = '均码'
            params_header = [new_zdno, new_cc, 0, start_num, '缸号↓', '颜色↓ 尺码→'] + sl_header
            cursor.execute(sql_jfzd3, params_header)

            color_totals = {}
            color_order = []
            for item in color_data:
                color = str(item.get('color', '') or '').strip()
                if not color:
                    continue
                if color not in color_totals:
                    color_totals[color] = 0
                    color_order.append(color)
                try:
                    color_totals[color] += int(item.get('pieces', 0) or 0) * int(item.get('quantity', 0) or 0)
                except (TypeError, ValueError):
                    pass

            for color_index, color in enumerate(color_order, start=1):
                sl_data = [''] * 50
                if color_totals[color] > 0:
                    sl_data[0] = str(color_totals[color])
                params_data = [new_zdno, new_cc, color_index, start_num, '', color] + sl_data
                cursor.execute(sql_jfzd3, params_data)

            # 汇总模式 jfzd2 保持一条汇总记录；颜色用“/”连接，数量为全部颜色总和。
            cursor.execute("SELECT ISNULL(MAX(CC), 0) + 1 FROM jfzd2 WHERE ZDNO = ?", new_zdno)
            jfzd2_cc = cursor.fetchone()[0]
            colors_text = '/'.join(color_order)
            cursor.execute("""
                INSERT INTO jfzd2 (ZDNO, CC, ZH, GH, YS, CM, JS, MDATE)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, new_zdno, jfzd2_cc, start_num, '', colors_text, '均码', total_qty, now)

        # 注意：jfzd2_detail 明细统计表不在这里写。
        # 明细表由界面上的「📊 补录明细表」按钮单独处理，
        # 这样插入工单不会顺带产生明细记录，也方便单独补录历史工单。

        conn.commit()

        # 落库后留一份快照：这是新增数据，撤销就是把新 ZDNO 的四张表记录删掉
        save_write_snapshot({
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'action': '插入新工单',
            'zdno': new_zdno,
            'table': 'ALL',
            'before': {},          # 插入前该 ZDNO 在四张表中没有任何数据
            'inserted': True,
            'type_name': type_name,
            'detail_zdno': detail_zdno,
        })
        write_audit("插入新工单", f"{new_zdno} | 明细制单号 {detail_zdno} | 类型 {type_name} | "
                                  f"模板 {template_zdno} | {mode} | 共{total_qty}件")

        if mode == "汇总":
            return True, (f"数据插入成功！汇总模式插入 1 条汇总记录（均码，共 {total_qty} 件）\n"
                          f"如需撤销，可点「↩ 撤销上次写库」。")
        else:
            return True, (f"数据插入成功！明细模式按 {total_count} 个编号插入明细数据（共 {total_qty} 件）\n"
                          f"如需撤销，可点「↩ 撤销上次写库」。")

    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return False, str(e)
    finally:
        try:
            conn.autocommit = True
        except Exception:
            pass


def undo_last_write():
    """撤销上一次写库操作（保存模板 / 删除模板 / 插入新工单）。

    - 删除模板：把快照里的四张表数据还原回去
    - 插入新工单：把该新 ZDNO 在四张表中的记录删掉
    - 保存模板工序：把 jfdj 还原成保存前的样子
    """
    snap = load_write_snapshot()
    if not snap:
        return False, "没有找到可撤销的写库记录。"

    action = snap.get('action')
    zdno = snap.get('zdno') or ''

    if action == '删除模板':
        return restore_zdno_snapshot(zdno, snap.get('before') or {})

    if action == '插入新工单':
        conn = get_conn(conn_str, autocommit=False)
        cursor = conn.cursor()
        try:
            removed = {}
            for table in SAFE_TABLES:
                cursor.execute(f"DELETE FROM {table} WHERE ZDNO = ?", zdno)
                removed[table] = cursor.rowcount
            # 插入工单不会写 jfzd2_detail，所以这里只回滚四张主表
            total = sum(removed.values())
            conn.commit()
            clear_write_snapshot()
            detail = '，'.join(f"{t}:{c}" for t, c in removed.items())
            write_audit("撒销插入", f"{zdno} | {detail}")
            return True, f"已撤销插入 {zdno}，删除该工单记录 {total} 条（{detail}）"
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return False, f"撒销失败：{friendly_error(e)}"
        finally:
            try:
                conn.autocommit = True
            except Exception:
                pass

    if action == '写入明细':
        conn = get_conn(conn_str, autocommit=False)
        cursor = conn.cursor()
        try:
            detail_zdno = snap.get('detail_zdno') or zdno
            beg = snap.get('order_begin')
            end = snap.get('order_end')
            if beg is None or end is None:
                cursor.execute("DELETE FROM jfzd2_detail WHERE ZDNO = ?", detail_zdno)
            else:
                cursor.execute(
                    "DELETE FROM jfzd2_detail WHERE ZDNO = ? "
                    "AND ORDER_NO >= ? AND ORDER_NO <= ?",
                    detail_zdno, int(beg), int(end))
            removed = cursor.rowcount
            conn.commit()
            clear_write_snapshot()
            write_audit("撒销写入明细", f"{detail_zdno} {beg}~{end} | 删除 {removed} 行")
            return True, f"已撤销写入明细，删除 {removed} 行（{detail_zdno}）"
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return False, f"撒销失败：{friendly_error(e)}"
        finally:
            try:
                conn.autocommit = True
            except Exception:
                pass

    if action == '补录明细表':
        # 补录是纯新增，撤销就是把这次写进去的行删掉
        zlist = [z for z in re.split(r'[,\s，、]+', str(zdno or '')) if z]
        conn = get_conn(conn_str, autocommit=False)
        cursor = conn.cursor()
        try:
            removed = 0
            for z in zlist:
                cursor.execute(
                    "DELETE FROM jfzd2_detail WHERE TEMPLATE_ZDNO = ?", z)
                removed += cursor.rowcount
            conn.commit()
            clear_write_snapshot()
            write_audit("撒销补录", f"{','.join(zlist)} | 删除 {removed} 行")
            return True, f"已撤销补录明细表，删除 {removed} 行（{', '.join(zlist)}）"
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return False, f"撒销失败：{friendly_error(e)}"
        finally:
            try:
                conn.autocommit = True
            except Exception:
                pass

    if action == '保存模板工序':
        before_rows = snap.get('before') or []
        conn = get_conn(conn_str, autocommit=False)
        cursor = conn.cursor()
        try:
            cursor.execute("DELETE FROM jfdj WHERE zdno = ?", zdno)
            for row in before_rows:
                cursor.execute("""
                    INSERT INTO jfdj (zdno, gx, gxname, dj, mdate, oksl, remark)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, zdno, row[0], row[1], row[2],
                    row[3].isoformat() if hasattr(row[3], 'isoformat') else row[3],
                    row[4], row[5])
            cursor.execute("SELECT COUNT(1) FROM jfdj WHERE zdno = ?", zdno)
            n = cursor.fetchone()[0]
            if int(n) != len(before_rows):
                conn.rollback()
                return False, f"还原行数校验失败（期望 {len(before_rows)}，实际 {n}），已回滚"
            conn.commit()
            clear_write_snapshot()
            write_audit("撒销保存模板", f"{zdno} | 还原 {n} 条")
            return True, f"已撤销保存模板，{zdno} 的 jfdj 已还原为 {n} 条原始工序"
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return False, f"撒销失败：{friendly_error(e)}"
        finally:
            try:
                conn.autocommit = True
            except Exception:
                pass

    return False, f"未知的快照类型：{action}"


# ==================== 条码打印模块 ====================
BARCODE_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "条码打印历史.json")
BARCODE_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "条码打印设置.json")

# 使用 ReportLab 内置中文 CID 字体，避免用户电脑没有特定字体导致 PDF 中文乱码
try:
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    PDF_FONT = "STSong-Light"
except Exception:
    PDF_FONT = "Helvetica"

DEFAULT_BARCODE_CONFIG = {
    "layout_version": 4,
    "page_width_mm": 210.0,
    "page_height_mm": 297.0,
    "label_width_mm": 86.0,
    "label_height_mm": 14.7895,
    "columns": 2,
    "rows": 19,
    "center_gap_mm": 38.0,
    "left_margin_mm": 0.0,
    "top_margin_mm": 8.0,
    "bottom_margin_mm": 8.0,
    "horizontal_offset_mm": 0.0,
    "first_column_offset_mm": 0.0,
    "vertical_offset_mm": 0.0,
    "fit_rows_to_page": False,
    "barcode_height_mm": 7.0,
    "barcode_width_mm": 34.0,
    "show_barcode_text": False,
    "title_font_size": 7.2,
    "body_font_size": 7.0,
    "barcode_text_font_size": 5.5,
    "center_font_size": 10.0,
    "center_zdno_font_size": 5.5,
    "center_top_distance_mm": 137.0,
    "center_line_leading": 6.0,
    "summary_qr_enabled": True,
    "summary_qr_size_mm": 25.0,
    "summary_qr_gap_mm": 3.0,
    # ===== 标签二维码（阶段 0）=====
    # 扫标签左下角的二维码进入报工页；与右侧条码并存，互不影响。
    # 默认关闭：确认扫码枪与标签纸尺寸无误后再开启。
    "label_qr_enabled": False,
    "label_qr_size_mm": 13.0,
    # 留空则不画二维码（避免印出扫不出用的码）。
    # 必须是 http(s) 地址，微信才能按网页打开。
    "label_qr_base_url": "",

    # 二维码必须是 http(s) 地址，微信才能按网页打开。
    # 留空时自动使用本机局域网IP，例如 http://192.168.0.100:8765
    "summary_qr_base_url": "https://cut.holyhbc.eu.org",
    "summary_qr_server_port": 8765,
    "summary_qr_text_font_size": 10.0,
    "summary_qr_text_gap_mm": 3.0
}


def load_barcode_config():
    cfg = DEFAULT_BARCODE_CONFIG.copy()
    try:
        if os.path.exists(BARCODE_CONFIG_FILE):
            with open(BARCODE_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 旧版本配置中的条码宽度/显示数字/字体会导致版式与新模板不一致。
            # 第一次升级到布局V2时，只升级这些旧版专用参数；用户后续自行修改的设置会保留。
            if not isinstance(data, dict):
                data = {}
            if int(data.get("layout_version", 1) or 1) < 2:
                data = dict(data)
                data["layout_version"] = 2
                data["barcode_width_mm"] = DEFAULT_BARCODE_CONFIG["barcode_width_mm"]
                data["show_barcode_text"] = DEFAULT_BARCODE_CONFIG["show_barcode_text"]
                data["title_font_size"] = DEFAULT_BARCODE_CONFIG["title_font_size"]
                data["body_font_size"] = DEFAULT_BARCODE_CONFIG["body_font_size"]
                data["center_font_size"] = DEFAULT_BARCODE_CONFIG["center_font_size"]
                data["center_zdno_font_size"] = DEFAULT_BARCODE_CONFIG["center_zdno_font_size"]
                data["center_top_distance_mm"] = DEFAULT_BARCODE_CONFIG["center_top_distance_mm"]
                data["center_line_leading"] = DEFAULT_BARCODE_CONFIG["center_line_leading"]
            # 新增中间信息定位参数；已有用户配置不覆盖。
            if "center_top_distance_mm" not in data:
                data["center_top_distance_mm"] = DEFAULT_BARCODE_CONFIG["center_top_distance_mm"]
            if "center_line_leading" not in data:
                data["center_line_leading"] = DEFAULT_BARCODE_CONFIG["center_line_leading"]
            if "first_column_offset_mm" not in data:
                data["first_column_offset_mm"] = DEFAULT_BARCODE_CONFIG["first_column_offset_mm"]
            cfg.update(data)
    except Exception as e:
        print(f"读取条码打印设置失败，使用默认值：{e}")
    return cfg


def save_barcode_config(cfg):
    with open(BARCODE_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def load_barcode_history():
    try:
        if os.path.exists(BARCODE_HISTORY_FILE):
            with open(BARCODE_HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        print(f"读取条码历史失败：{e}")
    return []


def save_barcode_history(record):
    history = load_barcode_history()
    history.insert(0, record)
    # 长期使用时避免历史文件无限增长
    history = history[:1000]
    with open(BARCODE_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def make_barcode_check_digit(body12):
    """13位条码最后1位校验码。
    采用标准 Mod10/GS1 风格交替权重：从最右侧开始权重3、1、3、1……。
    最终校验码 = (10 - 加权和 % 10) % 10。
    """
    body = str(body12)
    if len(body) != 12 or not body.isdigit():
        raise ValueError(f"条码主体必须是12位数字，当前：{body}")
    total = 0
    for pos, ch in enumerate(reversed(body)):
        total += int(ch) * (3 if pos % 2 == 0 else 1)
    return str((10 - total % 10) % 10)


def make_full_barcode(zdid, number, gx):
    body = f"{int(zdid):04d}{int(number):05d}{int(gx):03d}"
    return body + make_barcode_check_digit(body)


# ==================== 标签二维码（阶段 0）====================
# 扫码粒度是「一扎」(ZDNO + CC + ZH)，不含工序——工序由工人扫码后自选。
# CC 必须参与标识：实库存在 59 组 (ZDNO, ZH) 在不同 CC 下重复，省略会张冠李戴。
# 详见 docs/DECISIONS.md D-001 / D-002。

_BASE32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def gen_short_id(zdno, cc, zh):
    """把 (定单, 车间层, 扎号) 压成 8 位大写短 ID，用作二维码内容。

    - 无业务含义，纯索引；真正的对应关系存 jfzg_scan_map 表
    - 40bit 空间（约 1.1e12），按生日问题约 200 万条才有 50% 碰撞；
      服务端建唯一索引兜底（见 sql/V002__create_jfzg_scan_map.sql）
    - 参数顺序固定为 (zdno, cc, zh)，cc 不能省
    """
    if zdno is None or cc is None or zh is None:
        raise ValueError("生成短ID失败：zdno / cc / zh 都不能为空")
    raw = f"{str(zdno).strip()}|{int(cc)}|{int(zh)}"
    digest = hashlib.sha1(raw.encode("utf-8")).digest()[:5]   # 40 bit
    value = int.from_bytes(digest, "big")
    out = []
    for _ in range(8):
        out.append(_BASE32_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(out))


def build_qr_url(short_id, cfg):
    """拼出二维码内容。必须是 http(s) 地址，微信才能按网页打开。"""
    base = str((cfg or {}).get("label_qr_base_url", "") or "").strip().rstrip("/")
    if not base:
        return ""
    return f"{base}/s/{short_id}"


def _qr_opts():
    """标签二维码开关参数，供 build_barcode_items 调用。

    统一在这里判断，避免各处逻辑不一致导致「新建的标签有码、重打的模板没码」。
    关闭时返回空 base_url，build_barcode_items 就不会生成二维码。
    """
    try:
        cfg = load_barcode_config()
    except Exception:
        return {"cc": None, "qr_base_url": ""}
    if not cfg.get("label_qr_enabled", False):
        return {"cc": None, "qr_base_url": ""}
    return {"cc": None, "qr_base_url": str(cfg.get("label_qr_base_url", "") or "")}


def resolve_cc_for_zdno(zdno):
    """查该定单的车间层 CC。CC 只存在于 jfzd2，jfzd 表里没有。

    返回：
        int   —— 该定单只有唯一 CC，可安全用于二维码
        None  —— 查不到，或该定单跨多个 CC（实库 52/10295 个定单），
                 此时必须由用户在界面上指定，不能猜
    """
    try:
        conn = get_conn(conn_str, timeout=10)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT DISTINCT cc FROM jfzd2 WHERE zdno = ? ORDER BY cc", zdno)
            rows = [r[0] for r in cursor.fetchall() if r[0] is not None]
        finally:
            pass
    except Exception as e:
        print(f"查询定单 {zdno} 的车间层失败：{e}")
        return None
    if len(rows) == 1:
        return int(rows[0])
    if len(rows) > 1:
        print(f"定单 {zdno} 跨多个车间层 {rows}，二维码需在界面上指定车间层后才能生成")
    return None


def get_zdid_by_zdno(zdno):
    conn = get_conn(conn_str, timeout=10)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT TOP 1 ZDID FROM jfzd WHERE ZDNO = ?", zdno)
        row = cursor.fetchone()
        if not row:
            raise ValueError(f"找不到 ZDNO={zdno} 对应的 ZDID")
        return int(row[0])
    finally:
        pass


def get_print_processes(zdno):
    rows = get_jfdj_data(zdno)
    result = []
    for row in rows:
        result.append({
            "gx": int(row.gx),
            "gxname": str(row.gxname or ""),
            "dj": row.dj,
            "oksl": int(row.oksl or 0)
        })
    return result


def build_number_assignments(start_num, end_num, mode, color_data):
    """按照当前打菲程序已有的件数顺序，为每个编号分配颜色/尺码/数量。"""
    assignments = []
    if mode == "汇总":
        item = color_data[0] if color_data else {}
        total_qty = 0
        for row in color_data:
            try:
                pieces = int(row.get("pieces", 0) or 0)
                qty = int(row.get("quantity", 0) or 0)
                if pieces > 0 and qty > 0:
                    total_qty += pieces * qty
            except (ValueError, TypeError):
                pass
        for number in range(start_num, end_num + 1):
            assignments.append({
                "number": number,
                "color": str(item.get("color", "")),
                "size": "均码",
                "quantity": total_qty
            })
        return assignments

    # 与原 insert_data 中 size_list 的顺序保持一致，同时保留颜色
    expanded = []
    for item in color_data:
        pieces = int(item.get("pieces", 0) or 0)
        for _ in range(pieces):
            expanded.append({
                "color": str(item.get("color", "")),
                "size": str(item.get("size", "")),
                "quantity": int(item.get("quantity", 0) or 0)
            })

    for idx, number in enumerate(range(start_num, end_num + 1)):
        item = expanded[idx] if idx < len(expanded) else {"color": "", "size": "", "quantity": 0}
        assignments.append({"number": number, **item})
    return assignments


def build_barcode_items(zdno, zdid, start_num, end_num, mode, color_data, processes, style="", cc=None, qr_base_url=""):
    assignments = build_number_assignments(start_num, end_num, mode, color_data)

    # 汇总打印：一个编号范围只打印一份工序集合。
    # 条码中的“编号”按用户要求取界面开始编号，不再把每个编号都循环一遍。
    if mode == "汇总":
        assignments = assignments[:1]

    items = []
    print_date = datetime.now().strftime("%Y-%m-%d")
    # 标签二维码：标识一扎 (zdno, cc, number)。cc 为空或跨车间时不出二维码，
    # 宁可不出，也不出一个指错扎的码。
    for assignment in assignments:
        short_id = ""
        qr_text = ""
        if qr_base_url:
            zh_value = int(assignment["number"])
            use_cc = cc if cc is not None else resolve_cc_for_zdno(zdno)
            if use_cc is not None:
                try:
                    short_id = gen_short_id(zdno, use_cc, zh_value)
                    qr_text = build_qr_url(short_id, {"label_qr_base_url": qr_base_url})
                except Exception as e:
                    print(f"生成标签二维码失败（zdno={zdno} cc={use_cc} zh={zh_value}）：{e}")
                    short_id, qr_text = "", ""
            else:
                print(f"定单 {zdno} 未确定唯一车间层，跳过二维码（扎号 {zh_value}）")
        for proc in processes:
            items.append({
                "zdno": zdno,
                "zdid": int(zdid),
                "number": int(assignment["number"]),
                "gx": int(proc["gx"]),
                "gxname": proc["gxname"],
                "color": assignment.get("color", ""),
                "size": assignment.get("size", ""),
                "quantity": int(assignment.get("quantity", 0) or 0),
                "style": style,
                "mode": mode,
                "start_num": int(start_num),
                "end_num": int(end_num),
                # 汇总模式：界面“数量”填写的是整批总数量，绝对不能再乘手数。
                # 例如：10手、总数量500件 -> 标签显示500件，而不是5000件。
                # 明细模式不使用 summary_quantity。
                "summary_quantity": (
                    int(assignment.get("quantity", 0) or 0)
                    if mode == "汇总" else 0
                ),
                "qr_color_data": [dict(x) for x in color_data],
                "print_date": print_date,
                "barcode": make_full_barcode(zdid, assignment["number"], proc["gx"]),
                "cc": cc,
                "short_id": short_id,
                "qr_text": qr_text,
            })
    return items


def _safe_text(value):
    return str(value or "").replace("\n", " ").strip()



# ==================== 汇总二维码网页服务 ====================
_QR_HTTP_SERVER = None
_QR_HTTP_THREAD = None

def _get_local_lan_ip():
    """获取打印电脑在局域网中的IPv4地址。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return "127.0.0.1"


def _escape_html(text):
    import html
    return html.escape(str(text or ""), quote=True)


def _start_qr_http_server(port=8765):
    """启动一个只提供汇总信息网页的轻量HTTP服务。"""
    global _QR_HTTP_SERVER, _QR_HTTP_THREAD
    try:
        port = int(port)
    except Exception:
        port = 8765

    if _QR_HTTP_SERVER is not None:
        return _QR_HTTP_SERVER.server_address[1]

    class QRHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path.rstrip("/") not in ("/", "/summary"):
                    self.send_error(404)
                    return
                q = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                zdno = q.get("zdno", [""])[0]
                color = q.get("color", [""])[0]
                qty = q.get("qty", [""])[0]
                date = q.get("date", [""])[0]

                html_page = f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>裁剪汇总信息</title>
<style>
body{{font-family:"Microsoft YaHei","Noto Sans CJK SC",Arial,sans-serif;background:#f5f6f8;margin:0;padding:24px;color:#222}}
.card{{max-width:520px;margin:20px auto;background:#fff;border-radius:14px;padding:24px;box-shadow:0 3px 16px rgba(0,0,0,.10)}}
h2{{margin:0 0 20px;font-size:22px}}
.row{{padding:12px 0;border-bottom:1px solid #eee;font-size:17px;line-height:1.5}}
.row:last-child{{border-bottom:0}}
.label{{display:inline-block;width:72px;color:#666}}
.value{{font-weight:600;word-break:break-all}}
.tip{{margin-top:18px;color:#999;font-size:13px;text-align:center}}
</style></head><body>
<div class="card">
<h2>裁剪汇总信息</h2>
<div class="row"><span class="label">制单</span><span class="value">{_escape_html(zdno)}</span></div>
<div class="row"><span class="label">颜色</span><span class="value">{_escape_html(color)}</span></div>
<div class="row"><span class="label">数量</span><span class="value">{_escape_html(qty)}</span></div>
<div class="row"><span class="label">日期</span><span class="value">{_escape_html(date)}</span></div>
<div class="tip">由打菲程序生成 · 微信扫码即可查看</div>
</div></body></html>"""
                data = html_page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                try:
                    self.send_error(500, str(e))
                except Exception:
                    pass

        def log_message(self, fmt, *args):
            return

    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), QRHandler)
    except OSError:
        # 端口被占用时，让系统自动分配一个空闲端口
        server = ThreadingHTTPServer(("0.0.0.0", 0), QRHandler)
    _QR_HTTP_SERVER = server
    _QR_HTTP_THREAD = threading.Thread(target=server.serve_forever, daemon=True)
    _QR_HTTP_THREAD.start()
    return server.server_address[1]


def make_summary_qr_url(lines_raw, cfg, detail_lines=None, summary_line=""):
    """生成微信可打开的二维码地址；明细和最终汇总一并编码进二维码。"""
    base_url = str(cfg.get("summary_qr_base_url", "") or "").strip()
    port = int(cfg.get("summary_qr_server_port", 8765) or 8765)
    if base_url:
        base_url = base_url.rstrip("/")
    else:
        actual_port = _start_qr_http_server(port)
        base_url = f"http://{_get_local_lan_ip()}:{actual_port}"
    values = {}
    for line in lines_raw:
        if "：" in line:
            k, v = line.split("：", 1)
            values[k] = v
    query = urllib.parse.urlencode({
        "zdno": values.get("制单", ""),
        "color": values.get("颜色", ""),
        "qty": values.get("数量", ""),
        "date": values.get("日期", ""),
        "detail": "\n".join(detail_lines or []),
        "summary": summary_line,
    })
    return f"{base_url}/summary?{query}"


def create_barcode_pdf(items, template_name, cfg=None, output_path=None):
    """生成 A4 双列不干胶条码 PDF。

    版式：
    - A4 210×297mm
    - 物理尺寸模式：PDF页面严格按A4尺寸生成，不做自动缩放
    - 左右两列，默认86mm + 38mm + 86mm = 210mm
    - 顶部/底部默认各8mm，剩余高度严格平均为19行
    - 明细：多工序时同一编号工序左右对应；只有一个工序时编号左右交替
    - 汇总：只打印一套工序，不按编号范围重复
    - 中间39mm区域每页只显示一次裁剪信息，采用大字体并允许自动换行
    """
    cfg = cfg or load_barcode_config()
    page_w = float(cfg["page_width_mm"]) * mm
    page_h = float(cfg["page_height_mm"]) * mm
    label_w = float(cfg["label_width_mm"]) * mm
    label_h = 0  # physical mode placeholder
    cols = int(cfg.get("columns", 2))
    rows = int(cfg.get("rows", 19))
    gap = float(cfg.get("center_gap_mm", 39.0)) * mm
    h_off = float(cfg.get("horizontal_offset_mm", 0.0)) * mm
    first_col_off = float(cfg.get("first_column_offset_mm", 0.0)) * mm
    v_off = float(cfg.get("vertical_offset_mm", 0.0)) * mm

    if cols != 2:
        raise ValueError("当前条码模板固定为左右双列，请将列数设置为2。")
    if rows <= 0:
        raise ValueError("打印行数必须大于0。")

    # 纵向严格按照实际不干胶纸：顶部/底部留白后，剩余高度平均分成19行。
    # 默认 A4 297mm，上下各8mm： (297-8-8)/19 = 14.78947mm/行。
    top_margin = float(cfg.get("top_margin_mm", 8.0)) * mm
    bottom_margin = float(cfg.get("bottom_margin_mm", 8.0)) * mm
    available_h = page_h - top_margin - bottom_margin
    if available_h <= 0:
        raise ValueError("上下留白设置错误：有效打印高度必须大于0。")
    actual_row_h = available_h / rows
    label_h = actual_row_h
    y_top = top_margin

    # 两列基础位置：保留原有“整体横向偏移”，并允许第一列单独微调。
    # first_column_offset_mm 只影响第一列，不影响第二列。
    base_left_x = float(cfg.get("left_margin_mm", 0.0)) * mm + h_off
    x_positions = [base_left_x + first_col_off, base_left_x + label_w + gap]

    if output_path is None:
        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "条码打印文件")
        os.makedirs(out_dir, exist_ok=True)
        output_path = os.path.join(out_dir, f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{template_name}.pdf")
    else:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    c = canvas.Canvas(output_path, pagesize=(page_w, page_h))
    c.setTitle(f"{template_name} 条码打印")

    def _is_cjk(ch):
        """判断是否使用中文 CID 字体。中文、中文标点及全角字符走 STSong-Light；
        英文字母、数字和半角符号走 Helvetica，避免出现 W M 1105 这种字间距。"""
        code = ord(ch)
        return (
            0x2E80 <= code <= 0x9FFF or
            0xF900 <= code <= 0xFAFF or
            0xFF00 <= code <= 0xFFEF
        )

    def _font_for_char(ch):
        return PDF_FONT if _is_cjk(ch) else "Helvetica"

    def mixed_runs(text):
        """把一段文字自动拆成中文/英文数字字体片段。
        例如 WM1105 -> Helvetica 整段绘制；制单：WM1105 -> 中文 + Helvetica。"""
        text = _safe_text(text)
        if not text:
            return []
        runs = []
        current_font = _font_for_char(text[0])
        current = text[0]
        for ch in text[1:]:
            font = _font_for_char(ch)
            if font == current_font:
                current += ch
            else:
                runs.append((current, current_font))
                current = ch
                current_font = font
        runs.append((current, current_font))
        return runs

    def mixed_text_width(text, font_size):
        return sum(pdfmetrics.stringWidth(part, font, font_size) for part, font in mixed_runs(text))

    def draw_mixed_string(x, y, text, font_size, align="left", bold=False):
        """在同一行混合使用中文字体和英文字体；bold=True 时用轻微叠印模拟加粗。"""
        runs = mixed_runs(text)
        if not runs:
            return
        total_w = sum(pdfmetrics.stringWidth(part, font, font_size) for part, font in runs)
        if align == "center":
            cursor_x = x - total_w / 2
        elif align == "right":
            cursor_x = x - total_w
        else:
            cursor_x = x
        # STSong-Light 本身没有 Bold CID 字体，因此采用 0.22pt 轻微叠印，
        # 中文和英文/数字都能保持原来的字体，同时获得明显的加粗效果。
        offsets = (0.0, 0.22) if bold else (0.0,)
        for part, font in runs:
            width = pdfmetrics.stringWidth(part, font, font_size)
            for dx in offsets:
                c.setFont(font, font_size)
                c.drawString(cursor_x + dx, y, part)
            cursor_x += width

    def fit_font(text, max_width, start_size, min_size=4.2):
        text = _safe_text(text)
        size = float(start_size)
        while size > min_size:
            if mixed_text_width(text, size) <= max_width:
                return size
            size -= 0.2
        return min_size

    def wrap_text(text, max_width, font_size):
        """按实际中英文字体宽度自动换行，保证不超过中间区域。"""
        text = _safe_text(text)
        if not text:
            return [""]
        lines, current = [], ""
        for ch in text:
            test = current + ch
            if mixed_text_width(test, font_size) <= max_width:
                current = test
            else:
                if current:
                    lines.append(current)
                current = ch
        if current:
            lines.append(current)
        return lines or [""]

    # ---- 中间区域信息：每页只显示一次 ----
    first = items[0] if items else {}
    all_colors, seen_colors, quantities, numbers = [], set(), [], []
    # 汇总模式的颜色也从UI尺码数据读取，避免只显示第一行颜色。
    color_source = first.get("qr_color_data") or []
    if _safe_text(first.get("mode")) == "汇总" and color_source:
        for row in color_source:
            color = _safe_text(row.get("color"))
            if color and color not in seen_colors:
                seen_colors.add(color)
                all_colors.append(color)
    for item in items:
        color = _safe_text(item.get("color"))
        if color and color not in seen_colors:
            seen_colors.add(color)
            all_colors.append(color)
        try:
            quantities.append(int(item.get("quantity", 0) or 0))
        except Exception:
            pass
        try:
            numbers.append(int(item.get("number")))
        except Exception:
            pass

    zdno = _safe_text(first.get("zdno"))
    colors_text = "/".join(all_colors) if all_colors else ""
    start_num = first.get("start_num")
    end_num = first.get("end_num")
    try:
        hands = int(end_num) - int(start_num) + 1
    except Exception:
        hands = len(set(numbers)) if numbers else 0
    if hands <= 0:
        hands = 1

    unique_qty = sorted(set(q for q in quantities if q > 0))
    by_number = {}
    for item in items:
        try:
            n = int(item.get("number"))
            q = int(item.get("quantity", 0) or 0)
            by_number.setdefault(n, q)
        except Exception:
            pass
    total_qty = sum(by_number.values())

    # 汇总模式的 quantity 本身就是用户填写的“整批总数量”。
    # 不能按手数再次相乘。
    item_mode = _safe_text(first.get("mode"))
    if item_mode == "汇总":
        summary_total = int(first.get("summary_quantity", first.get("quantity", 0)) or 0)
        qty_line = f"数量：{summary_total}件"
    elif len(unique_qty) == 1 and unique_qty[0] > 0:
        qty_line = f"数量：{hands}手×{unique_qty[0]}件={hands * unique_qty[0]}件"
    elif total_qty > 0:
        qty_line = f"数量：{hands}手×多规格={total_qty}件"
    else:
        qty_line = f"数量：{hands}手"

    print_date = _safe_text(first.get("print_date")) or datetime.now().strftime("%Y-%m-%d")
    center_x = (x_positions[0] + label_w + x_positions[1]) / 2
    center_width = max(1 * mm, gap - 2.0 * mm)

    def draw_center_once():
        # 中间38mm区域每页只画一次。二维码仍使用原来的四个字段，
        # 打印纸上的文字则增加“明细”以及最终汇总。
        base_size = float(cfg.get("summary_qr_text_font_size", cfg.get("center_font_size", 10.0)))
        detail_size = min(base_size, 9.5)
        line_leading = float(cfg.get("center_line_leading", 6.0)) * mm

        qr_lines_raw = [
            f"制单：{zdno}",
            f"颜色：{colors_text}",
            qty_line,
            f"日期：{print_date}",
        ]

        # 二维码明细统一从UI尺码数据生成。
        # 汇总模式：每一行“尺码 + 件数 + 每手数量”直接生成明细；
        # 总数量 = Σ(件数 × 每手数量)，不读取顶部数量框。
        detail_groups = {}
        if item_mode == "汇总":
            source_rows = first.get("qr_color_data") or []
            detail_lines = []
            detail_total_hands = 0
            detail_total_qty = 0
            for row in source_rows:
                size_i = _safe_text(row.get("size")) or "均码"
                color_i = _safe_text(row.get("color"))
                try:
                    hands_i = int(row.get("pieces", 0) or 0)
                    q_i = int(row.get("quantity", 0) or 0)
                except (ValueError, TypeError):
                    continue
                if hands_i <= 0 or q_i <= 0:
                    continue
                amount_i = hands_i * q_i
                detail_total_hands += hands_i
                detail_total_qty += amount_i
                prefix_i = f"{color_i} / " if color_i else ""
                detail_lines.append(f"{prefix_i}{size_i}：{hands_i}手×{q_i}件={amount_i}件")
        else:
            for item in items:
                size_i = _safe_text(item.get("size")) or "均码"
                try:
                    n_i = int(item.get("number"))
                except Exception:
                    continue
                try:
                    q_i = int(item.get("quantity", 0) or 0)
                except Exception:
                    q_i = 0
                key = (size_i, q_i)
                detail_groups.setdefault(key, set()).add(n_i)

            detail_lines = []
            detail_total_hands = 0
            detail_total_qty = 0
            for (size_i, q_i), num_set in detail_groups.items():
                hands_i = len(num_set)
                if hands_i <= 0:
                    continue
                amount_i = hands_i * q_i
                detail_total_hands += hands_i
                detail_total_qty += amount_i
                detail_lines.append(f"{size_i}：{hands_i}手×{q_i}件={amount_i}件")

            if not detail_lines and hands > 0:
                q_fallback = unique_qty[0] if len(unique_qty) == 1 and unique_qty[0] > 0 else 0
                detail_total_hands = hands
                detail_total_qty = hands * q_fallback if q_fallback > 0 else total_qty
                detail_lines.append(
                    f"{_safe_text(first.get('size')) or '均码'}：{hands}手×{q_fallback}件={detail_total_qty}件"
                    if q_fallback > 0 else f"{_safe_text(first.get('size')) or '均码'}：{hands}手={detail_total_qty}件"
                )

        # 明细最后再加一个加粗汇总。
        summary_line = f"汇总：{detail_total_hands}手，共{detail_total_qty}件"

        if cfg.get("summary_qr_enabled", True):
            qr_text = make_summary_qr_url(qr_lines_raw, cfg, detail_lines=detail_lines, summary_line=summary_line)
            qr_size = float(cfg.get("summary_qr_size_mm", 25.0)) * mm
            qr_gap = float(cfg.get("summary_qr_text_gap_mm", cfg.get("summary_qr_gap_mm", 3.0))) * mm
            top_distance = float(cfg.get("center_top_distance_mm", 137.0)) * mm
            qr_top = page_h - top_distance
            qr_left = center_x - qr_size / 2
            qr_bottom = qr_top - qr_size
            try:
                qr = QrCodeWidget(qr_text)
                bounds = qr.getBounds()
                bw = bounds[2] - bounds[0]
                bh = bounds[3] - bounds[1]
                d = Drawing(qr_size, qr_size, transform=[qr_size / bw, 0, 0, qr_size / bh, -bounds[0] * qr_size / bw, -bounds[1] * qr_size / bh])
                d.add(qr)
                renderPDF.draw(d, c, qr_left, qr_bottom)
            except Exception as e:
                print(f"绘制汇总二维码失败：{e}")
            text_top_y = qr_bottom - qr_gap
        else:
            top_distance = float(cfg.get("center_top_distance_mm", 137.0)) * mm
            text_top_y = page_h - top_distance

        # 标签名单独一行并加粗；字段值正常字体。
        center_sections = [
            ("制单：", zdno, True, False),
            ("颜色：", colors_text, True, False),
            ("数量：", qty_line.replace("数量：", "", 1), True, False),
            ("日期：", print_date, True, False),
        ]

        # 逐行绘制，确保窄的38mm中间区域自动换行。
        start_y = text_top_y - line_leading * 0.20
        for label_text, value_text, label_bold, _ in center_sections:
            for line in wrap_text(label_text, center_width, base_size):
                draw_mixed_string(center_x, start_y, line, base_size, align="center", bold=label_bold)
                start_y -= line_leading * 0.78
            for line in wrap_text(value_text, center_width, base_size):
                draw_mixed_string(center_x, start_y, line, base_size, align="center")
                start_y -= line_leading * 0.78
            start_y -= line_leading * 0.18

        # 明细不在纸张中间打印；完整明细和最终汇总已集成到二维码，扫码后网页展示。

    def draw_label(x, y, item):
        if not item:
            return
        pad = 1.8 * mm
        title_size = float(cfg.get("title_font_size", 7.2))
        body_size = float(cfg.get("body_font_size", 7.0))
        bc_text_size = float(cfg.get("barcode_text_font_size", 5.5))

        zdno_i = _safe_text(item.get("zdno"))
        gx = _safe_text(item.get("gx"))
        gxname = _safe_text(item.get("gxname"))
        number = _safe_text(item.get("number"))
        color = _safe_text(item.get("color"))
        size = _safe_text(item.get("size"))
        # 汇总模板显示整批总数量，而不是单手数量。
        if _safe_text(item.get("mode")) == "汇总" or "汇总" in _safe_text(template_name):
            qty_value = item.get("summary_quantity", item.get("quantity", 0))
        else:
            qty_value = item.get("quantity", 0)
        qty = _safe_text(qty_value)
        barcode_value = _safe_text(item.get("barcode"))

        title = f"{zdno_i}    工序 {gx}    {number} 号    {qty} 件"
        if size:
            title += f"    {size}"

        # 标签二维码：画在左下角，并把标题/工序名整体右移避让。
        # 条码 34mm 居中后左右各留约 26mm，13mm 二维码只占左侧一小块，不会碰到条码。
        qr_text = _safe_text(item.get("qr_text"))
        qr_size = float(cfg.get("label_qr_size_mm", 13.0)) * mm
        qr_reserved = 0.0
        if qr_text and float(cfg.get("label_qr_size_mm", 13.0)) > 0:
            qr_reserved = qr_size + 2 * mm

        text_left = x + pad + qr_reserved
        text_w = label_w - 2 * pad - qr_reserved
        fs = fit_font(title, text_w, title_size, 5.2)
        draw_mixed_string(text_left, y + actual_row_h - 4.1 * mm, title, fs, align="left")

        draw_mixed_string(text_left, y + actual_row_h - 8.0 * mm, gxname[:18], body_size, align="left")
        if color:
            draw_mixed_string(x + label_w * 4 / 5 + 2 * mm , y + actual_row_h - 8.0 * mm, color[:8], body_size, align="right")

        try:
            target_w = float(cfg.get("barcode_width_mm", 34.0)) * mm
            bh = float(cfg.get("barcode_height_mm", 7.0)) * mm
            natural_bar_width = max(0.15 * mm, target_w / max(len(barcode_value) * 11.0, 1))
            barcode = code128.Code128(barcode_value, barHeight=bh, barWidth=natural_bar_width)
            max_w = label_w - 2 * pad
            if barcode.width > max_w:
                barcode = code128.Code128(
                    barcode_value, barHeight=bh,
                    barWidth=max(0.12 * mm, max_w / max(len(barcode_value) * 11.0, 1))
                )
            bx = x + (label_w - barcode.width) / 2
            by = y + 3.2 * mm
            barcode.drawOn(c, bx, by)
        except Exception as e:
            draw_mixed_string(x + pad, y + 1.5 * mm, f"条码错误: {e}", 5, align="left")

        if cfg.get("show_barcode_text", False):
            c.setFont("Helvetica", bc_text_size)
            c.drawCentredString(x + label_w / 2, y + 0.3 * mm, barcode_value)

        # 标签二维码：左下角，与条码并存。画失败只提示，不影响条码打印。
        if qr_text and qr_reserved > 0:
            try:
                qr = QrCodeWidget(qr_text)
                bounds = qr.getBounds()
                bw = bounds[2] - bounds[0]
                bh = bounds[3] - bounds[1]
                d = Drawing(qr_size, qr_size, transform=[
                    qr_size / bw, 0, 0, qr_size / bh,
                    -bounds[0] * qr_size / bw, -bounds[1] * qr_size / bh])
                d.add(qr)
                renderPDF.draw(d, c, x + pad, y + 0.8 * mm)
            except Exception as e:
                print(f"绘制标签二维码失败：{e}")

    grouped, order = {}, []
    for item in items:
        key = int(item.get("number", 0))
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(item)

    is_summary_template = "汇总" in str(template_name)
    row_groups = []
    if is_summary_template:
        # 汇总模板：只取第一手的工序集合，一套工序打印一次。
        if order:
            procs = grouped[order[0]]
            for i in range(0, len(procs), 2):
                row_groups.append(procs[i:i + 2])
    else:
        one_process_each = bool(grouped) and all(len(v) == 1 for v in grouped.values())
        if one_process_each:
            flat = [grouped[n][0] for n in order]
            for i in range(0, len(flat), 2):
                row_groups.append(flat[i:i + 2])
        else:
            for number in order:
                procs = grouped[number]
                for i in range(0, len(procs), 2):
                    row_groups.append(procs[i:i + 2])

    per_page = rows
    for page_start in range(0, len(row_groups), per_page):
        page_groups = row_groups[page_start:page_start + per_page]
        for row, row_items in enumerate(page_groups):
            y = page_h - y_top - (row + 1) * actual_row_h + v_off
            if row_items:
                draw_label(x_positions[0], y, row_items[0])
            if len(row_items) > 1:
                draw_label(x_positions[1], y, row_items[1])
        # 每页中间只显示一个裁剪信息块，不再每行重复。
        draw_center_once()
        c.showPage()

    if not row_groups:
        draw_center_once()
        c.showPage()
    c.save()
    return output_path


def open_file_default(path):
    path = os.path.abspath(path)
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception as e:
        messagebox.showerror(
            "无法打开PDF",
            f"系统没有找到可以打开PDF的程序：\n{e}\n\nPDF文件位置：\n{path}"
        )
        return False


def get_windows_printers():
    """读取Windows已安装打印机列表。优先使用pywin32；没有pywin32时返回空列表。"""
    if not sys.platform.startswith("win"):
        return []
    try:
        import win32print
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        printers = win32print.EnumPrinters(flags)
        names = []
        for p in printers:
            name = p[2]
            if name and name not in names:
                names.append(name)
        return names
    except Exception:
        return []


def get_default_windows_printer():
    if not sys.platform.startswith("win"):
        return ""
    try:
        import win32print
        return win32print.GetDefaultPrinter()
    except Exception:
        return ""


def find_sumatra_pdf():
    """寻找SumatraPDF。也支持把SumatraPDF.exe放在程序同目录。"""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "SumatraPDF.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "SumatraPDF", "SumatraPDF.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "SumatraPDF", "SumatraPDF.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "SumatraPDF", "SumatraPDF.exe"),
    ]
    for exe in candidates:
        if exe and os.path.isfile(exe):
            return exe
    return None


def print_pdf_windows(path, printer_name=None):
    """直接打印PDF到用户选择的Windows打印机，不打开PDF阅读器。

    优先使用SumatraPDF的-print-to，可精确指定打印机且不依赖PDF默认关联。
    若没有SumatraPDF，提示用户检查打印组件。
    """
    if not sys.platform.startswith("win"):
        raise RuntimeError("直接打印按钮目前针对Windows设计")

    path = os.path.abspath(path)
    printer_name = (printer_name or get_default_windows_printer()).strip()
    if not printer_name:
        raise RuntimeError("没有获取到Windows打印机，请安装pywin32后重试。")

    exe = find_sumatra_pdf()
    if exe:
        cmd = [exe, "-print-to", printer_name, "-print-settings", "noscale", "-silent", path]
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=60
        )
        if result.returncode == 0:
            return True, f"已发送到打印机：{printer_name}"
        err = result.stderr.decode(errors="ignore").strip()
        raise RuntimeError(f"SumatraPDF打印失败（返回码{result.returncode}）：{err or '未知错误'}")

    raise RuntimeError("未找到 SumatraPDF。物理尺寸打印模式要求使用 SumatraPDF 的 noscale（100%原始尺寸）打印，请将 SumatraPDF.exe 放到本程序同目录，或安装到 Program Files。")

class BarcodePreviewWindow:
    """PDF预览窗口：选择打印机后直接打印，不需要打开PDF。"""
    def __init__(self, parent, pdf_path, template_name, on_print=None):
        self.parent = parent
        self.pdf_path = pdf_path
        self.template_name = template_name
        self.on_print = on_print
        self.page_index = 0
        self.doc = None
        self.photo = None

        self.win = tk.Toplevel(parent)
        self.win.title(f"条码打印预览 - {template_name}")
        self.win.geometry("1100x850")
        self.win.minsize(850, 650)
        self.win.transient(parent)

        top = ttk.Frame(self.win, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text=f"打印模板：{template_name}", font=("Arial", 11, "bold")).pack(side="left")
        self.page_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.page_var).pack(side="left", padx=15)

        # 预览窗口直接选择Windows打印机。
        ttk.Label(top, text="打印机：").pack(side="left", padx=(15, 3))
        self.printer_var = tk.StringVar()
        printers = get_windows_printers()
        default_printer = get_default_windows_printer()
        self.printer_combo = ttk.Combobox(top, textvariable=self.printer_var, values=printers, width=34, state="readonly")
        self.printer_combo.pack(side="left", padx=3)
        if default_printer and default_printer in printers:
            self.printer_var.set(default_printer)
        elif printers:
            self.printer_var.set(printers[0])
        else:
            self.printer_var.set(default_printer or "未检测到打印机")

        ttk.Button(top, text="上一页", command=self.prev_page).pack(side="right", padx=2)
        ttk.Button(top, text="下一页", command=self.next_page).pack(side="right", padx=2)
        ttk.Button(top, text="📄 打开PDF", command=lambda: open_file_default(self.pdf_path)).pack(side="right", padx=8)
        self.print_btn = ttk.Button(top, text="🖨 选择打印机并打印", command=self.do_print)
        self.print_btn.pack(side="right", padx=2)

        self.canvas = tk.Canvas(self.win, background="#777777", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=8, pady=8)
        self.canvas.bind("<Configure>", lambda e: self.render_page())

        try:
            if pymupdf is None:
                raise ImportError("未安装 PyMuPDF，请运行：pip install pymupdf")
            self.doc = pymupdf.open(self.pdf_path)
        except Exception as e:
            messagebox.showerror("预览失败", f"无法打开PDF预览。\n请安装 PyMuPDF：pip install pymupdf\n\n{e}", parent=self.win)
            open_file_default(self.pdf_path)
            return
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.render_page()

    def render_page(self):
        if not self.doc or self.page_index >= len(self.doc):
            return
        try:
            page = self.doc.load_page(self.page_index)
            rect = page.rect
            cw = max(self.canvas.winfo_width() - 20, 300)
            ch = max(self.canvas.winfo_height() - 20, 400)
            scale = min(cw / rect.width, ch / rect.height)
            scale = max(scale, 0.1)
            pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            from PIL import Image, ImageTk
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            self.photo = ImageTk.PhotoImage(img)
            self.canvas.delete("all")
            self.canvas.create_image(cw / 2 + 10, ch / 2 + 10, image=self.photo, anchor="center")
            self.page_var.set(f"第 {self.page_index + 1} / {len(self.doc)} 页")
        except Exception as e:
            self.page_var.set(f"预览错误：{e}")

    def prev_page(self):
        if self.doc and self.page_index > 0:
            self.page_index -= 1
            self.render_page()

    def next_page(self):
        if self.doc and self.page_index + 1 < len(self.doc):
            self.page_index += 1
            self.render_page()

    def do_print(self):
        printer = self.printer_var.get().strip()
        if not printer or printer == "未检测到打印机":
            messagebox.showwarning("打印机", "没有检测到可用打印机。\n\n请先安装Windows打印机，并安装pywin32：\npip install pywin32", parent=self.win)
            return
        if self.on_print:
            self.on_print(self.pdf_path, printer)

    def close(self):
        try:
            if self.doc:
                self.doc.close()
        except Exception:
            pass
        self.win.destroy()


# ==================== GUI组件类 ====================

class AutocompleteCombobox(ttk.Combobox):
    """支持回车触发的下拉框"""
    def __init__(self, master=None, completevalues=None, **kwargs):
        self.completevalues = completevalues or []
        super().__init__(master, **kwargs)
        self.bind('<KeyRelease>', self.on_key_release)
        self.bind('<<ComboboxSelected>>', self.on_select)
        self.bind('<Return>', self.on_enter)
        self._current_value = ''
        self._search_timer = None
        self._typing = False
    
    def set_values(self, values):
        self.completevalues = values
        self['values'] = values
    
    def on_key_release(self, event):
        if event.keysym in ('Up', 'Down', 'Left', 'Right', 'Shift_L', 'Shift_R', 
                            'Control_L', 'Control_R', 'Alt_L', 'Alt_R', 'Tab', 'Return'):
            return
        value = self.get()
        if self._search_timer:
            self.after_cancel(self._search_timer)
            self._search_timer = None
        self._search_timer = self.after(500, self._do_search)
    
    def on_enter(self, event):
        self._do_search()
        return 'break'
    
    def _do_search(self):
        value = self.get()
        if self._search_timer:
            self.after_cancel(self._search_timer)
            self._search_timer = None
        
        if value == '':
            self['values'] = self.completevalues
        else:
            matches = [item for item in self.completevalues if value.lower() in item.lower()]
            self['values'] = matches
        
        if len(self['values']) == 1:
            self.set(self['values'][0])
            self.icursor(tk.END)
            self.event_generate('<<ComboboxSelected>>')
        elif self['values']:
            self.event_generate('<Down>')
        else:
            self['values'] = self.completevalues
    
    def on_select(self, event):
        self._current_value = self.get()


class EditableTreeview(ttk.Treeview):
    """支持双击编辑单元格的 Treeview"""
    COLUMN_NAMES = ('gx', 'gxname', 'dj', 'mdate', 'oksl')

    def __init__(self, master, app=None, **kwargs):
        super().__init__(master, **kwargs)
        self.app = app
        self._edit_entry = None
        self._edit_item = None
        self._edit_col = None
        self.bind('<Double-Button-1>', self.on_double_click)

    def on_double_click(self, event):
        if self._edit_entry:
            self.finish_edit()
        region = self.identify_region(event.x, event.y)
        if region != "cell":
            return
        column_id = self.identify_column(event.x)
        item = self.identify_row(event.y)
        if not item or not column_id:
            return
        col_index = int(column_id.replace('#', '')) - 1
        if col_index < 0 or col_index >= len(self.COLUMN_NAMES):
            return
        self.see(item)
        self.update_idletasks()
        bbox = self.bbox(item, column_id)
        if not bbox:
            return
        values = list(self.item(item, 'values'))
        while len(values) < len(self.COLUMN_NAMES):
            values.append('')
        current_value = values[col_index]
        x, y, width, height = bbox
        self._edit_item = item
        self._edit_col = col_index
        self._edit_entry = ttk.Entry(self)
        self._edit_entry.place(x=x, y=y, width=width, height=height)
        self._edit_entry.insert(0, current_value)
        self._edit_entry.select_range(0, tk.END)
        self._edit_entry.focus_set()
        self._edit_entry.bind('<Return>', lambda e: self.finish_edit())
        self._edit_entry.bind('<Escape>', lambda e: self.cancel_edit())
        self._edit_entry.bind('<FocusOut>', lambda e: self.after_idle(self.finish_edit))

    def finish_edit(self):
        if not self._edit_entry or not self._edit_item or self._edit_col is None:
            return
        item = self._edit_item
        col_index = self._edit_col
        new_value = self._edit_entry.get().strip()
        self._clear_editor()
        values = list(self.item(item, 'values'))
        while len(values) < len(self.COLUMN_NAMES):
            values.append('')
        values[col_index] = new_value
        self.item(item, values=values)
        if self.app:
            self.app.mark_template_modified()

    def cancel_edit(self):
        self._clear_editor()

    def _clear_editor(self):
        if self._edit_entry:
            self._edit_entry.destroy()
        self._edit_entry = None
        self._edit_item = None
        self._edit_col = None

    def get_data(self):
        if self._edit_entry:
            self.finish_edit()
        data = []
        for item in self.get_children():
            values = list(self.item(item, 'values'))
            while len(values) < len(self.COLUMN_NAMES):
                values.append('')
            data.append(values)
        return data

    def clear_data(self):
        self.cancel_edit()
        for item in self.get_children():
            self.delete(item)


# ==================== 库存入库对话框 ====================

class StockInDialog:
    """纯入库对话框 - 调用存储过程入库"""
    def __init__(self, parent, app, color_data, style, mode):
        self.parent = parent
        self.app = app
        self.color_data = color_data  # 裁剪数据
        self.style = style
        self.mode = mode
        self.result = None
        
        self.dialog = tk.Toplevel(parent)
        self.dialog.title("裁剪数据入库")
        self.dialog.geometry("650x500")
        self.dialog.transient(parent)
        self.dialog.grab_set()
        
        main_frame = ttk.Frame(self.dialog, padding="10")
        main_frame.pack(fill='both', expand=True)
        
        # 数据预览
        preview_frame = ttk.LabelFrame(main_frame, text="📋 裁剪数据预览", padding="5")
        preview_frame.pack(fill='both', expand=True, pady=5)
        
        preview_text = tk.Text(preview_frame, height=8, wrap='none')
        preview_text.pack(fill='both', expand=True)
        
        preview_text.insert('1.0', f"款式: {style}\n")
        preview_text.insert('end', f"模式: {mode}\n")
        preview_text.insert('end', f"\n尺码明细:\n")
        total_qty = 0
        for item in color_data:
            qty = item.get('quantity', 0)
            pieces = item.get('pieces', 1)
            total_qty += qty * pieces
            preview_text.insert('end', f"  {item.get('color', '')} | {item.get('size', '')} | {qty}件\n")
        preview_text.insert('end', f"\n{'='*30}\n")
        preview_text.insert('end', f"总数量: {total_qty} 件")
        preview_text.config(state='disabled')
        
        # 入库设置
        setting_frame = ttk.LabelFrame(main_frame, text="⚙️ 入库设置", padding="5")
        setting_frame.pack(fill='x', pady=5)
        
        # 目标货号
        row1 = ttk.Frame(setting_frame)
        row1.pack(fill='x', pady=2)
        ttk.Label(row1, text="目标货号 (入库):", width=14).pack(side='left')
        self.target_entry = ttk.Entry(row1, width=20)
        self.target_entry.pack(side='left', padx=2)
        self.target_entry.bind('<Return>', lambda e: self.search_target())
        ttk.Button(row1, text="🔍 查询", command=self.search_target).pack(side='left', padx=2)
        self.target_combo = ttk.Combobox(row1, width=20)
        self.target_combo.pack(side='left', padx=2)
        self.target_combo.bind('<<ComboboxSelected>>', self.on_target_selected)
        
        # 单价和操作人
        row2 = ttk.Frame(setting_frame)
        row2.pack(fill='x', pady=2)
        ttk.Label(row2, text="单价:", width=14).pack(side='left')
        self.price_var = tk.StringVar(value="52.00")
        ttk.Entry(row2, textvariable=self.price_var, width=15).pack(side='left', padx=2)
        ttk.Label(row2, text="操作人:", font=('Arial', 9)).pack(side='left', padx=(15, 2))
        self.operator_var = tk.StringVar(value="系统")
        ttk.Entry(row2, textvariable=self.operator_var, width=15).pack(side='left', padx=2)
        
        # 备注
        row3 = ttk.Frame(setting_frame)
        row3.pack(fill='x', pady=2)
        ttk.Label(row3, text="备注:", width=14).pack(side='left')
        self.remark_var = tk.StringVar(value="裁剪入库")
        ttk.Entry(row3, textvariable=self.remark_var, width=40).pack(side='left', padx=2)
        
        # 按钮
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill='x', pady=10)
        ttk.Button(btn_frame, text="✅ 执行入库", command=self.execute_stock_in, width=20).pack(side='left', padx=5)
        ttk.Button(btn_frame, text="❌ 取消", command=self.dialog.destroy, width=10).pack(side='left', padx=5)
        
        # 状态
        self.status_var = tk.StringVar(value="请输入目标货号")
        status_label = ttk.Label(main_frame, textvariable=self.status_var, relief='sunken', anchor='w', 
                                  background='#f0f0f0', padding=4)
        status_label.pack(fill='x')
        
        # 初始化
        self.load_users()
    
    def load_users(self):
        try:
            users = get_inventory_users()
            self.target_combo['values'] = users
        except Exception as e:
            self.status_var.set(f"加载货号列表失败: {e}")
    
    def search_target(self):
        keyword = self.target_entry.get().strip()
        if not keyword:
            users = get_inventory_users()
        else:
            users = get_inventory_users(keyword)
        self.target_combo['values'] = users
        if users:
            self.target_combo.set(users[0])
            self.status_var.set(f"找到 {len(users)} 个匹配货号")
        else:
            self.status_var.set("未找到匹配货号")
    
    def on_target_selected(self, event):
        self.target_entry.delete(0, tk.END)
        self.target_entry.insert(0, self.target_combo.get())
        self.status_var.set(f"已选择货号: {self.target_combo.get()}")
    
    def execute_stock_in(self):
        target = self.target_entry.get().strip() or self.target_combo.get().strip()
        
        if not target:
            messagebox.showerror("错误", "请输入或选择目标货号")
            return
        
        try:
            price = float(self.price_var.get().strip() or 52.00)
            if price <= 0:
                messagebox.showerror("错误", "单价必须大于0")
                return
        except ValueError:
            messagebox.showerror("错误", "单价必须是数字")
            return
        
        operator = self.operator_var.get().strip() or "系统"
        remark = self.remark_var.get().strip() or "裁剪入库"
        
        # ===== 直接使用 self.color_data 转换 =====
        stock_data = []
        for item in self.color_data:
            # 兼容两种字段名
            color = item.get('color') or item.get('颜色', '')
            size = item.get('size') or item.get('尺码', '')
            pieces = item.get('pieces') or item.get('件数', 1)
            qty = item.get('quantity') or item.get('数量', 0)
            
            if color and size and pieces > 0 and qty > 0:
                stock_data.append({
                    'color': color,
                    'size': size,
                    'qty': qty * pieces
                })
        
        # 调试
        print("【调试】stock_data:", stock_data)
        
        if not stock_data:
            messagebox.showerror("错误", "没有有效的入库数据，请检查颜色、尺码、数量")
            return
        
        # 确认
        total_qty = sum(item['qty'] for item in stock_data)
        confirm_msg = f"确认执行入库？\n\n"
        confirm_msg += f"目标货号: {target}\n"
        confirm_msg += f"单价: {price:.2f}\n"
        confirm_msg += f"操作人: {operator}\n"
        confirm_msg += f"备注: {remark}\n"
        confirm_msg += f"\n入库明细: {len(stock_data)} 条\n"
        confirm_msg += f"总数量: {total_qty} 件"
        
        if not messagebox.askyesno("确认入库", confirm_msg):
            return
        
        self.status_var.set("⏳ 正在执行入库...")
        self.dialog.update()
        
        try:
            success, msg, details = call_stock_in_procedure(
                target, stock_data, price, operator, remark
            )
            
            if success:
                log_record = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | [入库] {target} | {details.get('in_number', '')} | {details.get('total_qty', 0)}件 | {details.get('total_amount', 0)}元"
                write_log(log_record)
                messagebox.showinfo("✅ 入库成功", f"{msg}\n\n入库单号: {details.get('in_number', '')}\n总数量: {details.get('total_qty', 0)} 件\n总金额: {details.get('total_amount', 0):.2f}")
                self.status_var.set(f"✅ {msg}")
                self.dialog.destroy()
            else:
                messagebox.showerror("❌ 入库失败", msg)
                self.status_var.set(f"❌ 失败: {msg}")
        except Exception as e:
            messagebox.showerror("错误", str(e))
            self.status_var.set(f"❌ 错误: {e}")


# ==================== 主界面 App 类 ====================

class App:
    def __init__(self, root):
        global APP_INSTANCE
        APP_INSTANCE = self
        self.root = root
        root.title("工单数据插入工具 V7.7 人性化版 - 含库存入库")
        root.geometry("1200x850")
        root.minsize(900, 650)
        root.resizable(True, True)
        
        self.template_modified = False
        self.barcode_config = load_barcode_config()
        self.last_barcode_pdf = None
        
        # 样式设置
        style = ttk.Style()
        style.configure('Title.TLabel', font=('Arial', 12, 'bold'))
        style.configure('Header.TLabel', font=('Arial', 10, 'bold'))
        style.configure('Action.TButton', font=('Arial', 10))
        style.configure('Summary.TLabel', font=('Arial', 10, 'bold'), foreground='#0066cc')
        style.configure('Mode.TLabel', font=('Arial', 10, 'bold'))
        style.configure('Save.TButton', font=('Arial', 10, 'bold'), foreground='#0066cc')
        style.configure('Delete.TButton', font=('Arial', 10, 'bold'), foreground='#cc0000')
        style.configure('StockIn.TButton', font=('Arial', 10, 'bold'), foreground='#006600')
        
        # ===== 主容器 =====
        self.main_frame = ttk.Frame(root, padding="8")
        self.main_frame.pack(fill='both', expand=True)
        
        # ===== 顶部区域 =====
        top_frame = ttk.Frame(self.main_frame)
        top_frame.pack(fill='x', pady=(0, 8))
        
        # 标题
        title_frame = ttk.Frame(top_frame)
        title_frame.pack(fill='x', pady=(0, 8))
        ttk.Label(title_frame, text="📋 工单数据插入工具 V7.7", font=('Arial', 14, 'bold')).pack()
        ttk.Label(title_frame, text="按 1 → 2 → 3 → 4 → 5 操作即可；红色/黄色提示表示当前任务还有需要检查的地方", font=('Arial', 9), foreground='gray').pack(pady=(2, 0))
        
        # 第1行：模板选择
        row1 = ttk.Frame(top_frame)
        row1.pack(fill='x', pady=2)
        ttk.Label(row1, text="1. 选择模板 ZDNO：", width=16).pack(side='left')
        self.template_var = tk.StringVar()
        self.template_combo = AutocompleteCombobox(row1, width=30)
        self.template_combo.pack(side='left', padx=3)
        self.template_combo.bind('<<ComboboxSelected>>', self.on_template_selected)
        ttk.Label(row1, text="（输入后按回车搜索）", font=('Arial', 8), foreground='gray').pack(side='left', padx=3)
        self.view_template_btn = ttk.Button(row1, text="📋 加载模板", command=self.view_template_data, width=12)
        self.view_template_btn.pack(side='left', padx=3)
        self.refresh_btn = ttk.Button(row1, text="🔄 刷新列表", command=self.load_templates, width=10)
        self.refresh_btn.pack(side='left', padx=3)
        self.save_template_btn = ttk.Button(row1, text="保存模板", command=self.save_template_data, width=10, style='Save.TButton')
        self.save_template_btn.pack(side='left', padx=2)
        self.copy_template_btn = ttk.Button(row1, text="复制模板", command=self.copy_template, width=10, style='Action.TButton')
        self.copy_template_btn.pack(side='left', padx=2)
        self.delete_template_btn = ttk.Button(row1, text="删除模板", command=self.delete_template, width=10, style='Delete.TButton')
        self.delete_template_btn.pack(side='left', padx=2)
        
        # 第2行：新 ZDNO
        row2 = ttk.Frame(top_frame)
        row2.pack(fill='x', pady=2)
        ttk.Label(row2, text="2. 输入新 ZDNO：", width=16).pack(side='left')
        self.new_zdno_prefix_var = tk.StringVar()
        self.new_zdno_prefix_entry = ttk.Entry(row2, textvariable=self.new_zdno_prefix_var, width=25)
        self.new_zdno_prefix_entry.pack(side='left', padx=2)
        self.new_zdno_prefix_entry.bind('<KeyRelease>', self.on_zdno_change)
        ttk.Label(row2, text="开始：", font=('Arial', 9)).pack(side='left', padx=(8, 2))
        self.start_num_var = tk.StringVar(value="1")
        self.start_num_entry = ttk.Entry(row2, textvariable=self.start_num_var, width=8)
        self.start_num_entry.pack(side='left', padx=2)
        self.start_num_entry.bind('<KeyRelease>', self.on_zdno_change)
        ttk.Label(row2, text="结束：", font=('Arial', 9)).pack(side='left', padx=(5, 2))
        self.end_num_var = tk.StringVar(value="5")
        self.end_num_entry = ttk.Entry(row2, textvariable=self.end_num_var, width=8)
        self.end_num_entry.pack(side='left', padx=2)
        self.end_num_entry.bind('<KeyRelease>', self.on_zdno_change)
        self.zdno_preview_var = tk.StringVar(value="完整 ZDNO：")
        ttk.Label(row2, textvariable=self.zdno_preview_var, font=('Arial', 9, 'bold'), 
                  foreground='#0066cc').pack(side='left', padx=(15, 5))
        
        # 第3行：模式 + 款式 + 颜色 + 数量
        row3 = ttk.Frame(top_frame)
        row3.pack(fill='x', pady=4)
        ttk.Label(row3, text="3. 模式：", width=16).pack(side='left')
        self.mode_var = tk.StringVar(value="明细")
        mode_frame = ttk.Frame(row3)
        mode_frame.pack(side='left', padx=3)
        ttk.Radiobutton(mode_frame, text="📊 明细模式", variable=self.mode_var, value="明细", 
                        command=self.on_mode_change).pack(side='left', padx=2)
        ttk.Radiobutton(mode_frame, text="📦 汇总模式", variable=self.mode_var, value="汇总", 
                        command=self.on_mode_change).pack(side='left', padx=2)
        ttk.Separator(row3, orient='vertical').pack(side='left', padx=8, fill='y')
        ttk.Label(row3, text="款式：").pack(side='left', padx=(8, 2))
        self.style_var = tk.StringVar(value="女款")
        style_frame = ttk.Frame(row3)
        style_frame.pack(side='left', padx=2)
        ttk.Radiobutton(style_frame, text="👗 女款", variable=self.style_var, value="女款", 
                        command=self.on_style_change).pack(side='left', padx=2)
        ttk.Radiobutton(style_frame, text="👔 男款", variable=self.style_var, value="男款", 
                        command=self.on_style_change).pack(side='left', padx=2)
        ttk.Separator(row3, orient='vertical').pack(side='left', padx=8, fill='y')
        ttk.Label(row3, text="颜色：").pack(side='left', padx=(8, 2))
        self.color_var = tk.StringVar(value="黑色")
        self.color_combo = ttk.Combobox(row3, textvariable=self.color_var, width=12)
        self.color_combo.pack(side='left', padx=2)
        self.color_combo.bind("<<ComboboxSelected>>", self.on_color_changed)
        self.confirm_color_btn = ttk.Button(row3, text="确认颜色", command=self.confirm_color, width=10)
        self.confirm_color_btn.pack(side='left', padx=2)
        
        ttk.Separator(row3, orient='vertical').pack(side='left', padx=8, fill='y')
        ttk.Label(row3, text="数量：").pack(side='left', padx=(8, 2))
        self.qty_var = tk.StringVar(value="50")
        self.qty_entry = ttk.Entry(row3, textvariable=self.qty_var, width=12)
        self.qty_entry.pack(side='left', padx=2)
        self.qty_entry.bind('<Return>', lambda e: self.confirm_quantity())
        self.confirm_qty_btn = ttk.Button(row3, text="确认数量", command=self.confirm_quantity, width=10)
        self.confirm_qty_btn.pack(side='left', padx=2)

        # 第4行：货号关联
        row4 = ttk.Frame(top_frame)
        row4.pack(fill='x', pady=3)
        ttk.Label(row4, text="4. 关联货号：", width=16).pack(side='left')
        self.type_name_var = tk.StringVar(value=DEFAULT_TYPE)
        self.type_name_combo = ttk.Combobox(row4, textvariable=self.type_name_var,
                                           values=list(TYPE_PRESETS), width=9)
        self.type_name_combo.pack(side='left', padx=2)
        ttk.Label(row4, text="类型：", font=('Arial', 9)).pack(side='left', padx=(10, 2))
        self.type_name_combo.bind("<<ComboboxSelected>>", self.on_type_changed)
        ttk.Label(row4, text="（可下拉选，也可自己输入）", font=('Arial', 8),
                  foreground='gray').pack(side='left', padx=3)
        self.type_hint_var = tk.StringVar(value="")
        ttk.Label(row4, textvariable=self.type_hint_var, font=('Arial', 8, 'bold'),
                  foreground='#0066cc').pack(side='left', padx=6)
        self.b_usercode_var = tk.StringVar()
        self.b_usercode_entry = ttk.Entry(row4, textvariable=self.b_usercode_var, width=24)
        self.b_usercode_entry.pack(side='left', padx=2)
        self.b_usercode_entry.bind('<Return>', lambda e: self.search_b_usercode())
        ttk.Button(row4, text="查询货号", command=self.search_b_usercode, width=10).pack(side='left', padx=2)
        ttk.Label(row4, text="选择匹配货号：").pack(side='left', padx=(8, 2))
        self.b_product_combo = ttk.Combobox(row4, width=24, state='normal')
        self.b_product_combo.pack(side='left', padx=2)
        self.b_product_combo.bind('<<ComboboxSelected>>', self.on_b_product_selected)
        ttk.Button(row4, text="查询颜色", command=self.on_b_product_selected, width=10).pack(side='left', padx=2)
        ttk.Label(row4, text="支持手动输入颜色", font=('Arial', 8), foreground='gray').pack(side='left', padx=5)
        
        # 第5行：入库快捷按钮
        row5 = ttk.Frame(top_frame)
        row5.pack(fill='x', pady=3)
        ttk.Label(row5, text="5. 库存入库：", width=16).pack(side='left')
        self.stockin_btn = ttk.Button(row5, text="📦 裁剪入库", command=self.open_stock_in, 
                                       width=15, style='StockIn.TButton')
        self.stockin_btn.pack(side='left', padx=2)
        self.stockin_btn.config(state='disabled')
        ttk.Label(row5, text="（先填写上方数据，再点击入库）", font=('Arial', 8), foreground='gray').pack(side='left', padx=5)
        ttk.Button(row5, text="🚀 执行插入", command=self.submit, width=18).pack(side='left', padx=2)
        ttk.Button(row5, text="🖨 条码打印/预览", command=self.open_barcode_print_from_current, width=18).pack(side='left', padx=2)
        ttk.Button(row5, text="📚 重新打印历史条码", command=self.open_barcode_history, width=20).pack(side='left', padx=2)
        ttk.Button(row5, text="⚙ 打印尺寸", command=self.open_barcode_settings, width=12).pack(side='left', padx=2)
        ttk.Button(row5, text="🆕 新建任务", command=self.new_task, width=12).pack(side='left', padx=2)
        ttk.Button(row5, text="✓ 检查数据", command=self.validate_current_task, width=12).pack(side='left', padx=2)
        ttk.Separator(row5, orient='vertical').pack(side='left', fill='y', padx=6)
        self.detail_btn = ttk.Button(row5, text="📝 写入明细",
                                     command=self.open_write_detail, width=12)
        self.detail_btn.pack(side='left', padx=2)
        ttk.Button(row5, text="📊 补录明细表", command=self.open_detail_backfill,
                   width=13).pack(side='left', padx=2)
        ttk.Button(row5, text="📈 裁剪统计", command=self.open_stat_window,
                   width=12).pack(side='left', padx=2)
        self.undo_write_btn = ttk.Button(row5, text="↩ 撤销上次写库",
                                        command=self.undo_last_write, width=15)
        self.undo_write_btn.pack(side='left', padx=2)
        self.zdno_edit_btn = ttk.Button(row5, text="🧵 ZDNO维护",
                                       command=self.open_zdno_editor_window, width=14)
        self.zdno_edit_btn.pack(side='left', padx=2)

        # ===== V7.7 人性化：当前任务状态 =====
        task_frame = ttk.LabelFrame(top_frame, text="📌 当前任务状态", padding=(8, 5))
        task_frame.pack(fill='x', pady=(5, 2))
        self.task_summary_var = tk.StringVar(value="模板：-  |  新ZDNO：-  |  模式：明细  |  款式：女款  |  总件数：0  |  总数量：0")
        self.task_summary_label = ttk.Label(task_frame, textvariable=self.task_summary_var, font=('Arial', 9, 'bold'))
        self.task_summary_label.pack(side='left', fill='x', expand=True)
        self.data_check_var = tk.StringVar(value="⚠ 请填写任务数据")
        self.data_check_label = ttk.Label(task_frame, textvariable=self.data_check_var, font=('Arial', 9, 'bold'))
        self.data_check_label.pack(side='right', padx=(8, 0))

        # ===== 中间区域（左右两列） =====
        middle_frame = ttk.Frame(self.main_frame)
        middle_frame.pack(fill='both', expand=True, pady=5)
        
        self.paned = ttk.PanedWindow(middle_frame, orient='horizontal')
        self.paned.pack(fill='both', expand=True)
        
        # 左列：模板工序数据
        left_frame = ttk.Frame(self.paned)
        self.paned.add(left_frame, weight=1)
        
        left_title_frame = ttk.Frame(left_frame)
        left_title_frame.pack(fill='x', pady=(0, 3))
        ttk.Label(left_title_frame, text="📋 模板工序数据", font=('Arial', 10, 'bold')).pack(side='left')
        ttk.Label(left_title_frame, text="（双击单元格编辑）", font=('Arial', 8), foreground='gray').pack(side='left', padx=6)

        left_toolbar = ttk.Frame(left_frame)
        left_toolbar.pack(fill='x', pady=(0, 4))
        ttk.Button(left_toolbar, text="➕ 新增工序", command=self.add_template_row, width=12).pack(side='left', padx=2)
        ttk.Button(left_toolbar, text="🗑️ 删除选中", command=self.delete_template_row, width=12).pack(side='left', padx=2)
        ttk.Button(left_toolbar, text="💾 保存工序", command=self.save_template_data, width=12, style='Save.TButton').pack(side='left', padx=2)
        ttk.Button(left_toolbar, text="🔄 重新加载", command=self.view_template_data, width=12).pack(side='left', padx=2)
        
        left_tree_frame = ttk.Frame(left_frame)
        left_tree_frame.pack(fill='both', expand=True)
        
        self.template_tree = EditableTreeview(left_tree_frame, app=self,
                                               columns=('gx', 'gxname', 'dj', 'mdate', 'oksl'), 
                                               height=13, show='headings')
        self.template_tree.heading('gx', text='工序')
        self.template_tree.heading('gxname', text='工序名称')
        self.template_tree.heading('dj', text='单价')
        self.template_tree.heading('mdate', text='日期')
        self.template_tree.heading('oksl', text='数量')
        self.template_tree.column('gx', width=50, anchor='center')
        self.template_tree.column('gxname', width=120, anchor='center')
        self.template_tree.column('dj', width=70, anchor='center')
        self.template_tree.column('mdate', width=110, anchor='center')
        self.template_tree.column('oksl', width=60, anchor='center')
        
        scrollbar_tree = ttk.Scrollbar(left_tree_frame, orient='vertical', command=self.template_tree.yview)
        self.template_tree.configure(yscrollcommand=scrollbar_tree.set)
        self.template_tree.pack(side='left', fill='both', expand=True)
        scrollbar_tree.pack(side='right', fill='y')
        
        self.template_tree.bind('<Button-3>', self.show_context_menu)
        self.context_menu = tk.Menu(self.template_tree, tearoff=0)
        self.context_menu.add_command(label="➕ 新增工序", command=self.add_template_row)
        self.context_menu.add_command(label="🗑️ 删除选中", command=self.delete_template_row)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="💾 保存工序", command=self.save_template_data)
        self.context_menu.add_command(label="🔄 重新加载", command=self.view_template_data)
        
        # 右列：尺码数据
        right_frame = ttk.Frame(self.paned)
        self.paned.add(right_frame, weight=1)
        
        self.size_title_label = ttk.Label(right_frame, text="4. 尺码数据（件数、数量）：", font=('Arial', 10, 'bold'))
        self.size_title_label.pack(anchor='w', pady=(0, 3))
        
        self.header_frame = ttk.Frame(right_frame)
        self.header_frame.pack(fill='x', pady=2)
        
        right_canvas_frame = ttk.Frame(right_frame)
        right_canvas_frame.pack(fill='both', expand=True)
        
        self.canvas = tk.Canvas(right_canvas_frame, highlightthickness=1, highlightcolor='#cccccc')
        scrollbar = ttk.Scrollbar(right_canvas_frame, orient='vertical', command=self.canvas.yview)
        self.scrollable_frame = ttk.Frame(self.canvas)
        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        
        def on_mousewheel(event):
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        self.canvas.bind_all("<MouseWheel>", on_mousewheel)
        
        self.size_rows = []
        self.add_btn_frame = ttk.Frame(self.scrollable_frame)
        self.add_btn_frame.pack(fill='x', pady=5)
        self.add_btn = ttk.Button(self.add_btn_frame, text="➕ 添加尺码行", command=self.add_size_row, width=15)
        self.add_btn.pack(side='left', padx=3)
        self.copy_row_btn = ttk.Button(self.add_btn_frame, text="📋 复制上一行", command=self.copy_last_size_row, width=15)
        self.copy_row_btn.pack(side='left', padx=3)
        
        self.summary_hint_var = tk.StringVar(value="")
        self.summary_hint_label = ttk.Label(self.scrollable_frame, textvariable=self.summary_hint_var, 
                                             font=('Arial', 10), foreground='#0066cc')
        
        # ===== 底部汇总和按钮区域 =====
        bottom_frame = ttk.Frame(self.main_frame)
        bottom_frame.pack(fill='x', pady=(8, 0))
        
        summary_frame = ttk.Frame(bottom_frame)
        summary_frame.pack(fill='x', pady=3)
        ttk.Label(summary_frame, text="📊 汇总：", font=('Arial', 10, 'bold')).pack(side='left', padx=5)
        self.summary_pieces_var = tk.StringVar(value="总件数：0")
        ttk.Label(summary_frame, textvariable=self.summary_pieces_var, 
                  font=('Arial', 10, 'bold'), foreground='#0066cc').pack(side='left', padx=15)
        self.summary_qty_var = tk.StringVar(value="总数量：0")
        ttk.Label(summary_frame, textvariable=self.summary_qty_var, 
                  font=('Arial', 10, 'bold'), foreground='#0066cc').pack(side='left', padx=15)
        
        btn_frame = ttk.Frame(bottom_frame)
        btn_frame.pack(fill='x', pady=5)
        ttk.Button(btn_frame, text="🚀 执行插入", command=self.submit, width=20).pack(side='left', padx=5)
        ttk.Button(btn_frame, text="📦 裁剪入库", command=self.open_stock_in, 
                   width=20, style='StockIn.TButton').pack(side='left', padx=5)
        
        # ===== 日志区域 =====
        log_frame = ttk.LabelFrame(self.main_frame, text="操作日志（同时保存到 插入历史记录.txt）", padding=4)
        log_frame.pack(fill='x', pady=(5, 0))
        log_toolbar = ttk.Frame(log_frame)
        log_toolbar.pack(fill='x')
        ttk.Button(log_toolbar, text="复制选中", command=self.copy_selected_log, width=10).pack(side='left', padx=2)
        ttk.Button(log_toolbar, text="复制全部", command=self.copy_all_log, width=10).pack(side='left', padx=2)
        ttk.Button(log_toolbar, text="清空界面日志", command=self.clear_log_view, width=12).pack(side='left', padx=2)
        self.log_text = tk.Text(log_frame, height=6, wrap='none', undo=False)
        self.log_scroll = ttk.Scrollbar(log_frame, orient='vertical', command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=self.log_scroll.set)
        self.log_text.pack(side='left', fill='both', expand=True)
        self.log_scroll.pack(side='right', fill='y')
        self.log_text.bind('<Control-c>', self.copy_selected_log)
        self.log_text.bind('<Button-3>', self.show_log_context_menu)
        self.log_context_menu = tk.Menu(self.log_text, tearoff=0)
        self.log_context_menu.add_command(label='复制选中', command=self.copy_selected_log)
        self.log_context_menu.add_command(label='复制全部', command=self.copy_all_log)

        # 状态栏
        status_frame = ttk.Frame(self.main_frame)
        status_frame.pack(fill='x', pady=(5, 0))
        self.status_var = tk.StringVar(value="✅ 就绪")
        status_label = ttk.Label(status_frame, textvariable=self.status_var, relief='sunken', anchor='w', 
                                  background='#f0f0f0', padding=4)
        status_label.pack(fill='x')
        
        # 初始化
        self._setup_v77_shortcuts()
        self.load_templates()
        try:
            if os.path.exists(LOG_FILE):
                with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
                    for line in f.readlines()[-100:]:
                        self.append_log(line.rstrip())
        except Exception:
            pass
        self.on_mode_change()
        self.build_size_rows()
        self.update_zdno_preview()
        self.view_template_data()
        self.update_summary()
    
    # ===== 以下是各方法实现 =====
    
    def load_templates(self):
        try:
            current = self.template_combo.get().strip()
            zdnos = get_zdno_list(current)
            self.template_combo.set_values(zdnos)
            if current and current in zdnos:
                self.template_combo.set(current)
            elif zdnos:
                self.template_combo.set(zdnos[0])
            else:
                self.template_combo.set('')
            self.status_var.set(f"✅ 已刷新模板列表（共 {len(zdnos)} 个）")
            self.new_zdno_prefix_var.set(self.template_combo.get().strip()[:8])
            print(self.template_combo.get().strip())
        except Exception as e:
            messagebox.showerror("错误", f"连接数据库失败：{e}")
            self.status_var.set(f"❌ 刷新失败：{e}")
    
    def append_log(self, record):
        if not hasattr(self, 'log_text'):
            return
        def _append():
            try:
                self.log_text.insert(tk.END, record + '\n')
                self.log_text.see(tk.END)
            except tk.TclError:
                pass
        try:
            if threading.current_thread() is threading.main_thread():
                _append()
            else:
                self.root.after(0, _append)
        except Exception:
            pass

    def copy_selected_log(self, event=None):
        try:
            text = self.log_text.get(tk.SEL_FIRST, tk.SEL_LAST)
        except tk.TclError:
            return 'break' if event else None
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status_var.set('✅ 已复制选中的日志内容')
        return 'break' if event else None

    def copy_all_log(self):
        text = self.log_text.get('1.0', tk.END).rstrip()
        if not text:
            messagebox.showinfo('提示', '当前界面没有日志内容')
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status_var.set('✅ 已复制全部界面日志')

    def clear_log_view(self):
        self.log_text.delete('1.0', tk.END)
        self.status_var.set('✅ 已清空界面日志（TXT历史日志不受影响）')

    def show_log_context_menu(self, event):
        self.log_context_menu.post(event.x_root, event.y_root)

    def copy_template(self):
        source_zdno = self.template_combo.get().strip()
        new_zdno = self.new_zdno_prefix_var.get().strip()
        if not source_zdno:
            messagebox.showerror('错误', '请先选择原模板 ZDNO')
            return
        if not new_zdno:
            messagebox.showerror('错误', '请在"输入新 ZDNO"框中输入新的完整 ZDNO')
            return
        if not messagebox.askyesno('确认复制模板',
                                   f'确定将模板 {source_zdno} 的 jfdj 工序复制为新模板 {new_zdno} 吗？\n\n'
                                   '本操作只复制 jfdj，jfzd、jfzd2、jfzd3 不会新增或修改。'):
            return
        self.status_var.set(f'⏳ 正在复制模板 {source_zdno} → {new_zdno}...')
        self.root.update_idletasks()
        try:
            success, msg, count = copy_template_jfdj(source_zdno, new_zdno)
            if success:
                record = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | [复制模板] {source_zdno} → {new_zdno} | 仅jfdj | {count}条"
                write_log(record)
                self.load_templates()
                self.template_combo.set(new_zdno)
                self.view_template_data(force=True)
                messagebox.showinfo('复制成功', msg)
                self.status_var.set(f'✅ {msg}')
            else:
                messagebox.showerror('复制失败', msg)
                self.status_var.set(f'❌ 复制失败：{msg}')
        except Exception as e:
            messagebox.showerror('复制失败', str(e))
            self.status_var.set(f'❌ 复制失败：{e}')

    def search_b_usercode(self):
        keyword = self.b_usercode_var.get().strip()
        if not keyword:
            messagebox.showwarning('提示', '请输入要模糊查询的货号')
            return
        self.status_var.set('⏳ 正在查询货号...')
        self.root.update_idletasks()
        self.template_combo.set(keyword)
        self.load_templates()
        
        
        try:
            values = get_b_product_list(keyword)
            self.b_product_combo['values'] = values
            if values:
                self.b_product_combo.set(values[0])
                self.on_b_product_selected()
                self.status_var.set(f'✅ 找到 {len(values)} 个匹配货号')
            else:
                self.b_product_combo.set('')
                self.color_combo['values'] = []
                self.status_var.set('⚠️ 未找到匹配货号')
                messagebox.showinfo('查询结果', '没有找到匹配的货号')
        except Exception as e:
            messagebox.showerror('查询失败', str(e))
            self.status_var.set(f'❌ 查询失败：{e}')

    def on_b_product_selected(self, event=None):
        usercode = self.b_product_combo.get().strip()
        if not usercode:
            return
        try:
            colors = get_b_colors(usercode)
            self.color_combo['values'] = colors
            if colors and self.color_var.get().strip() not in colors:
                self.color_var.set(colors[0])
            if colors:
                self.status_var.set(f'✅ 货号 {usercode} 获取到 {len(colors)} 个颜色')
            else:
                self.status_var.set(f'⚠️ 货号 {usercode} 暂无颜色记录')
        except Exception as e:
            messagebox.showerror('颜色查询失败', str(e))
            self.status_var.set(f'❌ 颜色查询失败：{e}')

    def mark_template_modified(self):
        self.template_modified = True
        self.status_var.set("⚠️ 模板数据已修改，请点击「💾 保存模板」保存")
    
    def on_template_selected(self, event=None):
        self.view_template_data()
        self.new_zdno_prefix_var.set(self.template_combo.get().strip()[:8])
        self.update_zdno_preview()
        self.update_summary()
        self.new_zdno_prefix_entry.focus_set()

    def show_context_menu(self, event):
        row = self.template_tree.identify_row(event.y)
        if row:
            if row not in self.template_tree.selection():
                self.template_tree.selection_set(row)
        self.context_menu.post(event.x_root, event.y_root)

    def add_template_row(self):
        zdno = self.template_combo.get().strip()
        if not zdno:
            messagebox.showwarning("提示", "请先选择模板 ZDNO")
            return
        data = self.template_tree.get_data()
        max_gx = 0
        for row in data:
            try:
                gx = int(row[0])
                max_gx = max(max_gx, gx)
            except (TypeError, ValueError):
                pass
        new_gx = max_gx + 1
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
        item_id = self.template_tree.insert('', 'end', values=(new_gx, '', '0', now_str, '0'))
        self.template_tree.selection_set(item_id)
        self.template_tree.see(item_id)
        self.mark_template_modified()
        self.status_var.set(f"✅ 已添加工序 {new_gx}，请点击「保存工序」写入数据库")

    def delete_template_row(self):
        selected = self.template_tree.selection()
        if not selected:
            messagebox.showwarning("提示", "请先选中要删除的工序行")
            return
        if not messagebox.askyesno("确认", f"确定删除选中的 {len(selected)} 条工序吗？\n（需点击「保存工序」才会写入数据库）"):
            return
        for item in selected:
            self.template_tree.delete(item)
        self.mark_template_modified()
        self.status_var.set("✅ 已删除选中工序，请点击「保存工序」写入数据库")

    def _parse_template_rows(self):
        raw_rows = self.template_tree.get_data()
        if not raw_rows:
            return []
        parsed_rows = []
        seen_gx = set()
        for index, row in enumerate(raw_rows, start=1):
            gx_text = str(row[0]).strip()
            if not gx_text:
                raise ValueError(f"第 {index} 行：工序编号不能为空")
            try:
                gx = int(gx_text)
            except ValueError:
                raise ValueError(f"第 {index} 行：工序编号必须是整数（当前值：{gx_text}）")
            if gx in seen_gx:
                raise ValueError(f"第 {index} 行：工序编号 {gx} 重复")
            seen_gx.add(gx)
            gxname = str(row[1]).strip() if row[1] is not None else ''
            dj_text = str(row[2]).strip() if row[2] not in (None, '') else '0'
            try:
                dj = float(dj_text)
            except ValueError:
                raise ValueError(f"第 {index} 行：单价必须是数字（当前值：{dj_text}）")
            try:
                mdate = parse_mdate(row[3])
            except ValueError as e:
                raise ValueError(f"第 {index} 行：{e}")
            qty_text = str(row[4]).strip() if row[4] not in (None, '') else '0'
            try:
                oksl = int(float(qty_text))
            except ValueError:
                raise ValueError(f"第 {index} 行：数量必须是整数（当前值：{qty_text}）")
            parsed_rows.append((gx, gxname, dj, mdate, oksl))
        parsed_rows.sort(key=lambda item: item[0])
        return parsed_rows

    def save_template_data(self):
        zdno = self.template_combo.get().strip()
        if not zdno:
            messagebox.showerror("错误", "请先选择模板 ZDNO")
            return
        try:
            data_rows = self._parse_template_rows()
        except ValueError as e:
            messagebox.showerror("数据校验失败", str(e))
            return
        if not data_rows:
            if not messagebox.askyesno("确认", f"模板 {zdno} 的工序列表为空，确定清空数据库中的工序数据吗？"):
                return
        if not messagebox.askyesno("确认保存", f"确定将 {len(data_rows)} 条工序保存到模板 {zdno} 吗？\n（将覆盖该模板原有工序数据）"):
            return
        self.status_var.set("⏳ 正在保存模板工序...")
        self.root.update()
        success, msg = save_jfdj_data(zdno, data_rows)
        if success:
            self.template_modified = False
            write_log(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | [保存模板] {zdno} | jfdj工序 {len(data_rows)}条")
            self.load_templates()
            self.template_combo.set(zdno)
            self.view_template_data(force=True)
            messagebox.showinfo("成功", msg)
            self.status_var.set(f"✅ {msg}")
        else:
            messagebox.showerror("保存失败", msg)
            self.status_var.set(f"❌ 保存失败：{msg}")

    def delete_template(self):
        zdno = self.template_combo.get().strip()
        if not zdno:
            messagebox.showerror("错误", "请先选择要删除的模板 ZDNO")
            return
        if self.template_modified:
            if not messagebox.askyesno("提示", "当前工序有未保存的修改，继续删除模板将放弃这些修改。是否继续？"):
                return
        try:
            counts = get_template_record_counts(zdno)
        except Exception as e:
            messagebox.showerror("错误", f"查询模板数据失败：{e}")
            return
        total = sum(counts.values())
        if total == 0:
            messagebox.showinfo("提示", f"模板 {zdno} 在数据库中没有任何记录")
            return
        count_text = '\n'.join(f"  · {table}: {count} 条" for table, count in counts.items() if count > 0)
        confirm_msg = (
            f"确定要永久删除模板 {zdno} 吗？\n\n"
            f"将删除以下表中的记录：\n{count_text}\n\n"
            f"共 {total} 条记录。\n\n"
            f"删除前程序会自动把该工单在四张表中的全部数据备份到\n"
            f"「{os.path.basename(SNAPSHOT_FILE)}」，\n"
            f"删除后仍可通过「↩ 撤销上次写库」一键还原。"
        )
        if not messagebox.askyesno("确认删除模板", confirm_msg, icon='warning'):
            return
        self.status_var.set(f"⏳ 正在删除模板 {zdno}...")
        self.root.update()
        success, msg, deleted_counts = delete_template_by_zdno(zdno)
        if success:
            detail = '，'.join(f"{table}:{count}" for table, count in deleted_counts.items())
            total_deleted = sum(deleted_counts.values())
            log_record = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | [删除] {zdno} | {detail} | 共{total_deleted}条"
            write_log(log_record)
            self.template_modified = False
            self.template_tree.clear_data()
            self.load_templates()
            zdnos = self.template_combo['values']
            if zdnos:
                self.template_combo.set(zdnos[0])
                self.view_template_data(force=True)
            else:
                self.template_combo.set('')
                self.status_var.set(f"✅ {msg}")
            messagebox.showinfo("成功", msg)
        else:
            messagebox.showerror("删除失败", msg)
            self.status_var.set(f"❌ 删除失败：{msg}")

    def view_template_data(self, force=False):
        if not hasattr(self, 'template_tree') or not hasattr(self, 'status_var'):
            return
        if self.template_modified and not force:
            if not messagebox.askyesno("提示", "当前工序已修改但未保存，是否放弃修改并重新加载？"):
                return
        zdno = self.template_combo.get().strip()
        if not zdno:
            messagebox.showwarning("提示", "请先选择模板 ZDNO")
            return
        self.template_tree.clear_data()
        try:
            rows = get_jfdj_data(zdno)
            if not rows:
                self.template_modified = False
                self.status_var.set(f"⚠️ 模板 {zdno} 暂无工序数据，可点击「新增工序」添加")
                return
            for row in rows:
                mdate_str = row.mdate.strftime('%Y-%m-%d %H:%M') if row.mdate else ''
                self.template_tree.insert('', 'end', values=(
                    row.gx,
                    row.gxname or '',
                    format_dj(row.dj),
                    mdate_str,
                    row.oksl if row.oksl is not None else 0,
                ))
            self.template_modified = False
            self.status_var.set(f"✅ 已加载模板 {zdno} 的 {len(rows)} 条工序")
        except Exception as e:
            self.status_var.set(f"❌ 加载失败：{e}")
            messagebox.showerror("错误", f"加载模板工序失败：{e}")
    
    def update_zdno_preview(self):
        prefix = self.new_zdno_prefix_var.get().strip()
        start = self.start_num_var.get().strip()
        end = self.end_num_var.get().strip()
        if prefix and start and end:
            try:
                start_num = int(start)
                end_num = int(end)
                if start_num <= end_num:
                    full_zdno = f"{prefix}{start_num}-{end_num}"
                    self.zdno_preview_var.set(f"完整 ZDNO：{full_zdno},共{end_num-start_num+1}手")
                else:
                    self.zdno_preview_var.set("⚠️ 开始编号必须小于结束编号")
            except ValueError:
                self.zdno_preview_var.set("⚠️ 编号必须为数字")
        else:
            self.zdno_preview_var.set("完整 ZDNO：")
    
    def on_mode_change(self):
        mode = self.mode_var.get()
        if mode == "汇总":
            # 汇总模式也显示尺码数据，二维码明细和总数量均从这里计算。
            self.summary_hint_label.pack(fill='x', pady=10)
            self.summary_hint_var.set("📦 汇总模式：下方尺码数据用于二维码明细；总数量自动按“件数×每手数量”求和")
            self.add_btn_frame.pack(fill='x', pady=5)
            for row in self.size_rows:
                row['frame'].pack(fill='x', pady=2)
            self.update_header()
            self.size_title_label.config(text="4. 尺码数据（用于汇总及二维码明细）：")
            self.update_summary()
        else:
            self.summary_hint_label.pack_forget()
            self.add_btn_frame.pack(fill='x', pady=5)
            for row in self.size_rows:
                row['frame'].pack(fill='x', pady=2)
            self.update_header()
            self.size_title_label.config(text="4. 尺码数据（件数、数量）：")
            self.update_summary()
        self.update_stockin_btn_state()
    
    def get_default_sizes(self, style):
        if style == "男款":
            return ['3XL', 'XXL', 'XL', 'L']
        else:
            return ['S-M', 'L-XL']
    
    def update_header(self):
        for widget in self.header_frame.winfo_children():
            widget.destroy()
        sizes = []
        for row in self.size_rows:
            size = row['size_var'].get()
            if size and size not in sizes:
                sizes.append(size)
        col_widths = {'color': 10, 'size': 12, 'pieces': 8, 'qty': 8, 'action': 6}
        ttk.Label(self.header_frame, text="颜色", width=col_widths['color'], 
                  font=('Arial', 10, 'bold'), anchor='center').pack(side='left', padx=1)
        for size in sizes:
            ttk.Label(self.header_frame, text=size, width=col_widths['size'], 
                      font=('Arial', 10, 'bold'), anchor='center').pack(side='left', padx=1)
        ttk.Label(self.header_frame, text="件数", width=col_widths['pieces'], 
                  font=('Arial', 10, 'bold'), anchor='center').pack(side='left', padx=1)
        ttk.Label(self.header_frame, text="数量", width=col_widths['qty'], 
                  font=('Arial', 10, 'bold'), anchor='center').pack(side='left', padx=1)
        ttk.Label(self.header_frame, text="操作", width=col_widths['action'], 
                  font=('Arial', 10, 'bold'), anchor='center').pack(side='left', padx=1)
    
    def build_size_rows(self):
        children = self.scrollable_frame.winfo_children()
        for widget in children:
            if widget not in [self.add_btn_frame, self.summary_hint_label]:
                widget.destroy()
        self.size_rows = []
        style = self.style_var.get()
        default_sizes = self.get_default_sizes(style)
        main_color = self.color_var.get().strip()
        main_qty = self.qty_var.get().strip()
        self.add_btn_frame.pack(fill='x', pady=5)
        self.summary_hint_label.pack_forget()
        for size in default_sizes:
            self._create_size_row(size, main_color, main_qty)
        self.update_header()
        self.update_summary()
        self.update_stockin_btn_state()
    
    def _create_size_row(self, size, color, qty):
        row_frame = ttk.Frame(self.scrollable_frame)
        row_frame.pack(fill='x', pady=2)
        col_widths = {'color': 10, 'size': 12, 'pieces': 8, 'qty': 8, 'action': 6}
        color_entry = ttk.Entry(row_frame, width=col_widths['color'])
        color_entry.pack(side='left', padx=1)
        color_entry.insert(0, color)
        size_var = tk.StringVar(value=size)
        size_combo = ttk.Combobox(row_frame, textvariable=size_var, 
                                   values=['5XL', '4XL', '3XL', 'XXL', 'XL', 'L', 'M', 'S', 'L-XL', 'S-M', 'M-L', 'XL-2XL','均码1','均码'],
                                   width=col_widths['size'])
        size_combo.pack(side='left', padx=1)
        size_combo.bind('<<ComboboxSelected>>', lambda e: self.update_header())
        pieces_entry = ttk.Entry(row_frame, width=col_widths['pieces'])
        pieces_entry.pack(side='left', padx=1)
        pieces_entry.insert(0, "1")
        pieces_entry.bind('<KeyRelease>', lambda e: self.update_summary())
        qty_entry = ttk.Entry(row_frame, width=col_widths['qty'])
        qty_entry.pack(side='left', padx=1)
        qty_entry.insert(0, qty if qty else "50")
        qty_entry.bind('<KeyRelease>', lambda e: self.update_summary())
        del_btn = ttk.Button(row_frame, text="✕", width=col_widths['action'], 
                             command=lambda: self.delete_row(row_frame))
        del_btn.pack(side='left', padx=1)

        # Enter 键按生产人员习惯向下一格移动；最后一格自动进入下一行。
        color_entry.bind('<Return>', lambda e, w=size_combo: self._focus_widget(w))
        size_combo.bind('<Return>', lambda e, w=pieces_entry: self._focus_widget(w))
        pieces_entry.bind('<Return>', lambda e, w=qty_entry: self._focus_widget(w))
        qty_entry.bind('<Return>', lambda e, rf=row_frame: self._focus_next_size_row(rf))
        for widget in (color_entry, pieces_entry, qty_entry):
            widget.bind('<KeyRelease>', lambda e: self.update_summary())

        self.size_rows.append({
            'frame': row_frame,
            'color': color_entry,
            'size_var': size_var,
            'size_combo': size_combo,
            'pieces': pieces_entry,
            'quantity': qty_entry,
            'del_btn': del_btn
        })
        self.update_stockin_btn_state()
    
    def add_size_row(self):
        main_color = self.color_var.get().strip()
        main_qty = self.qty_var.get().strip()
        self._create_size_row('L', main_color, main_qty)
        self.update_header()
        self.update_summary()
        self.canvas.yview_moveto(1.0)
        self.status_var.set("✅ 已添加尺码行")
        self.update_stockin_btn_state()
    
    def delete_row(self, row_frame):
        if len(self.size_rows) <= 1:
            messagebox.showwarning("提示", "至少保留一行尺码数据")
            return
        for i, row in enumerate(self.size_rows):
            if row['frame'] == row_frame:
                row_frame.destroy()
                self.size_rows.pop(i)
                break
        self.update_header()
        self.update_summary()
        self.status_var.set("✅ 已删除尺码行")
        self.update_stockin_btn_state()
    
    def update_summary(self):
        total_pieces = 0
        total_qty = 0
        valid_rows = 0
        invalid_rows = 0
        for row in self.size_rows:
            color = row['color'].get().strip()
            size = row['size_var'].get().strip()
            try:
                pieces = int(row['pieces'].get().strip() or 0)
                qty = int(row['quantity'].get().strip() or 0)
                if color and size and pieces > 0 and qty > 0:
                    valid_rows += 1
                    total_pieces += pieces
                    total_qty += pieces * qty
                elif any((color, size, row['pieces'].get().strip(), row['quantity'].get().strip())):
                    invalid_rows += 1
            except ValueError:
                invalid_rows += 1
        self.summary_pieces_var.set(f"总件数：{total_pieces}")
        self.summary_qty_var.set(f"总数量：{total_qty}")
        self.update_stockin_btn_state()
        self.update_task_status(total_pieces, total_qty, valid_rows, invalid_rows)

    def update_task_status(self, total_pieces=None, total_qty=None, valid_rows=0, invalid_rows=0):
        """V7.7：实时显示当前任务摘要和数据完整性，不改变任何业务数据。"""
        if total_pieces is None:
            try:
                total_pieces = int(self.summary_pieces_var.get().split('：')[-1])
                total_qty = int(self.summary_qty_var.get().split('：')[-1])
            except Exception:
                total_pieces, total_qty = 0, 0
        prefix = self.new_zdno_prefix_var.get().strip()
        start = self.start_num_var.get().strip()
        end = self.end_num_var.get().strip()
        full_zdno = f"{prefix}{start}-{end}" if prefix and start and end else '-'
        self.task_summary_var.set(
            f"模板：{self.template_combo.get().strip() or '-'}  |  新ZDNO：{full_zdno}  |  "
            f"模式：{self.mode_var.get()}  |  款式：{self.style_var.get()}  |  "
            f"总件数：{total_pieces}  |  总数量：{total_qty}"
        )
        if invalid_rows:
            self.data_check_var.set(f"⚠ 有 {invalid_rows} 行需要检查")
        elif valid_rows and total_qty > 0:
            try:
                start_num, end_num = int(start), int(end)
                count = end_num - start_num + 1
                if start_num > end_num:
                    self.data_check_var.set("⚠ 开始编号大于结束编号")
                elif total_pieces != count:
                    self.data_check_var.set(f"⚠ 件数{total_pieces} ≠ 编号数{count}")
                else:
                    self.data_check_var.set("✓ 数据完整")
            except ValueError:
                self.data_check_var.set("⚠ 编号需为数字")
        else:
            self.data_check_var.set("⚠ 请填写任务数据")

    def validate_current_task(self, show_message=True):
        """V7.7：在执行插入/入库/打印前可人工检查一次，原有业务校验仍保留。"""
        errors = []
        if not self.template_combo.get().strip():
            errors.append("① 尚未选择模板 ZDNO")
        if not self.new_zdno_prefix_var.get().strip():
            errors.append("② 尚未填写新 ZDNO 前缀")
        try:
            start_num = int(self.start_num_var.get().strip())
            end_num = int(self.end_num_var.get().strip())
            if start_num > end_num:
                errors.append("③ 开始编号不能大于结束编号")
            if end_num - start_num + 1 > 50:
                errors.append("③ 编号总数不能超过 50")
        except ValueError:
            errors.append("③ 开始/结束编号必须为数字")
        valid = 0
        total_pieces = 0
        total_qty = 0
        for i, row in enumerate(self.size_rows, 1):
            color = row['color'].get().strip()
            size = row['size_var'].get().strip()
            try:
                pieces = int(row['pieces'].get().strip() or 0)
                qty = int(row['quantity'].get().strip() or 0)
            except ValueError:
                errors.append(f"④ 第 {i} 行件数/数量必须为整数")
                continue
            if not color or not size:
                errors.append(f"④ 第 {i} 行颜色和尺码不能为空")
                continue
            if pieces <= 0 or qty <= 0:
                errors.append(f"④ 第 {i} 行件数和数量必须大于 0")
                continue
            valid += 1
            total_pieces += pieces
            total_qty += pieces * qty
        if valid == 0:
            errors.append("④ 至少需要一行有效的尺码数据")
        if total_qty <= 0:
            errors.append("④ 总数量必须大于 0")
        if errors:
            self.data_check_var.set("⚠ 数据需要检查")
            if show_message:
                messagebox.showwarning("数据检查", "请先处理以下问题：\n\n" + "\n".join(errors))
            return False
        self.data_check_var.set("✓ 数据完整")
        self.status_var.set(f"✓ 检查完成：{total_pieces} 件 / {total_qty} 个")
        if show_message:
            messagebox.showinfo("数据检查", f"当前任务数据完整，可以继续执行。\n\n总件数：{total_pieces}\n总数量：{total_qty}")
        return True

    def _focus_widget(self, widget):
        try:
            widget.focus_set()
            widget.selection_range(0, tk.END)
        except Exception:
            pass
        return 'break'

    def _focus_next_size_row(self, row_frame):
        for i, row in enumerate(self.size_rows):
            if row['frame'] == row_frame:
                if i + 1 < len(self.size_rows):
                    self._focus_widget(self.size_rows[i + 1]['color'])
                else:
                    self.add_size_row()
                    self._focus_widget(self.size_rows[-1]['color'])
                return 'break'
        return 'break'

    def copy_last_size_row(self):
        if not self.size_rows:
            self.add_size_row()
            return
        src = self.size_rows[-1]
        try:
            pieces = src['pieces'].get().strip() or '1'
            qty = src['quantity'].get().strip() or self.qty_var.get().strip() or '50'
        except Exception:
            pieces, qty = '1', '50'
        self._create_size_row(src['size_var'].get().strip() or 'L', src['color'].get().strip(), qty)
        self.size_rows[-1]['pieces'].delete(0, tk.END)
        self.size_rows[-1]['pieces'].insert(0, pieces)
        self.update_header()
        self.update_summary()
        self.canvas.yview_moveto(1.0)
        self.status_var.set("✓ 已复制上一行尺码数据，可直接修改尺码/颜色")

    def new_task(self):
        """V7.7 新建任务：只清空当前生产任务，不删除/修改数据库模板。"""
        if self.template_modified:
            if not messagebox.askyesno("确认新建", "模板工序有未保存修改。\n\n新建任务不会删除数据库数据，但会放弃当前未保存的工序修改。\n\n确定继续吗？"):
                return
        if not messagebox.askyesno("新建任务", "确定清空当前生产任务并保留当前模板吗？"):
            return
        self.template_modified = False
        self.start_num_var.set("1")
        self.end_num_var.set("5")
        self.b_usercode_var.set("")
        self.b_product_combo.set("")
        self.b_product_combo['values'] = []
        self.color_var.set("黑色")
        self.qty_var.set("50")
        self.build_size_rows()
        self.update_zdno_preview()
        self.status_var.set("✓ 已新建任务，当前模板工序保持不变")
        self.new_zdno_prefix_entry.focus_set()

    def _setup_v77_shortcuts(self):
        """V7.7 快捷键：只调用现有功能，不改变业务流程。"""
        self.root.bind('<Control-Return>', lambda e: self.submit())
        self.root.bind('<Control-s>', lambda e: self.save_template_data())
        self.root.bind('<Control-S>', lambda e: self.save_template_data())
        self.root.bind('<Control-p>', lambda e: self.open_barcode_print_from_current())
        self.root.bind('<Control-P>', lambda e: self.open_barcode_print_from_current())
        self.root.bind('<F5>', lambda e: self.load_templates())
        self.root.bind('<F8>', lambda e: self.open_stock_in())
        self.root.bind('<Escape>', lambda e: self._close_top_window())

    def _close_top_window(self):
        try:
            windows = [w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel) and w.winfo_exists()]
            if windows:
                windows[-1].destroy()
        except Exception:
            pass

    def on_zdno_change(self, event):
        self.update_zdno_preview()
        self.update_summary()
        self.update_type_preview()

    def get_type_name(self):
        """取当前选择的类型；为空时回落到默认值。"""
        name = (self.type_name_var.get() or '').strip()
        return name or DEFAULT_TYPE

    def on_type_changed(self, event=None):
        self.update_type_preview()

    def update_type_preview(self):
        """实时显示「原工单号 → 明细制单号」，让用户看清类型替换的效果。"""
        if not hasattr(self, 'type_hint_var'):
            return
        type_name = self.get_type_name()
        try:
            check_len('type_name', type_name, '类型')
        except ValueError as e:
            self.type_hint_var.set(f"⚠ {str(e).splitlines()[0]}")
            return
        prefix = (self.new_zdno_prefix_var.get() or '').strip()
        start = (self.start_num_var.get() or '').strip()
        end = (self.end_num_var.get() or '').strip()
        if not (prefix and start.isdigit() and end.isdigit()):
            self.type_hint_var.set("")
            return
        new_zdno = f"{prefix}{start}-{end}"
        detail_zdno, _p, _n = build_detail_zdno(new_zdno, type_name)
        if detail_zdno == new_zdno:
            self.type_hint_var.set(f"明细制单号：{detail_zdno}（与工单号一致）")
        else:
            self.type_hint_var.set(f"明细制单号：{detail_zdno}")

    def on_color_changed(self, event=None):
        self.confirm_color()
    
    
    def confirm_color(self):
        main_color = self.color_var.get().strip()
        if not main_color:
            messagebox.showwarning("提示", "请输入颜色")
            return
        for row in self.size_rows:
            row['color'].delete(0, tk.END)
            row['color'].insert(0, main_color)
        self.status_var.set(f"✅ 已填充颜色：{main_color}")
    
    def confirm_quantity(self):
        main_qty = self.qty_var.get().strip()
        if not main_qty:
            messagebox.showwarning("提示", "请输入数量")
            return
        try:
            qty = int(main_qty)
            if qty <= 0:
                messagebox.showwarning("提示", "数量必须大于0")
                return
        except ValueError:
            messagebox.showerror("错误", "数量必须为数字")
            return
        for row in self.size_rows:
            row['quantity'].delete(0, tk.END)
            row['quantity'].insert(0, main_qty)
        self.update_summary()
        self.status_var.set(f"✅ 已填充数量：{main_qty}")
    
    def on_style_change(self):
        self.build_size_rows()
        self.status_var.set(f"✅ 已切换到：{self.style_var.get()}")
        self.update_stockin_btn_state()
    
    def update_stockin_btn_state(self):
        """更新入库按钮状态"""
        has_data = False
        for row in self.size_rows:
                try:
                    if (row['color'].get().strip() and 
                        row['size_var'].get().strip() and
                        int(row['pieces'].get().strip() or 0) > 0 and
                        int(row['quantity'].get().strip() or 0) > 0):
                        has_data = True
                        break
                except ValueError:
                    pass
        state = 'normal' if has_data else 'disabled'
        if hasattr(self, 'stockin_btn'):
            self.stockin_btn.config(state=state)
    
    def get_color_data(self):
        """获取当前界面的尺码数据"""
        color_data = []
        if self.mode_var.get() == "汇总":
            total_qty = 0
            first_color = self.color_var.get().strip()
            for row in self.size_rows:
                color = row['color'].get().strip()
                if color and not first_color:
                    first_color = color
                try:
                    pieces = int(row['pieces'].get().strip() or 0)
                    qty = int(row['quantity'].get().strip() or 0)
                    if color and pieces > 0 and qty > 0:
                        total_qty += pieces * qty
                except ValueError:
                    pass
            if first_color and total_qty > 0:
                color_data.append({
                    'color': first_color,
                    'size': '均码',
                    'pieces': 1,
                    'quantity': total_qty
                })
        else:
            for row in self.size_rows:
                color = row['color'].get().strip()
                size = row['size_var'].get().strip()
                try:
                    pieces = int(row['pieces'].get().strip() or 0)
                    qty = int(row['quantity'].get().strip() or 0)
                    if color and size and pieces > 0 and qty > 0:
                        color_data.append({
                            'color': color,
                            'size': size,
                            'pieces': pieces,
                            'quantity': qty
                        })
                except ValueError:
                    pass
        return color_data
    
    def open_stock_in_old(self):
        """打开入库对话框"""
        color_data = self.get_color_data()
        if not color_data:
            messagebox.showwarning("提示", "请先填写有效的颜色、尺码、件数和数量数据")
            return
        
        total_qty = sum(item['pieces'] * item['quantity'] for item in color_data)
        if total_qty <= 0:
            messagebox.showwarning("提示", "总数量必须大于0")
            return
        
        dialog = StockInDialog(
            self.root, 
            self, 
            color_data, 
            self.style_var.get(),
            self.mode_var.get()
        )
        self.root.wait_window(dialog.dialog)

    def open_stock_in(self):
        """跳过 Dialog 弹窗，直接调用 handle_direct_stock_in 执行后台入库"""
        color_data = self.get_color_data()
        if not color_data:
            messagebox.showwarning("提示", "请先填写有效的颜色、尺码、件数和数量数据")
            return
        
        total_qty = sum(item['pieces'] * item['quantity'] for item in color_data)
        if total_qty <= 0:
            messagebox.showwarning("提示", "总数量必须大于0")
            return

        # 获取关联货号（优先使用下拉选择的货号，其次使用输入框的货号）
        target_usercode = self.b_product_combo.get().strip() or self.b_usercode_var.get().strip()
        if not target_usercode:
            messagebox.showwarning("提示", "请先在「4. 关联货号」中选择或输入关联货号！")
            return

        prefix = self.new_zdno_prefix_var.get().strip()
        start_no = self.start_num_var.get().strip()
        end_no = self.end_num_var.get().strip()
        item_code = f"{prefix}{start_no}-{end_no}" if prefix else ""

        # 1. 组装 handle_direct_stock_in 需要的数据结构
        record_data = {
            'target_usercode': target_usercode,
            'item_usercode': item_code,
            'start_no': start_no,
            'end_no': end_no,
            'price': 52.00,
            # 将界面格式（pieces * quantity）转换为 handle_direct_stock_in 期待的格式
            'color_data': [
                {
                    'color': item['color'],
                    'size': item['size'],
                    'qty': item['pieces'] * item['quantity']
                }
                for item in color_data
            ]
        }

        # 2. 交互确认（如果不希望弹窗二次确认，可注释掉下面 3 行）
        confirm_msg = f"确认直接执行入库吗？\n\n关联货号: {target_usercode}\n单号/标识: {item_code}\n总入库件数: {total_qty} 件"
        if not messagebox.askyesno("直接入库确认", confirm_msg):
            return

        # 3. 直接调用 handle_direct_stock_in 函数
        self.status_var.set("⏳ 正在执行直接入库...")
        self.root.update()

        success, msg, result_data = handle_direct_stock_in(record_data)
        
        if success:
            # 从返回的 details 列表中提取颜色并去重
            details = result_data.get('details', [])
            colors = list(set(d['color'] for d in details if 'color' in d))
            color_str = ",".join(colors) if colors else "未知颜色"
    
            in_number = result_data.get('in_number', '未知单号')
            
            # 直接从返回的字典中拿到单号，无需正则匹配
            # in_number = result_data.get('in_number', '未知单号') if result_data else '未知单号'
            log_record = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | [直接入库] {target_usercode} | 单号:{in_number} | 货号:{item_code} | 颜色：{color_str} | 总数:{total_qty}件 | 结果:成功"
            write_log(log_record)
            messagebox.showinfo("✅ 入库成功", msg)
            self.status_var.set(f"✅ 入库成功: {msg}")
        else:
            messagebox.showerror("❌ 入库失败", msg)
            self.status_var.set(f"❌ 入库失败: {msg}")



    # ==================== 条码打印功能 ====================
    def collect_current_print_data(self):
        template_zdno = self.template_combo.get().strip()
        prefix = self.new_zdno_prefix_var.get().strip()
        start_str = self.start_num_var.get().strip()
        end_str = self.end_num_var.get().strip()
        mode = self.mode_var.get()
        if not template_zdno or not prefix or not start_str or not end_str:
            raise ValueError("请先填写模板ZDNO、ZDNO前缀、开始编号和结束编号")
        start_num = int(start_str)
        end_num = int(end_str)
        if start_num > end_num:
            raise ValueError("开始编号不能大于结束编号")
        if end_num - start_num + 1 > 50:
            raise ValueError("编号总数不能超过50")
        new_zdno = f"{prefix}{start_num}-{end_num}"

        # 汇总和明细打印统一从UI尺码数据读取。
        color_data = []
        for row in self.size_rows:
            color = row['color'].get().strip()
            size = row['size_var'].get().strip()
            pieces = row['pieces'].get().strip()
            qty = row['quantity'].get().strip()
            if not color or not size or not pieces or not qty:
                continue
            try:
                pieces = int(pieces)
                qty = int(qty)
            except ValueError:
                continue
            if pieces > 0 and qty > 0:
                color_data.append({"color": color, "size": size, "pieces": pieces, "quantity": qty})
        if not color_data:
            raise ValueError("没有有效的尺码数据，请填写颜色、尺码、件数和数量")

        zdid = get_zdid_by_zdno(new_zdno)
        processes = get_print_processes(new_zdno)
        if not processes:
            raise ValueError(f"{new_zdno} 没有工序数据，无法生成条码")
        items = build_barcode_items(
            new_zdno, zdid, start_num, end_num, mode, color_data, processes,
            self.style_var.get(), **_qr_opts())

        return {
            "zdno": new_zdno,
            "template_zdno": template_zdno,
            "zdid": zdid,
            "start_num": start_num,
            "end_num": end_num,
            "mode": mode,
            "style": self.style_var.get(),
            "color_data": color_data,
            "items": items
        }

    def choose_barcode_template_and_preview(self, print_data, history_id=None):
        """模板选择必须发生在预览之前。"""
        win = tk.Toplevel(self.root)
        win.title("选择条码打印模板")
        win.geometry("500x300")
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()

        ttk.Label(win, text="选择打印模板", font=("Arial", 14, "bold")).pack(pady=(25, 12))
        template_var = tk.StringVar(value="数量汇总模板" if print_data.get("mode") == "汇总" else "明细模板")
        ttk.Radiobutton(win, text="📦 数量汇总模板", variable=template_var, value="数量汇总模板").pack(anchor="w", padx=90, pady=8)
        ttk.Radiobutton(win, text="📋 明细模板", variable=template_var, value="明细模板").pack(anchor="w", padx=90, pady=8)
        ttk.Label(win, text="选择后点击【预览】，预览内容与所选模板完全对应。", foreground="gray").pack(pady=8)

        def preview():
            template_name = template_var.get()
            # 当前数据库数据不需要再次插入；只是根据选择模板生成对应打印内容。
            mode_for_print = "汇总" if template_name == "数量汇总模板" else "明细"
            data = dict(print_data)
            # 汇总模板：按每个编号+工序输出，但数量取汇总数量。
            # 明细模板：使用原始颜色/尺码/数量分配。
            if mode_for_print == "汇总":
                base_color = ""
                total_qty = 0
                source_color_data = data.get("color_data", [])
                if source_color_data:
                    base_color = str(source_color_data[0].get("color", ""))
                    for row in source_color_data:
                        try:
                            total_qty += int(row.get("pieces", 0) or 0) * int(row.get("quantity", 0) or 0)
                        except (ValueError, TypeError):
                            pass
                summary_color_data = [{"color": base_color, "size": "均码", "pieces": 1, "quantity": total_qty}]
                detail_source_color_data = [dict(x) for x in source_color_data]
                processes = get_print_processes(data["zdno"])
                data["items"] = build_barcode_items(data["zdno"], data["zdid"], data["start_num"], data["end_num"], "汇总", summary_color_data, processes, data.get("style", ""), **_qr_opts())
                for _item in data["items"]:
                    _item["qr_color_data"] = detail_source_color_data
            else:
                processes = get_print_processes(data["zdno"])
                data["items"] = build_barcode_items(data["zdno"], data["zdid"], data["start_num"], data["end_num"], "明细", data.get("color_data", []), processes, data.get("style", ""), **_qr_opts())

            try:
                out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "条码打印文件")
                os.makedirs(out_dir, exist_ok=True)
                filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{data['zdno']}_{template_name}.pdf"
                pdf_path = os.path.join(out_dir, filename)
                cfg = load_barcode_config()
                pdf_path = create_barcode_pdf(data["items"], template_name, cfg, pdf_path)
                self.last_barcode_pdf = pdf_path
                win.destroy()
                self.show_barcode_preview(data, template_name, pdf_path, history_id=history_id)
            except Exception as e:
                messagebox.showerror("生成预览失败", str(e), parent=win)

        btns = ttk.Frame(win)
        btns.pack(side="bottom", pady=20)
        ttk.Button(btns, text="取消", command=win.destroy, width=12).pack(side="left", padx=8)
        ttk.Button(btns, text="🔍 预览", command=preview, width=16).pack(side="left", padx=8)

    def show_barcode_preview(self, data, template_name, pdf_path, history_id=None):
        def do_print(path, printer_name):
            if not messagebox.askyesno(
                "确认打印",
                f"确定打印当前预览吗？\n\n模板：{template_name}\n标签数量：{len(data.get('items', []))}",
                parent=self.root
            ):
                return
            try:
                ok, print_msg = print_pdf_windows(path, printer_name)
                if ok:
                    messagebox.showinfo("打印", print_msg, parent=self.root)
                else:
                    raise RuntimeError("未能发送到打印机")

                # 第一次生成任务时立即保存历史，这样即使直接打印失败、改为PDF手动打印，
                # 也不会丢失“重新打印历史条码”记录。
                if history_id is None:
                    record = dict(data)
                    record["history_id"] = datetime.now().strftime('%Y%m%d%H%M%S%f')
                    record["created_at"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    record["template_name"] = template_name
                    record["config"] = load_barcode_config()
                    save_barcode_history(record)
            except Exception as e:
                messagebox.showerror(
                    "打印失败",
                    f"打印调用失败：\n{e}\n\n你也可以点击预览窗口的【打开PDF】，再从PDF阅读器手动打印。",
                    parent=self.root
                )

        BarcodePreviewWindow(self.root, pdf_path, template_name, on_print=do_print)

    def open_barcode_print_from_current(self):
        try:
            data = self.collect_current_print_data()
            self.choose_barcode_template_and_preview(data)
        except Exception as e:
            messagebox.showerror("条码预览", str(e))

    def open_stat_window(self):
        """打开 ZDNO 维护窗口并直接切到「④ 裁剪统计 / 导出」页。"""
        win = self._open_zdno_window()
        if win is None:
            return
        try:
            win.notebook.select(3)
            win.lift()
            win.focus_force()
            self.status_var.set("📈 已打开裁剪统计（可按时间段查询并导出 Excel）")
        except Exception as e:
            self.status_var.set(f"⚠ 打开裁剪统计失败：{e}")

    def _open_zdno_window(self):
        if _zdno_edit is None:
            messagebox.showerror(
                "ZDNO 维护不可用",
                f"未能加载 zdno_edit.py：\n{getattr(_zdno_edit, '__file__', '')}\n\n"
                f"原因：{globals().get('_zdno_edit_import_error', '未知错误')}\n\n"
                f"请确认 zdno_edit.py 与本程序放在同一目录下。")
            return None
        try:
            return _zdno_edit.open_zdno_editor(self.root, conn_str, on_log=self.append_log)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.append_log(f"[ZDNO维护] 打开失败：{e}")
            messagebox.showerror("ZDNO 维护", f"打开窗口失败：{e}")
            return None

    def open_zdno_editor_window(self):
        """打开 ZDNO 数据维护窗口（修改单价 / 工序名称 / 颜色 / 数量）。"""
        if _zdno_edit is None:
            messagebox.showerror(
                "ZDNO 维护不可用",
                f"未能加载 zdno_edit.py：\n{getattr(_zdno_edit, '__file__', '')}\n\n"
                f"原因：{globals().get('_zdno_edit_import_error', '未知错误')}\n\n"
                f"请确认 zdno_edit.py 与本程序放在同一目录下。")
            return
        try:
            win = _zdno_edit.open_zdno_editor(self.root, conn_str, on_log=self.append_log)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.append_log(f"[ZDNO维护] 打开失败：{e}")
            messagebox.showerror("ZDNO 维护", f"打开窗口失败：{e}")
            return

        # 把当前选中的模板带过去，省一次手工输入
        try:
            current = self.template_combo.get().strip()
        except Exception:
            current = ""
        if current and getattr(win, "current_zdno", None) != current:
            try:
                win.search_var.set(current)
                win.load_zdno(current)
                self.append_log(f"[ZDNO维护] 已打开 {current}")
            except Exception as e:
                self.append_log(f"[ZDNO维护] 自动载入 {current} 失败：{e}")

    def open_write_detail(self):
        """「📝 写入明细」：把界面上填的尺码数据写入明细表。

        和「补录明细表」的区别：
          * 本按钮读的是界面上正在填的数据，不要求工单已经存在于数据库；
          * 「补录明细表」读的是数据库里已有的 jfzd2 历史数据。
        两者都只写明细（每个顺序号一行），汇总数据不入表，由报表统计得出。
        """
        mode = self.mode_var.get()
        if mode == "汇总":
            messagebox.showwarning(
                "写入明细只收明细数据",
                "当前模式是「汇总模式」，界面上只有汇总数量，没有逐个顺序号的明细。\n\n"
                "写入明细需要的是「明细模式」下的尺码数据（每个顺序号对应一个颜色/尺码）。\n\n"
                "如果只是想在报表上看汇总，请直接点「📈 裁剪统计」，汇总由报表自动统计。",
                parent=self.root)
            return

        try:
            start_num = int(self.start_num_var.get().strip())
            end_num = int(self.end_num_var.get().strip())
        except ValueError:
            messagebox.showerror("编号有误", "开始编号和结束编号必须是数字。", parent=self.root)
            return
        if start_num > end_num:
            messagebox.showerror("编号有误", "开始编号不能大于结束编号。", parent=self.root)
            return

        prefix = self.new_zdno_prefix_var.get().strip()
        if not prefix:
            messagebox.showwarning(
                "还缺工单前缀",
                "请先在「2. 输入新 ZDNO」里填前缀，明细表需要一个工单标识。",
                parent=self.root)
            return

        color_data = self.get_color_data()
        if not color_data:
            messagebox.showwarning(
                "没有数据",
                "界面上没有有效的尺码数据。\n"
                "请在右侧「4. 尺码数据」里填写颜色、尺码、件数、数量（都要大于0）。",
                parent=self.root)
            return

        type_name = self.get_type_name()
        zdno_display = f"{prefix}{start_num}-{end_num}"
        ok, msg, rows = build_detail_rows_from_ui(color_data, start_num, end_num,
                                                  zdno_display, type_name, mode)
        if not ok:
            messagebox.showerror("无法写入明细", msg, parent=self.root)
            return

        detail_zdno = rows[0][0]
        total_qty = sum(r[7] for r in rows)

        already_rows = already_qty = 0
        try:
            conn = get_conn(conn_str)
            already_rows, already_qty = count_detail_in_range(
                conn, detail_zdno, zdno_display, start_num, end_num)
        except Exception:
            pass

        lines = [f"制单号：{detail_zdno}", f"原始标识：{zdno_display}",
                 f"顺序号：{start_num}~{end_num}（共 {end_num - start_num + 1} 个）",
                 f"类型：{type_name}", "",
                 f"将写入 {len(rows)} 行明细，合计 {total_qty:,} 件：", ""]
        by_color = {}
        for r in rows:
            key = (r[5], r[6])
            by_color.setdefault(key, [0, 0])
            by_color[key][0] += 1
            by_color[key][1] += r[7]
        for (ys, cm), (cnt, q) in sorted(by_color.items(), key=lambda x: -x[1][1]):
            lines.append(f"  {ys} / {cm}：{cnt} 个顺序号，合计 {q:,} 件")

        if already_rows:
            lines += ["",
                      f"⚠ 注意：该制单号在这个编号范围里已经写过 {already_rows} 行、"
                      f"{already_qty:,} 件。",
                      "重复写入会让统计数量翻倍，确定要继续吗？"]

        lines += ["", "确认写入明细表 jfzd2_detail？"]
        if not messagebox.askyesno("确认写入明细", "\n".join(lines), parent=self.root):
            return

        conn = get_conn(conn_str, autocommit=False)
        try:
            ok2, msg2 = write_detail_rows(conn, rows)
            if not ok2:
                messagebox.showerror("写入失败", msg2, parent=self.root)
                self.status_var.set(f"❌ {msg2}")
                return
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            messagebox.showerror("写入失败", friendly_error(exc), parent=self.root)
            return
        finally:
            try:
                conn.autocommit = True
            except Exception:
                pass

        save_write_snapshot({
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'action': '写入明细',
            'zdno': zdno_display,
            'table': 'jfzd2_detail',
            'before': {},
            'detail_inserted': len(rows),
            'detail_zdno': detail_zdno,
            'detail_type': type_name,
            'order_begin': start_num,
            'order_end': end_num,
        })
        write_audit("写入明细", f"{zdno_display} | 制单号 {detail_zdno} | 类型 {type_name} | "
                                f"{len(rows)}行 {total_qty}件")
        write_log(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | [写入明细] "
                  f"{zdno_display} | {detail_zdno} | {type_name} | {len(rows)}行 {total_qty}件")
        self.status_var.set(f"✅ 已写入明细 {len(rows)} 行，合计 {total_qty:,} 件")
        messagebox.showinfo(
            "写入完成",
            f"{msg2}\n\n制单号：{detail_zdno}\n合计 {len(rows)} 行 / {total_qty:,} 件\n\n"
            f"如需撤销，可点「↩ 撤销上次写库」。\n"
            f"点「📈 裁剪统计」可查看并导出 Excel。",
            parent=self.root)

    def open_detail_backfill(self):
        """打开「补录明细表」窗口：把指定 ZDNO 的 jfzd2 明细写入 jfzd2_detail。

        执行插入时不会顺带写明细表，这里是唯一的入口，
        因此历史工单也能按需补录。
        """
        existing = getattr(self, "_detail_win", None)
        if existing is not None and existing.winfo_exists():
            try:
                existing.deiconify()
                existing.lift()
                existing.focus_force()
                return
            except tk.TclError:
                pass

        win = tk.Toplevel(self.root)
        self._detail_win = win
        win.title("补录明细表 jfzd2_detail")
        win.geometry("760x620")
        win.minsize(680, 540)
        win.transient(self.root)

        top = ttk.Frame(win, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="📊 补录明细表", font=("Arial", 13, "bold")).pack(side="left")
        ttk.Label(top, text="执行插入时不会写这张表，需要在这里单独补录",
                  foreground="gray", font=("Arial", 9)).pack(side="left", padx=10)

        # ---- ZDNO ----
        f1 = ttk.LabelFrame(win, text="1. 要补录的工单", padding=8)
        f1.pack(fill="x", padx=10, pady=(4, 6))
        ttk.Label(f1, text="ZDNO：").pack(side="left")
        self._bd_zdno_var = tk.StringVar()
        e1 = ttk.Entry(f1, textvariable=self._bd_zdno_var)
        e1.pack(side="left", fill="x", expand=True, padx=4)
        e1.bind("<Return>", lambda ev: self._bd_preview())
        ttk.Button(f1, text="用当前模板", width=10,
                   command=self._bd_use_template).pack(side="left", padx=2)
        ttk.Label(f1, text="（多个工单用逗号或空格分隔，例如 268810套装7-12,268810单衣1-8）",
                  foreground="gray", font=("Arial", 8)).pack(side="left", padx=4)

        # ---- 类型 / 模式 ----
        f2 = ttk.LabelFrame(win, text="2. 类型与模式", padding=8)
        f2.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(f2, text="类型：").pack(side="left")
        self._bd_type_var = tk.StringVar(value=DEFAULT_TYPE)
        self._bd_type_combo = ttk.Combobox(f2, textvariable=self._bd_type_var,
                                           values=list(TYPE_PRESETS), width=10)
        self._bd_type_combo.pack(side="left", padx=(0, 4))
        ttk.Label(f2, text="可下拉选，也可自己输入", foreground="gray",
                  font=("Arial", 8)).pack(side="left", padx=(0, 10))
        ttk.Label(f2, text="数据模式：").pack(side="left")
        self._bd_mode_var = tk.StringVar(value="明细")
        ttk.Combobox(f2, textvariable=self._bd_mode_var, values=("明细", "汇总"),
                     width=7, state="readonly").pack(side="left", padx=(0, 10))
        ttk.Button(f2, text="🔄 自动识别类型", width=14,
                   command=self._bd_guess_type).pack(side="left", padx=2)
        ttk.Button(f2, text="🔍 预览", width=8, style="Accent.TButton",
                   command=self._bd_preview).pack(side="left", padx=2)

        # ---- 预览 ----
        f3 = ttk.LabelFrame(win, text="3. 预览（确认后才会写库）", padding=8)
        f3.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self._bd_text = tk.Text(f3, wrap="none", height=12)
        sb = ttk.Scrollbar(f3, orient="vertical", command=self._bd_text.yview)
        self._bd_text.configure(yscrollcommand=sb.set)
        self._bd_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # ---- 操作 ----
        bottom = ttk.Frame(win, padding=(10, 0, 10, 10))
        bottom.pack(fill="x")
        self._bd_status = tk.StringVar(value="填写 ZDNO 后点「🔍 预览」")
        ttk.Label(bottom, textvariable=self._bd_status, foreground="#0066cc",
                  font=("Arial", 9)).pack(side="left")
        ttk.Button(bottom, text="✅ 确认补录", width=14,
                   command=self._bd_commit).pack(side="right", padx=2)
        ttk.Button(bottom, text="关闭", width=8,
                   command=win.destroy).pack(side="right")

        try:
            self._bd_use_template()
        except Exception:
            pass

    def _bd_use_template(self):
        try:
            current = self.template_combo.get().strip()
        except Exception:
            current = ""
        if current:
            self._bd_zdno_var.set(current)
            self._bd_guess_type()

    def _bd_guess_type(self):
        """按 ZDNO 里出现的类型词自动选择类型。"""
        text = self._bd_zdno_var.get() or ""
        first = [p for p in re.split(r'[,\s，、]+', text) if p]
        if not first:
            return
        guessed = guess_type_from_zdno(first[0])
        if guessed:
            self._bd_type_var.set(guessed)
            self._bd_status.set(f"已根据工单号识别为「{guessed}」，可手工改")

    def _bd_preview(self):
        text = self._bd_zdno_var.get() or ""
        type_name = (self._bd_type_var.get() or "").strip() or DEFAULT_TYPE
        mode = self._bd_mode_var.get()
        if not text.strip():
            self._bd_status.set("❌ 请先填写要补录的 ZDNO")
            messagebox.showwarning("提示", "请先填写要补录的 ZDNO", parent=self._detail_win)
            return
        try:
            check_len('type_name', type_name, '类型')
        except ValueError as e:
            first = str(e).splitlines()[0]
            self._bd_status.set(f"❌ {first}")
            messagebox.showerror("类型有误", str(e), parent=self._detail_win)
            return

        conn = get_conn(conn_str)
        lines = []
        total_rows = 0
        total_qty = 0
        parts = [p for p in re.split(r'[,\s，、]+', text) if p]
        for z in parts:
            ok, msg, rows = build_detail_rows(conn, z, type_name, mode)
            if not ok:
                lines.append(f"✗ {msg}")
                continue
            already = count_detail_existing(conn, z)
            total_rows += len(rows)
            total_qty += sum(r[7] for r in rows)
            flag = (f"　⚠ 明细表已有 {already} 行，补录后会重复" if already
                    else "　✓ 尚未补录")
            lines.append(f"✓ {msg}{flag}")
            by_color = {}
            for r in rows:
                by_color[(r[5], r[6])] = by_color.get((r[5], r[6]), 0) + r[7]
            for (ys, cm), q in sorted(by_color.items(), key=lambda x: -x[1])[:12]:
                lines.append(f"      {ys or '(空)'} / {cm or '(空)'} = {q} 件")
            if len(by_color) > 12:
                lines.append(f"      …… 另有 {len(by_color) - 12} 个颜色/尺码组合")

        lines.append("")
        lines.append(f"合计将写入 {total_rows} 行，{total_qty:,} 件")
        if total_rows:
            lines.append("（重复补录不会报错，但会让统计数量翻倍，请留意上面标 ⚠ 的工单）")

        self._bd_text.delete("1.0", tk.END)
        self._bd_text.insert("1.0", "\n".join(lines))
        self._bd_status.set(f"预览完成：{total_rows} 行 / {total_qty:,} 件")
        return total_rows

    def _bd_commit(self):
        text = self._bd_zdno_var.get() or ""
        if not text.strip():
            self._bd_status.set("❌ 请先填写要补录的 ZDNO")
            messagebox.showwarning("提示", "请先填写要补录的 ZDNO", parent=self._detail_win)
            return
        type_name = (self._bd_type_var.get() or "").strip() or DEFAULT_TYPE
        mode = self._bd_mode_var.get()

        ok, msg, all_rows = backfill_detail(text, type_name, mode)
        if not ok:
            first = msg.splitlines()[0]
            self._bd_status.set(f"❌ {first}")
            messagebox.showerror("无法补录", msg, parent=self._detail_win)
            return
        if not all_rows:
            self._bd_status.set("❌ 没有可补录的明细")
            messagebox.showwarning("没有数据", "没有可补录的明细。", parent=self._detail_win)
            return

        total_qty = sum(r[7] for r in all_rows)
        detail = f"工单：{text.strip()}\n类型：{type_name}\n模式：{mode}\n\n" \
                 f"{msg}\n\n合计 {len(all_rows)} 行 / {total_qty:,} 件\n\n确认写入明细表？"
        if not messagebox.askyesno("确认补录", detail, parent=self._detail_win):
            return

        conn = get_conn(conn_str, autocommit=False)
        try:
            ok2, msg2 = write_detail_rows(conn, all_rows)
            if not ok2:
                messagebox.showerror("补录失败", msg2, parent=self._detail_win)
                self._bd_status.set(f"❌ {msg2}")
                return
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            messagebox.showerror("补录失败", friendly_error(e), parent=self._detail_win)
            return
        finally:
            try:
                conn.autocommit = True
            except Exception:
                pass

        save_write_snapshot({
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'action': '补录明细表',
            'zdno': ','.join(re.split(r'[,\s，、]+', text.strip()) if
                             [x for x in re.split(r'[,\s，、]+', text.strip())] else []),
            'table': 'jfzd2_detail',
            'before': {},
            'detail_inserted': len(all_rows),
            'detail_type': type_name,
        })
        write_audit("补录明细表", f"{text.strip()} | 类型 {type_name} | {len(all_rows)}行 {total_qty}件")

        messagebox.showinfo("补录完成",
                            f"{msg2}\n合计 {len(all_rows)} 行 / {total_qty:,} 件\n\n"
                            f"如需撤销，可点主界面的「↩ 撤销上次写库」。",
                            parent=self._detail_win)
        self._bd_status.set(f"✅ 已补录 {len(all_rows)} 行 / {total_qty:,} 件")
        self._bd_preview()
        self.append_log(f"[补录明细表] {text.strip()} | 类型 {type_name} | {len(all_rows)}行 {total_qty}件")

    def undo_last_write(self):
        """撤销上一次写库操作（保存模板 / 删除模板 / 插入新工单）。"""
        snap = load_write_snapshot()
        if not snap:
            messagebox.showinfo("无可撤销", "没有找到写库快照。\n\n"
                                          "只有保存模板、删除模板、插入新工单会生成快照。")
            self.status_var.set("ℹ️ 没有可撤销的写库记录")
            return

        action = snap.get('action', '')
        zdno = snap.get('zdno', '')
        explain = {
            '删除模板': f"把 {zdno} 在四张表中被删除的数据全部还原回来。",
            '插入新工单': f"把刚插入的 {zdno} 在四张表中的记录删除掉。",
            '保存模板工序': f"把 {zdno} 的 jfdj 工序还原成保存之前的状态。",
        }.get(action, "按快照还原数据。")

        if not messagebox.askyesno(
                "撤销上次写库",
                f"上一次写库操作：{action}\n"
                f"时间：{snap.get('time')}\n"
                f"ZDNO：{zdno}\n\n"
                f"{explain}\n\n确认执行撤销？"):
            return

        self.status_var.set("⏳ 正在撤销上次写库...")
        self.root.update_idletasks()
        try:
            ok, msg = undo_last_write()
        except Exception as e:
            ok, msg = False, friendly_error(e)

        if ok:
            write_log(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | [撒销写库] {zdno} | {msg}")
            self.template_modified = False
            self.load_templates()
            self.template_combo.set(zdno)
            self.view_template_data(force=True)
            messagebox.showinfo("撒销成功", msg)
            self.status_var.set(f"✅ {msg}")
        else:
            messagebox.showerror("撒销失败", msg)
            self.status_var.set(f"❌ {msg}")

    def open_barcode_history(self):
        history = load_barcode_history()
        win = tk.Toplevel(self.root)
        win.title("重新打印历史条码")
        win.geometry("1050x600")
        win.minsize(900, 500)
        win.transient(self.root)

        top = ttk.Frame(win, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="📚 条码打印历史", font=("Arial", 13, "bold")).pack(side="left")
        ttk.Label(top, text="双击记录可预览", foreground="gray").pack(side="left", padx=12)

        tree_frame = ttk.Frame(win, padding=(8, 0, 8, 0))
        tree_frame.pack(fill="both", expand=True)
        cols = ("time", "zdno", "mode", "template", "range", "count")
        tree = ttk.Treeview(tree_frame, columns=cols, show="headings")
        headings = {
            "time": "打印时间", "zdno": "ZDNO", "mode": "数据模式",
            "template": "打印模板", "range": "编号范围", "count": "标签数"
        }
        widths = {"time": 145, "zdno": 190, "mode": 80, "template": 130, "range": 100, "count": 80}
        for col in cols:
            tree.heading(col, text=headings[col])
            tree.column(col, width=widths[col], anchor="center")
        sb = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        for idx, rec in enumerate(history):
            tree.insert("", "end", iid=str(idx), values=(
                rec.get("created_at", ""),
                rec.get("zdno", ""),
                rec.get("mode", ""),
                rec.get("template_name", ""),
                f"{rec.get('start_num', '')}-{rec.get('end_num', '')}",
                len(rec.get("items", []))
            ))

        bottom = ttk.Frame(win, padding=8)
        bottom.pack(fill="x")

        def get_selected():
            sel = tree.selection()
            if not sel:
                messagebox.showwarning("提示", "请先选择一条历史记录", parent=win)
                return None
            return history[int(sel[0])]

        def preview_history():
            rec = get_selected()
            if not rec:
                return
            self.choose_barcode_template_and_preview(rec, history_id=rec.get("history_id"))

        def direct_reprint():
            rec = get_selected()
            if not rec:
                return
            template_name = rec.get("template_name", "历史模板")
            items = rec.get("items", [])
            try:
                cfg = rec.get("config") or load_barcode_config()
                out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "条码打印文件")
                os.makedirs(out_dir, exist_ok=True)
                path = os.path.join(out_dir, f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{rec.get('zdno','')}_重打.pdf")
                create_barcode_pdf(items, template_name, cfg, path)
                self.show_barcode_preview(rec, template_name, path, history_id=rec.get("history_id"))
            except Exception as e:
                messagebox.showerror("失败", str(e), parent=win)

        def delete_history():
            sel = tree.selection()
            if not sel:
                return
            idx = int(sel[0])
            rec = history[idx]
            if not messagebox.askyesno("确认", f"确定删除历史记录？\n{rec.get('zdno', '')} {rec.get('template_name', '')}", parent=win):
                return
            history.pop(idx)
            with open(BARCODE_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
            tree.delete(str(idx))
            # Tree iid 重新整理
            for i, item in enumerate(tree.get_children()):
                tree.item(item, iid=str(i))
            messagebox.showinfo("成功", "历史记录已删除", parent=win)

        ttk.Button(bottom, text="🔍 预览", command=preview_history, width=14).pack(side="left", padx=4)
        ttk.Button(bottom, text="🖨 重新预览并打印", command=direct_reprint, width=18).pack(side="left", padx=4)
        ttk.Button(bottom, text="🗑 删除记录", command=delete_history, width=14).pack(side="right", padx=4)
        tree.bind("<Double-1>", lambda e: preview_history())

    def open_barcode_settings(self):
        cfg = load_barcode_config()
        win = tk.Toplevel(self.root)
        win.title("条码打印尺寸设置（物理尺寸）")
        win.geometry("580x755")
        win.transient(self.root)
        win.grab_set()

        fields = [
            ("纸张宽度(mm)", "page_width_mm", "float"),
            ("纸张高度(mm)", "page_height_mm", "float"),
            ("左/右标签宽度(mm)", "label_width_mm", "float"),
            ("中间空白(mm)", "center_gap_mm", "float"),
            ("列数", "columns", "int"),
            ("行数", "rows", "int"),
            ("左边距(mm)", "left_margin_mm", "float"),
            ("顶部留白(mm)", "top_margin_mm", "float"),
            ("底部留白(mm)", "bottom_margin_mm", "float"),
            ("横向偏移(mm)", "horizontal_offset_mm", "float"),
            ("第一列单独横向偏移(mm)", "first_column_offset_mm", "float"),
            ("纵向偏移(mm)", "vertical_offset_mm", "float"),
            ("中间内容距顶部(mm)", "center_top_distance_mm", "float"),
            ("中间内容行距(mm)", "center_line_leading", "float"),
            ("条码高度(mm)", "barcode_height_mm", "float"),
            ("条码目标宽度(mm)", "barcode_width_mm", "float"),
            ("标题字体(pt)", "title_font_size", "float"),
            ("正文/数量字体(pt)", "body_font_size", "float"),
        ]
        entries = {}
        frm = ttk.Frame(win, padding=14)
        frm.pack(fill="both", expand=True)
        for r, (label, key, typ) in enumerate(fields):
            ttk.Label(frm, text=label, width=24).grid(row=r, column=0, sticky="w", pady=3)
            ent = ttk.Entry(frm, width=20)
            ent.insert(0, str(cfg.get(key, "")))
            ent.grid(row=r, column=1, sticky="w", pady=3)
            entries[key] = (ent, typ)

        info = ttk.Label(frm, text="物理尺寸模式：保存后立即生效。标签高度不需要手动填写，系统会根据纸张高度、顶部/底部留白和行数自动计算。\n默认：A4 210×297mm｜86 + 38 + 86 = 210mm｜上下各8mm｜19行。\n第一列可单独横向微调，例如 +1.0mm 表示第一列整体向右移动1mm；负数表示向左移动。\n打印时使用100%原始尺寸，不自动缩放。", foreground="#555555", justify="left", wraplength=510)
        info.grid(row=len(fields), column=0, columnspan=2, sticky="w", pady=(12, 8))

        result_var = tk.StringVar()
        ttk.Label(frm, textvariable=result_var, foreground="#0066aa", wraplength=510).grid(row=len(fields)+1, column=0, columnspan=2, sticky="w", pady=4)

        def update_calc(*_):
            try:
                pw = float(entries["page_width_mm"][0].get())
                ph = float(entries["page_height_mm"][0].get())
                lw = float(entries["label_width_mm"][0].get())
                gap = float(entries["center_gap_mm"][0].get())
                rows = int(float(entries["rows"][0].get()))
                lm = float(entries["left_margin_mm"][0].get())
                first_off = float(entries["first_column_offset_mm"][0].get())
                top = float(entries["top_margin_mm"][0].get())
                bottom = float(entries["bottom_margin_mm"][0].get())
                row_h = (ph-top-bottom)/rows if rows > 0 else 0
                used_w = lm + lw*2 + gap
                result_var.set(f"计算结果：每行高度 {row_h:.4f} mm；横向占用 {used_w:.2f} / {pw:.2f} mm；第一列额外偏移 {first_off:+.2f} mm")
            except Exception:
                result_var.set("计算结果：请输入有效的数字。")

        for ent, _ in entries.values():
            ent.bind("<KeyRelease>", update_calc)
        update_calc()

        def save():
            try:
                new_cfg = dict(cfg)
                for key, (ent, typ) in entries.items():
                    value = ent.get().strip()
                    if not value:
                        raise ValueError(f"{key} 不能为空")
                    new_cfg[key] = int(float(value)) if typ == "int" else float(value)

                if new_cfg["page_width_mm"] <= 0 or new_cfg["page_height_mm"] <= 0:
                    raise ValueError("纸张宽度和高度必须大于0")
                if new_cfg["columns"] != 2:
                    raise ValueError("当前模板固定为2列")
                if new_cfg["rows"] <= 0:
                    raise ValueError("行数必须大于0")
                if new_cfg["top_margin_mm"] < 0 or new_cfg["bottom_margin_mm"] < 0:
                    raise ValueError("顶部/底部留白不能小于0")
                if new_cfg["top_margin_mm"] + new_cfg["bottom_margin_mm"] >= new_cfg["page_height_mm"]:
                    raise ValueError("上下留白之和必须小于纸张高度")
                used_w = new_cfg["left_margin_mm"] + new_cfg["label_width_mm"]*2 + new_cfg["center_gap_mm"]
                if used_w > new_cfg["page_width_mm"] + 1e-6:
                    raise ValueError(f"横向尺寸超出纸张：{used_w:.2f}mm > {new_cfg['page_width_mm']:.2f}mm")

                new_cfg["layout_version"] = 4
                new_cfg["fit_rows_to_page"] = False
                new_cfg["label_height_mm"] = (new_cfg["page_height_mm"] - new_cfg["top_margin_mm"] - new_cfg["bottom_margin_mm"]) / new_cfg["rows"]
                save_barcode_config(new_cfg)
                self.barcode_config = new_cfg
                messagebox.showinfo("保存成功", f"打印尺寸已保存。\n\n实际每行高度：{new_cfg['label_height_mm']:.4f} mm", parent=win)
                win.destroy()
            except Exception as e:
                messagebox.showerror("保存失败", str(e), parent=win)

        btns = ttk.Frame(win)
        btns.pack(fill="x", pady=(0, 12))
        ttk.Button(btns, text="保存设置", command=save, width=18).pack(side="left", padx=8)
        ttk.Button(btns, text="取消", command=win.destroy, width=12).pack(side="left", padx=4)

    def submit(self):
        template_zdno = self.template_combo.get().strip()
        prefix = self.new_zdno_prefix_var.get().strip()
        start_str = self.start_num_var.get().strip()
        end_str = self.end_num_var.get().strip()
        mode = self.mode_var.get()

        if not template_zdno:
            messagebox.showerror("错误", "请选择模板 ZDNO")
            return
        if not prefix:
            messagebox.showerror("错误", "请输入 ZDNO 前缀")
            return
        if not start_str or not end_str:
            messagebox.showerror("错误", "请输入开始编号和结束编号")
            return

        try:
            start_num = int(start_str)
            end_num = int(end_str)
            if start_num > end_num:
                messagebox.showerror("错误", "开始编号必须小于或等于结束编号")
                return
            if end_num - start_num + 1 > 50:
                messagebox.showerror("错误", f"编号总数 {end_num - start_num + 1} 超过 50")
                return
        except ValueError:
            messagebox.showerror("错误", "开始编号和结束编号必须为数字")
            return

        new_zdno = f"{prefix}{start_num}-{end_num}"
        color_data = []

        # ============================================================
        # 汇总、明细统一读取 UI 的“尺码数据”表。
        # 汇总模式也不再读取顶部“数量”框。
        #
        # 每一行：
        #   件数 = 手数/缸号数量
        #   数量 = 每手件数
        #
        # 总数量：
        #   SUM(件数 × 数量)
        #
        # 例如：
        #   L  5手 × 50 = 250
        #   XL 3手 × 50 = 150
        #   XL 1手 × 30 = 30
        #   最终总数量 = 430
        # ============================================================
        for row in self.size_rows:
            color = row['color'].get().strip()
            size = row['size_var'].get().strip()
            pieces_str = row['pieces'].get().strip()
            qty_str = row['quantity'].get().strip()

            # 完全空白的行直接跳过
            if not color and not size and not pieces_str and not qty_str:
                continue

            # 只要填写了一部分，就要求这一行完整
            if not color or not size or not pieces_str or not qty_str:
                messagebox.showerror(
                    "错误",
                    f"尺码数据填写不完整：\n"
                    f"颜色：{color or '(空)'}\n"
                    f"尺码：{size or '(空)'}\n"
                    f"件数：{pieces_str or '(空)'}\n"
                    f"数量：{qty_str or '(空)'}"
                )
                return

            try:
                pieces = int(pieces_str)
                qty = int(qty_str)
            except ValueError:
                messagebox.showerror("错误", f"尺码 {size} 的件数和数量必须为数字")
                return

            if pieces <= 0 or qty <= 0:
                messagebox.showerror("错误", f"尺码 {size} 的件数和数量必须大于0")
                return

            color_data.append({
                'color': color,
                'size': size,
                'pieces': pieces,
                'quantity': qty
            })

        if not color_data:
            messagebox.showerror(
                "错误",
                "没有有效的尺码数据，请先在UI的尺码数据中填写颜色、尺码、件数和数量"
            )
            return

        # 所有模式统一由尺码数据计算
        total_pieces = sum(item['pieces'] for item in color_data)
        total_qty = sum(
            item['pieces'] * item['quantity']
            for item in color_data
        )

        if total_qty <= 0:
            messagebox.showerror("错误", "根据尺码数据计算出的总数量必须大于0")
            return

        # 明细模式原有校验保留；汇总模式也增加同样校验，
        # 防止编号数量与尺码表中的“件数/手数”对不上。
        total_count = end_num - start_num + 1
        if total_pieces != total_count:
            if not messagebox.askyesno(
                "警告",
                f"总件数 ({total_pieces}) 与编号总数 ({total_count}) 不一致！\n\n"
                f"编号范围：{start_num} ~ {end_num}，共 {total_count} 个\n"
                f"尺码件数合计：{total_pieces} 件\n"
                f"按尺码数据计算的总数量：{total_qty} 件\n\n"
                f"是否继续插入？"
            ):
                return

        info = f"确认插入以下数据？\n\n"
        info += f"模式：{mode}\n"
        info += f"模板：{template_zdno}\n"
        info += f"新 ZDNO：{new_zdno}\n"
        info += f"款式：{self.style_var.get()}\n"
        type_name = self.get_type_name()
        detail_zdno, _pfx, _num = build_detail_zdno(new_zdno, type_name)
        info += f"类型：{type_name}（写入 jfdj.remark；明细表需另行点「📊 补录明细表」）\n"
        info += f"明细制单号：{detail_zdno}（补录明细表时使用）\n"
        info += f"总件数：{total_pieces}\n"
        info += f"总数量：{total_qty}\n\n"
        info += "尺码明细：\n"
        for item in color_data:
            info += f"  {item['color']} {item['size']}: {item['pieces']}件 × 数量{item['quantity']}\n"
        
        if not messagebox.askyesno("确认", info):
            return
        
        self.status_var.set("⏳ 正在插入数据...")
        self.root.update()
        
        type_name = self.get_type_name()
        success, msg = insert_data(template_zdno, prefix, start_num, end_num, color_data, mode,
                                   type_name=type_name)
        
        if success:
            log_record = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | [{mode}] {new_zdno} | {self.style_var.get()} | {total_pieces}件 | {total_qty} | 颜色: {', '.join(set(item['color'] for item in color_data))}"
            write_log(log_record)
            self.load_templates()
            self.template_combo.set(new_zdno)
            messagebox.showinfo("成功", msg)
            self.status_var.set("✅ 插入成功！已刷新模板列表")
            self.update_stockin_btn_state()
            # 数据库插入成功后：选择打印模板 → 预览 → 用户确认后打印。
            try:
                print_data = self.collect_current_print_data()
                if messagebox.askyesno("条码打印", "数据已成功插入。\n\n是否进入条码打印？", parent=self.root):
                    self.choose_barcode_template_and_preview(print_data)
            except Exception as print_e:
                messagebox.showwarning("条码打印", f"数据已经成功插入，但生成条码预览失败：\n{print_e}")
        else:
            messagebox.showerror("失败", msg)
            self.status_var.set(f"❌ 失败：{msg}")


# ==================== 启动 ====================
if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)

    def _cleanup():
        try:
            root.destroy()
        finally:
            close_connections()

    root.protocol("WM_DELETE_WINDOW", _cleanup)
    root.mainloop()
    root.mainloop()
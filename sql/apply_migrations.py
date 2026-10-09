# -*- coding: utf-8 -*-
"""
数据库升级脚本执行器

用法
----
  # 看看会执行什么，不真跑（推荐先跑这个）
  python sql/apply_migrations.py --dry-run

  # 对测试库执行
  python sql/apply_migrations.py --target test

  # 对生产库执行（需加 --yes 二次确认）
  python sql/apply_migrations.py --target prod --yes

  # 只跑到某个版本
  python sql/apply_migrations.py --target test --to V003

特性
----
- 幂等：已执行的版本记在 hbc_schema_version 表，重跑自动跳过
- 记账：每次执行写入版本号、脚本名、执行人、时间
- 批量分句：按 GO 拆分整份 .sql
- 默认拒绝碰生产库，必须 --target prod --yes
"""
import argparse
import os
import re
import sys
import io
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

SQL_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION_TABLE = 'hbc_schema_version'

DB = {
    'server': os.environ.get('HBC_SERVER', '192.168.0.73'),
    'user':   os.environ.get('HBC_USER', 'sa'),
    'passwd': os.environ.get('HBC_PWD', '1'),
    'test':   os.environ.get('HBC_DB', 'ShintHrmDb-test'),
    'prod':   os.environ.get('HBC_PROD_DB', 'ShintHrmDb'),
}

OK, FAIL, WARN, INFO = '  [OK]', '  [!!]', '  [??]', '  --'


def connect(which):
    import pyodbc
    name = DB[which]
    cs = ("DRIVER={SQL Server};SERVER=%s;DATABASE=%s;UID=%s;PWD=%s"
          % (DB['server'], name, DB['user'], DB['passwd']))
    return pyodbc.connect(cs, timeout=20, autocommit=True), name


def ensure_version_table(cur):
    cur.execute("""
        IF OBJECT_ID('%s', 'U') IS NULL
        BEGIN
            CREATE TABLE %s (
                version     varchar(20)  NOT NULL,
                script_name nvarchar(100) NOT NULL,
                checksum    varchar(64)  NULL,
                applied_at  datetime     NOT NULL,
                applied_by  nvarchar(50) NOT NULL,
                db_name     nvarchar(100) NOT NULL,
                CONSTRAINT PK_%s PRIMARY KEY (version)
            )
        END
    """ % (VERSION_TABLE, VERSION_TABLE, VERSION_TABLE))


def applied_versions(cur):
    cur.execute("SELECT version, script_name, checksum FROM %s ORDER BY version"
                % VERSION_TABLE)
    return {r[0]: (r[1], r[2]) for r in cur.fetchall()}


def split_batches(sql_text):
    """按 GO 拆成可逐条执行的批次（忽略注释里的 GO）"""
    batches, buf = [], []
    for line in sql_text.splitlines():
        if re.match(r'^\s*GO\s*(--.*)?$', line, re.IGNORECASE):
            if buf:
                batches.append('\n'.join(buf))
                buf = []
        else:
            buf.append(line)
    if buf:
        batches.append('\n'.join(buf))
    return [b for b in batches if b.strip()]


def load_scripts():
    files = sorted(f for f in os.listdir(SQL_DIR)
                   if f.lower().endswith('.sql')
                   and re.match(r'^V\d{3}__', f))
    out = []
    for f in files:
        ver = f.split('__')[0]
        path = os.path.join(SQL_DIR, f)
        with open(path, 'r', encoding='utf-8') as fh:
            text = fh.read()
        import hashlib
        out.append({
            'version': ver,
            'name': f,
            'path': path,
            'batches': split_batches(text),
            'checksum': hashlib.sha256(text.encode('utf-8')).hexdigest()[:32],
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--target', choices=['test', 'prod'], default='test')
    ap.add_argument('--dry-run', action='store_true', help='只打印不执行')
    ap.add_argument('--yes', action='store_true', help='确认执行生产库')
    ap.add_argument('--to', dest='until', help='执行到指定版本，如 V003')
    args = ap.parse_args()

    scripts = load_scripts()
    if args.until:
        scripts = [s for s in scripts if s['version'] <= args.until]

    if not scripts:
        print('没有找到版本脚本');  return 0

    print('=' * 64)
    print(' 数据库升级脚本执行器')
    print(' 服务器 : %s' % DB['server'])
    print(' 目标   : %s (%s)' % (DB[args.target], '生产库' if args.target == 'prod' else '测试库'))
    print(' 模式   : %s' % ('DRY-RUN 只预览' if args.dry_run else '实际执行'))
    print('=' * 64)

    if args.target == 'prod' and not args.dry_run and not args.yes:
        print('\n拒绝执行：生产库必须加 --yes 明确确认。\n')
        return 2

    conn, dbname = connect(args.target)
    cur = conn.cursor()

    ensure_version_table(cur)
    done = applied_versions(cur)

    print('\n已执行版本: %s' % (', '.join(sorted(done)) if done else '(无)'))

    plan = []
    for s in scripts:
        if s['version'] in done:
            mark = '跳过(已执行)'
            if done[s['version']][1] != s['checksum']:
                mark += ' ⚠️内容已变更'
            plan.append((s, mark, False))
        else:
            plan.append((s, '待执行', True))

    print('\n执行计划:')
    for s, mark, _ in plan:
        print('  %-8s %-46s %s' % (s['version'], s['name'], mark))

    if args.dry_run:
        print('\nDRY-RUN 结束，未做任何改动。')
        conn.close()
        return 0

    print('\n开始执行:\n')
    applied = 0
    for s, _mark, need in plan:
        if not need:
            print('%s %-8s %s -> 跳过' % (INFO, s['version'], s['name']))
            continue
        try:
            for i, batch in enumerate(s['batches'], 1):
                cur.execute(batch)
                # PRINT / DDL 不产生结果集，直接 fetchall 会抛
                # "No results. Previous SQL was not a query."
                if cur.description:
                    for row in cur.fetchall():
                        msg = str(row[0]).strip() if row and row[0] else ''
                        if msg:
                            print('       | %s' % msg)
            cur.execute(
                "INSERT INTO %s (version, script_name, checksum, applied_at, "
                "applied_by, db_name) VALUES (?,?,?,?,?,?)"
                % VERSION_TABLE,
                s['version'], s['name'], s['checksum'],
                datetime.now(), DB['user'], dbname)
            applied += 1
            print('%s %-8s %s -> 成功' % (OK, s['version'], s['name']))
        except Exception as e:
            conn.rollback()
            print('%s %-8s %s -> 失败: %s' % (FAIL, s['version'], s['name'], e))
            print('\n已中止。已成功的 %d 个版本保持生效。' % applied)
            conn.close()
            return 1

    print('\n完成：新执行 %d 个版本。' % applied)
    print('当前 %s 的版本状态:' % dbname)
    cur.execute("SELECT version, script_name, applied_at, applied_by FROM %s "
                "ORDER BY version" % VERSION_TABLE)
    for r in cur.fetchall():
        print('  %-8s %-46s %s  by %s' % (r[0], r[1], str(r[2])[:19], r[3]))

    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
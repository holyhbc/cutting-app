# 数据库升级脚本

所有**新增表**和**字段修改**都必须放在这里，版本化管理，方便日后升级生产库。

## 目录

```
sql/
├── README.md
├── V001__create_jfzd2_detail.sql   裁剪明细表
├── V002__create_jfzg_scan_map.sql  扫码短ID映射（阶段 0）
├── V003__create_jfzg_change.sql    改量审批（阶段 3）
└── apply_migrations.py             执行器
```

## 用法

```bash
cd /mnt/g/hbc/opencode

# 1. 先预览（不动数据库）—— 建议每次都先跑这个
/mnt/c/Windows/py.exe sql/apply_migrations.py --dry-run

# 2. 对测试库执行
/mnt/c/Windows/py.exe sql/apply_migrations.py --target test

# 3. 确认无误后，对生产库执行（需 --yes）
/mnt/c/Windows/py.exe sql/apply_migrations.py --target prod --yes
```

## 版本记账表

执行器会自动建 `hbc_schema_version`：

| 字段 | 说明 |
|---|---|
| `version` | 主键，如 `V001` |
| `script_name` | 脚本文件名 |
| `checksum` | 内容 SHA256 前 32 位 —— **改了已发布的脚本会被检出** |
| `applied_at` | 执行时间 |
| `applied_by` | 执行账号 |
| `db_name` | 执行时的库名 |

查看某库执行到哪一版：

```sql
SELECT * FROM hbc_schema_version ORDER BY version;
```

## 新增脚本的规矩

1. **文件名**：`Vxxx__简短描述.sql`，`xxx` 三位递增（V004、V005…）
2. **只增不改** —— 已发布的脚本**永远不要修改**，否则 checksum 对不上。
   要改就加新版本。
3. **必须幂等** —— 重复执行不能报错。用 2000 兼容写法：
   ```sql
   IF OBJECT_ID('表名', 'U') IS NULL
   BEGIN
       CREATE TABLE ...
   END
   ```
   索引用：
   ```sql
   IF NOT EXISTS (SELECT 1 FROM sysindexes WHERE name = '索引名')
   BEGIN
       CREATE INDEX ...
   END
   ```
4. **SQL Server 2000 限制** —— 禁用：
   - `CREATE TABLE IF NOT EXISTS`（2005+）
   - CTE（`WITH`）
   - `MERGE`（2008+）
   - `sys.indexes`、`sys.columns`、`syscolumns.is_identity`、`type_desc`（2005+）
   - `datetime2`、`TIME`、`DATEPART` 部分新参数
   - filtered index（`CREATE INDEX ... WHERE`）
5. **给新表写字段注释**，说明字段来源与对齐的既有表
6. 写完后跑一次 `--dry-run`，再跑测试库，最后才考虑生产

## 加字段的标准写法

```sql
-- V004__add_xxx.sql 示例
IF COL_LENGTH('表名', '新字段') IS NULL       -- SQL Server 2000 支持 COL_LENGTH
BEGIN
    ALTER TABLE 表名 ADD 新字段 nvarchar(50) NULL
    PRINT 'V004: 字段已添加'
END
ELSE
BEGIN
    PRINT 'V004: 字段已存在，跳过'
END
```

> ⚠️ SQL Server 2000 **不支持** `ALTER TABLE ... DROP COLUMN` 之外的很多改动，
> 且不支持 `ALTER COLUMN` 改类型/加约束。要删列只能重建表，需单独评估。

## 当前各库状态（2026-10-09）

| 库 | `hbc_schema_version` | 备注 |
|---|---|---|
| `ShintHrmDb`（生产） | 表尚未建 | `jfzd2_detail` 已于 2026-10-08 手工创建，含 14 行数据 |
| `ShintHrmDb-test` | 表尚未建 | 缺 `jfzd2_detail`，待 V001 补齐 |

## 上生产库的检查清单

- [ ] `--dry-run` 输出符合预期
- [ ] 已在 `ShintHrmDb-test` 完整跑过一遍
- [ ] 已在测试库跑过程序功能验证
- [ ] 已对生产库做数据备份（`jfdj_bak` / `jfzd_bak_2026` 是既有备份惯例）
- [ ] 选在**非生产时段**（避开发工时间）
- [ ] 执行后立即核对 `hbc_schema_version` 记录
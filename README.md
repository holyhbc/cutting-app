# cutting-app 裁剪工序与扫码报工系统

车间裁剪工序管理 + 条码打印 + 微信扫码报工。

---

## 目录结构

```
cutt/       桌面工具（Windows 运行）
  hbc-print-v7.7.4-多颜色完整优化版.py   条码/二维码打印、入库
  zdno_edit.py                          工序数据维护、裁剪统计、Excel 导出

webapp/     扫码报工 Web 服务（部署在 VPS）
  app.py            移动端扫码页（只用标准库）
  db.py             SQLite 访问层
  rules.py          业务规则（登录/上限/组装）
  schema.sql        表结构
  sync_snapshot.py  快照同步器（跑在局域网）
  DEPLOY.md         部署步骤

sql/        数据库升级脚本（版本化，幂等）
  V001__create_jfzd2_detail.sql
  V002__create_jfzg_scan_map.sql
  V003__create_jfzg_change.sql
  apply_migrations.py

docs/       设计与决策文档
  CURRENT_STATE.md   ← 接手先读这份
  DECISIONS.md       12+ 条已确认决策
  DESIGN.md          总体设计
  TODO.md            任务清单与进度
  CHANGELOG.md       改动记录
```

---

## 快速上手

### 桌面工具

```bash
cd cutt
python hbc-print-v7.7.4-多颜色完整优化版.py    # 打印标签
python zdno_edit.py                            # 工序数据维护
```

数据库连接全部走环境变量（**默认连测试库 `ShintHrmDb-test`**）：

```bash
export HBC_SERVER=192.168.0.73
export HBC_DB=ShintHrmDb-test     # 切生产改 ShintHrmDb
export HBC_USER=sa
export HBC_PWD=1
```

### 扫码报工服务

```bash
cd webapp
PORT=10080 SYNC_TOKEN=你的令牌 python app.py
# 浏览器打开 http://127.0.0.1:10080
```

部署到 VPS 见 [`webapp/DEPLOY.md`](webapp/DEPLOY.md)。

### 数据库升级

```bash
python sql/apply_migrations.py --dry-run                    # 先预览
python sql/apply_migrations.py --target test               # 测试库
python sql/apply_migrations.py --target prod --yes         # 生产库
```

---

## 扫码报工怎么用

1. 打印标签（`hbc-print` 里开 `label_qr_enabled`），标签左下角带二维码
2. 工人微信扫码 → 打开 `https://cut.holyhbc.eu.org/s/{短ID}`
3. 页面显示定单、扎号、应做数
4. 填工号 → 点工序 → 填件数 → 提交
5. 首次报工直接生效；改量需主管审批

### 上限规则

同一扎同一道工序，**全体员工合计不得超过应做数**：

```
已报(该扎该工序所有人合计) ≤ 应做数
```

实测每道工序的报工量恰好等于整扎应做数（倍率 1.00）。

---

## 技术约束（改代码前必读）

- **SQL Server 2000**：禁用 CTE、MERGE、`sys.indexes`、
  `syscolumns.is_identity`、`datetime2`、filtered index
  - IDENTITY 检测用 `COLUMNPROPERTY(OBJECT_ID(t), col, 'IsIdentity')`
  - `plan` 是保留字，不可作别名
- **VPS 不连 SQL Server**：数据由局域网电脑经 `sync_snapshot.py --push` 推送
  （实测全量 22.7 MB / 23.7 万行，约 7.4 秒）
- **改条码前先 commit**：`make_full_barcode` 从 13 位改 14 位会让老扫码枪失效
- **新增表/字段必须写 `sql/Vxxx__*.sql`**，便于日后升级生产库
- **工序名不能等值匹配**：历史写法不统一（`印商标`/`印衣标`、`上安纶`/`上氨纶`）
- **`jfdj.gx` 是定单内工序序号**（1~18），查询**必须**加 `gx BETWEEN 1 AND 18`
  ——有 5 个定单存在 gx>18 的脏数据

---

## 数据库

| 表 | 用途 |
|---|---|
| `jfzd` | 裁剪工单头（ZDNO/ZDID 唯一） |
| `jfzd2` | **扎 × 颜色 × 尺码**（无工序字段！），含应做数 JS |
| `jfdj` | **工序 + 单价权威表**（gx 为定单内工序序号 1~18） |
| `jfdjgxk` | 工序字典（48 条），不用于排产取工序 |
| `jfgz` | 报工表（128148 行），`barcode` 1=扫码 0=手动 |
| `ygzl` | 员工花名册（116 人） |
| `jfzd2_detail` | 裁剪明细（本项目新增） |

**注意**：`jfgz.barcode` 已有 125333 行为 1，是历史真实数据，**不是**本项目新建的字段。

---

## 提交代码

```bash
git add -A
git commit -m "描述"
git push          # 推送到 https://github.com/holyhbc/cutting-app
```

---

## ⚠️ 安全提示

本仓库为**公开仓库**，以下信息已可公开查看到：

- SQL Server 账号 `sa` / 密码 `1`
- 内网地址 `192.168.0.73`
- VPS 地址 `64.110.73.90`、域名 `cut.holyhbc.eu.org`
- 真实定单号

**这些已进入 GitHub 历史缓存**。若要彻底清除，必须**改密码 + 重写历史**。

新增代码请不要再引入凭据；用环境变量。

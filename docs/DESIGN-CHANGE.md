# 阶段 3 设计：改量审批

> 目标：工人报错了能改，但**不能直接改** —— 必须经主管审批，留痕可查。
> 状态：待用户确认（3 个决策见第十节）
> 关联：[TODO.md](TODO.md) · [DECISIONS.md D-005](DECISIONS.md) · [DESIGN.md](DESIGN.md)

---

## 一、为什么要做这个

### 实库发现

历史 `jfgz` 里有 **139 组**同 `(定单, 层, 扎, 工序, 工号)` 出现了多条记录。

这说明历史上的做法是**追加补报**，而不是改量。工人报错了只能再报一次，
系统里就留下两条，`SUM(js)` 算工资时会把两条加在一起 —— 结果可能对，
但**明细乱了，事后查不出哪条是错的**。

### 缺口

现在扫码报工只能"首次报工"，报错了：
- 只能重新扫一次追加 → 数据重复、无法纠错
- 累计数量可能超过应做数 → 被上限校验拦住，工人卡住

**改量审批**补上这个缺口：允许改，但必须主管点头，全程留痕。

---

## 二、核心原则（不可妥协）

### 铁律：已生效的 `jfgz` 记录禁止直接 UPDATE

```sql
-- ❌ 绝对不做
UPDATE jfgz SET js = 500 WHERE jfgzid = 12345
```

**所有改动必须经 `jfzg_change` 审批表留痕**，通过后由审批流程改写。
理由：工资直接由 `SUM(js)` 算出，无痕修改意味着出错时无法追溯。

### 审批链

```
工人：申请改量 (旧值→新值 + 理由)
        ↓
jfzg_change 表 (status=0 待审)
        ↓
主管：查看角标 → 通过 / 驳回
        ↓
┌─ 通过 → 事务：改 jfgz.js 和 je + 写审计 + 标记 status=1
└─ 驳回 → 记录原因，工人看到 "被驳回：xxx"
```

### 上限规则仍然生效

改量**不是**想改多少改多少。D-006 的上限照常校验：

```
该扎该工序「已报合计」必须 <= 应做数
```

且改量要允许"往下改"（报多了要减），只要不违反上限就行。

---

## 三、数据模型

### SQL Server（对应 `sql/V003__create_jfzg_change.sql`，已写好待启用）

```sql
CREATE TABLE jfzg_change (
    id          int IDENTITY(1,1) NOT NULL,
    short_id    CHAR(8)      NOT NULL,   -- 来源二维码
    jfgzid      BIGINT       NOT NULL,   -- 要改的那条报工
    ygno        NVARCHAR(10) NOT NULL,   -- 申请人
    gx          SMALLINT     NOT NULL,   -- 工序
    gxname      NVARCHAR(30) NULL,
    zdno        NVARCHAR(15) NOT NULL,
    cc          SMALLINT     NULL,       -- 实库存在 NULL
    zh          INT          NULL,
    old_num     INT          NOT NULL,   -- 改前
    new_num     INT          NOT NULL,   -- 改后
    reason      NVARCHAR(500) NULL,      -- 申请理由
    status      TINYINT      NOT NULL DEFAULT 0,  -- 0待审 1通过 2驳回
    approve_gh  NVARCHAR(10) NULL,       -- 审批人
    approve_note NVARCHAR(500) NULL,     -- 驳回原因
    created_at  DATETIME     NOT NULL,
    approve_at  DATETIME     NULL,
    CONSTRAINT PK_jfzg_change PRIMARY KEY (id)
);
```

> ⚠️ **注意**：该脚本里有 `UNIQUE INDEX (jfgzid, status)`，
> 这会导致同一 `jfgzid` 通过后无法再提交第二条同状态申请。
> 实际使用中更常见的是"改完还想再改"，**建议去掉这个唯一索引**，
> 改为在应用层校验「同一 jfgzid 同时只能有一条待审」。
> → 见第十节 Q1。

### VPS 本地（`webapp/schema.sql`，工作副本）

```sql
CREATE TABLE IF NOT EXISTS change_request (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id  INTEGER NOT NULL,   -- 指向本地 report.id
    jfgzid     INTEGER,            -- 同步后的 jfgzid
    new_js     INTEGER NOT NULL,
    reason     TEXT    NOT NULL DEFAULT '',
    status     TEXT    NOT NULL DEFAULT 'pending',  -- pending/approved/rejected
    created_at TEXT    NOT NULL,
    approve_at TEXT,
    approve_gh TEXT
);
```

**为什么本地也要一张表**：改量申请是工人当场发起的（手机），
不能等局域网同步往返。VPS 先存下来，主管审批也在 VPS 完成，
最后由同步器统一写回 SQL Server。

---

## 四、状态机

```
        申请
         │
         ▼
    ┌─────────┐   主管通过   ┌──────────┐
    │ pending │ ──────────► │ approved │ → 同步器写回 jfgz
    └────┬────┘             └──────────┘
         │ 主管驳回
         ▼
    ┌──────────┐
    │ rejected │ → 工人看到驳回原因
    └──────────┘
```

**只允许 `pending → approved` 或 `pending → rejected`，单向不可逆。**

| 非法操作 | 处理 |
|---|---|
| 重复审批同一条 | 接口拒绝，返回"该申请已处理" |
| 并发审批 | 数据库层用条件更新 `WHERE status='pending'`，只有一条能成功 |
| 改已审批过的申请 | 不支持。要再改就重新申请一条 |

---

## 五、接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/change` | 申请改量 `{report_id or jfgzid, new_js, reason}` |
| `GET` | `/api/change?status=pending` | 主管查待审列表 |
| `POST` | `/api/change/decide` | 审批 `{id, approve: 1/0, note}` |
| `GET` | `/api/mychanges` | 工人查自己的申请及状态 |

**鉴权**：`/api/change/decide` 和 `GET /api/change` 只允许主管
（`SUPERVISORS` 环境变量里的工号），工人只能看自己的。

---

## 六、主管身份怎么定

### 实库核查结论：没有现成的"主管"字段

| 字段 | 有值/116 | 能否用 |
|---|---|---|
| `zwno` 职务编号 | **1** | ❌ |
| `gzname` 岗位名称 | **0** | ❌ |
| `yggz` 员工工种 | **0** | ❌ |
| `zz` 证照 | **0** | ❌ |
| `bmno` 部门 | 82 | ⚠️ 是部门不是职务 |
| `ygzl` 重名 | **2 组**（林和女×2、杨尚听×2） | ❌ 不能靠姓名 |

**结论：只能用环境变量白名单。**

```bash
SUPERVISORS="A001,A003"
```

**风险**：主管换人时要改环境变量重启服务。
**改进**：加一张 `worker_login.must_change` 同级的 `is_supervisor` 字段，
让主管能自助授权，不用找运维。

→ 见第十节 Q2。

---

## 七、页面

### 工人端

报工成功弹窗加一个次要按钮：
```
      ✓
   报工成功
已记录：陈秋雪 裤线头 50 件，金额 5.00 元

[ 继续报这扎 ]  [ 扫下一扎 ]
[ 🛠 报错了，要改量 ]        ← 新增
```

点「报错了，要改量」：
```
┌──────────────────────────────┐
│ 修改报工数量                  │
├──────────────────────────────┤
│ 陈秋雪 · 裤线头 · 第1扎       │
│ 原报  50 件                   │
│ 改成 [  45  ] 件              │
│                              │
│ 理由（必填）                  │
│ [ 少算了，实际只裁了45件      ]│
│                              │
│ [ 取消 ]  [ 提交审批 ]        │
└──────────────────────────────┘
```

**理由必填** —— 没有理由的改量主管没法判断，容易被滥用。

### 主管端 `/approve`

```
┌──────────────────────────────┐
│ 待审批 3 条          [刷新]   │
├──────────────────────────────┤
│ 陈秋雪 申请改量               │
│ 269830套装1-22 扎1 裤线头     │
│  50 件 → 45 件  (-5)          │
│ 理由：少算了，实际只裁了45件   │
│  2026-10-09 19:30             │
│ [驳回] [通过]                 │
├──────────────────────────────┤
│ ...                          │
└──────────────────────────────┘
```

角标：主管登录后在任何页面显示待审数量，方便随时进入。

---

## 八、上限校验（改量专属）

改量时重算，已报合计 = `SUM(jfgz.js)` + 本地待同步 + 本次新值 − 本次旧值。

```
改后合计 = 已报合计 - old_num + new_num
必须满足：改后合计 <= 该扎应做数
```

**允许往下改**（报多了要减），只校验上限，不管方向。

个人改量上限遵循 D-006：
```
个人上限 = 应做数 - 他人已报 + 自己已报
```

---

## 九、风险

| 风险 | 缓解 |
|---|---|
| 主管滥批 | 全程留痕；`jfzg_change` 有申请人和审批人 |
| 工人滥改刷量 | 改量受上限约束；理由必填；主管能看到历史 |
| 并发审批同一单 | `WHERE status='pending'` 条件更新，只有一条成功 |
| 同步重复执行 | 审批记录带唯一标识，同步器幂等 |
| 主管离职 | 环境变量失效 → 无人能审批 → 申请堆积 |
| 已同步的报工改量时对方已锁定 | 事务内重新读 `jfgz`，用最新值算上限 |

---

## 十、需要你确认的 4 件事

### Q1. `jfzg_change` 上的唯一索引怎么处理？

现脚本里有 `UNIQUE INDEX (jfgzid, status)`。

| 选项 | 说明 |
|---|---|
| **A. 去掉这个索引**（建议） | 改完还能再改，更符合实际。同一 `jfgzid` 同时只有一条待审，改用应用层校验 |
| B. 保留 | 同一报工记录只能有一条"通过"和一条"驳回"，改第二次就没法提 |

### Q2. 主管名单怎么维护？

| 选项 | 说明 |
|---|---|
| **A. 先用环境变量 `SUPERVISORS`**（建议） | 简单。但换人要改配置重启 |
| B. 直接做 `worker_login.is_supervisor` | 主管可自助授权，但要加导入脚本和页面 |

我建议**先 A 后 B** —— 先跑通，B 以后加个字段就行。

### Q3. 主管在哪审批？

| 选项 | 说明 |
|---|---|
| **A. 手机浏览器 `/approve`**（建议） | 主管用手机随时批，最灵活 |
| B. 只在 hbc-print 电脑上 | 主管得回到办公室 |
| C. 两个都要 | 手机为主，电脑为辅 |

**建议 A** —— 主管在车间巡视时就顺手批了。

### Q4. 改量有没有时间限制？

比如只允许改**当天或昨天**的报工，更早的要走线下流程。

| 选项 | 说明 |
|---|---|
| **A. 不限制** | 任何时候都能申请，主管判断 |
| **B. 限制 7 天内**（建议） | 防止陈年旧账翻出来改，工资已经算完了就改不动 |
| C. 限制 30 天内 | 更宽 |

---

## 十一、实施步骤

1. 调整 `sql/V003__create_jfzg_change.sql`（按 Q1 结论）
2. `webapp/schema.sql` 加 `change_request` 表
3. `db.py` / `rules.py`：申请、审批、上限重算
4. `app.py`：4 个接口 + 主管鉴权
5. 工人端：报工后「报错了要改量」入口 + 申请页
6. 主管端：`/approve` 页 + 角标
7. 同步器：`change_request` → `jfzg_change` → 改 `jfgz`
8. 测试：状态机、并发审批、上限、下调、改已审批
9. 提交 + 部署
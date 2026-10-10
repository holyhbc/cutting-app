-- =====================================================================
-- 扫码报工系统 · 本地暂存库 (SQLite)
--
-- 定位：VPS 上的暂存库。**不直连 SQL Server**（见 DECISIONS.md D-009），
--       数据由局域网电脑通过 sync_snapshot.py 推送到这里。
--
-- 命名与 SQL Server 保持一致，便于同步器双向映射。
-- =====================================================================

-- ---------------------------------------------------------------------
-- 扫码映射：short_id <-> (定单, 车间层, 扎号)
-- 对应 SQL Server 的 jfzg_scan_map (sql/V002)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scan_map (
    short_id   TEXT    PRIMARY KEY,
    zdno       TEXT    NOT NULL,
    cc         INTEGER NOT NULL,
    zh         INTEGER NOT NULL,
    gxname     TEXT,                    -- 冗余显示用，不作标识依据
    used_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL
);
-- 防哈希碰撞：同一三元组不允许两个 short_id
CREATE UNIQUE INDEX IF NOT EXISTS ux_scan_map_triple
    ON scan_map (zdno, cc, zh);

-- ---------------------------------------------------------------------
-- 扎快照：应做数按 (定单,车间层,扎号,颜色,尺码) 拆行，合计才是该扎总数
-- 对应 SQL Server 的 jfzd2
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bundle (
    zdno     TEXT    NOT NULL,
    cc       INTEGER NOT NULL,
    zh       INTEGER NOT NULL,
    yn       TEXT    NOT NULL DEFAULT '',   -- 颜色
    cm       TEXT    NOT NULL DEFAULT '',   -- 尺码
    plan_qty INTEGER NOT NULL DEFAULT 0,    -- 应做数
    PRIMARY KEY (zdno, cc, zh, yn, cm)
);
CREATE INDEX IF NOT EXISTS ix_bundle_triple ON bundle (zdno, cc, zh);

-- ---------------------------------------------------------------------
-- 工序快照：一扎可做的全部工序及单价
-- 对应 SQL Server 的 jfdj（只取 gx BETWEEN 1 AND 18，避开历史脏数据）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS process (
    zdno   TEXT    NOT NULL,
    gx     INTEGER NOT NULL,
    gxname TEXT    NOT NULL DEFAULT '',
    dj     REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (zdno, gx)
);

-- ---------------------------------------------------------------------
-- 历史已报基线：SQL Server 里 jfgz 已有的报工合计
-- 上限校验 = 基线 + 本地待同步之和，两者相加才是真实已报量
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS report_baseline (
    zdno         TEXT    NOT NULL,
    cc           INTEGER NOT NULL,
    zh           INTEGER NOT NULL,
    gx           INTEGER NOT NULL,
    reported_qty INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT    NOT NULL,
    PRIMARY KEY (zdno, cc, zh, gx)
);

-- ---------------------------------------------------------------------
-- 报工暂存：扫码产生，等待局域网同步器写回 SQL Server 的 jfgz
--   barcode=1 表示扫条形码/二维码录入（DECISIONS.md D-003）
--   gzdate 存完整日期时间（D-004）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS report (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    short_id  TEXT    NOT NULL DEFAULT '',
    zdno      TEXT    NOT NULL,
    cc        INTEGER NOT NULL,
    zh        INTEGER NOT NULL,
    gx        INTEGER NOT NULL,
    gxname    TEXT    NOT NULL DEFAULT '',
    ygno      TEXT    NOT NULL,              -- 报工人，对齐 ygzl.ygno
    ygname    TEXT    NOT NULL DEFAULT '',
    js        INTEGER NOT NULL,              -- 报工数量
    dj        REAL    NOT NULL DEFAULT 0,   -- 单价，取自 jfdj.dj
    je        REAL    NOT NULL DEFAULT 0,   -- 金额 = js * dj
    gzdate    TEXT    NOT NULL,              -- 完整日期时间
    barcode   INTEGER NOT NULL DEFAULT 1,
    status    TEXT    NOT NULL DEFAULT 'pending',  -- pending/synced/failed
    retry     INTEGER NOT NULL DEFAULT 0,
    last_err  TEXT,
    created_at TEXT   NOT NULL,
    synced_at TEXT,
    jfgzid    INTEGER                        -- 回填 jfgz.jfgzid
);
CREATE INDEX IF NOT EXISTS ix_report_pending ON report (status, id);
CREATE INDEX IF NOT EXISTS ix_report_triple  ON report (zdno, cc, zh, gx);

-- 幂等键：同一短ID + 同一扎/工序/员工/数量/时间 只允许一条
CREATE UNIQUE INDEX IF NOT EXISTS ux_report_idem
    ON report (short_id, zdno, cc, zh, gx, ygno, js, gzdate);

-- ---------------------------------------------------------------------
-- 员工花名册快照：登录校验用，只读
-- 对应 SQL Server 的 ygzl
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ygzl (
    ygno   TEXT PRIMARY KEY,
    ygname TEXT NOT NULL DEFAULT '',
    ygout  INTEGER NOT NULL DEFAULT 0     -- 离职标志
);

-- ---------------------------------------------------------------------
-- 登录凭据：工号 / 手机号 / PIN 哈希
--   手机号可空；登录时「工号 或 手机号」都能查到（D-016）
--   PIN 只存哈希+盐（D-017），初始 PIN 由导入脚本按手机号后4位生成
--   active=0 表示暂时不能登录（如调去别的车间），与 ygzl.ygout(离职)职责分离
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS worker_login (
    ygno        TEXT    PRIMARY KEY,          -- 工号，主标识
    ygname      TEXT    NOT NULL DEFAULT '',  -- 姓名，仅显示（ygzl 有重名，不能作标识）
    phone       TEXT,                         -- 手机号，已归一化为纯数字
    pin_hash    TEXT    NOT NULL,             -- PIN 哈希，不存明文
    pin_salt    TEXT    NOT NULL,
    must_change INTEGER NOT NULL DEFAULT 1,   -- 1=提示工人改 PIN
    active      INTEGER NOT NULL DEFAULT 1,   -- 0=不能登录
    created_at  TEXT    NOT NULL,
    updated_at  TEXT
);
-- phone 可为 NULL，SQLite 的 UNIQUE 允许多个 NULL
CREATE UNIQUE INDEX IF NOT EXISTS ux_worker_login_phone
    ON worker_login (phone);

-- ---------------------------------------------------------------------
-- 登录态令牌：长期有效（D-018），每次使用都重新查 ygzl 确认在职
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS worker_session (
    token      TEXT    PRIMARY KEY,
    ygno       TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    expires_at TEXT    NOT NULL,
    last_seen  TEXT    NOT NULL,
    device     TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_session_ygno ON worker_session (ygno);

-- ---------------------------------------------------------------------
-- R2 工序偏好：覆盖式，只留最新一道
--   scope='bundle' 按(工号+扎)精确记忆；scope='zdno'按(工号+定单)款式记忆
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS worker_pref (
    ygno       TEXT    NOT NULL,
    scope      TEXT    NOT NULL,
    zdno       TEXT    NOT NULL,
    cc         INTEGER NOT NULL,
    zh         INTEGER NOT NULL,
    gx         INTEGER NOT NULL,
    updated_at TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_pref_bundle
    ON worker_pref (ygno, scope, zdno, cc, zh);

-- ---------------------------------------------------------------------
-- 改量申请（阶段 3）
-- 对应 SQL Server 的 jfzg_change (sql/V003)
-- 铁律：已生效的 jfgz 禁止直接 UPDATE，必须经本表留痕后由审批流程改写。
-- 状态：pending -> approved / rejected，单向不可逆
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS change_request (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id  INTEGER NOT NULL,          -- 指向本地 report.id
    jfgzid     INTEGER,                   -- 已同步后的 jfgzid；未同步时为 NULL
    ygno       TEXT    NOT NULL,          -- 申请人
    ygname     TEXT    NOT NULL DEFAULT '',
    gx         INTEGER NOT NULL,
    gxname     TEXT    NOT NULL DEFAULT '',
    zdno       TEXT    NOT NULL,
    cc         INTEGER,
    zh         INTEGER,
    old_num    INTEGER NOT NULL,          -- 改前
    new_num    INTEGER NOT NULL,          -- 改后
    reason     TEXT    NOT NULL DEFAULT '',
    status     TEXT    NOT NULL DEFAULT 'pending',  -- pending/approved/rejected
    approve_gh TEXT,
    approve_note TEXT,
    created_at TEXT    NOT NULL,          -- 用于 30 天时限判断
    approve_at TEXT,
    synced     INTEGER NOT NULL DEFAULT 0 -- 1=已写回 SQL Server
);
CREATE INDEX IF NOT EXISTS ix_change_status ON change_request (status, id);
CREATE INDEX IF NOT EXISTS ix_change_report ON change_request (report_id);

-- ---------------------------------------------------------------------
-- 操作审计
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    action     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    ip         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_audit_at ON audit (at);

-- ---------------------------------------------------------------------
-- 报工明细（阶段4 报表数据源，由局域网电脑从 SQL Server 推送）
-- 只读副本：VPS 不连 SQL Server（D-009），报表全部基于这张表
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jfgz_report (
    jfgzid  INTEGER PRIMARY KEY,   -- 对齐 SQL Server，便于对账
    gzdate  TEXT    NOT NULL,      -- 完整日期时间
    gzday   TEXT    NOT NULL,      -- 日期部分 YYYY-MM-DD，聚合用
    ygno    TEXT    NOT NULL,
    ygname  TEXT    NOT NULL DEFAULT '',
    zdno    TEXT    NOT NULL,
    gx      INTEGER NOT NULL,
    cc      INTEGER,
    zh      INTEGER,
    js      INTEGER NOT NULL,
    dj      REAL    NOT NULL DEFAULT 0,
    je      REAL    NOT NULL DEFAULT 0,
    barcode INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_rep_day  ON jfgz_report (gzday);
CREATE INDEX IF NOT EXISTS ix_rep_ygno ON jfgz_report (ygno, gzday);
CREATE INDEX IF NOT EXISTS ix_rep_zdno ON jfgz_report (zdno, gx);

-- 同步水位（增量推送用）
CREATE TABLE IF NOT EXISTS sync_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

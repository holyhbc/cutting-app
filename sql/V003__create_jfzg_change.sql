-- =====================================================================
-- V003  创建改量审批表 jfzg_change
-- 用途  : 工人申请修改已报数量，需主管审批通过后才落 jfgz
-- 阶段  : 阶段 3（改量审批）
-- 兼容  : SQL Server 2000
-- 幂等  : 表/索引已存在则跳过
--
-- 铁律：已生效的 jfgz 记录**禁止直接 UPDATE**，
--       所有改动必须经本表留痕后由审批流程改写（决策 D-005）
-- =====================================================================

IF OBJECT_ID('jfzg_change', 'U') IS NULL
BEGIN

    CREATE TABLE jfzg_change (
        id          int IDENTITY(1,1) NOT NULL,   -- 2000 无 SEQUENCE
        short_id    char(8)      NOT NULL,   -- 来源二维码，可为空（手工补录场景）
        jfgzid      bigint       NOT NULL,   -- 指向要改的那条报工（对齐 jfgz.jfgzid）
        ygno        nvarchar(10) NOT NULL,   -- 申请人（对齐 ygzl.ygno）
        gx          smallint     NOT NULL,   -- 工序序号（对齐 jfdj.gx，1~18）
        gxname      nvarchar(30) NULL,       -- 冗余名称，仅显示用
        zdno        nvarchar(15) NOT NULL,   -- 冗余，便于查询
        cc          smallint     NULL,       -- 实库存在 NULL，故可空
        zh          int          NULL,       -- 实库存在 NULL，故可空
        old_num     int          NOT NULL,   -- 改前数量
        new_num     int          NOT NULL,   -- 改后数量
        reason      nvarchar(500) NULL,      -- 申请理由
        status      tinyint      NOT NULL DEFAULT 0,  -- 0待审 1通过 2驳回
        approve_gh  nvarchar(10) NULL,      -- 审批人（主管工号）
        approve_note nvarchar(500) NULL,     -- 驳回原因 / 审批备注
        created_at  datetime     NOT NULL,  -- 申请时间
        approve_at  datetime     NULL,      -- 审批时间
        CONSTRAINT PK_jfzg_change PRIMARY KEY (id)
    )

    PRINT 'V003: 表 jfzg_change 已创建'

END
ELSE
BEGIN
    PRINT 'V003: 表 jfzg_change 已存在，跳过'
END

GO

IF NOT EXISTS (SELECT 1 FROM sysindexes WHERE name = 'IX_jfzg_change_status')
BEGIN
    CREATE INDEX IX_jfzg_change_status ON jfzg_change (status, created_at)
    PRINT 'V003: 索引 IX_jfzg_change_status 已创建（主管页按待审+时间取）'
END

IF NOT EXISTS (SELECT 1 FROM sysindexes WHERE name = 'IX_jfzg_change_jfgzid')
BEGIN
    CREATE INDEX IX_jfzg_change_jfgzid ON jfzg_change (jfgzid)
    PRINT 'V003: 索引 IX_jfzg_change_jfgzid 已创建'
END

IF NOT EXISTS (SELECT 1 FROM sysindexes WHERE name = 'IX_jfzg_change_ygno')
BEGIN
    CREATE INDEX IX_jfzg_change_ygno ON jfzg_change (ygno)
    PRINT 'V003: 索引 IX_jfzg_change_ygno 已创建（查我的申请）'
END

GO

-- ---------------------------------------------------------------------
-- ⚠️ 刻意不加 UNIQUE INDEX (jfgzid, status)
--
-- 原稿有该唯一索引，会导致「同一条报工只能有一条通过、一条驳回」，
-- 工人改完还想再改时就提不了申请。
-- 现改为应用层校验：同一 jfgzid **同时**只能有一条 pending。
--   → 对应 SQL Server 侧 WHERE status=0 的条件更新，
--     并发审批时只有一条能成功。
-- 见 docs/DESIGN-CHANGE.md 第十节 Q1。
-- ---------------------------------------------------------------------
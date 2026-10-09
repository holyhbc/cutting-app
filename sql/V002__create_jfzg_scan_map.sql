-- =====================================================================
-- V002  创建扫码映射表 jfzg_scan_map
-- 用途  : 二维码短ID <-> (定单, 车间层, 扎号) 的双向映射
-- 阶段  : 阶段 0（标签二维码）落地时启用
-- 兼容  : SQL Server 2000
-- 幂等  : 表/索引已存在则跳过
--
-- 设计要点（来自实库核查，见 docs/CURRENT_STATE.md 第三节）：
--   1. CC 必填 —— 实库存在 59 组 (ZDNO,ZH) 跨 CC 重复，省略 CC 会张冠李戴
--   2. short_id 无业务含义，纯索引；服务端再做唯一性兜底
--   3. 不存工序 —— 扫码粒度是「一扎」，工序由工人扫码后自选（决策 D-001）
-- =====================================================================

IF OBJECT_ID('jfzg_scan_map', 'U') IS NULL
BEGIN

    CREATE TABLE jfzg_scan_map (
        short_id   char(8)      NOT NULL,   -- sha1(zdno|cc|zh) 截断后 base32，8位大写
        zdno       nvarchar(15) NOT NULL,   -- 对齐 jfgz.zdno / jfzd2.ZDNO 的长度
        cc         smallint     NOT NULL,   -- 车间层（必填，见上方要点1）
        zh         int          NOT NULL,   -- 扎号
        gxname     nvarchar(30) NULL,       -- 冗余：便于列表显示，非标识依据
        used_count int          NOT NULL DEFAULT 0,  -- 扫码次数，防刷统计
        created_at datetime     NOT NULL,
        CONSTRAINT PK_jfzg_scan_map PRIMARY KEY (short_id)
    )

    PRINT 'V002: 表 jfzg_scan_map 已创建'

END
ELSE
BEGIN
    PRINT 'V002: 表 jfzg_scan_map 已存在，跳过'
END

GO

IF NOT EXISTS (SELECT 1 FROM sysindexes WHERE name = 'IX_jfzg_scan_map_zdcczh')
BEGIN
    -- 用于服务端按三元组反查（二维码损坏时可人工搜索）
    CREATE INDEX IX_jfzg_scan_map_zdcczh ON jfzg_scan_map (zdno, cc, zh)
    PRINT 'V002: 索引 IX_jfzg_scan_map_zdcczh 已创建'
END

GO

-- ---------------------------------------------------------------------
-- 唯一性兜底：同一三元组不允许两个不同 short_id
-- 防止哈希碰撞导致一个二维码指向两扎
-- ---------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sysindexes WHERE name = 'UX_jfzg_scan_map_zdcczh')
BEGIN
    CREATE UNIQUE INDEX UX_jfzg_scan_map_zdcczh ON jfzg_scan_map (zdno, cc, zh)
    PRINT 'V002: 唯一索引 UX_jfzg_scan_map_zdcczh 已创建（防哈希碰撞）'
END
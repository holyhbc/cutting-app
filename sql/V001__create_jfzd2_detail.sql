-- =====================================================================
-- V001  创建裁剪明细表 jfzd2_detail
-- 用途  : 记录每扎的颜色/尺码明细，支持裁剪统计与 Excel 导出
-- 兼容  : SQL Server 2000（无 CREATE TABLE IF NOT EXISTS，用 OBJECT_ID 判断）
-- 幂等  : 表已存在则跳过
-- 生产  : 生产库已于 2026-10-08 手工创建（14 行数据），
--         本脚本仅补齐 ShintHrmDb-test，重复执行安全
-- =====================================================================

IF OBJECT_ID('jfzd2_detail', 'U') IS NULL
BEGIN

    CREATE TABLE jfzd2_detail (
        ID            int             IDENTITY(1,1)  NOT NULL,
        ZDNO          nvarchar(40)    NOT NULL,   -- 定单号
        TEMPLATE_ZDNO nvarchar(40)    NULL,       -- 模板定单号（明细模式可为空）
        ORDER_NO      int             NOT NULL,   -- 第几扎
        ORDER_BEGIN   int             NULL,       -- 起始序号
        ORDER_END     int             NULL,       -- 结束序号
        YS            nvarchar(10)    NULL,       -- 颜色
        CM            nvarchar(8)     NULL,       -- 尺码
        JS            int             NULL,       -- 数量
        TYPE_NAME     nvarchar(20)    NOT NULL,   -- 类型：套装/明细
        MODE          nvarchar(10)    NULL,       -- 模式
        CREATED_AT    smalldatetime   NOT NULL,   -- 创建时间
        CREATED_BY    nvarchar(20)    NULL,       -- 操作人
        REMARK        nvarchar(200)   NULL,       -- 备注
        CONSTRAINT PK_jfzd2_detail PRIMARY KEY (ID)
    )

    PRINT 'V001: 表 jfzd2_detail 已创建'

END
ELSE
BEGIN
    PRINT 'V001: 表 jfzd2_detail 已存在，跳过'
END

GO

-- ---------------------------------------------------------------------
-- 索引（分开一段，因为 CREATE TABLE 内不能引用已建表；且便于重复执行）
-- 全部用 2000 兼容写法，IF NOT EXISTS 需借助 OBJECT_ID 判断
-- ---------------------------------------------------------------------

IF NOT EXISTS (SELECT 1 FROM sysindexes WHERE name = 'IX_jfzd2_detail_zdno')
BEGIN
    CREATE INDEX IX_jfzd2_detail_zdno ON jfzd2_detail (ZDNO)
    PRINT 'V001: 索引 IX_jfzd2_detail_zdno 已创建'
END

IF NOT EXISTS (SELECT 1 FROM sysindexes WHERE name = 'IX_jfzd2_detail_type')
BEGIN
    CREATE INDEX IX_jfzd2_detail_type ON jfzd2_detail (TYPE_NAME)
    PRINT 'V001: 索引 IX_jfzd2_detail_type 已创建'
END

IF NOT EXISTS (SELECT 1 FROM sysindexes WHERE name = 'IX_jfzd2_detail_created')
BEGIN
    CREATE INDEX IX_jfzd2_detail_created ON jfzd2_detail (CREATED_AT)
    PRINT 'V001: 索引 IX_jfzd2_detail_created 已创建'
END

IF NOT EXISTS (SELECT 1 FROM sysindexes WHERE name = 'IX_jfzd2_detail_type_time')
BEGIN
    CREATE INDEX IX_jfzd2_detail_type_time ON jfzd2_detail (TYPE_NAME, CREATED_AT)
    PRINT 'V001: 索引 IX_jfzd2_detail_type_time 已创建'
END

GO
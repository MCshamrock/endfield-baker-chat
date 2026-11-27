-- ============================================================================
-- 012_import_batch.sql —— 数据包导入批次
--
-- 老客户端和网页版都能"导出数据"（.zip 或 .json），换到新后端时要把这些包
-- 导回来。导入是个**批量、可能重来、必须能反悔**的动作，所以留一张批次表：
--
--   1. 幂等：包内容的 sha256 是唯一的。同一台设备再把同一个包传一次，
--      直接回上次的结果，不会把会话建两遍。唯一键 uk_batch_device_fp 就是它。
--   2. 可撤销：conversations.import_batch_id 记住"这条会话是哪次导入建的"，
--      撤销时按批次整体软删，不会碰到用户自己建的会话。
--   3. 可回溯：报告（未收录的角色、丢掉的图、被丢弃的上下文）存在 report 里，
--      用户问"我那张图怎么没了"时能查到原因，不用猜。
--
-- 为什么 fingerprint 不含设备：包内容本身相同就是同一个包，设备是"谁导的"，
-- 放进同一个唯一键里才能表达"每台设备各导一次"。
-- ============================================================================

CREATE TABLE import_batches (
  id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  public_id        CHAR(32)        NOT NULL,
  device_id        BIGINT UNSIGNED NOT NULL COMMENT '谁导进来的',
  fingerprint      CHAR(64)        NOT NULL COMMENT '上传包原始字节的 sha256，幂等键',
  source           ENUM('zip','json')            NOT NULL COMMENT '包的类型',
  mode             ENUM('merge','replace')       NOT NULL DEFAULT 'merge',
  package_version  INT             NOT NULL DEFAULT 0 COMMENT '包自己声明的版本号',
  card_count       INT             NOT NULL DEFAULT 0,
  conversation_count INT           NOT NULL DEFAULT 0,
  message_count    INT             NOT NULL DEFAULT 0,
  image_count      INT             NOT NULL DEFAULT 0,
  skipped_count    INT             NOT NULL DEFAULT 0 COMMENT '丢掉的消息/图片条数',
  report           JSON            NULL COMMENT '明细：未收录角色、丢失项、自定义提示词等',
  created_at       DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  revoked_at       DATETIME(6)     NULL COMMENT '撤销时间；非空表示这次导入的会话已软删',
  PRIMARY KEY (id),
  UNIQUE KEY uk_batch_public_id (public_id),
  UNIQUE KEY uk_batch_device_fp (device_id, fingerprint),
  KEY ix_batch_device (device_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='数据包导入批次。一个包一次导入 = 一行';

-- 会话记住自己的来源批次。刻意**不加外键**：批次是可撤销的历史记录，
-- 会话是主数据，两者生命周期不同步（和 conversations.user_id 一样只做软引用）。
ALTER TABLE conversations
    ADD COLUMN import_batch_id BIGINT UNSIGNED NULL COMMENT '由哪次导入创建；NULL = 用户自己建的',
    ADD KEY ix_conv_batch (import_batch_id);

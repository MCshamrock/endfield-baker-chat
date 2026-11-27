-- ============================================================================
-- 018_upload_tickets.sql —— 取件码：免登录上传的数据包，等账号来领
--
-- 场景：用户刚装好 APK、还没有账号，但手机里有一个「导出数据包」（几十兆）。
-- 让他先注册再选一次文件，等于在最陌生的时刻干最麻烦的事。所以拆成两步：
--
--   1. POST /v1/uploads（免登录）→ 包存进 upload_tickets/<code>.bin，拿到 8 位码
--   2. 注册时带上 claim_code → 同一个事务里把包导进这个新账号
--
-- 几个决定：
--   * code 是 CHAR(8) + 唯一索引。字符集去掉 0 O 1 I L（手抄最容易错的五个）。
--   * **码不是一次性的**：24 小时内同一个码可以被多台设备/多个账号分别领取。
--     所以可领条件只有「没过期」，没有"claimed_at IS NULL"那种一次性判断。
--     同一台设备重复领 = 导入管线按 (device, sha256) 幂等，变成 duplicate。
--   * claim_count / last_claimed_at 记录领取情况，只用于排查和前端提示。
--   * sha256 不做唯一：同一个包传两次会拿到两个码（各自独立，互不影响）。
--
-- 文件和行是分开存的：文件在 <upload_dir>/tickets/<code>.bin，行在这里。
-- 清理任务删行的时候必须同时删文件，否则磁盘只会涨不会落（见
-- scripts/cleanup_uploads.py）。
-- ============================================================================

CREATE TABLE upload_tickets (
  id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  code              CHAR(8)         NOT NULL COMMENT '8 位取件码，大写，去掉易混字符',
  sha256            CHAR(64)        NOT NULL COMMENT '包内容的指纹，领取时做导入幂等',
  filename          VARCHAR(255)    NOT NULL DEFAULT '' COMMENT '用户上传时的文件名（仅用于展示/兜底判类型）',
  bytes             BIGINT          NOT NULL COMMENT '包的大小（字节）',
  source            ENUM('json','zip') NOT NULL COMMENT '包的类型：单文件 JSON / ZIP',
  created_at        DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  expires_at        DATETIME(6)     NOT NULL COMMENT '过期时间（默认 +24h），过期后可领条件不再成立',
  last_claimed_at   DATETIME(6)     NULL COMMENT '最后一次被领取的时间；NULL = 还没人领过（不是不能再领）',
  claim_count       INT             NOT NULL DEFAULT 0 COMMENT '被领取过几次',
  claimed_device_id BIGINT UNSIGNED NULL COMMENT '最后一次领取的设备（软引用，不加外键）',
  claimed_user_id   BIGINT UNSIGNED NULL COMMENT '最后一次领取的账号（软引用，不加外键）',
  import_batch_id   BIGINT UNSIGNED NULL COMMENT '最后一次领取产生的导入批次',
  PRIMARY KEY (id),
  UNIQUE KEY uk_ticket_code (code),
  KEY ix_ticket_expires (expires_at),
  KEY ix_ticket_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='取件码：免登录上传的数据包，等账号来领取';

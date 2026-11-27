-- ============================================================================
-- 001_init.sql —— BakerAI 新后端第一版表结构（13 张）
--
-- 这份 DDL 是**权威**：字段以这里为准，app/models.py 只是它的镜像。
-- 改表流程：新建 002_xxx.sql（不要改这个文件），然后把 models.py 同步改掉，
-- 跑 tests/test_schema.py —— 两边不一致会直接挂，忘改一边跑不过。
--
-- 全局约定（每一条都是踩过或想清楚才定的）：
--   1. 字符集 utf8mb4 / 排序 utf8mb4_0900_ai_ci —— 中文、emoji 都要能存，比对不分大小写。
--   2. 主键 BIGINT UNSIGNED AUTO_INCREMENT —— 索引比字符串小一半，顺序写快。
--   3. 对外 id 用 public_id CHAR(32)（uuid4 的 hex）—— 不把自增 id 暴露给客户端，防遍历。
--   4. 时间全 DATETIME(6)，存 UTC。不用 TIMESTAMP：它有 2038 上限，还会按服务器时区隐式转换。
--   5. 名字全部小写 —— Windows 版 MySQL 表名大小写不敏感、Linux 敏感，全小写两边一致。
--   6. 长文本用 MEDIUMTEXT（16MB），别用 TEXT（64KB 存不下长人设）。
-- ============================================================================

SET NAMES utf8mb4;

-- ---------------------------------------------------------------------------
-- 1. users —— 账号。第一版不做登录，表先建好，设备可以挂到账号名下。
-- ---------------------------------------------------------------------------
CREATE TABLE users (
    id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    public_id    CHAR(32)        NOT NULL,
    login_type   ENUM('none', 'email', 'qq') NOT NULL DEFAULT 'none',
    login_id     VARCHAR(190)    NULL COMMENT '邮箱等；访客为 NULL',
    pass_hash    VARCHAR(255)    NULL,
    display_name VARCHAR(64)     NOT NULL DEFAULT '',
    status       ENUM('active', 'disabled') NOT NULL DEFAULT 'active',
    created_at   DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    last_seen_at DATETIME(6)     NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_users_public_id (public_id),
    -- MySQL 的唯一索引允许多个 NULL，所以访客（login_id 为 NULL）不会互相撞
    UNIQUE KEY uk_users_login_id (login_type, login_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='账号。第一版不启用登录，device 可挂到 user 名下';

-- ---------------------------------------------------------------------------
-- 2. devices —— 设备。第一版真正的身份就是它（匿名 token）。
-- ---------------------------------------------------------------------------
CREATE TABLE devices (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    public_id     CHAR(32)        NOT NULL,
    token_hash    CHAR(64)        NOT NULL COMMENT 'token 的 sha256；明文只在注册时返回一次',
    owner_user_id BIGINT UNSIGNED NULL COMMENT 'NULL = 访客；登录后挂到 user',
    platform      VARCHAR(32)     NOT NULL DEFAULT 'unknown',
    app_ver       VARCHAR(32)     NOT NULL DEFAULT '',
    app_build     INT             NOT NULL DEFAULT 0,
    model         VARCHAR(64)     NOT NULL DEFAULT '',
    os_ver        VARCHAR(32)     NOT NULL DEFAULT '',
    tz            VARCHAR(64)     NOT NULL DEFAULT '',
    locale        VARCHAR(16)     NOT NULL DEFAULT '',
    created_at    DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    last_seen_at  DATETIME(6)     NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_devices_public_id (public_id),
    UNIQUE KEY uk_devices_token_hash (token_hash),
    KEY ix_devices_owner (owner_user_id),
    CONSTRAINT fk_devices_user FOREIGN KEY (owner_user_id) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='设备。token 只存哈希，库泄露也拿不到可用 token';

-- ---------------------------------------------------------------------------
-- 3. characters / 4. character_prompts —— 角色与提示词
-- ---------------------------------------------------------------------------
CREATE TABLE characters (
    id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    slug       VARCHAR(64)     NOT NULL COMMENT '稳定英文 id，客户端不再硬编码角色表',
    name       VARCHAR(64)     NOT NULL COMMENT '中文名',
    avatar     VARCHAR(255)    NOT NULL DEFAULT '',
    attribute  VARCHAR(32)     NOT NULL DEFAULT '',
    sort_order INT             NOT NULL DEFAULT 0,
    enabled    TINYINT(1)      NOT NULL DEFAULT 1,
    PRIMARY KEY (id),
    UNIQUE KEY uk_characters_slug (slug),
    KEY ix_characters_enabled (enabled, sort_order)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='角色。服务端权威，客户端只缓存';

CREATE TABLE character_prompts (
    id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    character_id BIGINT UNSIGNED NOT NULL,
    ver          INT             NOT NULL COMMENT '版本号，改人设就 +1，不原地覆盖',
    body         MEDIUMTEXT      NOT NULL COMMENT '自然语言人设，约 5000 字',
    active       TINYINT(1)      NOT NULL DEFAULT 0,
    created_at   DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uk_prompts_char_ver (character_id, ver),
    KEY ix_prompts_active (character_id, active),
    CONSTRAINT fk_prompts_char FOREIGN KEY (character_id) REFERENCES characters (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='角色提示词按版本存：改人设不覆盖历史，出问题能回退';

-- ---------------------------------------------------------------------------
-- 5. conversations —— 会话。这一版的核心：状态归服务端。
-- ---------------------------------------------------------------------------
CREATE TABLE conversations (
    id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    public_id      CHAR(32)        NOT NULL,
    device_id      BIGINT UNSIGNED NOT NULL COMMENT '归属设备（访客也有）',
    user_id        BIGINT UNSIGNED NULL COMMENT '登录后才有',
    character_id   BIGINT UNSIGNED NULL COMMENT '单聊的角色；群聊为 NULL',
    mode           ENUM('single', 'group') NOT NULL DEFAULT 'single',
    title          VARCHAR(120)    NOT NULL DEFAULT '',
    next_seq       BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '下一条消息的序号；分配时先 SELECT ... FOR UPDATE 锁行',
    api_key_id     BIGINT UNSIGNED NULL COMMENT 'key 亲和绑定：这个会话固定用哪把 key（第 4 刀才用）',
    summary_head   TEXT            NULL COMMENT '摘要头（最近一次压缩结果的引用）',
    created_at     DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    last_active_at DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    deleted_at     DATETIME(6)     NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_conv_public_id (public_id),
    KEY ix_conv_device_active (device_id, last_active_at),
    KEY ix_conv_user (user_id, last_active_at),
    CONSTRAINT fk_conv_device FOREIGN KEY (device_id) REFERENCES devices (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='会话。客户端只拿 public_id 当指针，历史在服务端';

-- ---------------------------------------------------------------------------
-- 6. conversation_members —— 群聊成员（第 13 张，只建表，第 8 刀才读写）
-- ---------------------------------------------------------------------------
CREATE TABLE conversation_members (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    conversation_id BIGINT UNSIGNED NOT NULL,
    character_id    BIGINT UNSIGNED NOT NULL,
    ord             INT             NOT NULL COMMENT '发言顺序，从 0 开始',
    role            ENUM('ai', 'player') NOT NULL DEFAULT 'ai',
    PRIMARY KEY (id),
    UNIQUE KEY uk_members_conv_ord (conversation_id, ord),
    CONSTRAINT fk_members_conv FOREIGN KEY (conversation_id) REFERENCES conversations (id),
    CONSTRAINT fk_members_char FOREIGN KEY (character_id) REFERENCES characters (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='群聊成员。单聊不用这张表';

-- ---------------------------------------------------------------------------
-- 7. messages —— 消息。两个唯一键就是全部幂等保证。
-- ---------------------------------------------------------------------------
CREATE TABLE messages (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    conversation_id BIGINT UNSIGNED NOT NULL,
    seq             BIGINT UNSIGNED NOT NULL COMMENT '会话内序号，从 1 连续递增',
    client_msg_id   VARCHAR(64)     NOT NULL COMMENT '幂等键；assistant 用 a:{seq}，禁止空串（空串会撞唯一键）',
    side            ENUM('user', 'assistant', 'system') NOT NULL,
    speaker         VARCHAR(64)     NOT NULL DEFAULT '' COMMENT '玩家代演的角色名（R-001）；空 = 用户本人',
    text            MEDIUMTEXT      NOT NULL,
    attachment_id   BIGINT UNSIGNED NULL,
    visible_scope   ENUM('private', 'group') NOT NULL DEFAULT 'group' COMMENT '群聊可见性，防串台',
    mood            VARCHAR(32)     NOT NULL DEFAULT '',
    tokens          INT             NOT NULL DEFAULT 0,
    origin          ENUM('v1', 'legacy') NOT NULL DEFAULT 'v1' COMMENT 'legacy = 老 /chat 垫片写入',
    created_at      DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uk_msg_conv_seq (conversation_id, seq),
    UNIQUE KEY uk_msg_conv_client (conversation_id, client_msg_id),
    KEY ix_msg_conv_id (conversation_id, id),
    CONSTRAINT fk_msg_conv FOREIGN KEY (conversation_id) REFERENCES conversations (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='消息。弱网重发靠 (conversation_id, client_msg_id) 唯一键挡住，不靠应用层判重';

-- ---------------------------------------------------------------------------
-- 8. attachments —— 附件（图片不再 base64 塞进消息体）
-- ---------------------------------------------------------------------------
CREATE TABLE attachments (
    id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    public_id  CHAR(32)        NOT NULL,
    sha256     CHAR(64)        NOT NULL COMMENT '按内容去重：同一张图传两次只存一份',
    mime       VARCHAR(64)     NOT NULL DEFAULT '',
    bytes      BIGINT UNSIGNED NOT NULL DEFAULT 0,
    width      INT             NOT NULL DEFAULT 0,
    height     INT             NOT NULL DEFAULT 0,
    path       VARCHAR(255)    NOT NULL DEFAULT '' COMMENT '相对存储根路径',
    created_at DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uk_att_public_id (public_id),
    UNIQUE KEY uk_att_sha256 (sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='附件。第 7 刀才实现上传';

-- ---------------------------------------------------------------------------
-- 9. summaries —— 摘要（结构化 JSON，不写散文；版本化才能回退）
-- ---------------------------------------------------------------------------
CREATE TABLE summaries (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    conversation_id BIGINT UNSIGNED NOT NULL,
    ver             INT             NOT NULL COMMENT '第几版摘要',
    body            JSON            NOT NULL COMMENT '{memory, plot, state, timeline}',
    covered_to_seq  BIGINT UNSIGNED NOT NULL COMMENT '这条摘要覆盖到第几条消息',
    active          TINYINT(1)      NOT NULL DEFAULT 0,
    created_at      DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uk_sum_conv_ver (conversation_id, ver),
    CONSTRAINT fk_sum_conv FOREIGN KEY (conversation_id) REFERENCES conversations (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='摘要。第 6 刀才写';

-- ---------------------------------------------------------------------------
-- 10. usage_events —— 用量记账。每个请求一行。
-- ---------------------------------------------------------------------------
CREATE TABLE usage_events (
    id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    request_id        CHAR(32)        NOT NULL COMMENT '串起日志与这条记录',
    device_id         BIGINT UNSIGNED NULL,
    user_id           BIGINT UNSIGNED NULL,
    conversation_id   BIGINT UNSIGNED NULL,
    api_key_id        BIGINT UNSIGNED NULL,
    model             VARCHAR(64)     NOT NULL DEFAULT '',
    prompt_tokens     INT             NOT NULL DEFAULT 0,
    completion_tokens INT             NOT NULL DEFAULT 0,
    cached_tokens     INT             NOT NULL DEFAULT 0 COMMENT '上游前缀缓存命中量：粘性到底有没有生效，看这个',
    first_token_ms    INT             NOT NULL DEFAULT 0,
    total_ms          INT             NOT NULL DEFAULT 0,
    status            ENUM('ok', 'error', 'timeout') NOT NULL DEFAULT 'ok',
    created_at        DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY ix_usage_device_time (device_id, created_at),
    KEY ix_usage_time (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='用量记账。服务端记，客户端重装也不丢';

-- ---------------------------------------------------------------------------
-- 11. api_keys —— 上游密钥（加密存库，不再有明文 api_pool.json）
-- ---------------------------------------------------------------------------
CREATE TABLE api_keys (
    id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    label          VARCHAR(64)     NOT NULL DEFAULT '',
    key_enc        VARBINARY(512)  NOT NULL COMMENT 'AES-GCM 密文，密钥只在环境变量里',
    base_url       VARCHAR(255)    NOT NULL DEFAULT '',
    model          VARCHAR(64)     NOT NULL DEFAULT '',
    rpm            INT             NOT NULL DEFAULT 10,
    max_concurrent INT             NOT NULL DEFAULT 1,
    enabled        TINYINT(1)      NOT NULL DEFAULT 1,
    cooldown_until DATETIME(6)     NULL,
    last_used_at   DATETIME(6)     NULL,
    fail_count     INT             NOT NULL DEFAULT 0,
    created_at     DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY ix_keys_enabled (enabled, cooldown_until)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='上游 key。第 4 刀才写入';

-- ---------------------------------------------------------------------------
-- 12. config —— 运行时可改的开关与阈值（改完不用重启）
-- ---------------------------------------------------------------------------
CREATE TABLE config (
    k          VARCHAR(64) NOT NULL,
    v          JSON        NOT NULL,
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (k)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='开关与阈值：压缩阈值、灰度比例、min_version 等';

-- ---------------------------------------------------------------------------
-- 13. jobs —— 后台任务（MySQL 表 + 进程内轮询，不上 Celery）
-- ---------------------------------------------------------------------------
CREATE TABLE jobs (
    id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    kind       VARCHAR(48)     NOT NULL COMMENT 'summarize / export / backup / cleanup',
    payload    JSON            NOT NULL,
    state      ENUM('pending', 'running', 'done', 'failed') NOT NULL DEFAULT 'pending',
    tries      INT             NOT NULL DEFAULT 0,
    last_error TEXT            NULL,
    run_after  DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '到期才执行',
    created_at DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY ix_jobs_state_time (state, run_after)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='后台任务。第 6 刀才用';

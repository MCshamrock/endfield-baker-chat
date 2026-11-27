-- ============================================================================
-- 015_memory_and_weight.sql —— 干员记忆 + 发言权重（第 15 刀 + 记忆项目书）
--
-- 三张新表 + 两列改动，都是同一件事的两个面：
--   * 干员记忆：跨会话记住"你这个人"（人情账），见 docs/04-干员记忆.md
--   * 发言权重：群聊里按 active 加权随机选下一个说话的人
--
-- 两条设计要点（别改）：
--   1. 记忆挂在 owner_key 上，不挂在会话上 —— 所以换对话还在。
--      `u:<user_id>` = 登录账号，`d:<device_id>` = 访客设备，和会话归属同一套口径。
--   2. 记忆**软删**（status='deleted'）且保留 fingerprint：
--      删掉的条目下次提炼不能再写回来，否则"让我忘掉"就是句空话。
-- ============================================================================

-- ---------------------------------------------------------------- 发言权重
-- 每个干员一个默认权重：值越大越容易抢到话。1~100。
ALTER TABLE characters
    ADD COLUMN active_weight TINYINT UNSIGNED NOT NULL DEFAULT 50
        COMMENT '群聊发言权重 1~100（越大越容易开口）';

-- 默认值不能全是 50 —— 全员等权等于没有权重。按主键给一个稳定但有差异的分布，
-- 这样每个干员天生"话多话少"不同，用户还能再改。
UPDATE characters SET active_weight = 20 + (id * 37) % 61;

-- ---------------------------------------------------------------- 会话级覆盖
-- 这一局要不要让她记得你：inherit 跟随全局，on/off 单独覆盖。
ALTER TABLE conversations
    ADD COLUMN memory_mode ENUM('inherit', 'on', 'off') NOT NULL DEFAULT 'inherit'
        COMMENT '干员记忆：inherit 跟随全局 / on / off';

-- ---------------------------------------------------------------- 干员记忆
CREATE TABLE character_memories (
    id                     BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    owner_key              VARCHAR(32)     NOT NULL COMMENT 'u:<user_id> 或 d:<device_id>',
    character_id           BIGINT UNSIGNED NOT NULL,
    kind                   VARCHAR(16)     NOT NULL COMMENT 'nickname/call/appearance/relation/preference/dislike/promise/fact',
    text                   VARCHAR(300)    NOT NULL COMMENT '一条只记一件事，≤40 字',
    fingerprint            CHAR(16)        NOT NULL COMMENT '去重指纹，删过的也留着防复活',
    status                 ENUM('active', 'superseded', 'deleted') NOT NULL DEFAULT 'active',
    supersedes_id          BIGINT UNSIGNED NULL COMMENT '被它推翻的那条',
    source_conversation_id BIGINT UNSIGNED NULL COMMENT '哪来的（便于回看上下文）',
    pinned                 TINYINT(1)      NOT NULL DEFAULT 0,
    created_at             DATETIME(6)     NOT NULL,
    updated_at             DATETIME(6)     NOT NULL,
    last_used_at           DATETIME(6)     NOT NULL COMMENT '上次被注入的时间（淘汰依据）',
    PRIMARY KEY (id),
    UNIQUE KEY uk_mem_owner_char_fp (owner_key, character_id, fingerprint),
    KEY ix_mem_lookup (owner_key, character_id, status, kind),
    KEY ix_mem_char_status (character_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='干员记忆（跨会话的人情账）';

-- ---------------------------------------------------------------- 记忆开关
-- 没有行 = 默认 on。单独一张小表，不动 devices / users。
CREATE TABLE memory_settings (
    owner_key  VARCHAR(32) NOT NULL,
    mode       ENUM('on', 'off') NOT NULL DEFAULT 'on',
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (owner_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='玩家级记忆默认开关';

-- ---------------------------------------------------------------- 玩家自定义权重
CREATE TABLE character_weights (
    owner_key    VARCHAR(32)     NOT NULL,
    character_id BIGINT UNSIGNED NOT NULL,
    active       TINYINT UNSIGNED NOT NULL COMMENT '1~100',
    updated_at   DATETIME(6)     NOT NULL,
    PRIMARY KEY (owner_key, character_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='玩家对某个干员的发言权重覆盖（没行就用 characters.active_weight）';

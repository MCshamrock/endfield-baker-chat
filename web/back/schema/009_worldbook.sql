-- ============================================================================
-- 009_worldbook.sql —— 世界书条目表
--
-- 为什么现在才建：切片 5 之前没有检索，也没有世界书数据。现在拿到现成的
-- 《终末地通用世界书》（147 条、字段齐全），才值得为它建表。
--
-- 字段对应聊天终端内核里的条目结构：
--   source_id ↔ id（e001）        title ↔ t
--   keys      ↔ k                fuzzy ↔ fz
--   semantic  ↔ s                priority ↔ p
--   is_constant ↔ c（常驻）
--
-- 为什么 keys/fuzzy 用 JSON 而不用单独的 keys 表：
--   检索是**全量装内存**算的（147 条、30 KB 正文），不进 SQL 查询。
--   所以没有"按 key 查库"的需求，拆表只会多一层 join。
--   如果哪天条目涨到几万、必须进库里筛，再拆表也不迟（那时才需要索引）。
-- ============================================================================

CREATE TABLE worldbook (
    id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    world_slug  VARCHAR(64)     NOT NULL COMMENT '世界书标识（如 endfield）',
    source_id   VARCHAR(32)     NOT NULL COMMENT '条目编号 e001（模型引用它）',
    kind        VARCHAR(16)     NOT NULL DEFAULT 'term' COMMENT 'term/place/person/event/rule',
    priority    VARCHAR(8)      NOT NULL DEFAULT 'mid' COMMENT 'high/mid/low，参与排序',
    title       VARCHAR(120)    NOT NULL,
    semantic    VARCHAR(600)    NOT NULL DEFAULT '' COMMENT '一句话简介，语义档的主要依据',
    `keys`      JSON            NOT NULL COMMENT '触发词数组（keys 是 MySQL 保留字，必须加反引号）',
    fuzzy       JSON            NULL COMMENT '易错写法（错别字/谐音）',
    body        MEDIUMTEXT      NOT NULL COMMENT '正文，直接拼进 prompt',
    is_constant TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '1=常驻，每轮无条件注入',
    updated_at  DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uk_world_source (world_slug, source_id),
    KEY ix_world_kind (world_slug, kind, priority)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='世界书条目。整份装进内存做检索，不靠 SQL 查询';

-- ============================================================================
-- 019_prompt_variant.sql —— 给角色提示词加「变体」：v1（老包）/ v2（实验性长卡）
--
-- 为什么需要这一列：
--   老客户端有一个 `use_new_prompt` 开关。打开时，老后端去读 characters_v2/ 里的
--   长卡；那套卡不存在时再回落到 characters/（v1）。新后端原来只有一套提示词，
--   开关收了字段却完全没用 —— 这一列就是让两套提示词能在同一张表里各存各的。
--
-- 为什么必须动唯一键（这一步最容易做错）：
--   原来的唯一键是 (character_id, ver)。两套提示词的「第 1 版」都叫 ver = 1：
--   同一个角色写 v2 时就会和已有的 v1 ver=1 撞键，第二条根本插不进去。
--   所以唯一键必须改成 (character_id, variant, ver) —— 把变体也放进键里。
--
-- 语句顺序不能反（MySQL 会按顺序执行）：
--   ① 先加列（后面两个键都要用到它）
--   ② 再换唯一键
--   ③ 最后换查询用的普通索引
--   如果先建带 variant 的键、后加列，会报 "Unknown column 'variant'"。
--
-- 老数据不用搬：加列时 DEFAULT 'v1' 会把现有所有行自动填成 v1。
-- ============================================================================

-- ① 加列。VARCHAR(8) 只放 'v1' / 'v2' 这种短标记，留一点余量。
ALTER TABLE character_prompts
    ADD COLUMN variant VARCHAR(8) NOT NULL DEFAULT 'v1'
        COMMENT '提示词变体：v1 = 老包常规长卡 / v2 = 实验性长卡'
        AFTER ver;

-- ② 换唯一键：(角色, 变体, 版本) 三列一起才唯一。
--    这样 v1 和 v2 可以各自都有 ver=1，互不冲突。
ALTER TABLE character_prompts
    DROP INDEX uk_prompts_char_ver,
    ADD UNIQUE KEY uk_prompts_char_variant_ver (character_id, variant, ver);

-- ③ 换取词用的索引：查询条件是
--    WHERE character_id = ? AND variant = ? AND active = 1 ORDER BY ver DESC
--    所以 (character_id, variant, active) 正好跟上这个查询的形状。
--    ⚠️ 这一句同时做了「删旧索引」和「建新索引」：中间不会出现
--    「character_id 上没有索引」的空档（外键要靠它，缺了会报错）。
ALTER TABLE character_prompts
    DROP INDEX ix_prompts_active,
    ADD KEY ix_prompts_active (character_id, variant, active);

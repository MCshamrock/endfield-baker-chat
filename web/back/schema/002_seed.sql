-- ============================================================================
-- 002_seed.sql —— 种子数据
--
-- 第一版只放 3 个占位角色：没有它们，就建不了会话（conversations.character_id 是外键），
-- 也就没法验"会话/消息/幂等"这一刀。
-- 第 3 刀会把 36 个真实角色和他们的提示词灌进来，那时这三条会被真实数据覆盖。
-- ============================================================================

SET NAMES utf8mb4;

INSERT INTO characters (slug, name, avatar, attribute, sort_order, enabled) VALUES
    ('perlica',         '佩丽卡',   '', '终末地工业', 1, 1),
    ('chenqianyu',      '陈千语',   '', '终末地工业', 2, 1),
    ('endministrator',  '管理员',   '', '',           3, 1)
ON DUPLICATE KEY UPDATE
    name       = VALUES(name),
    attribute  = VALUES(attribute),
    sort_order = VALUES(sort_order);

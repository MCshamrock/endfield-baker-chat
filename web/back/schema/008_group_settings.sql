-- ============================================================================
-- 008_group_settings.sql —— 群聊的两个运行时设置
--
-- speak_mode：谁来说（round 轮转 / smart 主持人判断 / assign 玩家指定）
-- player_role：玩家在群里是什么身份（observer 旁观 / admin 以管理员出场 / 某个角色名）
--
-- 为什么放在 conversations 上而不是新建群聊设置表：
--   它们和会话一一对应、每次发言都要读，放同一行就是一次主键查询的事。
-- ============================================================================

ALTER TABLE conversations
    ADD COLUMN speak_mode VARCHAR(16) NOT NULL DEFAULT 'smart'
        COMMENT '群聊发言策略：round / smart / assign',
    ADD COLUMN player_role VARCHAR(64) NOT NULL DEFAULT 'observer'
        COMMENT '玩家在群里的身份：observer / admin / 角色名';

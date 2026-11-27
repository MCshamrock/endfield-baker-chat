-- ============================================================================
-- 014_username_login.sql —— 登录标识从"邮箱"改成"用户名"
--
-- 为什么改：BakerAI 的用户是玩家，注册时让他填邮箱只是多一步、还要考虑
-- "收不到验证信怎么办"；而这一版根本没有发信能力。用户名 + 密码就够了，
-- 将来真要发信/找回密码再加。
--
-- 只动 ENUM，不动唯一键：`uk_users_login_id (login_type, login_id)` 本来就是
-- 按"登录方式 + 标识"设计的，加一种登录方式不用改索引。
--
-- 'email' 故意**留着**：将来加邮箱登录时不用再改表，只多一种 login_type。
-- ============================================================================

ALTER TABLE users
    MODIFY COLUMN login_type ENUM('none','username','email','qq')
        NOT NULL DEFAULT 'none'
        COMMENT '登录方式；v1 只用 username，email/qq 是预留';

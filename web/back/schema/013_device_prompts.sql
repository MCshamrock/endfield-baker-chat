-- ============================================================================
-- 013_device_prompts.sql —— 设备级的角色提示词覆盖
--
-- 老客户端允许用户自己改某个角色的提示词（导出的包里叫 promptOverrides，
-- ZIP 里是 prompts/characters/<角色名>.txt）。这是用户一个字一个字写出来的，
-- 导入时不能丢，但也**不能写进 character_prompts** —— 那是全局的，
-- 一个人的改动会立刻影响所有人。
--
-- 所以单独一张表：一台设备 + 一个角色 只能有一条覆盖（唯一键）。
-- 生成时优先用它，没有就回落到 character_prompts 的 active 版本。
-- ============================================================================

CREATE TABLE device_prompts (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  device_id    BIGINT UNSIGNED NOT NULL,
  character_id BIGINT UNSIGNED NOT NULL,
  body         MEDIUMTEXT      NOT NULL COMMENT '用户自己写/改的提示词正文',
  created_at   DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uk_dprompt_device_char (device_id, character_id),
  CONSTRAINT fk_dprompt_device FOREIGN KEY (device_id) REFERENCES devices (id),
  CONSTRAINT fk_dprompt_char FOREIGN KEY (character_id) REFERENCES characters (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='设备级角色提示词覆盖。空着就用 character_prompts 的全局版本';

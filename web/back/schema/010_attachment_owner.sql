-- ============================================================================
-- 010_attachment_owner.sql —— 附件归属
--
-- 为什么要归属：附件按 sha256 去重（同一张图只存一份），但"谁的图"必须记得住，
-- 否则任何人拿到 public_id 就能看别人的图。
--
-- 为什么归属在设备而不是用户：这一版没有登录，设备就是身份。
-- 将来接登录时，把 device_id 挂到 user 上即可（和 conversations 一样）。
-- ============================================================================

ALTER TABLE attachments
    ADD COLUMN device_id BIGINT UNSIGNED NULL COMMENT '上传者设备；NULL = 历史遗留数据',
    ADD KEY ix_att_device (device_id);

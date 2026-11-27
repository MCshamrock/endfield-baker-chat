"""models/uploads.py —— 取件码：免登录传上来的数据包，先存着等账号来取。

一行 = 一个上传的包。它存在的唯一理由是**把"上传"和"注册"拆成两步**：

    用户刚装 APK、还没有账号，但手上有个几十兆的「导出数据包」想一起带过来。
    让他先注册再选一次文件，等于让他在最陌生的时刻干最麻烦的事。
    所以：先匿名传 → 拿到 8 位码 → 注册时把码填上 → 数据直接落到自己账号下。

四个字段值得单独说：

  * `code` —— 8 位大写字母数字，用户要手抄/口述。字符集和线上老服务一致：
    `A-HJ-NP-Z2-9`（去掉 `0 O 1 I`，但保留 L —— 老客户端的格式校验认 L）。
  * `sha256` —— 包内容的指纹。同一个包传两次会得到两个码（各自独立），
    但领取时导入管线按 `(设备, sha256)` 做幂等，所以数据只会进来一份。
  * `expires_at` —— 码的寿命（默认 24 小时）。过期后包会被清理任务删掉。
  * `claim_count` / `last_claimed_at` —— 这个码被领过几次、最后一次是什么时候。
    **码不是一次性的**：24 小时内同一个码可以被多台设备/多个账号分别领取，
    每次领取都是独立的一次导入（同一台设备重复领则靠导入幂等变成 duplicate）。

`claimed_device_id` / `claimed_user_id` / `import_batch_id` 记的是**最后一次**领取
的人和那次导入批次 —— 排查"这个码到底被谁用了"时看这三个。
"""

from datetime import datetime

from sqlalchemy import BigInteger, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.mysql import CHAR, ENUM
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UInt64, UtcDateTime

# 取件码宽度（schema 里是 CHAR(8)）。和 app/uploads.py 的 CODE_LENGTH 必须一致。
TicketCode = CHAR(8)


class UploadTicket(Base):
    """一个待领取的数据包。文件本体在 `<upload_dir>/tickets/<code>.bin`。"""

    __tablename__ = "upload_tickets"
    __table_args__ = (
        # 码是给用户抄的，撞了就两个人取到同一个包 —— 唯一索引是硬保证，
        # 生成时的"撞了就重来"只是让正常流程不报错（见 repos/uploads.create）。
        UniqueConstraint("code", name="uk_ticket_code"),
        # 清理任务按过期时间扫，没索引的话它会全表扫
        Index("ix_ticket_expires", "expires_at"),
        # 惰性清理按创建时间兜底（防止 expires_at 被人手工改坏）
        Index("ix_ticket_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(UInt64, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(TicketCode, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # 包的字节数。用 BigInteger（有符号）是因为它不是主键，不需要 64 位无符号；
    # 2 GB 的全站上限也远在 int32 之内，但留着 BIGINT 省得以后改表。
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source: Mapped[str] = mapped_column(ENUM("json", "zip"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    # 最后一次被领取的时间。NULL = 还没人领过（不是"不能再领"！）
    last_claimed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    # 被领过几次。0 = 还没人领
    claim_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 最后一次是谁领的。只做软引用（不加外键）：设备/账号可能被删，
    # 而这条记录要留着给运维查"这个包去哪儿了"。
    claimed_device_id: Mapped[int | None] = mapped_column(UInt64, nullable=True)
    claimed_user_id: Mapped[int | None] = mapped_column(UInt64, nullable=True)
    # 最后一次领取产生的导入批次（import_batches.id）
    import_batch_id: Mapped[int | None] = mapped_column(UInt64, nullable=True)


__all__ = ["UploadTicket"]

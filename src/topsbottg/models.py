from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

json_type = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class RecipientStatus(StrEnum):
    pending = "pending"
    sending = "sending"
    sent = "sent"
    failed = "failed"
    payment_required = "payment_required"
    payment_received = "payment_received"
    paid = "paid"
    cancelled = "cancelled"


class PayoutStatus(StrEnum):
    draft = "draft"
    sending = "sending"
    sent = "sent"
    partially_failed = "partially_failed"
    completed = "completed"
    cancelled = "cancelled"


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)

    payment_profile: Mapped[PaymentProfile | None] = relationship(back_populates="user")
    payout_recipients: Mapped[list[PayoutRecipient]] = relationship(back_populates="user")


class PaymentProfile(Base, TimestampMixin):
    __tablename__ = "payment_profiles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    raw_payment_details: Mapped[str | None] = mapped_column(Text, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="payment_profile")


class Payout(Base, TimestampMixin):
    __tablename__ = "payouts"
    __table_args__ = (
        CheckConstraint("period_start_day BETWEEN 1 AND 31", name="ck_payouts_period_start_day"),
        CheckConstraint("period_start_month BETWEEN 1 AND 12", name="ck_payouts_period_start_month"),
        CheckConstraint("period_end_day BETWEEN 1 AND 31", name="ck_payouts_period_end_day"),
        CheckConstraint("period_end_month BETWEEN 1 AND 12", name="ck_payouts_period_end_month"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    period_start_day: Mapped[int] = mapped_column(nullable=False)
    period_start_month: Mapped[int] = mapped_column(nullable=False)
    period_end_day: Mapped[int] = mapped_column(nullable=False)
    period_end_month: Mapped[int] = mapped_column(nullable=False)
    message_template: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=PayoutStatus.draft.value)
    created_by_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    recipients: Mapped[list[PayoutRecipient]] = relationship(back_populates="payout")


class PayoutRecipient(Base, TimestampMixin):
    __tablename__ = "payout_recipients"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    payout_id: Mapped[int] = mapped_column(ForeignKey("payouts.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=RecipientStatus.pending.value)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_by_admin_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    paid_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_profile_snapshot: Mapped[dict | None] = mapped_column(json_type, nullable=True)

    payout: Mapped[Payout] = relationship(back_populates="recipients")
    user: Mapped[User] = relationship(back_populates="payout_recipients")
    payment_replies: Mapped[list[PaymentReply]] = relationship(back_populates="recipient")


class PaymentReply(Base, TimestampMixin):
    __tablename__ = "payment_replies"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    payout_recipient_id: Mapped[int] = mapped_column(
        ForeignKey("payout_recipients.id", ondelete="CASCADE"), nullable=False
    )
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    parsed: Mapped[dict | None] = mapped_column(json_type, nullable=True)

    recipient: Mapped[PayoutRecipient] = relationship(back_populates="payment_replies")


class AuditLog(Base, TimestampMixin):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    actor_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[dict | None] = mapped_column("metadata", json_type, nullable=True)


Index("ix_users_full_name_lower", func.lower(User.full_name))
Index("ix_payout_recipients_payout_status", PayoutRecipient.payout_id, PayoutRecipient.status)
Index("ix_payout_recipients_user_status", PayoutRecipient.user_id, PayoutRecipient.status)
Index("ix_audit_log_created_at", AuditLog.created_at)

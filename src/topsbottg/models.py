from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
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


class StudyGroup(Base, TimestampMixin):
    __tablename__ = "study_groups"
    __table_args__ = (UniqueConstraint("normalized_code", name="uq_study_groups_normalized_code"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    display_code: Mapped[str] = mapped_column(String(32), nullable=False)
    normalized_code: Mapped[str] = mapped_column(String(32), nullable=False)

    users: Mapped[list[User]] = relationship(back_populates="study_group")


class AdminGrant(Base, TimestampMixin):
    __tablename__ = "admin_grants"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    granted_by_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AdminInvitation(Base, TimestampMixin):
    __tablename__ = "admin_invitations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    target_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_by_owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivery_state: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", server_default="queued")
    last_delivery_error: Mapped[str | None] = mapped_column(String(80), nullable=True)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    study_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("study_groups.id", ondelete="SET NULL"), nullable=True
    )
    registration_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    study_group: Mapped[StudyGroup | None] = relationship(back_populates="users")
    payment_profile: Mapped[PaymentProfile | None] = relationship(back_populates="user")
    payout_recipients: Mapped[list[PayoutRecipient]] = relationship(back_populates="user")
    conversation: Mapped[Conversation | None] = relationship(back_populates="user")



class Conversation(Base, TimestampMixin):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="conversation")
    messages: Mapped[list[ChatMessage]] = relationship(back_populates="conversation")
    reads: Mapped[list[ConversationRead]] = relationship(back_populates="conversation")



class ConversationListSnapshot(Base):
    __tablename__ = "conversation_list_snapshots"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    admin_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    search_query: Mapped[str] = mapped_column(Text, nullable=False)
    study_group_id: Mapped[int | None] = mapped_column(nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ConversationListSnapshotItem(Base):
    __tablename__ = "conversation_list_snapshot_items"

    snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("conversation_list_snapshots.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(nullable=False)
    last_message_id: Mapped[int | None] = mapped_column(nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        CheckConstraint("direction IN ('incoming', 'outgoing')", name="ck_chat_messages_direction"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="text", server_default="text")
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reply_to_telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    author_admin_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="telegram", server_default="telegram")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class ConversationRead(Base):
    __tablename__ = "conversation_reads"
    __table_args__ = (
        UniqueConstraint("conversation_id", "admin_telegram_id", name="uq_conversation_reads_admin"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    admin_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_read_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True
    )

    conversation: Mapped[Conversation] = relationship(back_populates="reads")


class BotFSMState(Base):
    __tablename__ = "bot_fsm_states"

    storage_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    state: Mapped[str | None] = mapped_column(String(128), nullable=True)
    data: Mapped[dict] = mapped_column(json_type, nullable=False, default=dict)


class BotSensitiveMessage(Base):
    __tablename__ = "bot_sensitive_messages"

    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)


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
    message_before_period: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_after_period: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=PayoutStatus.draft.value)
    created_by_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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
    payment_snapshot_version: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    payment_snapshot_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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


class PayoutReadyNotification(Base, TimestampMixin):
    __tablename__ = "payout_ready_notifications"
    __table_args__ = (UniqueConstraint("payout_id", name="uq_payout_ready_payout"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    payout_id: Mapped[int] = mapped_column(ForeignKey("payouts.id", ondelete="CASCADE"), nullable=False)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuditLog(Base, TimestampMixin):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    actor_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[dict | None] = mapped_column("metadata", json_type, nullable=True)


Index("ix_users_full_name_lower", func.lower(User.full_name))
Index(
    "uq_admin_invitations_one_pending",
    AdminInvitation.target_telegram_id,
    unique=True,
    postgresql_where=AdminInvitation.status == "pending",
    sqlite_where=AdminInvitation.status == "pending",
)
Index("ix_users_study_group_id", User.study_group_id)
Index("ix_payout_recipients_payout_status", PayoutRecipient.payout_id, PayoutRecipient.status)
Index("ix_payout_recipients_user_status", PayoutRecipient.user_id, PayoutRecipient.status)
Index("ix_payouts_deleted_id", Payout.deleted_at, Payout.id)
Index("ix_payout_ready_pending", PayoutReadyNotification.delivered_at, PayoutReadyNotification.next_attempt_at)
Index("ix_payment_replies_recipient", PaymentReply.payout_recipient_id, PaymentReply.id)
Index("ix_audit_log_created_at", AuditLog.created_at)

Index("ix_conversations_last_message_at_id", Conversation.last_message_at.desc(), Conversation.id.desc())
Index("ix_conversation_list_snapshots_expires_at", ConversationListSnapshot.expires_at)
Index("ix_chat_messages_conversation_id_id", ChatMessage.conversation_id, ChatMessage.id)
Index(
    "uq_chat_messages_telegram_direction",
    ChatMessage.direction,
    ChatMessage.telegram_chat_id,
    ChatMessage.telegram_message_id,
    unique=True,
    postgresql_where=(
        ChatMessage.telegram_chat_id.is_not(None) & ChatMessage.telegram_message_id.is_not(None)
    ),
    sqlite_where=(
        ChatMessage.telegram_chat_id.is_not(None) & ChatMessage.telegram_message_id.is_not(None)
    ),
)

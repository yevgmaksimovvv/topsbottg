from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from topsbottg.constants import MAX_PAYOUT_MESSAGE_LENGTH


class AdminInvitationCreateIn(BaseModel):
    target_telegram_id: int = Field(gt=0, strict=True)
    model_config = ConfigDict(extra="forbid")


class StudyGroupOut(BaseModel):
    id: int
    code: str


class UserOut(BaseModel):
    id: int
    telegram_user_id: int
    telegram_id: int
    full_name: str
    study_group: StudyGroupOut | None = None
    has_payment_profile: bool
    payment_profile_id: int | None = None


class UserPageOut(BaseModel):
    items: list[UserOut]
    limit: int
    offset: int
    has_more: bool


class PaymentProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_payment_details: str


class PaymentProfileOut(BaseModel):
    id: int
    user_id: int
    raw_payment_details: str | None = None
    deleted_at: datetime | None = None


class PaymentProfileRevealOut(BaseModel):
    user_id: int
    telegram_user_id: int
    full_name: str
    raw_payment_details: str


class PayoutCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period_start_day: int
    period_start_month: int
    period_end_day: int
    period_end_month: int
    message_before_period: str | None = Field(default=None, max_length=MAX_PAYOUT_MESSAGE_LENGTH)
    message_after_period: str | None = Field(default=None, max_length=MAX_PAYOUT_MESSAGE_LENGTH)


class PayoutUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period_start_day: int | None = None
    period_start_month: int | None = None
    period_end_day: int | None = None
    period_end_month: int | None = None
    message_before_period: str | None = Field(default=None, max_length=MAX_PAYOUT_MESSAGE_LENGTH)
    message_after_period: str | None = Field(default=None, max_length=MAX_PAYOUT_MESSAGE_LENGTH)


class AddRecipientsIn(BaseModel):
    user_ids: list[int]


class AdminEventsTokenOut(BaseModel):
    expires_in: int


class MarkPaidIn(BaseModel):
    expected_snapshot_version: int = Field(ge=1, strict=True)
    paid_at: datetime | None = None
    paid_note: str | None = Field(default=None, max_length=500)


class ReplyOut(BaseModel):
    id: int
    raw_text: str
    parsed: dict | None = None
    created_at: datetime


class RecipientOut(BaseModel):
    id: int
    user_id: int
    full_name: str
    telegram_user_id: int
    telegram_id: int
    status: str
    sent_at: datetime | None = None
    failed_at: datetime | None = None
    failure_reason: str | None = None
    replied_at: datetime | None = None
    paid_at: datetime | None = None
    paid_by_admin_id: int | None = None
    paid_note: str | None = None
    payment_profile_snapshot: dict | None = None
    payment_snapshot_version: int = 0
    payment_snapshot_updated_at: datetime | None = None
    reply: ReplyOut | None = None


class PayoutOut(BaseModel):
    id: int
    period_start_day: int
    period_start_month: int
    period_end_day: int
    period_end_month: int
    period_label: str
    message_template: str
    message_before_period: str | None = None
    message_after_period: str | None = None
    status: str
    workflow_state: Literal[
        "draft", "sending", "no_recipients", "delivery_errors", "no_active_recipients",
        "paid", "ready_for_payment", "awaiting_confirmation", "closed", "cancelled",
    ]
    paid_recipient_count: int = 0
    recipient_count: int = 0
    active_recipient_count: int = 0
    ready_for_payment_count: int = 0
    awaiting_confirmation_count: int = 0
    delivery_error_count: int = 0
    cancelled_recipient_count: int = 0
    created_by_telegram_id: int
    created_at: datetime
    updated_at: datetime


class CampaignRecipientsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_ids: list[int] = Field(default_factory=list, max_length=5000)
    study_group_ids: list[int] = Field(default_factory=list, max_length=5000)

    @field_validator("user_ids", "study_group_ids", mode="before")
    @classmethod
    def _ids_must_be_positive(cls, values: object) -> object:
        if not isinstance(values, list) or any(
            type(item) is not int or item <= 0 for item in values
        ):
            raise ValueError("IDs must be positive integers")
        return values


class CampaignCreateIn(CampaignRecipientsIn):
    text: str = Field(min_length=1, max_length=4096)

    @field_validator("text")
    @classmethod
    def _campaign_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("campaign text must not be blank")
        return value


class CampaignPreviewRecipientOut(BaseModel):
    user_id: int
    full_name: str
    study_group_name: str | None


class CampaignPreviewOut(BaseModel):
    recipient_count: int
    recipients: list[CampaignPreviewRecipientOut]


class CampaignOut(BaseModel):
    id: int
    recipient_count: int
    status: str
    created_at: datetime
    admin_telegram_id: int
    queued_count: int
    sending_count: int
    sent_count: int
    failed_count: int


class CampaignRecipientOut(BaseModel):
    user_id: int
    full_name: str
    study_group_name: str | None
    conversation_id: int
    message_id: int
    status: str
    error_code: str | None
    retry_count: int


class CampaignDetailOut(CampaignOut):
    recipients: list[CampaignRecipientOut]


class CampaignPageOut(BaseModel):
    items: list[CampaignOut]
    next_cursor: str | None


class ConversationCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: int = Field(gt=0, strict=True)


class ConversationOpenOut(BaseModel):
    conversation_id: int


class ConversationUserOut(BaseModel):
    id: int
    telegram_id: int
    full_name: str
    study_group_id: int | None
    study_group_name: str | None


class ChatMessageOut(BaseModel):
    id: int
    conversation_id: int
    direction: Literal["incoming", "outgoing"]
    kind: str
    text: str | None
    caption: str | None = None
    created_at: datetime
    author_admin_telegram_id: int | None = None
    telegram_message_id: int | None = None
    reply_to_telegram_message_id: int | None = None
    delivery_status: Literal["queued", "sending", "sent", "failed"] | None = None
    delivery_error_code: str | None = None
    attachment: dict | None = None


class ChatMessageCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4096)
    reply_to_message_id: int | None = Field(default=None, gt=0, strict=True)

    @field_validator("text")
    @classmethod
    def _nonempty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message text must not be blank")
        return value


class ChatMessageAcceptedOut(BaseModel):
    message: ChatMessageOut


class ConversationItemOut(BaseModel):
    id: int
    user: ConversationUserOut
    last_message: ChatMessageOut | None
    unread_count: int
    last_message_at: datetime | None


class ConversationPageOut(BaseModel):
    items: list[ConversationItemOut]
    next_cursor: str | None


class ChatMessagePageOut(BaseModel):
    items: list[ChatMessageOut]
    next_cursor: str | None


class ConversationSummaryOut(BaseModel):
    unread_count: int


class ConversationReadIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    last_read_message_id: int = Field(gt=0, strict=True)


class ConversationReadOut(BaseModel):
    last_read_message_id: int
    unread_count: int

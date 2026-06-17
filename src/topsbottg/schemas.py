from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class UserOut(BaseModel):
    id: int
    telegram_user_id: int
    telegram_id: int
    full_name: str
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
    message_template: str | None = Field(default=None, max_length=4000)


class PayoutUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period_start_day: int | None = None
    period_start_month: int | None = None
    period_end_day: int | None = None
    period_end_month: int | None = None
    message_template: str | None = Field(default=None, max_length=4000)


class AddRecipientsIn(BaseModel):
    user_ids: list[int]


class AdminEventsTokenOut(BaseModel):
    token: str
    expires_in: int


class MarkPaidIn(BaseModel):
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
    reply: ReplyOut | None = None


class PayoutOut(BaseModel):
    id: int
    period_start_day: int
    period_start_month: int
    period_end_day: int
    period_end_month: int
    period_label: str
    message_template: str
    status: str
    created_by_telegram_id: int
    created_at: datetime
    updated_at: datetime

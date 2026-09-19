"""Pure data contracts used by the customer-service workflow.

This module deliberately contains no Qt, Playwright, HTTP, or persistence
imports.  The worker and adapters in later phases can therefore depend on
these objects without making the business rules difficult to unit test.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path


class ReceptionMode(StrEnum):
    """The one global sending mode selected before a reception run starts."""

    HUMAN_CONFIRMATION = "human_confirmation"
    AUTO_SEND = "auto_send"


class ReceptionStatus(StrEnum):
    """Lifecycle states for one long-lived customer-service run."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    HALTED = "halted"


class ReplyJobStatus(StrEnum):
    """Lifecycle states for a reply generated from one incoming batch."""

    OBSERVED = "observed"
    DEBOUNCING = "debouncing"
    READY = "ready"
    READING_CONTEXT = "reading_context"
    GENERATING = "generating"
    POLICY_CHECK = "policy_check"
    AWAITING_REVIEW = "awaiting_review"
    SENDING = "sending"
    SENT = "sent"
    HANDOFF = "handoff"
    SUPERSEDED = "superseded"
    FAILED = "failed"


class PriceChangeStatus(StrEnum):
    """Lifecycle for a guarded manual or automatic order-price proposal."""

    AWAITING_REVIEW = "awaiting_review"
    APPLYING = "applying"
    APPLIED = "applied"
    NEEDS_CONFIGURATION = "needs_configuration"
    SUPERSEDED = "superseded"
    FAILED = "failed"


class SalesStage(StrEnum):
    """Deterministic commercial-conversation stages used outside the model."""

    QUALIFY = "qualify"
    RECOMMEND = "recommend"
    PROVE_VALUE = "prove_value"
    CLOSE = "close"
    FOLLOWED_UP = "followed_up"
    PAUSED = "paused"


class MessageDirection(StrEnum):
    """Whether a message was sent by the customer or the merchant."""

    INCOMING = "incoming"
    OUTGOING = "outgoing"


class MessageKind(StrEnum):
    """Message types supported by the V1 input/output contract."""

    TEXT = "text"
    IMAGE = "image"
    VOICE = "voice"
    UNKNOWN = "unknown"


class PageHealthStatus(StrEnum):
    """Page conditions relevant to safe continuation of a reception run."""

    HEALTHY = "healthy"
    LOGIN_REQUIRED = "login_required"
    CAPTCHA = "captcha"
    RISK_CONTROL = "risk_control"
    STRUCTURE_CHANGED = "structure_changed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class CustomerServiceConfig:
    """Fixed V1 runtime defaults and bounded context settings.

    The mode is intentionally part of the run configuration: changing it
    while a worker is running is not supported by the product contract.
    Secrets are not included here and must be supplied by a credential store.
    """

    mode: ReceptionMode = ReceptionMode.HUMAN_CONFIRMATION
    poll_interval_seconds: int = 2
    max_conversations_per_poll: int = 10
    debounce_seconds: int = 6
    max_context_messages: int = 20
    max_context_characters: int = 12_000
    max_reply_text_length: int = 2_000
    max_model_attempts: int = 2
    max_send_failures: int = 3
    max_model_failures: int = 3
    sales_follow_up_delay_minutes: int = 30
    sales_follow_up_start_hour: int = 9
    sales_follow_up_end_hour: int = 22

    def __post_init__(self) -> None:
        """Normalize enum-like input and reject unsafe runtime parameters."""
        if not isinstance(self.mode, ReceptionMode):
            object.__setattr__(self, "mode", ReceptionMode(self.mode))
        positive_fields = (
            "poll_interval_seconds",
            "max_conversations_per_poll",
            "debounce_seconds",
            "max_context_messages",
            "max_context_characters",
            "max_reply_text_length",
            "max_model_attempts",
            "max_send_failures",
            "max_model_failures",
            "sales_follow_up_delay_minutes",
        )
        for field_name in positive_fields:
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} 必须大于 0。")
        if not 0 <= self.sales_follow_up_start_hour <= 23:
            raise ValueError("sales_follow_up_start_hour 必须在 0 到 23 之间。")
        if not 1 <= self.sales_follow_up_end_hour <= 24:
            raise ValueError("sales_follow_up_end_hour 必须在 1 到 24 之间。")
        if self.sales_follow_up_start_hour >= self.sales_follow_up_end_hour:
            raise ValueError("销售追问结束时间必须晚于开始时间。")


@dataclass(frozen=True, slots=True)
class DeepSeekSettings:
    """Non-secret model endpoint settings used by the future API client."""

    base_url: str = "https://api.deepseek.com"
    text_model: str = "deepseek-chat"
    vision_model: str = "deepseek-chat"

    def __post_init__(self) -> None:
        if not self.base_url.strip():
            raise ValueError("DeepSeek Base URL 不能为空。")
        if not self.text_model.strip() or not self.vision_model.strip():
            raise ValueError("DeepSeek 模型名称不能为空。")


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    """A lightweight entry from the dedicated customer-service page."""

    conversation_key: str
    display_name: str | None = None
    latest_message_key: str | None = None
    has_unread: bool = False
    changed_at: datetime | None = None
    platform_product_id: str | None = None
    product_title: str | None = None
    listed_price: str | None = None
    last_message_text: str | None = None

    def __post_init__(self) -> None:
        if not self.conversation_key.strip():
            raise ValueError("conversation_key 不能为空。")


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """A normalized message read from the logged-in page."""

    message_key: str
    direction: MessageDirection
    kind: MessageKind
    text: str | None = None
    platform_time: datetime | None = None
    observed_at: datetime | None = None
    content_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not self.message_key.strip():
            raise ValueError("message_key 不能为空。")
        if not isinstance(self.direction, MessageDirection):
            object.__setattr__(self, "direction", MessageDirection(self.direction))
        if not isinstance(self.kind, MessageKind):
            object.__setattr__(self, "kind", MessageKind(self.kind))


@dataclass(frozen=True, slots=True)
class ConversationSnapshot:
    """The minimum page snapshot needed before generating or sending a reply."""

    conversation_key: str
    messages: tuple[ChatMessage, ...]
    platform_product_id: str | None = None
    product_title: str | None = None
    product_url: str | None = None

    def __post_init__(self) -> None:
        if not self.conversation_key.strip():
            raise ValueError("conversation_key 不能为空。")
        object.__setattr__(self, "messages", tuple(self.messages))

    @property
    def last_message(self) -> ChatMessage | None:
        """Return the latest message in page order, if the conversation is non-empty."""
        return self.messages[-1] if self.messages else None

    @property
    def last_incoming_message(self) -> ChatMessage | None:
        """Return the latest customer message in page order."""
        for message in reversed(self.messages):
            if message.direction is MessageDirection.INCOMING:
                return message
        return None

    @property
    def last_message_from_customer(self) -> bool:
        """Whether sending is eligible based on the current final message."""
        return self.last_message is not None and self.last_message.direction is MessageDirection.INCOMING


@dataclass(frozen=True, slots=True)
class ImagePayload:
    """In-memory customer image payload; no local path is exposed to the model."""

    message_key: str
    content: bytes
    mime_type: str

    def __post_init__(self) -> None:
        if not self.message_key.strip():
            raise ValueError("message_key 不能为空。")
        if not self.content:
            raise ValueError("图片内容不能为空。")
        if not self.mime_type.startswith("image/"):
            raise ValueError("顾客图片必须使用 image/* MIME 类型。")


@dataclass(frozen=True, slots=True)
class ProductKnowledge:
    """User-maintained product facts; model output cannot replace these facts."""

    product_key: str
    name: str
    aliases: tuple[str, ...] = ()
    platform_product_id: str | None = None
    listed_price: str | None = None
    minimum_price: str | None = None
    specifications: str = ""
    inventory_notes: str = ""
    shipping_notes: str = ""
    after_sales_notes: str = ""
    supplementary_knowledge: str = ""
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.product_key.strip() or not self.name.strip():
            raise ValueError("商品知识必须包含 product_key 和 name。")
        object.__setattr__(self, "aliases", tuple(self.aliases))


@dataclass(frozen=True, slots=True)
class MediaAsset:
    """A user-registered local image that may be selected by policy."""

    asset_id: str
    product_key: str | None
    display_name: str
    scene_tag: str
    description: str
    path: Path
    sha256: str
    mime_type: str
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.asset_id.strip() or not self.display_name.strip():
            raise ValueError("媒体资源必须包含 asset_id 和 display_name。")
        if not self.mime_type.startswith("image/"):
            raise ValueError("V1 媒体资源只能是图片。")


@dataclass(frozen=True, slots=True)
class HistoricalExample:
    """A Q&A retrieval item whose trust level determines facts versus style use."""

    example_id: str
    customer_text: str
    merchant_text: str
    trust_level: str


@dataclass(frozen=True, slots=True)
class ReplyProposal:
    """Structured model output before local policy validation."""

    reply_text: str
    media_asset_id: str | None = None
    intent: str = "unknown"
    requires_handoff: bool = False
    handoff_reason: str | None = None
    needs_clarification: bool = False
    offered_price: str | None = None
    facts_used: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "facts_used", tuple(self.facts_used))


@dataclass(frozen=True, slots=True)
class SalesState:
    """Persisted sales progression and at-most-once silent-customer follow-up."""

    conversation_key: str
    product_key: str | None
    stage: SalesStage
    follow_up_text: str | None = None
    follow_up_due_at: datetime | None = None
    follow_up_count: int = 0
    last_customer_message_key: str | None = None
    last_merchant_fingerprint: str | None = None
    status: str = "pending"
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.conversation_key.strip():
            raise ValueError("销售状态必须绑定会话。")
        if not isinstance(self.stage, SalesStage):
            object.__setattr__(self, "stage", SalesStage(self.stage))
        if self.follow_up_count < 0:
            raise ValueError("销售追问次数不能小于 0。")
        if self.status not in {"pending", "followed_up", "cancelled", "paused"}:
            raise ValueError("销售状态无效。")
        if self.status == "pending" and (
            not self.follow_up_text or self.follow_up_due_at is None
        ):
            raise ValueError("待追问状态必须包含话术和到期时间。")


@dataclass(frozen=True, slots=True)
class ReplyDraft:
    """A locally validated draft waiting for human confirmation or sending."""

    job_id: str
    conversation_key: str
    batch_fingerprint: str
    reply_text: str
    media_asset_id: str | None = None
    status: ReplyJobStatus = ReplyJobStatus.AWAITING_REVIEW
    failure_reason: str | None = None


@dataclass(frozen=True, slots=True)
class PriceChangeDraft:
    """An audited, locally checked final order-price action."""

    task_id: str
    conversation_key: str
    customer_message_key: str
    product_key: str
    product_name: str
    proposed_price: str
    minimum_price: str | None
    listed_price: str | None
    customer_offer: str | None = None
    rationale: str = ""
    status: PriceChangeStatus = PriceChangeStatus.AWAITING_REVIEW
    failure_reason: str | None = None
    price_adjustment_key: str | None = None
    price_adjustment_amount: str | None = None

    def __post_init__(self) -> None:
        if not self.task_id.strip() or not self.conversation_key.strip():
            raise ValueError("改价任务必须包含任务 ID 和会话。")
        if not self.customer_message_key.strip() or not self.product_key.strip():
            raise ValueError("改价任务必须绑定顾客消息和商品。")
        if bool(self.price_adjustment_key) != bool(self.price_adjustment_amount):
            raise ValueError("改价任务的加价项名称和金额必须同时存在。")


@dataclass(frozen=True, slots=True)
class PriceChangeReceipt:
    """Evidence that the page accepted one explicitly approved price change."""

    previous_price: str | None
    applied_price: str


@dataclass(frozen=True, slots=True)
class HandoffEvent:
    """A persisted, non-blocking notification that needs human attention."""

    event_id: int
    conversation_key: str
    reason: str
    status: str
    created_at: datetime

    def __post_init__(self) -> None:
        if self.event_id <= 0:
            raise ValueError("event_id 必须大于 0。")
        if not self.conversation_key.strip() or not self.reason.strip():
            raise ValueError("转人工通知必须包含会话和原因。")


@dataclass(frozen=True, slots=True)
class PageHealth:
    """A page health result that can trigger a safe global halt."""

    status: PageHealthStatus
    detail: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, PageHealthStatus):
            object.__setattr__(self, "status", PageHealthStatus(self.status))

    @property
    def is_healthy(self) -> bool:
        return self.status is PageHealthStatus.HEALTHY


@dataclass(frozen=True, slots=True)
class SendReceipt:
    """Evidence returned by an adapter after a send action."""

    message_key: str
    content_fingerprint: str


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """Minimal model-client response retained by orchestration and audit code."""

    text: str
    model: str | None = None
    request_id: str | None = None

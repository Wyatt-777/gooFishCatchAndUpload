"""Dependency-injection protocols for the customer-service layers.

Implementations are intentionally deferred to later phases.  Keeping these
interfaces free of concrete browser, HTTP, Qt, and SQLite classes makes the
orchestrator independently testable.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol

from xianyu_assistant.customer_service.importer import ImportBundle
from xianyu_assistant.customer_service.knowledge_importer import CleanedKnowledgeBundle
from xianyu_assistant.customer_service.models import (
    ConversationFulfillmentState,
    ConversationSnapshot,
    ConversationSummary,
    CustomerIntentLevel,
    CustomerLifecycle,
    CustomerOrderStatus,
    CustomerProfile,
    CustomerStateEvent,
    HandoffEvent,
    HistoricalExample,
    ImagePayload,
    MediaAsset,
    ModelResponse,
    PageHealth,
    PriceChangeDraft,
    PriceChangeReceipt,
    ProductKnowledge,
    ReplyDraft,
    SalesStage,
    SalesState,
    SendReceipt,
)


class TextSendNotPerformedError(RuntimeError):
    """The page retained the text and no new outgoing message was observed."""


class PriceChangeNotPerformedError(RuntimeError):
    """The order price was not submitted, so the operator may safely retry."""


class Clock(Protocol):
    """Time source that can be replaced by a deterministic test clock."""

    def now(self) -> datetime:
        """Return the current timezone-aware application time."""

    def monotonic(self) -> float:
        """Return a monotonic value for wait/debounce calculations."""


class XianyuChatAdapter(Protocol):
    """All live Xianyu DOM operations used by the customer-service worker."""

    def open_dedicated_chat_page(self) -> None:
        """Create or focus the application-owned dedicated chat page."""

    def check_page_health(self) -> PageHealth:
        """Check login, captcha, risk-control, and DOM health."""

    def list_changed_conversations(self, limit: int) -> list[ConversationSummary]:
        """List changed/unread conversations subject to the per-round limit."""

    def list_all_conversations(self) -> list[ConversationSummary]:
        """Read every currently rendered and virtualized conversation entry."""

    def open_conversation(self, conversation_key: str) -> None:
        """Open one conversation on the dedicated page."""

    def read_conversation(self) -> ConversationSnapshot:
        """Read the current conversation snapshot from the page."""

    def request_voice_transcript(self, message_key: str) -> str | None:
        """Request the page-provided transcript for one voice message."""

    def capture_incoming_image(self, message_key: str) -> ImagePayload:
        """Capture one customer image in memory without exposing its URL."""

    def send_text(self, text: str) -> SendReceipt:
        """Send one text message and return adapter-level evidence."""

    def send_approved_image(self, path: Path) -> SendReceipt:
        """Send one previously registered and locally policy-approved image."""

    def verify_outgoing(self, receipt: SendReceipt) -> bool:
        """Read the page back and confirm that the outgoing message exists."""

    def change_order_price(self, approved_price: str) -> PriceChangeReceipt:
        """Apply one already policy-checked and explicitly approved order price."""


class DeepSeekClient(Protocol):
    """Text/vision model boundary; no implementation is used in CS-0."""

    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
    ) -> ModelResponse:
        """Generate a structured text response from sanitized prompts."""

    def complete_with_image(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        image: ImagePayload,
        model: str,
    ) -> ModelResponse:
        """Generate a response using an in-memory customer image."""


class CustomerServiceRepository(Protocol):
    """Persistence boundary for knowledge, drafts, audit, and deduplication."""

    def get_setting(self, name: str, default: str | None = None) -> str | None:
        """Read one non-secret runtime setting."""

    def find_product_knowledge(
        self,
        *,
        platform_product_id: str | None = None,
        normalized_title: str | None = None,
    ) -> ProductKnowledge | None:
        """Find a product using only deterministic local matching."""

    def find_product_knowledge_for_query(self, query: str) -> ProductKnowledge | None:
        """Find a current product from an explicit customer model or alias."""

    def save_product_knowledge(self, product: ProductKnowledge) -> None:
        """Persist one merged product knowledge record."""

    def get_product_knowledge(self, product_key: str) -> ProductKnowledge | None:
        """Load one enabled product by its stable local key."""

    def find_historical_examples(self, query: str, limit: int) -> Sequence[HistoricalExample]:
        """Return local wording examples, never authoritative product facts."""

    def find_curated_knowledge_answers(
        self, query: str, limit: int
    ) -> Sequence[HistoricalExample]:
        """Return user-curated question/answer facts relevant to the query."""

    def find_knowledge_answers(self, query: str, limit: int) -> Sequence[HistoricalExample]:
        """Return ranked curated and cleaned conversation knowledge."""

    def has_prior_reply_job(self, conversation_key: str, *, exclude_job_id: str) -> bool:
        """Return whether this conversation already produced an earlier reply job."""

    def has_import_batch(self, source_sha256: str) -> bool:
        """Check import idempotence by source hash."""

    def store_import(self, bundle: ImportBundle) -> bool:
        """Persist a redacted import, returning False if already imported."""

    def store_cleaned_knowledge(self, bundle: CleanedKnowledgeBundle) -> bool:
        """Merge a reviewed knowledge bundle, returning False if already imported."""

    def store_live_snapshot(
        self,
        snapshot: ConversationSnapshot,
        *,
        display_name: str | None = None,
        examples: Sequence[HistoricalExample] = (),
    ) -> int:
        """Upsert one redacted live snapshot and its historical examples."""

    def store_live_summary(self, summary: ConversationSummary) -> None:
        """Persist one redacted conversation index row before full-body reading."""

    def get_media_asset(self, asset_id: str) -> MediaAsset | None:
        """Load one user-registered image asset by ID."""

    def save_reply_draft(self, draft: ReplyDraft) -> None:
        """Persist a sanitized draft or its updated user-edited form."""

    def save_price_change_draft(self, draft: PriceChangeDraft) -> None:
        """Persist one reviewed price proposal and its execution state."""

    def list_price_change_drafts(self, limit: int = 100) -> Sequence[PriceChangeDraft]:
        """List recent price proposals for operator review."""

    def find_reply_draft(
        self, *, conversation_key: str, batch_fingerprint: str
    ) -> ReplyDraft | None:
        """Load an existing job for one conversation batch to avoid restart duplicates."""

    def recover_interrupted_reply_jobs(self, *, recovered_at: datetime) -> int:
        """Safely close incomplete jobs left by a crash or forced stop."""

    def save_handoff_event(
        self, *, conversation_key: str, reason: str, created_at: datetime
    ) -> None:
        """Record a short-lived local handoff reason without sending anything."""

    def record_delivered_conversation(
        self,
        *,
        conversation_key: str,
        platform_product_id: str | None,
        evidence_key: str,
        evidence_text: str,
        observed_at: datetime,
    ) -> ConversationFulfillmentState:
        """Persist platform-confirmed delivery and its conversation automation lock."""

    def record_shipped_conversation(
        self,
        *,
        conversation_key: str,
        platform_product_id: str | None,
        evidence_key: str,
        evidence_text: str,
        observed_at: datetime,
    ) -> ConversationFulfillmentState:
        """Persist platform-confirmed shipment and its conversation automation lock."""

    def get_conversation_fulfillment(
        self, conversation_key: str
    ) -> ConversationFulfillmentState | None:
        """Read fulfillment state for one order conversation."""

    def is_post_delivery_human_owned(self, conversation_key: str) -> bool:
        """Return whether shipped/delivered messages require a human."""

    def save_post_delivery_handoff(
        self, *, conversation_key: str, reason: str, created_at: datetime
    ) -> None:
        """Create a post-delivery notification without releasing its durable lock."""

    def has_open_handoff(self, conversation_key: str) -> bool:
        """Return whether automatic handling is suspended for this conversation."""

    def list_open_handoff_events(self, limit: int = 100) -> Sequence[HandoffEvent]:
        """List persisted in-app notifications awaiting human handling."""

    def resolve_handoff_event(self, event_id: int) -> bool:
        """Close a notification; sticky post-delivery locks remain in force."""

    def restore_conversation_automation(
        self, conversation_key: str, *, restored_at: datetime
    ) -> bool:
        """Explicitly restore automation for one delivered-order conversation."""

    def has_processed_fingerprint(self, batch_fingerprint: str) -> bool:
        """Check the permanent duplicate-send guard."""

    def add_processed_fingerprint(self, batch_fingerprint: str, observed_at: datetime) -> None:
        """Persist a one-way batch fingerprint without storing message text."""

    def observe_customer_turn(
        self,
        *,
        turn_id: str,
        conversation_key: str,
        message_keys: Sequence[str],
        customer_text: str,
        platform_product_id: str | None,
        observed_at: datetime,
    ) -> int:
        """Persist a complete customer turn and return its conversation version."""

    def observe_customer_summary(
        self,
        summary: ConversationSummary,
        *,
        observed_at: datetime,
        platform_customer_id: str | None = None,
    ) -> CustomerProfile:
        """Create or refresh a durable profile without merging on display name."""

    def record_customer_state(
        self,
        *,
        event_key: str,
        conversation_key: str,
        event_type: str,
        lifecycle: CustomerLifecycle,
        intent_level: CustomerIntentLevel,
        observed_at: datetime,
        product_key: str | None = None,
        platform_product_id: str | None = None,
        sales_stage: SalesStage | None = None,
        order_status: CustomerOrderStatus | None = None,
        automation_status: str | None = None,
        battery_model: str | None = None,
        required_range_km: str | None = None,
        motor_power_w: str | None = None,
        quantity: int | None = None,
        budget_amount: str | None = None,
        last_customer_offer: str | None = None,
        accepted_price: str | None = None,
        next_follow_up_at: datetime | None = None,
        clear_next_follow_up: bool = False,
        payload: dict[str, object] | None = None,
    ) -> CustomerProfile:
        """Apply one idempotent deterministic profile transition."""

    def mark_customer_reply_sent(
        self,
        conversation_key: str,
        *,
        event_key: str,
        sales_stage: SalesStage | None,
        next_follow_up_at: datetime | None,
        sent_at: datetime,
    ) -> None:
        """Record a verified merchant reply and its next-follow-up time."""

    def get_customer_profile(self, customer_key: str) -> CustomerProfile | None:
        """Read one durable customer profile."""

    def find_customer_profile_by_conversation(
        self, conversation_key: str
    ) -> CustomerProfile | None:
        """Resolve one conversation to its durable customer profile."""

    def list_customer_profiles(self, limit: int = 100) -> Sequence[CustomerProfile]:
        """List durable profiles by recent activity."""

    def list_customer_state_events(
        self, customer_key: str, *, limit: int = 100
    ) -> Sequence[CustomerStateEvent]:
        """List audited state transitions for one customer."""

    def update_customer_turn(
        self,
        turn_id: str,
        *,
        status: str,
        semantic_json: str | None = None,
        decision_json: str | None = None,
        reply_text: str | None = None,
        failure_reason: str | None = None,
        updated_at: datetime | None = None,
    ) -> None:
        """Update the replayable semantic, decision, reply, and status audit."""

    def should_send_first_contact_catalog(
        self,
        conversation_key: str,
        *,
        snapshot_has_outgoing: bool,
    ) -> bool:
        """Return whether the one-time first-conversation catalog is still eligible."""

    def reserve_first_contact_catalog(
        self,
        conversation_key: str,
        *,
        reservation_id: str,
        reply_fingerprint: str,
        created_at: datetime,
    ) -> bool:
        """Reserve the welcome catalog before attempting its combined send."""

    def release_first_contact_catalog_reservation(
        self,
        conversation_key: str,
        *,
        reservation_id: str,
    ) -> None:
        """Release a welcome reservation after a proven no-op send."""

    def mark_first_contact_catalog_sent(
        self,
        conversation_key: str,
        *,
        reservation_id: str,
        updated_at: datetime,
    ) -> None:
        """Permanently mark the one-time catalog after outgoing read-back."""

    def reserve_send(
        self,
        *,
        send_id: str,
        turn_id: str,
        conversation_key: str,
        expected_version: int,
        expected_last_message_key: str,
        expected_product_id: str | None,
        reply_fingerprint: str,
        created_at: datetime,
    ) -> bool:
        """Reserve a send only if the expected conversation version is current."""

    def mark_send_verified(
        self,
        send_id: str,
        *,
        outgoing_message_key: str,
        handled_message_key: str,
        updated_at: datetime,
    ) -> None:
        """Close the outbox entry after the exact outgoing message is observed."""

    def save_human_confirmed_example(self, example: HistoricalExample) -> None:
        """Store a manually approved Q&A as a high-trust wording example."""

    def save_sales_state(self, state: SalesState) -> None:
        """Persist one sales stage and its optional at-most-once follow-up."""

    def cancel_sales_follow_up(self, conversation_key: str, *, updated_at: datetime) -> None:
        """Cancel a pending follow-up because the customer or operator acted."""

    def list_due_sales_follow_ups(
        self, *, now: datetime, limit: int
    ) -> Sequence[SalesState]:
        """Return pending follow-ups due at or before the supplied wall clock."""

    def mark_sales_follow_up_sent(
        self,
        conversation_key: str,
        *,
        merchant_fingerprint: str,
        updated_at: datetime,
    ) -> None:
        """Atomically close a pending follow-up after verified delivery."""

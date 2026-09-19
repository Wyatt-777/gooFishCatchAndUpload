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
    ConversationSnapshot,
    ConversationSummary,
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

    def save_handoff_event(
        self, *, conversation_key: str, reason: str, created_at: datetime
    ) -> None:
        """Record a short-lived local handoff reason without sending anything."""

    def has_open_handoff(self, conversation_key: str) -> bool:
        """Return whether automatic handling is suspended for this conversation."""

    def list_open_handoff_events(self, limit: int = 100) -> Sequence[HandoffEvent]:
        """List persisted in-app notifications awaiting human handling."""

    def resolve_handoff_event(self, event_id: int) -> bool:
        """Mark a notification handled so future customer messages may resume."""

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

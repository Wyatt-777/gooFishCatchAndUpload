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

    def save_human_confirmed_example(self, example: HistoricalExample) -> None:
        """Store a manually approved Q&A as a high-trust wording example."""

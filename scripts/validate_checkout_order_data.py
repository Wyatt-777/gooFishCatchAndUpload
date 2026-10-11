"""Offline checks of actual order-card data and SQLite commitments; no chat simulation."""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xianyu_assistant.customer_service.browser_adapter import (
    REAL_XIANYU_CHAT_CONTRACT,
    PlaywrightXianyuChatAdapter,
)
from xianyu_assistant.customer_service.checkout import (
    approved_binding_matches,
    binding_matches,
    checkout_intent_matches,
    current_order_state,
    normalized_order_text,
    order_binding,
    price_already_changed,
    reliable_order_binding,
)
from xianyu_assistant.customer_service.fact_guard import ProductMatch, critical_answer
from xianyu_assistant.customer_service.fingerprints import content_fingerprint
from xianyu_assistant.customer_service.models import (
    ChatMessage,
    ConversationSnapshot,
    MessageDirection,
    MessageKind,
)
from xianyu_assistant.customer_service.price_change import check_price_change
from xianyu_assistant.customer_service.quality_export import export_quality_bundle
from xianyu_assistant.persistence.customer_service_repository import CustomerServiceRepository


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-copy", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    checks = {}

    def require(condition: bool, name: str) -> None:
        checks[name] = bool(condition)
        if not condition:
            raise AssertionError(name)

    evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    record = next(row for row in evidence["matched_records"]["messages"]
                  if "我已拍下" in (row.get("text") or "") and "请双方沟通" in row["text"])
    card = ChatMessage(record["message_key"], MessageDirection.INCOMING, MessageKind.TEXT,
                       record["text"], key_source="react_message_id")
    require("二手" in card.text, "actual_card_contains_nickname_condition_word")
    require(normalized_order_text(card.text) == "我已拍下，待付款", "actual_order_card_drops_nickname_and_button")
    require(critical_answer(card.text, ProductMatch((), "unresolved")) is None,
            "raw_platform_card_does_not_trigger_product_condition_handoff")
    snapshot = ConversationSnapshot("offline-commitment", (card,), platform_product_id="offline-item",
                                    platform_customer_id="offline-buyer", merchant_account_id="offline-merchant",
                                    order_state_text="¥398.00\n含运费0.00元\n等待买家付款")
    require(current_order_state(snapshot) == "pending_payment", "pending_header_recognized")
    require(current_order_state(replace(snapshot, order_state_text="等待卖家发货")) == "paid",
            "current_paid_header_overrides_old_pending_card")
    require(current_order_state(replace(snapshot, order_state_text="退款中")) == "refund",
            "current_refund_header_overrides_old_card")
    require(PlaywrightXianyuChatAdapter._header_order_price(snapshot) == "398.00",
            "pending_order_currency_amount_readback")
    require(PlaywrightXianyuChatAdapter._header_shipping(snapshot) == "0.00", "shipping_readback_exact_zero")
    require(PlaywrightXianyuChatAdapter._header_order_price(replace(
        snapshot, order_state_text="活动价¥240.00\n含运费0.00元")) is None, "activity_price_is_not_order_readback")
    require(PlaywrightXianyuChatAdapter._header_order_price(replace(
        snapshot, order_state_text="¥398.00 ¥240.00\n等待买家付款")) is None,
            "ambiguous_header_amount_not_accepted")
    require(REAL_XIANYU_CHAT_CONTRACT.selectors.price_dialog_readback_enabled,
            "existing_price_dialog_contract_is_enabled")
    binding = order_binding(snapshot)
    require(reliable_order_binding(binding) and binding_matches(snapshot, binding), "stable_pending_card_binds_order_without_id")
    require(not reliable_order_binding(order_binding(replace(snapshot, messages=(replace(card, key_source="position_fallback"),)))),
            "scroll_position_cannot_authorize_order")
    require(not binding_matches(replace(snapshot, merchant_account_id="different-merchant"), binding),
            "merchant_identity_change_rejected")
    changed = ChatMessage("offline-price-card", MessageDirection.OUTGOING, MessageKind.TEXT,
                          "我已修改价格，等待你付款")
    require(price_already_changed(replace(snapshot, messages=(card, changed))), "manual_price_change_supersedes_pending_card")

    # Only a previously created disposable copy is allowed here.
    require(args.database_copy.resolve().is_relative_to(Path(__file__).resolve().parents[1] / "output"),
            "checks_write_only_disposable_output_database")
    repository = CustomerServiceRepository(args.database_copy)
    repository.initialize(migrate_confirmed_facts=False)
    product = next(p for p in repository.list_product_knowledge() if p.listed_price and p.minimum_price)
    amount = check_price_change(product.listed_price, product).approved_price
    now = datetime.now(timezone(timedelta(hours=8)))
    reply = f"已确认价格{amount}元"
    repository.save_checkout_intent(send_id="offline-checkout", snapshot=snapshot, product_key=product.product_key,
                                    approved_price=amount, price_source="configured_price", price_variant="base",
                                    settings_version="offline-version", reply_fingerprint=content_fingerprint(reply),
                                    updated_at=now)
    require(repository.load_checkout_intent(snapshot.conversation_key, now=now) is None,
            "unverified_quote_cannot_authorize_price")
    repository.record_quality_send(send_id="offline-checkout", run_id=None, conversation_key=snapshot.conversation_key,
                                   job_id=None, mode="auto_send", origin="application_auto", draft_text=reply,
                                   final_text=reply, created_at=now)
    repository.update_quality_send("offline-checkout", status="verified", updated_at=now)
    restored = CustomerServiceRepository(args.database_copy)
    intent = restored.load_checkout_intent(snapshot.conversation_key, now=now)
    require(intent is not None and intent["approved_price"] == amount, "verified_quote_survives_repository_restart")
    require(checkout_intent_matches(intent, snapshot, product_key=product.product_key,
                                   settings_version="offline-version", price_variant="base", quantity=1),
            "confirmed_price_binds_catalog_model_option_and_quantity")
    for key, change in (("quantity", {"quantity": 2}), ("price_variant", {"price_variant": "bluetooth"}),
                        ("settings_version", {"settings_version": "changed"}), ("product_key", {"product_key": "other"})):
        parameters = {"product_key": product.product_key, "settings_version": "offline-version",
                      "price_variant": "base", "quantity": 1}
        parameters.update(change)
        require(not checkout_intent_matches(intent, snapshot, **parameters), f"changed_{key}_invalidates_commitment")
    require(restored.load_checkout_intent(snapshot.conversation_key, now=now + timedelta(days=8)) is None,
            "old_quote_expires_without_price_guessing")
    with repository._connection() as connection:
        connection.execute("UPDATE cs_quality_sends SET final_text='人工编辑了不同内容' WHERE send_id='offline-checkout'")
    require(restored.load_checkout_intent(snapshot.conversation_key, now=now) is None,
            "edited_send_cannot_activate_original_quote")
    with repository._connection() as connection:
        connection.execute("UPDATE cs_quality_sends SET final_text=? WHERE send_id='offline-checkout'", (reply,))
    manual = ChatMessage("offline-manual-intervention", MessageDirection.OUTGOING, MessageKind.TEXT,
                         "我另外核对一下这单价格")
    repository.record_quality_snapshot(replace(snapshot, messages=(manual,)), run_id=None,
                                       observed_at=now + timedelta(seconds=2))
    require(restored.load_checkout_intent(snapshot.conversation_key, now=now + timedelta(seconds=3)) is None,
            "external_merchant_intervention_invalidates_previous_commitment")
    with repository._connection() as connection:
        connection.execute("DELETE FROM cs_quality_messages WHERE conversation_key=? AND message_key=?",
                           (snapshot.conversation_key, manual.message_key))
    require(restored.load_checkout_intent(snapshot.conversation_key, now=now) is not None,
            "removing_disposable_intervention_record_restores_valid_data")
    repository.invalidate_checkout_intents(snapshot.conversation_key, updated_at=now)
    require(restored.load_checkout_intent(snapshot.conversation_key, now=now) is None,
            "cancelled_checkout_cannot_be_reused")
    permit = {**binding, "approved_price": amount, "shipping_price": "0.00",
              "last_customer_key": card.message_key, "submitted_at": now.isoformat(), "order_price_verifiable": True,
              "checkout_send_id": "offline-checkout"}
    expected = {**binding, "last_customer_key": card.message_key}
    require(approved_binding_matches(permit, expected), "parent_price_approval_matches_final_order")
    require(not approved_binding_matches({**permit, "product_id": "other"}, expected),
            "product_change_between_parent_approval_and_final_click_rejected")
    require(not approved_binding_matches({**permit, "last_customer_key": "new-message"}, expected),
            "new_customer_message_between_approval_and_final_click_rejected")
    repository.prepare_submission(action_id="offline-price", action_type="price", conversation_key=snapshot.conversation_key,
                                   updated_at=now)
    repository.authorize_submission(action_id="offline-price", action_type="price", conversation_key=snapshot.conversation_key,
                                     evidence=permit, updated_at=now)
    for identifier in ("offline-price", "offline-second-task"):
        try:
            repository.authorize_submission(action_id=identifier, action_type="price",
                                             conversation_key=snapshot.conversation_key, evidence=permit, updated_at=now)
        except ValueError:
            require(True, f"duplicate_permit_rejected_{identifier}")
        else:
            require(False, f"duplicate_permit_rejected_{identifier}")
    repository.finish_submission("offline-price", state="verified", evidence=permit, updated_at=now)
    try:
        repository.authorize_submission(action_id="offline-changed-card", action_type="price",
            conversation_key=snapshot.conversation_key,
            evidence={**permit, "last_customer_key": "new-card", "order_card_message_key": "new-card"}, updated_at=now)
    except ValueError:
        require(True, "same_verified_quote_cannot_submit_again_with_different_card_or_task")
    else:
        require(False, "same_verified_quote_cannot_submit_again_with_different_card_or_task")
    bundle = args.report.parent / "checkout-evidence.zip"
    export_quality_bundle(args.database_copy, bundle, now=now)
    with zipfile.ZipFile(bundle) as archive:
        require("checkout_intents.jsonl" in archive.namelist(), "quality_export_contains_checkout_commitment_evidence")
        rows = [json.loads(line) for line in archive.read("checkout_intents.jsonl").decode().splitlines()]
        require(all(row["platform_customer_id"] != "offline-buyer" for row in rows), "export_anonymizes_buyer_identity")
    args.report.write_text(json.dumps({"success": True, "checks": checks, "check_count": len(checks),
                                      "actual_order_card_source": str(args.evidence), "browser_started": False,
                                      "llm_started": False, "live_database_modified": False}, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print(json.dumps({"success": True, "check_count": len(checks), "report": str(args.report)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

"""Export the local customer-service corpus into one Sol-friendly JSON file."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def export_customer_service_data(database_path: Path, output_path: Path) -> dict[str, object]:
    """Export products, conversations, messages, and historical examples together."""
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        products = [dict(row) for row in connection.execute(
            """
            SELECT product_key, name, platform_product_id, listed_price, minimum_price,
                   specifications, inventory_notes, shipping_notes, after_sales_notes,
                   supplementary_knowledge, enabled, created_at, updated_at
            FROM cs_products
            ORDER BY product_key
            """
        )]
        aliases = defaultdict(list)
        for row in connection.execute(
            "SELECT product_key, alias FROM cs_product_aliases ORDER BY product_key, alias"
        ):
            aliases[str(row["product_key"])].append(str(row["alias"]))
        for product in products:
            product["aliases"] = aliases.get(str(product["product_key"]), [])

        product_by_platform_id = {
            str(product["platform_product_id"]): str(product["product_key"])
            for product in products
            if product["platform_product_id"]
        }
        messages_by_conversation: dict[str, list[dict[str, object]]] = defaultdict(list)
        for row in connection.execute(
            """
            SELECT conversation_key, message_key, platform_message_id, direction,
                   message_type, text, platform_time, observed_at,
                   content_fingerprint, created_at
            FROM cs_messages
            ORDER BY conversation_key, id
            """
        ):
            messages_by_conversation[str(row["conversation_key"])].append(
                {
                    "message_key": row["message_key"],
                    "platform_message_id": row["platform_message_id"],
                    "direction": row["direction"],
                    "message_type": row["message_type"],
                    "text": row["text"],
                    "platform_time": row["platform_time"],
                    "observed_at": row["observed_at"],
                    "content_fingerprint": row["content_fingerprint"],
                    "created_at": row["created_at"],
                }
            )

        conversations: list[dict[str, object]] = []
        conversation_keys_by_product: dict[str, list[str]] = defaultdict(list)
        for row in connection.execute(
            """
            SELECT conversation_key, display_name, platform_product_id,
                   updated_at, content_expires_at
            FROM cs_conversations
            ORDER BY updated_at, conversation_key
            """
        ):
            conversation_key = str(row["conversation_key"])
            product_key = product_by_platform_id.get(str(row["platform_product_id"]))
            conversation = {
                "conversation_key": conversation_key,
                "display_name": row["display_name"],
                "platform_product_id": row["platform_product_id"],
                "product_key": product_key,
                "updated_at": row["updated_at"],
                "content_expires_at": row["content_expires_at"],
                "messages": messages_by_conversation.get(conversation_key, []),
            }
            conversations.append(conversation)
            if product_key:
                conversation_keys_by_product[product_key].append(conversation_key)

        for product in products:
            product["conversation_keys"] = conversation_keys_by_product.get(
                str(product["product_key"]), []
            )

        examples = [
            dict(row)
            for row in connection.execute(
                """
                SELECT example_id, customer_text, merchant_text, trust_level, created_at
                FROM cs_training_examples
                ORDER BY created_at, example_id
                """
            )
        ]

    payload = {
        "export_version": "customer_service_corpus_v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": {
            "database": str(database_path),
            "privacy": "聊天消息已使用本地隐私脱敏；图片/语音仅保留消息类型和可用文字。",
            "purpose": "交给 Sol 模型做商品、会话、问答和口吻清洗。",
        },
        "statistics": {
            "product_count": len(products),
            "conversation_count": len(conversations),
            "message_count": sum(len(messages) for messages in messages_by_conversation.values()),
            "historical_example_count": len(examples),
            "conversation_without_messages": sum(
                not conversation["messages"] for conversation in conversations
            ),
        },
        "products": products,
        "conversations": conversations,
        "historical_examples": examples,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = export_customer_service_data(args.database, args.output)
    print(json.dumps(payload["statistics"], ensure_ascii=False))


if __name__ == "__main__":
    main()

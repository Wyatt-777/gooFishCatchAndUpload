"""CS-1 tests for safe chat-history parsing and round splitting."""

import json
import zipfile
from pathlib import Path

import pytest

from xianyu_assistant.customer_service.importer import ChatHistoryImporter, ChatImportError
from xianyu_assistant.customer_service.models import MessageDirection


def test_json_rounds_and_csv_newlines_are_parsed_without_physical_line_counting(
    tmp_path: Path,
) -> None:
    json_path = tmp_path / "history.json"
    json_path.write_text(
        json.dumps(
            {
                "conversations": [
                    {
                        "platform_conversation_id": "conversation-1",
                        "messages": [
                            {"message_db_id": 1, "direction": "incoming", "raw_content": "在吗"},
                            {
                                "message_db_id": 2,
                                "direction": "incoming",
                                "raw_content": "可以发货吗？",
                            },
                            {
                                "message_db_id": 3,
                                "direction": "outgoing",
                                "raw_content": "可以，今天发。",
                            },
                        ],
                    },
                    {
                        "platform_conversation_id": "conversation-2",
                        "messages": [
                            {"message_db_id": 4, "direction": "incoming", "raw_content": "只有问题没有回复"}
                        ],
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    preview = ChatHistoryImporter().preview(json_path)

    assert preview.conversation_count == 2
    assert preview.message_count == 4
    assert preview.customer_message_count == 3
    assert preview.merchant_message_count == 1
    assert preview.customer_turn_count == 2
    assert preview.answered_turn_count == 1
    assert preview.example_count == 1

    csv_path = tmp_path / "history.csv"
    csv_path.write_text(
        "conversation_key,direction,text\n"
        'c1,incoming,"第一行\n第二行"\n'
        'c1,outgoing,"已收到"\n',
        encoding="utf-8",
    )
    bundle = ChatHistoryImporter().parse(csv_path)

    assert bundle.preview.message_count == 2
    assert bundle.conversations[0].messages[0].text == "第一行\n第二行"
    assert bundle.conversations[0].messages[0].direction is MessageDirection.INCOMING


def test_zip_is_read_without_extraction_and_rejects_path_traversal(tmp_path: Path) -> None:
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../escape.json", "{}")

    with pytest.raises(ChatImportError, match="不安全的路径"):
        ChatHistoryImporter().parse(archive_path)


def test_zip_prefers_json_when_both_supported_files_are_present(tmp_path: Path) -> None:
    archive_path = tmp_path / "history.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("export.csv", "conversation_key,direction,text\nc1,incoming,错误来源\n")
        archive.writestr(
            "export.json",
            json.dumps(
                {
                    "conversations": [
                        {
                            "conversation_key": "json-source",
                            "messages": [{"direction": "incoming", "text": "JSON 来源"}],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
        )

    bundle = ChatHistoryImporter().parse(archive_path)

    assert bundle.conversations[0].conversation_key == "json-source"

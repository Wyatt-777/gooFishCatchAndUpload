"""CS-1 tests for local history ranking."""

from xianyu_assistant.customer_service.models import HistoricalExample
from xianyu_assistant.customer_service.retrieval import HistoricalExampleRetriever


def test_retrieval_ranks_similar_chinese_questions_and_excludes_auto_replies() -> None:
    retriever = HistoricalExampleRetriever(
        [
            HistoricalExample("shipping", "什么时候发货", "今天可以发出", "imported_history"),
            HistoricalExample("price", "最低多少钱", "最低八百元", "human_confirmed"),
            HistoricalExample("auto", "什么时候发货", "自动生成的错误答案", "auto_generated"),
        ]
    )

    results = retriever.search("请问什么时候能发货", limit=5)

    assert results
    assert results[0].example.example_id == "shipping"
    assert all(result.example.trust_level != "auto_generated" for result in results)


def test_retrieval_limit_and_empty_query_are_bounded() -> None:
    retriever = HistoricalExampleRetriever(
        [HistoricalExample("one", "有现货吗", "有的", "imported_history")]
    )

    assert retriever.search("", limit=5) == []
    assert retriever.search("有现货吗", limit=1)[0].example.example_id == "one"
    assert retriever.search("有现货吗", limit=0) == []


def test_cleaned_history_replaces_duplicate_raw_reply() -> None:
    retriever = HistoricalExampleRetriever(
        [
            HistoricalExample("raw", "旧问题", "可以正常使用", "imported_history"),
            HistoricalExample("cleaned", "清洗后的问题", "可以正常使用", "cleaned_history"),
        ]
    )

    results = retriever.search("清洗后的问题", limit=5)

    assert [result.example.example_id for result in results] == ["cleaned"]

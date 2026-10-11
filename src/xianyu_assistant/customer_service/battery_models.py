"""One model-token grammar shared by product, semantic and reply routing code."""
from __future__ import annotations

import re

from xianyu_assistant.customer_service.fingerprints import normalize_for_matching

MODEL_RE = re.compile(
    # Keep four capture groups: existing fact and semantic readers share them.
    # Missing Ah is allowed only after a voltage unit; a following typed value
    # (30W/30元/30公里) is not a capacity. Never accept a prefix of 30.5/60300.
    r"(?<![a-z\d.])(\d{2,3})\s*(?:v|伏)\s*[-/~～—–·*x×]?\s*(\d{1,3})"
    r"(?:\s*(?:ah|a|安时|安)(?![a-z\d.])"
    r"|(?![\d.]|\s*(?:[a-z]|元|块|公里|千米|瓦|组|套|只|个|年|月|日)))"
    r"|(?<![a-z\d.])(48|60|72)\s*[-/~～—–·*x×\s]?\s*"
    r"(20|24|30|40|50|60)(?:\s*(?:ah|a|安时|安)(?![a-z\d.])"
    r"|(?![\d.]|\s*(?:[a-z]|元|块|公里|千米|瓦|组|套|只|个|年|月|日)))",
    re.IGNORECASE,
)


def model_text(text: str) -> str:
    # Currency/date tokens must never become a battery selection.
    folded = normalize_for_matching(text)
    folded = re.sub(r"[¥￥]?\s*\d+(?:\.\d+)?\s*(?:元|块钱|块)|[¥￥]\s*\d+(?:\.\d+)?", lambda match: " " * len(match[0]), folded)
    return re.sub(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", lambda match: " " * len(match[0]), folded)


def model_matches(text: str) -> tuple[re.Match[str], ...]:
    return tuple(MODEL_RE.finditer(model_text(text)))


def model_selections(text: str) -> tuple[tuple[str, str], ...]:
    result = []
    for match in model_matches(text):
        voltage, capacity = match[1] or match[3], match[2] or match[4]
        selection = (voltage + capacity, f"{voltage}V{capacity}Ah")
        if selection not in result:
            result.append(selection)
    return tuple(result)

"""Operator-confirmed battery chemistry for the three current tower products."""

from __future__ import annotations

import re

CURRENT_TOWER_PRODUCT_KEYS = (
    "current-tieta-60v30ah",
    "current-tieta-60v20ah",
    "current-tieta-48v30ah",
)
BATTERY_CHEMISTRY_LINE = "电池类型：磷酸铁锂（LiFePO4）"
BATTERY_CHEMISTRY_MIGRATION_SETTING = "current_tower_battery_chemistry_backfill_v1"

_CHEMISTRY_RE = re.compile(
    r"磷酸\s*铁\s*[锂鋰]|\blifepo4\b|\blfp\b|[铅鉛]酸|三元|钛酸锂|锰酸锂|"
    r"钴酸锂|镍氢|镍镉|钠离子|钠电",
    re.IGNORECASE,
)
_LFP_RE = re.compile(r"磷酸\s*铁\s*[锂鋰]|\blifepo4\b|\blfp\b", re.IGNORECASE)
_NEGATION_RE = re.compile(r"(?:不是|并非|非|不属于|不含|不采用)\s*(?:的|款|型)?\s*$")


def battery_chemistry_status(specifications: str, supplementary_knowledge: str) -> str:
    """Classify facts conservatively; conflicting user text must never be rewritten.

    A mention of a different chemistry without an explicit negation needs review,
    even if it might describe a compatible vehicle rather than the battery itself.
    """
    known = False
    for field in (specifications, supplementary_knowledge):
        for match in _CHEMISTRY_RE.finditer(field):
            is_lfp = _LFP_RE.fullmatch(match.group()) is not None
            negated = _NEGATION_RE.search(field[max(0, match.start() - 10):match.start()])
            if (is_lfp and negated) or (not is_lfp and not negated):
                return "conflict"
            if is_lfp and not negated:
                known = True
    return "known" if known else "missing"

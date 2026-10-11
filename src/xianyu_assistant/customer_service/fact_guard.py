"""Product assertions checked against current operator data, never chat history.

This is deliberately a bounded recognizer, not a language-model verifier. An
unresolved critical question is routed to clarification/human review. Original
operator text is retained alongside every extracted value for audit.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from xianyu_assistant.customer_service.models import ProductKnowledge

GUARD_VERSION = "product-facts-v1"
_NUMBER = r"\d+(?:\.\d+)?"
_CN_NUMBERS = {"一": "1", "二": "2", "两": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9", "十": "10"}
_CHEMISTRY = re.compile(r"磷酸\s*铁\s*[锂鋰]|铁锂|lifepo4|\blfp\b|[铅鉛]酸|三元(?:锂)?|钛酸锂|锰酸锂|钴酸锂|镍氢|镍镉|钠离子|钠电", re.IGNORECASE)
_NEGATION = re.compile(r"(?:不是|并非|非|不属于|不含|不用|不采用|不带|不送)\s*(?:的|款|型)?\s*$")
from xianyu_assistant.customer_service.battery_models import MODEL_RE as _MODEL
from xianyu_assistant.customer_service.battery_models import model_matches, model_text

_DIMENSIONS = re.compile(rf"尺寸\s*[:：]?\s*({_NUMBER})\s*[-×x*]\s*({_NUMBER})\s*[-×x*]\s*({_NUMBER})\s*(厘米|cm|毫米|mm|米|m)?", re.IGNORECASE)
_CAPACITY = re.compile(rf"(?:(实测|实际|标称|额定)\s*)?容量\s*[:：为是有]?\s*({_NUMBER})\s*(ah|a|安时|安|毫安时|mah)?", re.IGNORECASE)
_HEALTH = re.compile(rf"健康(?:度)?\s*[:：为是]?\s*({_NUMBER})\s*(?:%|％)?")
_RANGE = re.compile(rf"(?:续航(?:预估)?|能跑|可以跑|跑)\s*[:：约大概估计是为]*\s*({_NUMBER})(?:\s*[-~至到]\s*({_NUMBER}))?\s*(?:公里|千米|km)", re.IGNORECASE)
_WARRANTY = re.compile(rf"(?:质保|保修)\s*[:：为是有期]*\s*({_NUMBER}|[一二两三四五六七八九十])\s*(年|个月|月)")
_CYCLES = re.compile(rf"循环(?:次数|数)?\s*[:：约大概基本都在是为有 ]*\s*({_NUMBER})\s*次")
_CHARGE = re.compile(rf"(?:充电|充满)(?:时间|时长|需要|要|约|正常)?\s*[:：约大概需要要为是 ]*({_NUMBER})(?:\s*[-~至到]\s*({_NUMBER}))?\s*(?:小时|h)", re.IGNORECASE)
_LIFETIME = re.compile(rf"(?:寿命|能用|可以用|使用寿命)\s*[:：约大概为是 ]*({_NUMBER}|[一二两三四五六七八九十])\s*年")
_QUESTIONS = (
    ("chemistry", re.compile(r"磷酸|铅酸|三元|铁锂|电池类型|什么电池|哪种电池|什么材质|化学体系|锂电池|锂电")),
    ("dimensions", re.compile(r"尺寸|长宽高|多大|多高|多宽|多长")),
    ("capacity", re.compile(r"容量|多少安|几安|安时")),
    ("health", re.compile(r"健康")),
    ("range", re.compile(r"续航|能跑|跑多远|多少公里")),
    ("warranty", re.compile(r"质保|保修")),
    ("cycles", re.compile(r"循环|充放电次数")),
    ("charging", re.compile(r"充多久|多久充满|充满.*多久|充电.*小时")),
    ("lifetime", re.compile(r"寿命|能用几年|可以用几年")),
    ("condition", re.compile(r"全新|二手|翻新|原装|拆机")),
)
_LABELS = {"chemistry": "电池类型", "dimensions": "尺寸", "capacity": "实测容量", "nominal_capacity": "标称容量", "health": "健康度", "range": "续航", "warranty": "质保", "cycles": "循环次数", "charging": "充电时长", "lifetime": "使用寿命", "condition": "成色/来源"}


def _num(value: str) -> str:
    value = _CN_NUMBERS.get(value, value)
    return format(float(value), "g")


def _chem(value: str) -> str:
    folded = value.lower().replace(" ", "").replace("鉛", "铅").replace("鋰", "锂")
    return "磷酸铁锂" if folded in {"lifepo4", "lfp", "磷酸铁锂", "铁锂"} else folded.removesuffix("锂") if folded.startswith("三元") else folded


def _other_subject(text: str) -> bool:
    # Only explicit vehicle subjects are exempted. "以前回复是铅酸" is not evidence.
    return bool(re.search(r"(?:你(?:的)?原车|原车如果|原车要是|原来的电池|顾客原车|车上原有)", text)) and not bool(re.search(r"我们卖|我卖|这款电池|本商品|我这边的电池|我们的电池", text))


@dataclass(frozen=True)
class FactValue:
    value: tuple[str, ...]
    unit: str
    evidence: str
    qualifier: str = ""


@dataclass(frozen=True)
class ProductFacts:
    product_key: str
    version: str
    values: dict[str, FactValue]
    conflicts: tuple[str, ...]

    def as_data(self) -> dict[str, object]:
        return {"guard_version": GUARD_VERSION, "product_key": self.product_key, "version": self.version,
                "values": {key: asdict(value) for key, value in self.values.items()},
                "conflicts": list(self.conflicts)}


def product_version(product: ProductKnowledge) -> str:
    # All operator fields participate, including aliases/binding and operational facts.
    return hashlib.sha256(json.dumps(asdict(product), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def product_facts(product: ProductKnowledge) -> ProductFacts:
    values: dict[str, FactValue] = {}
    conflicts: set[str] = set()
    denied_chemistries: set[str] = set()

    def add(key: str, value: tuple[str, ...], unit: str, evidence: str) -> None:
        qualifier = "以上" if "以上" in evidence else "约" if re.search(r"左右|预估|约|大概", evidence) else ""
        fact = FactValue(value, unit, evidence.strip(), qualifier)
        if key in values and (values[key].value, values[key].unit) != (value, unit):
            conflicts.add(key)
        else:
            values[key] = fact

    source = f"{product.name}\n{product.specifications}\n{product.supplementary_knowledge}\n{product.after_sales_notes}"
    for line in re.split(r"[\n。；;]", source):
        if not line.strip() or _other_subject(line):
            continue
        for m in _CHEMISTRY.finditer(line):
            if not _NEGATION.search(line[:m.start()]):
                add("chemistry", (_chem(m.group()),), "", line)
            else:
                denied_chemistries.add(_chem(m.group()))
        for m in _DIMENSIONS.finditer(line):
            unit = {"厘米": "cm", "毫米": "mm", "米": "m"}.get((m[4] or "").lower(), (m[4] or "").lower())
            add("dimensions", tuple(_num(m[i]) for i in (1, 2, 3)), unit, line)
        for m in _CAPACITY.finditer(line):
            kind = "nominal_capacity" if m[1] in {"标称", "额定"} else "capacity"
            add(kind, (_num(m[2]),), "Ah" if (m[3] or "").lower() in {"ah", "a", "安", "安时"} else (m[3] or ""), line)
        for key, pattern in (("health", _HEALTH), ("range", _RANGE), ("warranty", _WARRANTY), ("cycles", _CYCLES), ("charging", _CHARGE), ("lifetime", _LIFETIME)):
            for m in pattern.finditer(line):
                if _NEGATION.search(line[:m.start()]):
                    continue
                val = (_num(m[1]),)
                unit = {"health": "%", "range": "km", "cycles": "次", "charging": "小时", "lifetime": "年"}.get(key, "")
                if key in {"range", "charging"} and m[2]:
                    val += (_num(m[2]),)
                if key == "warranty":
                    unit = "年" if m[2] == "年" else "月"
                add(key, val, unit, line)
        for word in ("全新", "二手", "翻新", "拆机", "原装"):
            if word in line and not (word == "原装" and "原装车" in line) and not re.search(rf"(?:不是|非|不属于){word}", line):
                add("condition_" + word, (word,), "", line)
    model = _MODEL.search(product.name + "\n" + product.specifications)
    if model:
        add("voltage", (_num(model[1] or model[3]),), "V", model.group())
        add("nominal_capacity", (_num(model[2] or model[4]),), "Ah", model.group())
    if "condition_全新" in values and any(key in values for key in ("condition_二手", "condition_翻新", "condition_拆机")):
        conflicts.add("condition")
    if "chemistry" in values and values["chemistry"].value[0] in denied_chemistries:
        conflicts.add("chemistry")
    for key, raw in (("shipping_policy", product.shipping_notes), ("after_sales_policy", product.after_sales_notes), ("inventory_note", product.inventory_notes)):
        if raw.strip():
            add(key, (raw.strip(),), "", raw)
    if re.search(r"(?:赠送|送|带)充电器", product.supplementary_knowledge + product.specifications):
        add("charger", ("赠送充电器",), "", product.supplementary_knowledge)
    if re.search(r"蓝牙.{0,15}选装|选装.{0,15}蓝牙", product.supplementary_knowledge):
        add("bluetooth", ("选装",), "", product.supplementary_knowledge)
    return ProductFacts(product.product_key, product_version(product), values, tuple(sorted(conflicts)))


@dataclass(frozen=True)
class ProductMatch:
    products: tuple[ProductKnowledge, ...]
    source: str
    reason: str = ""

    def as_data(self) -> dict[str, object]:
        return {"source": self.source, "reason": self.reason,
                "products": [product_facts(product).as_data() for product in self.products]}


def explicit_products(text: str, products: Sequence[ProductKnowledge]) -> tuple[ProductKnowledge, ...]:
    # Currency numbers must not be reinterpreted as compact battery model aliases.
    text = re.sub(r"(?<!\d)\d+(?:\.\d+)?\s*(?:元|块钱|块)", "", text)
    folded = model_text(text)
    requested = {(m[1] or m[3], m[2] or m[4]) for m in model_matches(folded)}
    found = []
    for product in products:
        aliases = (*product.aliases, product.name)
        alias_match = any(len(alias) >= 3 and re.search(rf"(?<![\w]){re.escape(alias.casefold())}(?![\w])", folded) for alias in aliases)
        # Chinese adjacent words are legitimate; digit boundaries protect 60300.
        alias_match = alias_match or any(len(alias) >= 3 and re.search(rf"(?<!\d){re.escape(alias.casefold())}(?!\d)", folded) for alias in aliases)
        facts = product_facts(product).values
        signature = (facts.get("voltage"), facts.get("nominal_capacity"))
        model_match = all(signature) and (signature[0].value[0], signature[1].value[0]) in requested
        if alias_match or model_match:
            found.append(product)
    return tuple(found)


def resolve_products(products: Sequence[ProductKnowledge], query: str, prior_customer_texts: Sequence[str] = (), *, platform_product_id: str | None = None, title: str | None = None) -> ProductMatch:
    enabled = tuple(product for product in products if product.enabled)
    for source, text in [("current_customer_model", query), *(("recent_customer_model", text) for text in reversed(prior_customer_texts))]:
        matched = explicit_products(text, enabled)
        requested = model_matches(text)
        if requested:
            covered = all(explicit_products(m.group(), enabled) for m in requested)
            if not covered:
                return ProductMatch((), source, "顾客明确提到的型号不在当前商品资料中")
        if matched:
            signatures = {(product_facts(p).values.get("voltage").value if product_facts(p).values.get("voltage") else (),
                           product_facts(p).values.get("nominal_capacity").value if product_facts(p).values.get("nominal_capacity") else ()) for p in matched}
            if len(matched) > len(signatures):
                names = tuple(p for p in matched if p.name in text)
                if len(names) == 1:
                    return ProductMatch(names, source)
                return ProductMatch((), source, "同一型号有多个商品或版本，需要确认具体款式")
            return ProductMatch(matched, source)
    candidates = tuple(product for product in enabled if platform_product_id and product.platform_product_id == platform_product_id)
    if not candidates and title:
        candidates = explicit_products(title, enabled)
    if len(candidates) == 1:
        return ProductMatch(candidates, "platform_binding")
    return ProductMatch((), "unresolved", "商品尚未唯一确定")


def critical_fields(query: str) -> tuple[str, ...]:
    fields = [key for key, pattern in _QUESTIONS if pattern.search(query)]
    if "capacity" in fields and re.search(r"标称|额定", query):
        if not re.search(r"实测|实际", query):
            fields.remove("capacity")
        fields.append("nominal_capacity")
    return tuple(fields)


def is_fact_correction(query: str) -> bool:
    return bool(critical_fields(query) and re.search(r"介绍|页面|详情|明明|说错|不对|不是|刚才|你说|写的|写着", query))


@dataclass(frozen=True)
class FactAnswer:
    text: str
    handoff: bool = False
    clarification: bool = False
    reason: str = ""


def critical_answer(query: str, match: ProductMatch) -> FactAnswer | None:
    from xianyu_assistant.customer_service.contact_policy import is_order_card

    if is_order_card(query):
        return None
    fields = critical_fields(query)
    if not fields:
        return None
    if re.search(r"原车|原来的电池", query) and re.search(r"换|能用|可以用|能装|适配|兼容", query):
        return FactAnswer("适配我核实下", handoff=True, reason="原车电池类型不足以判断当前商品的车辆/接口适配")
    if not match.products:
        if match.source != "unresolved":
            if "多个商品或版本" in match.reason:
                return FactAnswer("同一型号有不同版本，你问的是哪个版本的电池？",
                                  clarification=True, reason=match.reason)
            return FactAnswer("这个型号的资料我核实下", handoff=True, reason=match.reason)
        return FactAnswer("你问的是哪个型号的电池？", clarification=True, reason=match.reason)
    answers = []
    correction = is_fact_correction(query)
    for product in match.products:
        facts = product_facts(product)
        if facts.conflicts:
            return FactAnswer("这个我核实下", handoff=True, reason="商品资料存在冲突：" + "、".join(facts.conflicts))
        pieces = []
        for key in fields:
            value = facts.values.get(key)
            if key == "condition":
                asked = next((word for word in ("全新", "二手", "翻新", "原装", "拆机") if word in query), None)
                value = facts.values.get("condition_" + (asked or ""))
            if value is None or (key == "dimensions" and not value.unit):
                return FactAnswer("这个我核实下", handoff=True, reason=f"{product.name}未确认{_LABELS[key]}，禁止从聊天推断")
            if key == "chemistry":
                pieces.append("是磷酸铁锂电池" if value.value == ("磷酸铁锂",) else "是" + value.value[0] + "电池")
            elif key == "dimensions":
                pieces.append("尺寸" + "×".join(value.value) + {"cm": "厘米", "mm": "毫米", "m": "米"}[value.unit])
            elif key in {"capacity", "nominal_capacity"}:
                pieces.append(_LABELS[key] + value.value[0] + "Ah" + ("左右" if value.qualifier == "约" else ""))
            elif key == "health":
                pieces.append("健康度" + value.value[0] + "%" + value.qualifier)
            elif key == "range":
                pieces.append(value.evidence)  # Retain the stated vehicle/load/road conditions.
            elif key in {"warranty", "cycles", "charging", "lifetime"}:
                pieces.append(_LABELS[key] + ("约" if value.qualifier == "约" else "") + "-".join(value.value) + value.unit)
            else:
                pieces.append(value.evidence)
        answer = "，".join(pieces)
        answers.append((product.name + "：" if len(match.products) > 1 else "") + answer)
    # Compound price/other-business questions require the ordinary planner too.
    if not correction and re.search(r"多少钱|多钱|价格|包邮|物流|自提|怎么拍|改价", query):
        return None
    return FactAnswer(("我重新核对了当前商品资料，" if correction else "") + "\n".join(answers))


class FactGuardError(ValueError):
    """A critical assertion is unsupported, ambiguous or contradictory."""


def validate_reply(text: str, products: Sequence[ProductKnowledge], *, selected: Sequence[ProductKnowledge] = (), query: str = "") -> dict[str, object]:
    enabled = tuple(product for product in products if product.enabled)
    scope = tuple(selected)
    current_scope = scope
    evidence: list[dict[str, str]] = []
    if re.search(r"^不是\s+[^，,。]*铅酸的?\s*$", text.strip()):
        raise FactGuardError("电池类型的否定对象不明确，必须直接写明已核实的电池类型")

    def check(key: str, val: tuple[str, ...], unit: str, clause: str, *, negated: bool = False) -> None:
        targets = current_scope
        if not targets:
            raise FactGuardError(f"{_LABELS.get(key, key)}未绑定明确商品")
        for product in targets:
            facts = product_facts(product)
            if facts.conflicts:
                raise FactGuardError("商品资料冲突：" + "、".join(facts.conflicts))
            fact = facts.values.get(key)
            if fact is None:
                raise FactGuardError(f"{product.name}未确认{_LABELS.get(key, key)}")
            same = fact.value == val and (not unit or fact.unit == unit)
            if (not negated and not same) or (negated and same):
                raise FactGuardError(f"{product.name}的{_LABELS.get(key, key)}回复与当前资料不符")
            if not negated and fact.qualifier == "约" and not re.search(r"约|左右|大概|预估|预计|估计", clause) and key not in {"dimensions", "chemistry"}:
                raise FactGuardError(f"{_LABELS.get(key, key)}必须保留估计含义")
            if not negated and fact.qualifier == "以上" and "以上" not in clause and key == "health":
                raise FactGuardError("健康度必须保留“以上”限定")
            evidence.append({"product_key": product.product_key, "field": key, "evidence": fact.evidence, "version": facts.version})

    for line in text.splitlines():
        mentioned = explicit_products(line, enabled)
        if mentioned:
            current_scope = mentioned
        elif re.search(r"所有电池|全部电池|全店|各型号|全国", line):
            current_scope = enabled
        elif not current_scope:
            current_scope = enabled  # Shared operational policies must agree across all active rows.
        for clause in re.split(r"[。；;!?！？，,]", line):
            if not clause.strip() or _other_subject(clause):
                continue
            clause = re.sub(r"(\d+(?:\.\d+)?|[一二两三四五六七八九十])\s*(年|个月|月)\s*(质保|保修)", r"\3\1\2", clause)
            clause = re.sub(r"崭新(?:的)?电池|新(?:的)?电池", "全新电池", clause)
            # Clarification/unknown phrases make no assertion.
            if re.fullmatch(r".*(?:核实下|确认下|核对下|哪个型号|什么型号)[？?]?", clause.strip()) and not any(pattern.search(clause) for pattern in (_CAPACITY, _HEALTH, _DIMENSIONS, _WARRANTY, _CYCLES)):
                continue
            for m in _CHEMISTRY.finditer(clause):
                negated = bool(_NEGATION.search(clause[:m.start()]))
                check("chemistry", (_chem(m.group()),), "", clause, negated=negated)
            for m in re.finditer("锂电池|锂电的", clause):
                if any("chemistry" not in product_facts(p).values or "锂" not in product_facts(p).values["chemistry"].value[0] for p in current_scope) or _NEGATION.search(clause[:m.start()]):
                    raise FactGuardError("锂电类别回复与已确认电池类型不符或类型未知")
            for m in _DIMENSIONS.finditer(clause):
                unit = {"厘米": "cm", "毫米": "mm", "米": "m"}.get((m[4] or "").lower(), (m[4] or "").lower())
                if not unit:
                    raise FactGuardError("回复尺寸必须明确单位")
                check("dimensions", tuple(_num(m[i]) for i in (1, 2, 3)), unit, clause)
            for m in _CAPACITY.finditer(clause):
                kind = "nominal_capacity" if m[1] in {"标称", "额定"} else "capacity"
                unit = "Ah" if (m[3] or "").lower() in {"ah", "a", "安", "安时"} else m[3] or ""
                check(kind, (_num(m[2]),), unit, clause)
            # Recognize units even without a label ("电池29安左右", "电池97%").
            typed = [m.span() for pattern in (_MODEL, _CAPACITY, _HEALTH, _DIMENSIONS, _RANGE, _CYCLES, _CHARGE, _WARRANTY, _LIFETIME) for m in pattern.finditer(clause)]
            for m in re.finditer(rf"({_NUMBER})(?:\s*[-~至到]\s*({_NUMBER}))?\s*(ah|安时|安|%|％|公里|千米|km|小时)(?![a-z])", clause, re.IGNORECASE):
                if any(start <= m.start() < end for start, end in typed):
                    continue
                unit = m[3].lower()
                key = {"ah": "capacity", "安时": "capacity", "安": "capacity", "%": "health", "％": "health", "公里": "range", "千米": "range", "km": "range", "小时": "charging"}[unit]
                val = (_num(m[1]),) + ((_num(m[2]),) if m[2] else ())
                check(key, val, {"capacity": "Ah", "health": "%", "range": "km", "charging": "小时"}[key], clause)
                if key == "range" and not all(word in line for word in ("二轮", "单人", "平路")):
                    raise FactGuardError("续航回复缺少适用条件")
            for key, pattern in (("health", _HEALTH), ("range", _RANGE), ("warranty", _WARRANTY), ("cycles", _CYCLES), ("charging", _CHARGE), ("lifetime", _LIFETIME)):
                for m in pattern.finditer(clause):
                    val = (_num(m[1]),)
                    unit = {"health": "%", "range": "km", "cycles": "次", "charging": "小时", "lifetime": "年"}.get(key, "")
                    if key in {"range", "charging"} and m[2]:
                        val += (_num(m[2]),)
                    if key == "warranty":
                        unit = "年" if m[2] == "年" else "月"
                    check(key, val, unit, clause, negated=bool(_NEGATION.search(clause[:m.start()])))
                    if key == "range" and any("二轮" in product_facts(p).values[key].evidence for p in current_scope) and not all(word in line for word in ("二轮", "单人", "平路")):
                        raise FactGuardError("续航回复缺少二轮、单人、平路等适用条件")
            for word in ("全新", "二手", "翻新", "原装", "拆机"):
                for m in re.finditer(word, clause):
                    if _NEGATION.search(clause[:m.start()]):
                        if any("condition_" + word in product_facts(p).values for p in current_scope):
                            raise FactGuardError("成色/来源否定与资料冲突")
                    elif "原装车" not in clause:
                        check("condition_" + word, (word,), "", clause)
            if re.search(r"保证|至少|肯定|一定", clause) and any(marker in clause for marker in ("续航", "公里", "寿命")):
                raise FactGuardError("不得将续航/寿命估计改为保证")
            for promise in ("终身质保", "终身保修", "无理由退", "随时退", "包换新"):
                if promise in clause and any(promise not in p.after_sales_notes for p in current_scope):
                    raise FactGuardError("未确认的售后承诺：" + promise)
            if re.search(r"(?:有货|库存充足|现货充足)", clause) and any(not re.search(r"现货|有货|库存\s*[:：]\s*[1-9]", p.inventory_notes) for p in current_scope):
                raise FactGuardError("在售不等于已确认库存")
            if re.search(r"(?:今天|明天|当天|24小时|两天|三天).{0,4}(?:发货|发出|到货|送达)", clause):
                raise FactGuardError("实时发货/到货时间须人工确认")
            if "全国包邮" in clause and any(re.search(r"不包邮|除外", p.shipping_notes) for p in enabled):
                raise FactGuardError("全国包邮与地区限制冲突")
            for region in ("新疆", "内蒙古", "西藏", "海南"):
                if region not in clause:
                    continue
                if re.search(rf"{region}.{{0,6}}(?:免费|包邮)", clause) and not re.search(r"不包邮|除外", clause) and any(region in p.shipping_notes and "不包邮" in p.shipping_notes for p in enabled):
                    raise FactGuardError(f"{region}包邮与当前发货限制不符")
                if region == "海南" and re.search(r"(?:可以发|能发|可发|发货|发海南|海南也发)", clause) and "不发" not in clause and any("海南不发" in p.shipping_notes for p in enabled):
                    raise FactGuardError("海南发货与当前发货限制不符")
            if "充电器" in clause and re.search(r"赠送|默认送|带充电器|送充电器", clause) and any(not re.search(r"(?:赠送|送|带)充电器", p.supplementary_knowledge + p.specifications) for p in current_scope):
                raise FactGuardError("赠送充电器未在商品资料确认")
            if "蓝牙" in clause and re.search(r"自带|默认带|免费|赠送", clause) and any("选装" in p.supplementary_knowledge for p in current_scope):
                raise FactGuardError("选装蓝牙不得回复为自带或赠送")
    # Bind bare answers such as "29安" or "97%" to the customer's requested attribute.
    bare = re.fullmatch(rf"\s*({_NUMBER})\s*(安|ah|%|％)(左右|以上)?\s*", text, re.IGNORECASE)
    if bare:
        current_scope = scope
        key = "health" if bare[2] in {"%", "％"} else "capacity"
        check(key, (_num(bare[1]),), "%" if key == "health" else "Ah", text)
    requested = critical_fields(query)
    short_number = re.fullmatch(rf"\s*(?:正常|大概|约)?\s*({_NUMBER})(?:\s*[-~至到]\s*({_NUMBER}))?\s*(小时|年|次|公里|安|ah|%|％)\s*(?:左右|以上)?\s*", text, re.IGNORECASE)
    if short_number and not evidence:
        unit_to_key = {"小时": "charging", "年": "warranty" if "warranty" in requested else "lifetime", "次": "cycles", "公里": "range", "安": "capacity", "ah": "capacity", "%": "health", "％": "health"}
        key = unit_to_key[short_number[3].lower()]
        current_scope = scope
        value = (_num(short_number[1]),) + ((_num(short_number[2]),) if short_number[2] else ())
        check(key, value, {"charging": "小时", "warranty": "年", "lifetime": "年", "cycles": "次", "range": "km", "capacity": "Ah", "health": "%"}[key], text)
        if key == "range":
            raise FactGuardError("续航数字必须说明适用条件，不能只回距离")
    if not evidence and critical_fields(query) and re.fullmatch(r"(?:是的?|对的?|不是|不对|有的?)\s*[。！!]?", text.strip()):
        raise FactGuardError("关键属性不能仅用是/不是回答，必须给出经核实的事实")
    return {"guard_version": GUARD_VERSION, "status": "passed", "evidence": evidence}


def validate_fixed_message(repository: object, message: str) -> dict[str, object]:
    listing = getattr(repository, "list_product_knowledge", None)
    if not callable(listing):
        raise FactGuardError("当前商品资料不可用")
    return validate_reply(message, listing(include_disabled=False))

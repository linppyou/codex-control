"""模型价目表与费用估算。

价格来自面板维护的价目表，单位为美元 / 百万 tokens。用量日志里的 input_tokens
包含 cached_input_tokens，所以计费时要把缓存部分拆出来单独按缓存输入单价计算。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Iterable

PER_MILLION = 1_000_000
MAX_NAME = 120
MAX_ALIASES = 20
MAX_RATE = 1_000_000


@dataclass(frozen=True)
class Price:
    name: str
    input_usd: float
    cached_usd: float
    output_usd: float
    aliases: tuple[str, ...] = ()
    id: int | None = None


# 默认价目表：首次创建数据库时写入 model_prices；之后以面板里编辑后的价格为准。
# aliases 用于兼容会话日志里的模型 ID（如 gpt-6.1-sol）。
PRICES: tuple[Price, ...] = (
    # ChatGPT 登录的 auto-review 安全检查免费；不代表 API 自动审查的定价。
    Price("Codex Auto Review (ChatGPT)", 0.0, 0.0, 0.0, ("codex-auto-review",)),
    Price("GPT-6.1 Sol", 2.00, 0.10, 10.00, ("gpt-6.1-sol",)),
    Price("GPT-6.1 Terra", 1.00, 0.05, 5.00, ("gpt-6.1-terra",)),
    Price("GPT-6 Astra", 10.00, 1.00, 50.00, ("gpt-6-astra",)),
    Price("GPT-6 Astra Law*", 10.00, 1.00, 50.00, ("gpt-6-astra-law",)),
    Price("GPT-6 Sol", 2.00, 0.20, 10.00, ("gpt-6-sol",)),
    Price("GPT-6 Luna", 0.10, 0.01, 0.50, ("gpt-6-luna",)),
    Price("GPT-5.6 Sol", 4.00, 0.40, 20.00, ("gpt-5.6-sol",)),
    Price("GPT-5.6 Terra", 2.00, 0.20, 12.00, ("gpt-5.6-terra",)),
    Price("GPT-5.6 Luna", 0.20, 0.02, 1.20, ("gpt-5.6-luna",)),
    Price("GPT-6 Rosalind Research", 15.00, 1.50, 75.00, ("gpt-6-rosalind-research",)),
    Price("GPT-5.5 Rosalind Research", 5.00, 0.50, 25.00, ("gpt-5.5-rosalind-research",)),
    Price("GPT-5.5 Rosalind Discovery", 5.00, 0.50, 25.00, ("gpt-5.5-rosalind-discovery",)),
    Price("Daybreak Blue (GPT-5.6 Sol)", 4.00, 0.40, 20.00, ("daybreak-blue",)),
    # 两档 Daybreak Red 的日志模型 ID 可能不同：通用 daybreak-red 归到 $12.50 档，
    # GPT-6 Cyber（仅限 Codex）按 $20 档匹配。
    Price(
        "Daybreak Red (GPT-5.5 Cyber 和 GPT-5.6 Cyber)", 12.50, 1.25, 75.00,
        ("daybreak-red", "gpt-5.5-cyber", "gpt-5.6-cyber"),
    ),
    Price(
        "Daybreak Red (GPT-6 Cyber, 仅限 Codex)", 20.00, 2.00, 100.00,
        ("gpt-6-cyber", "daybreak-red-codex", "daybreak-red-gpt-6-cyber"),
    ),
    Price("GPT-5.3-Codex", 1.75, 0.175, 14.00, ("gpt-5.3-codex",)),
    Price("GPT-5.2", 1.75, 0.175, 14.00, ("gpt-5.2",)),
)


def model_key(value: str) -> str:
    """把模型名归一化成只含字母数字的键，忽略大小写、空格、连字符与点号。"""
    return "".join(ch for ch in value.casefold() if ch.isalnum())


def estimate(
    price: Price | None, input_tokens: int | None, cached_input_tokens: int | None, output_tokens: int | None
) -> float | None:
    """估算一次或一组用量的费用；价格未知时返回 None。

    input_tokens 已包含 cached_input_tokens，缓存部分按缓存输入单价计费，
    其余输入按普通输入单价计费；输出（含推理输出）按输出单价计费。
    """
    if price is None:
        return None
    total_input = max(int(input_tokens or 0), 0)
    cached = min(max(int(cached_input_tokens or 0), 0), total_input)
    output = max(int(output_tokens or 0), 0)
    billable_input = total_input - cached
    return (
        billable_input * price.input_usd + cached * price.cached_usd + output * price.output_usd
    ) / PER_MILLION


def build_index(prices: Iterable[Price]) -> dict[str, Price]:
    index: dict[str, Price] = {}
    for price in prices:
        for label in (price.name, *price.aliases):
            key = model_key(label)
            if key:
                index.setdefault(key, price)
    return index


class PriceBook:
    """一组可编辑的价格，负责按模型名/别名查价和估算费用。"""

    def __init__(self, prices: Iterable[Price]):
        self.prices = tuple(prices)
        self._index = build_index(self.prices)

    def lookup(self, model: str | None) -> Price | None:
        if not model:
            return None
        return self._index.get(model_key(model))

    def cost(
        self, model: str | None, input_tokens: int | None, cached_input_tokens: int | None, output_tokens: int | None
    ) -> float | None:
        return estimate(self.lookup(model), input_tokens, cached_input_tokens, output_tokens)


DEFAULT_BOOK = PriceBook(PRICES)


def lookup(model: str | None) -> Price | None:
    """按默认价目表查价；生产代码使用从数据库加载的 PriceBook。"""
    return DEFAULT_BOOK.lookup(model)


def cost_usd(
    model: str | None, input_tokens: int | None, cached_input_tokens: int | None, output_tokens: int | None
) -> float | None:
    """按默认价目表估算费用；未知模型返回 None。"""
    return DEFAULT_BOOK.cost(model, input_tokens, cached_input_tokens, output_tokens)


def parse_usd(raw: str) -> float:
    """解析用户输入的价格（美元 / 百万 tokens），拒绝负数、无穷和异常大的值。"""
    text = raw.strip().lstrip("$").strip().replace(",", "")
    if not text:
        raise ValueError("价格不能为空")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError("价格必须是数字") from None
    if not value.is_finite() or value < 0 or value > MAX_RATE:
        raise ValueError(f"价格必须在 0 到 {MAX_RATE} 之间")
    return float(value)


def parse_aliases(raw: str) -> tuple[str, ...]:
    """按逗号或换行拆分别名，去掉空白与重复项。"""
    aliases: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[,，\n\r]+", raw):
        label = part.strip()
        if not label:
            continue
        if len(label) > MAX_NAME:
            raise ValueError(f"别名过长：{label[:20]}…")
        key = model_key(label)
        if not key or key in seen:
            continue
        seen.add(key)
        aliases.append(label)
        if len(aliases) > MAX_ALIASES:
            raise ValueError(f"别名最多 {MAX_ALIASES} 个")
    return tuple(aliases)


def validate_prices(prices: Iterable[Price]) -> str | None:
    """检查模型名与别名是否冲突；可用时返回 None。"""
    owners: dict[str, int] = {}
    for index, price in enumerate(prices):
        if not price.name.strip():
            return "模型名不能为空"
        if not model_key(price.name):
            return f"模型名必须包含字母或数字：{price.name}"
        for label in (price.name, *price.aliases):
            key = model_key(label)
            if not key:
                continue
            previous = owners.setdefault(key, index)
            if previous != index:
                return f"模型名或别名重复：{label}"
    return None


def fmt_usd(value: float | None) -> str:
    """费用显示：大额保留 2 位小数，小额自动增加精度，避免显示成 $0.00。"""
    if value is None:
        return "-"
    if value == 0:
        return "$0.00"
    magnitude = abs(value)
    if magnitude >= 1:
        places = 2
    elif magnitude >= 0.01:
        places = 4
    else:
        places = 6
    amount = Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    if places == 2:
        return f"${amount:,.2f}"
    return f"${amount:.{places}f}"


def fmt_rate(value: float) -> str:
    """价目表单价显示：至少 2 位小数，像 0.175 这样的价格保留第 3 位。"""
    text = f"{value:.4f}".rstrip("0")
    whole, _, frac = text.partition(".")
    if len(frac) < 2:
        frac = (frac + "00")[:2]
    return f"${whole}.{frac}"


def fmt_rate_value(value: float) -> str:
    """编辑框里的单价：不带货币符号，避免用户改价时还要删掉 $。"""
    return fmt_rate(value)[1:]

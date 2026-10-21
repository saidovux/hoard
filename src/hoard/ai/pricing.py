"""Immutable tariff snapshots and Decimal-based estimated-cost calculation."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import AIUsage

MILLION = Decimal(1_000_000)


def decimal_text(value: str | int | Decimal | None, *, field: str) -> str | None:
    """Validate a non-negative decimal and retain an exact SQLite-safe string."""
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} должен быть десятичным числом") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{field} не может быть отрицательным")
    return format(parsed, "f")


@dataclass(frozen=True)
class CostEstimate:
    amount: str | None
    currency: str | None
    status: str
    components: dict[str, str]

    def __post_init__(self) -> None:
        if self.status not in {"complete", "partial", "unknown"}:
            raise ValueError("Invalid cost status")


def estimate_cost(usage: AIUsage | None, tariff: dict[str, Any] | None) -> CostEstimate:
    """Calculate only components supported by both actual usage and a tariff."""
    if usage is None or tariff is None:
        return CostEstimate(None, None, "unknown", {})

    currency = str(tariff.get("currency") or "").upper() or None
    components: dict[str, str] = {}
    expected: list[bool] = []

    def add(name: str, tokens: int | None, rate_name: str) -> None:
        if tokens is None:
            expected.append(False)
            return
        raw_rate = tariff.get(rate_name)
        if raw_rate is None or raw_rate == "":
            expected.append(False)
            return
        rate = Decimal(str(raw_rate))
        amount = Decimal(tokens) * rate / MILLION
        components[name] = format(amount, "f")
        expected.append(True)

    # DeepSeek reports cached and uncached input separately. A total prompt count
    # without that split is insufficient for a two-rate tariff and remains unknown.
    add("input_cache_hit", usage.prompt_cache_hit_tokens, "input_cache_hit_per_million")
    add("input_cache_miss", usage.prompt_cache_miss_tokens, "input_cache_miss_per_million")
    add("output", usage.completion_tokens, "output_per_million")

    if not components:
        return CostEstimate(None, currency, "unknown", {})
    total = sum((Decimal(value) for value in components.values()), Decimal(0))
    status = "complete" if expected and all(expected) else "partial"
    return CostEstimate(format(total, "f"), currency, status, components)

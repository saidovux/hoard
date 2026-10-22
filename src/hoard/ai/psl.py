"""Versioned, explicitly subjective PSL result contract for Telegram photos."""

from __future__ import annotations

from typing import Any

PSL_SCHEMA_VERSION = 2
PSL_STATUSES = {"completed", "insufficient_data", "ineligible", "invalid_result"}

PSL_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "status",
        "presentation",
        "estimated_age",
        "raw_psl_min",
        "raw_psl_max",
        "boosted_psl",
    ],
    "properties": {
        "status": {
            "type": "string",
            "enum": ["completed", "insufficient_data", "ineligible"],
        },
        "presentation": {
            "type": "string",
            "enum": ["female", "male", "unknown"],
        },
        "estimated_age": {"type": ["integer", "null"], "minimum": 0, "maximum": 120},
        "raw_psl_min": {"type": ["number", "null"], "minimum": 0, "maximum": 8},
        "raw_psl_max": {"type": ["number", "null"], "minimum": 0, "maximum": 8},
        "boosted_psl": {"type": ["number", "null"], "minimum": 0, "maximum": 8},
    },
}

DEFAULT_PSL_INSTRUCTIONS = """IMPORTANT: First determine whether the supplied images contain a
suitable real-person photograph. Do not assign PSL scores to animals, drawings, anime, fictional
characters, memes, logos, clearly synthetic avatars or photographs obviously unrelated to the
intended subject. An image alone cannot verify the identity of a Telegram account owner. When the
images are unsuitable or their relevance is uncertain, return an ineligible or insufficient-data
result with null PSL scores. Do not identify people by face or automatically recognize celebrities.

Analyze only the explicitly selected local profile photos.
Return only JSON matching the supplied schema. This is a subjective, non-scientific visual
estimate, not a factual measurement. PSL scores apply only when visual presentation is female,
the person is clearly an adult, and the images are sufficient. If presentation is male or
unknown, use status=ineligible and all three PSL values must be null. If adulthood or image
quality is uncertain, use status=insufficient_data and all three PSL values must be null. Never
score a possible minor. estimated_age is approximate and may be null. For a completed result,
raw_psl_min <= raw_psl_max and every PSL value is between 0 and 8."""


def validate_psl_result(value: Any) -> dict[str, Any]:
    """Apply domain rules beyond JSON Schema and return normalized scalar values."""
    if not isinstance(value, dict):
        raise ValueError("PSL-ответ должен быть JSON-объектом")
    status = str(value.get("status") or "")
    presentation = str(value.get("presentation") or "")
    if status not in PSL_STATUSES - {"invalid_result"}:
        raise ValueError("PSL-ответ содержит неизвестный статус")
    if presentation not in {"female", "male", "unknown"}:
        raise ValueError("PSL-ответ содержит неизвестную презентацию")

    age = value.get("estimated_age")
    invalid_age = isinstance(age, bool) or not isinstance(age, int) or not 0 <= age <= 120
    if age is not None and invalid_age:
        raise ValueError("estimated_age должен быть целым числом 0–120 или null")

    scores: list[float | None] = []
    for name in ("raw_psl_min", "raw_psl_max", "boosted_psl"):
        raw = value.get(name)
        if raw is None:
            scores.append(None)
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"{name} должен быть числом 0–8 или null")
        number = float(raw)
        if not 0 <= number <= 8:
            raise ValueError(f"{name} выходит за диапазон 0–8")
        scores.append(number)
    raw_min, raw_max, boosted = scores

    eligible = presentation == "female" and age is not None and age >= 18
    if status == "completed":
        if not eligible or None in scores:
            raise ValueError("PSL-оценка допустима только для явно взрослой female-презентации")
        if raw_min is not None and raw_max is not None and raw_min > raw_max:
            raise ValueError("raw_psl_min не может быть больше raw_psl_max")
    elif any(score is not None for score in scores):
        raise ValueError("Для insufficient_data/ineligible PSL-значения должны быть null")
    if presentation in {"male", "unknown"} and status != "ineligible":
        raise ValueError("male/unknown presentation должна иметь статус ineligible")
    if age is None and status == "completed":
        raise ValueError("Нельзя оценивать PSL при неопределённом возрасте")
    if age is not None and age < 18 and status != "ineligible":
        raise ValueError("Возможный несовершеннолетний должен иметь статус ineligible")

    return {
        "status": status,
        "presentation": presentation,
        "estimated_age": age,
        "raw_psl_min": raw_min,
        "raw_psl_max": raw_max,
        "boosted_psl": boosted,
    }

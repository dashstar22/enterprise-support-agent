"""Clean equipment identity fields from extracted page text. / 从提取页面文字中清洗设备身份字段。"""

from __future__ import annotations

import re

from app.ocr.models import ExtractedEquipmentFields, ExtractedField, OcrToken

DEVICE_MODEL_PATTERN = re.compile(r"\b[A-Z]{1,4}[- ]\d{2,5}[A-Z]?\b")
FAULT_CODE_PATTERN = re.compile(r"\b(?:ERR(?:OR)?[- ]?)?[A-Z]\d{1,4}\b")
VERSION_PATTERN = re.compile(
    r"(?:firmware|version|固件|版本)\s*[\x3a\uff1a]?\s*v?(\d+(?:\.\d+){1,3})", re.IGNORECASE
)


def extract_equipment_fields(
    text: str, tokens: list[OcrToken], *, confirmation_threshold: float
) -> ExtractedEquipmentFields:
    """Find model, fault code, and version without inventing missing values. / 找出型号、故障码和版本号，不编造缺失字段。"""

    return ExtractedEquipmentFields(
        device_model=_field_for(DEVICE_MODEL_PATTERN, text, tokens, confirmation_threshold),
        fault_code=_field_for(FAULT_CODE_PATTERN, text, tokens, confirmation_threshold),
        firmware_version=_version_field(text, tokens, confirmation_threshold),
    )


def _field_for(
    pattern: re.Pattern[str],
    text: str,
    tokens: list[OcrToken],
    confirmation_threshold: float,
) -> ExtractedField | None:
    match = pattern.search(text.upper())
    if match is None:
        return None
    value = re.sub(r"\s+", "", match.group(0)).upper()
    return _make_field(value, tokens, confirmation_threshold)


def _version_field(
    text: str, tokens: list[OcrToken], confirmation_threshold: float
) -> ExtractedField | None:
    match = VERSION_PATTERN.search(text)
    if match is None:
        return None
    return _make_field(match.group(1), tokens, confirmation_threshold)


def _make_field(
    value: str, tokens: list[OcrToken], confirmation_threshold: float
) -> ExtractedField:
    normalized_value = value.upper()
    matching = [token.confidence for token in tokens if normalized_value in token.text.upper()]
    confidence = min(matching) if matching else 1.0
    return ExtractedField(
        value=normalized_value,
        confidence=confidence,
        requires_confirmation=confidence < confirmation_threshold,
    )

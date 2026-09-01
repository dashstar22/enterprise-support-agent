"""Small, conservative redaction helpers. / 小而保守的脱敏辅助函数。"""

import re
from collections.abc import Mapping

REDACTED = "[REDACTED]"
_SENSITIVE_KEY_PARTS = ("api_key", "apikey", "authorization", "password", "secret", "token")
_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|authorization|password|secret|token)\b\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"
)
_BEARER_PATTERN = re.compile(r"(?i)bearer\s+[^\s,;]+")
_EMAIL_PATTERN = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


def redact_text(value: str) -> str:
    """Remove common credentials and direct contact details. / 删除常见密钥和直接联系方式。"""

    value = _ASSIGNMENT_PATTERN.sub(r"\1=" + REDACTED, value)
    value = _BEARER_PATTERN.sub("Bearer " + REDACTED, value)
    value = _EMAIL_PATTERN.sub(REDACTED, value)
    return _PHONE_PATTERN.sub(REDACTED, value)


def redact_mapping(value: Mapping[str, object]) -> dict[str, object]:
    """Deep-copy a mapping while redacting sensitive key values. / 深拷贝映射并隐藏敏感键的值。"""

    result: dict[str, object] = {}
    for key, item in value.items():
        normalized_key = key.lower().replace("-", "_")
        if any(part in normalized_key for part in _SENSITIVE_KEY_PARTS):
            result[key] = REDACTED
        elif isinstance(item, str):
            result[key] = redact_text(item)
        elif isinstance(item, Mapping):
            result[key] = redact_mapping(item)
        elif isinstance(item, list):
            result[key] = [redact_mapping(x) if isinstance(x, Mapping) else x for x in item]
        else:
            result[key] = item
    return result

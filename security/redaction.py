"""审计、异常和RAG元数据共用的递归脱敏工具。"""

import re
from typing import Any


_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "password",
    "passwd",
    "private_key",
    "secret",
    "signature_secret",
    "token",
}

_VALUE_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
)

_REDACTED = "[REDACTED]"


def redact(value: Any) -> Any:
    """
    递归脱敏任意可序列化对象。

    Args:
        value: 字典、列表、元组、字符串或其他标量。

    Returns:
        不修改原对象的脱敏副本。敏感字段值完全替换，普通字符串中的
        Bearer令牌、sk前缀密钥和私钥块按模式替换。
    """
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized_key = str(key).strip().lower().replace("-", "_")
            if normalized_key in _SENSITIVE_KEYS:
                result[key] = _REDACTED
            else:
                result[key] = redact(item)
        return result
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, str):
        redacted = value
        for pattern in _VALUE_PATTERNS:
            redacted = pattern.sub(_REDACTED, redacted)
        return redacted
    return value


__all__ = ["redact"]

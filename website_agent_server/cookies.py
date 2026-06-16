from __future__ import annotations

import json
from typing import Any


def _escape_non_ascii(value: str) -> str:
    parts: list[str] = []
    for character in value:
        if ord(character) < 128:
            parts.append(character)
        else:
            parts.append(json.dumps(character, ensure_ascii=True)[1:-1])
    return "".join(parts)


def cookie_value_to_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _escape_non_ascii(value)
    if isinstance(value, (dict, list, tuple, bool, int, float)):
        try:
            return json.dumps(
                value,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            pass
    return str(value)

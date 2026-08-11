"""CAS validation rules."""

from __future__ import annotations

import re

CAS_TOKEN = re.compile(r"^\d{2,7}-\d{2}-\d$")


def validate_cas(value: str | None) -> list[str]:
    if not value:
        return []
    if not CAS_TOKEN.match(str(value).strip()):
        return [f"CAS format invalid: {value!r}"]
    return []

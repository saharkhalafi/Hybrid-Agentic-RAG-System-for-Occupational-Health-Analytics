"""CAS Registry Number format + checksum validation (deterministic, no network)."""

from __future__ import annotations

import re

CAS_SHAPE = re.compile(r"^(\d{2,7})-(\d{2})-(\d)$")


def is_valid_cas(cas: str) -> bool:
    """Validate CAS number shape and checksum digit.

    CAS checksum: drop the last (check) digit, reverse the remaining digits,
    multiply each by its 1-based position, sum, and take mod 10.
    """
    m = CAS_SHAPE.match(cas.strip())
    if not m:
        return False
    body = m.group(1) + m.group(2)
    check_digit = int(m.group(3))
    total = 0
    for i, ch in enumerate(reversed(body), start=1):
        total += i * int(ch)
    return total % 10 == check_digit

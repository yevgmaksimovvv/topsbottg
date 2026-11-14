"""Study group code validation shared by bot handlers and services."""

from __future__ import annotations

import re

_GROUP_CODE = re.compile(r"[A-ZА-ЯЁ0-9]+(?:-[A-ZА-ЯЁ0-9]+)*\Z")


def normalize_study_group_code(raw_code: str) -> str:
    """Normalize case and spaces around hyphens, but never transliterate look-alike characters."""
    code = re.sub(r"\s*-\s*", "-", (raw_code or "").strip().upper())
    if not 3 <= len(code) <= 32 or _GROUP_CODE.fullmatch(code) is None:
        raise ValueError("Введите код учебной группы, например М3О-207СВ-25.")
    return code

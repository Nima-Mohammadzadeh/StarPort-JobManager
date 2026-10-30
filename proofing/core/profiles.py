from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

from .settings import TEMPLATE_PROFILES_PATH, load_json, save_json


def load_template_profiles() -> List[Dict[str, Any]]:
    value = load_json(TEMPLATE_PROFILES_PATH, [])
    return value if isinstance(value, list) else []


def save_template_profiles(items: List[Dict[str, Any]]) -> None:
    clean = sorted((deepcopy(x) for x in items if str(x.get("template_path", "")).strip()), key=lambda x: str(x.get("display_name", x.get("template_path", ""))).casefold())
    save_json(TEMPLATE_PROFILES_PATH, clean)


def normalize_template_path(path: str) -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    return str(Path(raw).expanduser().absolute())


def find_template_profile(items: List[Dict[str, Any]], template_path: str) -> Optional[Dict[str, Any]]:
    key = normalize_template_path(template_path).casefold()
    if not key:
        return None
    for item in items:
        if normalize_template_path(str(item.get("template_path", ""))).casefold() == key:
            return item
    return None


def next_revision(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "2"
    if raw.isdigit():
        return str(int(raw) + 1)
    if raw.isalpha():
        chars = list(raw.upper())
        i = len(chars) - 1
        while i >= 0:
            if chars[i] != "Z":
                chars[i] = chr(ord(chars[i]) + 1)
                return "".join(chars)
            chars[i] = "A"
            i -= 1
        return "A" + "".join(chars)
    return raw + "-2"

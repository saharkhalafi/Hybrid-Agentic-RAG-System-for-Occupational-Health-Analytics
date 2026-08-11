"""Cache for LLM OCR repair decisions — avoids repeated token spend for same patterns."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

CACHE_VERSION = 1


def _normalize_key(original: str) -> str:
    return re.sub(r"\s+", " ", (original or "").strip())


class LlmOcrRepairCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, Any] = {"version": CACHE_VERSION, "patterns": {}, "chunks": {}}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("version") == CACHE_VERSION:
                self._data = payload
        except (json.JSONDecodeError, OSError):
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_pattern_repairs(self, original: str) -> list[dict[str, Any]] | None:
        key = _normalize_key(original)
        entry = self._data.get("patterns", {}).get(key)
        if entry and entry.get("repairs") is not None:
            return list(entry["repairs"])
        return None

    def set_pattern_repairs(
        self,
        original: str,
        repairs: list[dict[str, Any]],
        *,
        source: str = "llm",
    ) -> None:
        key = _normalize_key(original)
        self._data.setdefault("patterns", {})[key] = {
            "original": key,
            "repairs": repairs,
            "source": source,
            "hit_count": self._data.get("patterns", {}).get(key, {}).get("hit_count", 0) + 1,
        }

    def get_chunk_result(self, chunk_id: str) -> dict[str, Any] | None:
        return self._data.get("chunks", {}).get(chunk_id)

    def set_chunk_result(self, chunk_id: str, result: dict[str, Any]) -> None:
        self._data.setdefault("chunks", {})[chunk_id] = result

    def suggest_deterministic_rules(self, min_hits: int = 3) -> list[dict[str, Any]]:
        """Patterns accepted multiple times — candidates for deterministic dictionary."""
        suggestions: list[dict[str, Any]] = []
        for key, entry in self._data.get("patterns", {}).items():
            repairs = entry.get("repairs") or []
            hits = entry.get("hit_count", 0)
            if hits < min_hits or not repairs:
                continue
            for repair in repairs:
                if repair.get("validation_status") == "ACCEPT" or repair.get("status") == "ACCEPT":
                    suggestions.append(
                        {
                            "pattern": key,
                            "replacement": repair.get("replacement"),
                            "hit_count": hits,
                            "source": entry.get("source"),
                        }
                    )
        return suggestions

    def export_rule_suggestions(self, out_path: Path, min_hits: int = 3) -> Path:
        suggestions = self.suggest_deterministic_rules(min_hits=min_hits)
        counts = Counter(s["pattern"] for s in suggestions)
        lines = [
            "OCR DETERMINISTIC RULE SUGGESTIONS (from LLM cache)",
            "=" * 60,
            f"{'pattern':<30} {'replacement':<25} hits",
            "-" * 60,
        ]
        seen: set[str] = set()
        for item in suggestions:
            pat = item["pattern"]
            if pat in seen:
                continue
            seen.add(pat)
            lines.append(f"{pat!r:<30} {item['replacement']!r:<25} {counts[pat]}")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n".join(lines), encoding="utf-8")
        return out_path

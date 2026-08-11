"""Schema Registry — versioned table-type definitions for mapping layer."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

REGISTRY_DIR = Path(__file__).resolve().parent


class SchemaRegistry:
    def __init__(self, schemas: dict[str, dict[str, Any]]) -> None:
        self._schemas = schemas

    def get(self, schema_id: str) -> dict[str, Any] | None:
        return self._schemas.get(schema_id)

    def for_table_type(self, table_type: str) -> dict[str, Any] | None:
        for schema in self._schemas.values():
            if schema.get("table_type") == table_type:
                return schema
        return None

    def field_names(self, schema_id: str) -> list[str]:
        schema = self.get(schema_id)
        if not schema:
            return []
        return [f["name"] for f in schema.get("fields", [])]

    def semantic_role(self, schema_id: str, field_name: str) -> str | None:
        schema = self.get(schema_id)
        if not schema:
            return None
        for field in schema.get("fields", []):
            if field["name"] == field_name:
                return field.get("semantic_role")
        return None


@lru_cache(maxsize=1)
def get_schema_registry() -> SchemaRegistry:
    schemas: dict[str, dict[str, Any]] = {}
    for path in REGISTRY_DIR.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        schemas[payload["schema_id"]] = payload
    return SchemaRegistry(schemas)

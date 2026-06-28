"""Convert energy device model JSON to HA instruction set and panel schema."""

from __future__ import annotations

import json
import re
from typing import Any

DEFAULT_CATEGORY = "xnyjcn"

PANEL_DP_TYPES = frozenset({"Boolean", "Integer", "Enum", "String", "hourmin"})
PANEL_EXCLUDED_GROUPS = frozenset({0})

DATA_SPEC_TYPE_MAP: dict[str, str] = {
    "value": "Integer",
    "enum": "Enum",
    "boolean": "Boolean",
    "hourmin": "hourmin",
    "string": "String",
    "date": "String",
}

SEMANTIC_TO_BUCKET: dict[str, str] = {
    "instant": "status",
    "setting": "functions",
}

ENERGY_MODEL_TYPES = frozenset({"instant", "setting", "alarm"})

_WIRE_ENUM_TOKEN = re.compile(r"^[a-z][a-z0-9_]*$")


def _is_wire_enum_token(value: str) -> bool:
    """Return whether a range_info name looks like a wire enum value."""
    if not value:
        return False
    if " " in value or "-" in value:
        return False
    return bool(_WIRE_ENUM_TOKEN.match(value))


def _to_int(value: Any) -> int:
    """Convert numeric spec field to int for values JSON."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return int(float(str(value)))


def is_energy_model_entry(entry: Any) -> bool:
    """Return whether an object is an energy model schema entry."""
    if not isinstance(entry, dict):
        return False
    if "data_spec" in entry:
        return True
    return entry.get("type") in ENERGY_MODEL_TYPES


def extract_energy_model_entries(result: Any) -> list[dict[str, Any]] | None:
    """Extract energy model entries from an API or mock payload."""
    if isinstance(result, list):
        if result and is_energy_model_entry(result[0]):
            return result
        return None

    if not isinstance(result, dict):
        return None

    for key in ("model", "items", "data", "schemas", "specifications"):
        value = result.get(key)
        if isinstance(value, list) and value and is_energy_model_entry(value[0]):
            return value

    return None


def map_data_spec_type(data_spec: dict[str, Any] | None) -> str | None:
    """Map energy model data_spec.type to Tuya HA DP type."""
    if not data_spec:
        return None
    spec_type = data_spec.get("type")
    if not spec_type:
        return None
    return DATA_SPEC_TYPE_MAP.get(str(spec_type))


def build_enum_range(data_spec: dict[str, Any]) -> list[str]:
    """Build enum range for values JSON."""
    range_info = data_spec.get("range_info")
    if isinstance(range_info, list) and range_info:
        result: list[str] = []
        for item in range_info:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", ""))
            code = str(item.get("code", ""))
            if _is_wire_enum_token(name):
                result.append(name)
            elif code:
                result.append(code)
        if result:
            return result
    raw_range = data_spec.get("range")
    if isinstance(raw_range, list):
        return [str(item) for item in raw_range]
    return []


def build_values_payload(dp_type: str, data_spec: dict[str, Any]) -> dict[str, Any]:
    """Build the values object before JSON string serialization."""
    if dp_type in {"Boolean", "hourmin"}:
        return {}
    if dp_type == "Integer":
        payload: dict[str, Any] = {
            "min": _to_int(data_spec.get("min", 0)),
            "max": _to_int(data_spec.get("max", 0)),
            "scale": _to_int(data_spec.get("scale", 0)),
            "step": _to_int(data_spec.get("step", 1)),
        }
        unit = data_spec.get("unit")
        payload["unit"] = "" if unit is None else str(unit)
        return payload
    if dp_type == "Enum":
        return {"range": build_enum_range(data_spec)}
    if dp_type == "String":
        length = data_spec.get("length") or data_spec.get("maxlen")
        if length is not None:
            return {"maxlen": _to_int(length)}
        return {}
    return {}


def build_values_string(dp_type: str, data_spec: dict[str, Any]) -> str:
    """Serialize values field as compact JSON string (Tuya SDK format)."""
    payload = build_values_payload(dp_type, data_spec)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def convert_model_entry(entry: dict[str, Any]) -> dict[str, str] | None:
    """Convert one energy model entry to instruction-set item."""
    semantic_type = entry.get("type")
    if semantic_type not in SEMANTIC_TO_BUCKET:
        return None

    data_spec = entry.get("data_spec")
    if not isinstance(data_spec, dict):
        return None

    dp_type = map_data_spec_type(data_spec)
    if dp_type is None:
        return None

    code = entry.get("code")
    if not code:
        return None

    return {
        "code": str(code),
        "type": dp_type,
        "values": build_values_string(dp_type, data_spec),
    }


def convert_energy_model(
    model_entries: list[dict[str, Any]],
    *,
    category: str = DEFAULT_CATEGORY,
) -> dict[str, Any]:
    """Convert energy model list to report/command instruction set document."""
    result: dict[str, Any] = {
        "category": category,
        "functions": [],
        "status": [],
    }

    for entry in model_entries:
        if not isinstance(entry, dict):
            continue
        converted = convert_model_entry(entry)
        if converted is None:
            continue
        bucket = SEMANTIC_TO_BUCKET[str(entry["type"])]
        result[bucket].append(converted)

    return result


def _dedupe_panel_functions_by_type(
    functions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep one representative item per DP type in a panel group."""
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in functions:
        dp_type = item["type"]
        if dp_type in seen:
            continue
        seen.add(dp_type)
        deduped.append(item)
    return deduped


def convert_panel_functions(
    model_entries: list[dict[str, Any]],
    *,
    category: str = DEFAULT_CATEGORY,
) -> list[dict[str, Any]]:
    """Convert setting entries to flat panel function list."""
    functions: list[dict[str, Any]] = []

    for entry in model_entries:
        if not isinstance(entry, dict) or entry.get("type") != "setting":
            continue
        converted = convert_model_entry(entry)
        if converted is None or converted["type"] not in PANEL_DP_TYPES:
            continue

        group_id = entry.get("group", 0)
        if group_id in PANEL_EXCLUDED_GROUPS:
            continue

        item: dict[str, Any] = dict(converted)
        if name := entry.get("name"):
            item["name"] = str(name)
        functions.append(item)

    functions.sort(key=lambda item: item["code"])
    return _dedupe_panel_functions_by_type(functions)


def parse_energy_specifications_response(
    result: Any,
    *,
    category: str = DEFAULT_CATEGORY,
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """Parse energy specifications API result into instruction set + panel list."""
    model_entries = extract_energy_model_entries(result)
    if model_entries is not None:
        return (
            convert_energy_model(model_entries, category=category),
            convert_panel_functions(model_entries, category=category),
        )

    if not isinstance(result, dict):
        return None

    functions = result.get("functions")
    status = result.get("status")
    if isinstance(functions, list) and isinstance(status, list):
        panel_functions = result.get("groups")
        if panel_functions is None:
            panel_functions = functions
        if isinstance(panel_functions, dict):
            panel_functions = [
                item
                for items in panel_functions.values()
                if isinstance(items, list)
                for item in items
                if isinstance(item, dict) and "code" in item
            ]
        if not isinstance(panel_functions, list):
            panel_functions = []
        return (
            {
                "category": result.get("category") or category,
                "functions": functions,
                "status": status,
            },
            panel_functions,
        )

    return None

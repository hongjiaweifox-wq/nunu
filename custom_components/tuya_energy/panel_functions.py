"""Dynamic grouped panel functions for Tuya devices."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tuya_sharing import CustomerDevice, Manager
from tuya_sharing.device import DeviceFunction, DeviceStatusRange
from tuya_sharing.exceptions import ApiRequestException

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import LOGGER
from .energy_model_converter import parse_energy_specifications_response
from .hourmin import hourmin_to_time

DYNAMIC_PANEL_CATEGORIES = frozenset({"xnyjcn"})
PANEL_FUNCTIONS_MOCK_DIR = "mock"
PANEL_FUNCTIONS_MOCK_FILE = "tuya_panel_functions.json"
PANEL_ENTITY_WHITELIST_DIR = "filter"
PANEL_ENTITY_WHITELIST_FILE = "code.json"
USE_MOCK_PANEL_FUNCTIONS = True
SPECIFICATIONS_API_PATH = "/v1.0/m/life/ha/{device_id}/energy/specifications"
COMMANDS_API_PATH = "/v1.0/m/life/ha/{device_id}/energy/commands"

TYPE_PLATFORM_MAP = {
    "Boolean": "switch",
    "Integer": "number",
    "Enum": "select",
    "String": "text",
    "hourmin": "time",
}


def format_group_label(group_id: str) -> str:
    """Return a human-readable group label."""
    if group_id.startswith("group") and group_id[5:].isdigit():
        return f"Group {group_id[5:]}"
    if group_id.isdigit():
        return f"Group {group_id}"
    return group_id.replace("_", " ").title()


def format_function_label(function) -> str:
    """Return a human-readable function label."""
    if name := getattr(function, "name", None):
        return name
    return function.code.replace("_", " ").title()


def parse_function_spec(function) -> dict[str, Any]:
    """Parse a DeviceFunction values JSON string into a dict."""
    if not function.values:
        return {}
    try:
        parsed = json.loads(function.values)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def is_value_filled(value_type: str, value: Any) -> bool:
    """Return whether a panel value is considered non-empty for group submit."""
    if value_type == "Boolean":
        return value is not None
    if value_type == "Integer":
        return value is not None and value != ""
    if value_type == "Enum":
        return value is not None and value != ""
    if value_type == "String":
        return value is not None and str(value).strip() != ""
    if value_type == "hourmin":
        return hourmin_to_time(value) is not None
    return value is not None


def build_panel_state(hass: HomeAssistant, device: CustomerDevice) -> dict[str, Any]:
    """Serialize grouped panel functions and current values for the frontend."""
    from .panel_entity_discovery import resolve_entity_id_for_function

    groups: dict[str, Any] = {}
    for group_id, functions in sorted(
        getattr(device, "function_groups", {}).items()
    ):
        items: list[dict[str, Any]] = []
        for function in functions:
            if function.type not in TYPE_PLATFORM_MAP:
                continue
            items.append(
                {
                    "code": function.code,
                    "type": function.type,
                    "label": format_function_label(function),
                    "value": device.status.get(function.code),
                    "spec": parse_function_spec(function),
                    "entity_id": resolve_entity_id_for_function(
                        hass, device, function
                    ),
                }
            )
        groups[group_id] = {
            "id": group_id,
            "label": format_group_label(group_id),
            "functions": items,
        }
    return {
        "tuya_device_id": device.id,
        "name": device.name,
        "online": device.online,
        "category": device.category,
        "groups": groups,
    }


def normalize_function_groups_payload(
    grouped_functions: Any,
) -> dict[str, list[dict[str, Any]]] | None:
    """Normalize function-groups payload to {group_id: [functions]}.

    Supports a flat list (wrapped as group ``1``) or a grouped dict.
    """
    if isinstance(grouped_functions, list):
        functions = [
            item for item in grouped_functions if isinstance(item, dict) and "code" in item
        ]
        if not functions:
            return None
        return {"1": functions}

    if isinstance(grouped_functions, dict):
        normalized: dict[str, list[dict[str, Any]]] = {}
        for group_id, functions in grouped_functions.items():
            if not isinstance(functions, list):
                continue
            items = [
                item for item in functions if isinstance(item, dict) and "code" in item
            ]
            if items:
                normalized[str(group_id)] = items
        return normalized or None

    return None


def apply_specifications(
    device: CustomerDevice, specifications: dict[str, Any]
) -> None:
    """Merge instruction-set specifications into device.function and status_range."""
    for function in specifications.get("functions", []):
        if not isinstance(function, dict) or "code" not in function:
            continue
        code = function["code"]
        existing = device.function.get(code)
        if existing is not None:
            merged = DeviceFunction(**{**existing.__dict__, **function})
        else:
            merged = DeviceFunction(**function)
        device.function[code] = merged

    for status in specifications.get("status", []):
        if not isinstance(status, dict) or "code" not in status:
            continue
        code = status["code"]
        existing = device.status_range.get(code)
        if existing is not None:
            merged = DeviceStatusRange(**{**existing.__dict__, **status})
        else:
            merged = DeviceStatusRange(**status)
        device.status_range[code] = merged


def _apply_function_groups(
    device: CustomerDevice, grouped_functions: dict[str, Any]
) -> None:
    """Apply grouped function definitions to a device."""
    function_groups: dict[str, list[DeviceFunction]] = {}
    for group_id, functions in grouped_functions.items():
        if not isinstance(functions, list):
            continue
        group_list: list[DeviceFunction] = []
        for function in functions:
            if not isinstance(function, dict) or "code" not in function:
                continue
            code = function["code"]
            existing = device.function.get(code)
            if existing is not None:
                merged = DeviceFunction(**{**existing.__dict__, **function})
            else:
                merged = DeviceFunction(**function)

            device.function[code] = merged
            if code not in device.status_range:
                device.status_range[code] = DeviceStatusRange(
                    code=code,
                    type=merged.type,
                    values=merged.values or "{}",
                )
            group_list.append(merged)
        if group_list:
            function_groups[group_id] = group_list
    device.function_groups = function_groups


def apply_panel_status_entities(
    device: CustomerDevice, status_entities: list[dict[str, Any]]
) -> None:
    """Merge read-only status schema entries into device.status_range only."""
    for entry in status_entities:
        code = str(entry["code"])
        dp_type = str(entry["type"])
        values = entry.get("values", "{}")

        existing_status = device.status_range.get(code)
        status_payload = {"code": code, "type": dp_type, "values": values}
        if existing_status is not None:
            merged_status = DeviceStatusRange(
                **{**existing_status.__dict__, **status_payload}
            )
        else:
            merged_status = DeviceStatusRange(**status_payload)
        device.status_range[code] = merged_status


def normalize_status_entities(raw: Any) -> list[dict[str, Any]]:
    """Normalize status entity schema entries from API or mock payload."""
    if not isinstance(raw, list):
        return []
    return [
        item
        for item in raw
        if isinstance(item, dict) and item.get("code") and item.get("type")
    ]


def set_device_panel_status_entities(
    device: CustomerDevice, status_entities: list[dict[str, Any]]
) -> None:
    """Store and apply read-only status entity schema on a device."""
    normalized = normalize_status_entities(status_entities)
    device.panel_status_entities = normalized
    if normalized:
        apply_panel_status_entities(device, normalized)


def _get_mock_panel_functions_path() -> Path:
    """Return bundled panel functions mock path."""
    return Path(__file__).parent / PANEL_FUNCTIONS_MOCK_DIR / PANEL_FUNCTIONS_MOCK_FILE


def _get_panel_entity_whitelist_path() -> Path:
    """Return bundled panel entity whitelist path."""
    return Path(__file__).parent / PANEL_ENTITY_WHITELIST_DIR / PANEL_ENTITY_WHITELIST_FILE


_PANEL_ENTITY_WHITELIST: frozenset[str] | None = None


def load_panel_entity_whitelist() -> frozenset[str]:
    """Load allowed panel entity codes from filter/code.json."""
    whitelist_path = _get_panel_entity_whitelist_path()
    if not whitelist_path.is_file():
        LOGGER.warning(
            "Panel entity whitelist file missing: %s", whitelist_path
        )
        return frozenset()

    try:
        data = json.loads(whitelist_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        LOGGER.warning(
            "Failed to read panel entity whitelist from %s: %s",
            whitelist_path,
            err,
        )
        return frozenset()

    if not isinstance(data, list):
        LOGGER.warning(
            "Invalid panel entity whitelist format in %s: expected JSON array",
            whitelist_path,
        )
        return frozenset()

    return frozenset(str(code) for code in data if code)


def get_panel_entity_whitelist_codes() -> frozenset[str]:
    """Return cached panel entity whitelist codes."""
    global _PANEL_ENTITY_WHITELIST
    if _PANEL_ENTITY_WHITELIST is None:
        _PANEL_ENTITY_WHITELIST = load_panel_entity_whitelist()
        LOGGER.debug(
            "Panel entity whitelist loaded: %s",
            sorted(_PANEL_ENTITY_WHITELIST),
        )
    return _PANEL_ENTITY_WHITELIST


def attach_panel_entity_whitelist(device: CustomerDevice) -> frozenset[str]:
    """Attach panel entity whitelist to a device."""
    whitelist = get_panel_entity_whitelist_codes()
    device.panel_entity_whitelist = whitelist
    return whitelist


def filter_grouped_functions_by_whitelist(
    grouped_functions: dict[str, list[dict[str, Any]]] | None,
    whitelist: frozenset[str],
) -> dict[str, list[dict[str, Any]]] | None:
    """Keep only function groups whose codes are in the whitelist."""
    if not grouped_functions or not whitelist:
        return None

    filtered: dict[str, list[dict[str, Any]]] = {}
    for group_id, functions in grouped_functions.items():
        items = [
            function
            for function in functions
            if isinstance(function, dict)
            and str(function.get("code", "")) in whitelist
        ]
        if items:
            filtered[str(group_id)] = items
    return filtered or None


def filter_status_entities_by_whitelist(
    status_entities: list[dict[str, Any]],
    whitelist: frozenset[str],
) -> list[dict[str, Any]]:
    """Keep only status schema entries whose codes are in the whitelist."""
    if not whitelist:
        return []
    return [
        entry
        for entry in status_entities
        if isinstance(entry, dict) and str(entry.get("code", "")) in whitelist
    ]


def filter_spec_doc_by_whitelist(
    spec_doc: dict[str, Any], whitelist: frozenset[str]
) -> dict[str, Any]:
    """Filter writable/read-only specification entries by whitelist."""
    filtered = dict(spec_doc)
    functions = filtered.get("functions")
    if isinstance(functions, list):
        filtered["functions"] = [
            function
            for function in functions
            if isinstance(function, dict)
            and str(function.get("code", "")) in whitelist
        ]
    filtered["status"] = filter_status_entities_by_whitelist(
        filtered.get("status") or [], whitelist
    )
    return filtered


def _read_mock_panel_file(
    mock_path: Path, category: str
) -> dict[str, Any] | None:
    """Read panel mock schema from local file (executor-safe)."""
    if not mock_path.is_file():
        return None

    try:
        data = json.loads(mock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    if data.get("category") and data["category"] != category:
        return None

    return {
        "functions": normalize_function_groups_payload(data.get("functions")),
        "status": normalize_status_entities(data.get("status")),
    }


async def _load_mock_panel_schema(
    hass: HomeAssistant, device: CustomerDevice
) -> dict[str, Any] | None:
    """Load panel function groups and status entities from local mock file."""
    mock_path = _get_mock_panel_functions_path()
    mock_schema = await hass.async_add_executor_job(
        _read_mock_panel_file, mock_path, device.category
    )
    if mock_schema is None:
        LOGGER.debug(
            "No panel mock for device %s (file missing or invalid: %s)",
            device.id,
            mock_path,
        )
    return mock_schema


def _flatten_grouped_functions(
    grouped_functions: dict[str, list[dict[str, Any]]] | None,
) -> list[dict[str, Any]]:
    """Flatten grouped panel functions into a single list."""
    if not grouped_functions:
        return []
    flat: list[dict[str, Any]] = []
    for functions in grouped_functions.values():
        flat.extend(functions)
    return flat


async def _apply_mock_energy_schema(
    hass: HomeAssistant, device: CustomerDevice
) -> None:
    """Load panel schema from local mock without calling cloud API."""
    mock_schema = await _load_mock_panel_schema(hass, device)
    if mock_schema is None:
        LOGGER.warning(
            "Panel mock unavailable for device %s, skipping energy schema",
            device.id,
        )
        attach_panel_entity_whitelist(device)
        device.function_groups = {}
        return

    whitelist = attach_panel_entity_whitelist(device)
    grouped_functions = filter_grouped_functions_by_whitelist(
        mock_schema.get("functions"), whitelist
    )
    status_entities = filter_status_entities_by_whitelist(
        mock_schema.get("status") or [], whitelist
    )
    spec_doc = filter_spec_doc_by_whitelist(
        {
            "category": device.category,
            "functions": _flatten_grouped_functions(grouped_functions),
            "status": status_entities,
        },
        whitelist,
    )
    apply_specifications(device, spec_doc)
    set_device_panel_status_entities(device, status_entities)

    if grouped_functions:
        _apply_function_groups(device, grouped_functions)
    else:
        device.function_groups = {}

    from .panel_entity_discovery import restrict_device_functions_to_panel

    restrict_device_functions_to_panel(device)

    LOGGER.debug(
        "Loaded mock energy schema for device %s: %d functions, %d status, "
        "%d panel groups from %s",
        device.id,
        len(spec_doc["functions"]),
        len(status_entities),
        len(device.function_groups),
        PANEL_FUNCTIONS_MOCK_FILE,
    )


async def _fetch_energy_specifications(
    hass: HomeAssistant, manager: Manager, device_id: str
) -> dict[str, Any] | None:
    """Fetch energy specifications from cloud API."""
    api_path = SPECIFICATIONS_API_PATH.format(device_id=device_id)
    LOGGER.debug("Fetching energy specifications from %s", api_path)
    try:
        response = await hass.async_add_executor_job(
            manager.customer_api.get, api_path
        )
    except ApiRequestException as err:
        LOGGER.warning(
            "Energy specifications API failed for device %s: %s", device_id, err
        )
        return None
    if not response.get("success"):
        LOGGER.warning(
            "Failed to fetch energy specifications for device %s", device_id
        )
        return None
    return response


async def ensure_device_energy_schema(
    hass: HomeAssistant, manager: Manager, device: CustomerDevice
) -> None:
    """Load energy specifications and panel groups from cloud API."""
    if device.category not in DYNAMIC_PANEL_CATEGORIES:
        return

    attach_panel_entity_whitelist(device)

    if USE_MOCK_PANEL_FUNCTIONS:
        await _apply_mock_energy_schema(hass, device)
        return

    response = await _fetch_energy_specifications(hass, manager, device.id)
    if response is None:
        device.function_groups = {}
        return

    parsed = parse_energy_specifications_response(
        response.get("result"), category=device.category
    )
    if parsed is None:
        LOGGER.warning(
            "Invalid energy specifications payload for device %s", device.id
        )
        device.function_groups = {}
        return

    spec_doc, panel_functions = parsed
    whitelist = attach_panel_entity_whitelist(device)
    spec_doc = filter_spec_doc_by_whitelist(spec_doc, whitelist)
    apply_specifications(device, spec_doc)
    set_device_panel_status_entities(device, spec_doc.get("status", []))

    grouped_functions = filter_grouped_functions_by_whitelist(
        normalize_function_groups_payload(panel_functions), whitelist
    )
    panel_source = SPECIFICATIONS_API_PATH.format(device_id=device.id)

    if grouped_functions:
        _apply_function_groups(device, grouped_functions)
    else:
        device.function_groups = {}

    from .panel_entity_discovery import restrict_device_functions_to_panel

    restrict_device_functions_to_panel(device)

    LOGGER.debug(
        "Loaded energy schema for device %s: %d functions, %d status, "
        "%d panel groups, %d status entities from %s",
        device.id,
        len(spec_doc.get("functions", [])),
        len(spec_doc.get("status", [])),
        len(device.function_groups),
        len(getattr(device, "panel_status_entities", [])),
        panel_source,
    )


async def preload_panel_devices(hass: HomeAssistant, manager: Manager) -> None:
    """Load panel function groups for all supported devices before entity setup."""
    from .panel_entity_discovery import (
        normalize_panel_device_status,
        prune_obsolete_panel_config_entities,
        restore_panel_entity_visibility,
    )

    for device in manager.device_map.values():
        await ensure_device_energy_schema(hass, manager, device)
        normalize_panel_device_status(device)
        prune_obsolete_panel_config_entities(hass, device)
        restore_panel_entity_visibility(hass, device)


async def apply_panel_commands_via_api(
    hass: HomeAssistant,
    manager: Manager,
    device: CustomerDevice,
    commands: list[dict[str, Any]],
) -> None:
    """Apply panel dynamic entity commands via the thing commands API."""
    api_path = COMMANDS_API_PATH.format(device_id=device.id)
    body = {"commands": commands}
    LOGGER.info(
        "Applying panel commands on device %s via %s: %s",
        device.id,
        api_path,
        commands,
    )

    response = await hass.async_add_executor_job(
        manager.customer_api.post, api_path, None, body
    )
    if not response.get("success"):
        raise HomeAssistantError(
            f"Failed to apply panel commands on device {device.id}"
        )


def notify_panel_commands_applied(
    hass: HomeAssistant, device: CustomerDevice, commands: list[dict[str, Any]]
) -> None:
    """Optimistically update local device status after panel commands succeed."""
    from homeassistant.helpers.dispatcher import dispatcher_send

    from .const import TUYA_HA_SIGNAL_UPDATE_ENTITY

    updated_codes = [command["code"] for command in commands]
    for command in commands:
        device.status[command["code"]] = command["value"]
    dispatcher_send(
        hass,
        f"{TUYA_HA_SIGNAL_UPDATE_ENTITY}_{device.id}",
        updated_codes,
        None,
    )


async def apply_function_group_via_api(
    hass: HomeAssistant,
    manager: Manager,
    device: CustomerDevice,
    group_id: str,
    commands: list[dict[str, Any]],
) -> None:
    """Apply a grouped panel configuration via thing commands API."""
    LOGGER.debug(
        "Applying function group %s on device %s: %s",
        group_id,
        device.id,
        commands,
    )
    await apply_panel_commands_via_api(hass, manager, device, commands)


async def fetch_function_history_from_api(
    hass: HomeAssistant,
    manager: Manager,
    device: CustomerDevice,
    code: str,
) -> list[dict[str, Any]]:
    """Return function change history (not available yet)."""
    _ = (hass, manager, device, code)
    return []


def get_group_function_map(device: CustomerDevice) -> dict[str, set[str]]:
    """Return allowed function codes grouped by group id."""
    grouped: dict[str, set[str]] = {}
    for group_id, functions in getattr(device, "function_groups", {}).items():
        grouped[group_id] = {function.code for function in functions}
    return grouped


def validate_group_commands(
    device: CustomerDevice, group_id: str, commands: list[dict[str, Any]]
) -> None:
    """Validate that a batch command payload is complete and allowed."""
    group_map = get_group_function_map(device)
    allowed_codes = group_map.get(group_id)
    if not allowed_codes:
        raise ValueError(f"Unknown group {group_id}")

    if len(commands) != len(allowed_codes):
        raise ValueError("All functions in the group must be submitted together")

    submitted_codes = {command["code"] for command in commands}
    if submitted_codes != allowed_codes:
        raise ValueError("Submitted functions do not match the group schema")

    function_types = {
        function.code: function.type
        for functions in device.function_groups.values()
        for function in functions
    }
    for command in commands:
        code = command["code"]
        value = command.get("value")
        if not is_value_filled(function_types[code], value):
            raise ValueError(f"Function {code} must not be empty")

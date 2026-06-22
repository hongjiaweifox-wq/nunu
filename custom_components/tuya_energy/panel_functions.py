"""Dynamic grouped panel functions for Tuya devices."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tuya_sharing import CustomerDevice, Manager
from tuya_sharing.device import DeviceFunction, DeviceStatusRange

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import LOGGER

DYNAMIC_PANEL_CATEGORIES = frozenset({"xnyjcn"})
PANEL_FUNCTIONS_MOCK_DIR = "mock"
PANEL_FUNCTIONS_MOCK_FILE = "tuya_panel_functions.json"
FUNCTION_GROUPS_API_PATH = "/v1.0/m/life/ha/{device_id}/function-groups"
THING_COMMANDS_API_PATH = "/v1.1/m/thing/{device_id}/commands"
FUNCTION_HISTORY_API_PATH = "/v1.0/m/life/ha/{device_id}/function-history/{code}"
USE_MOCK_FUNCTION_GROUPS_API = True
USE_MOCK_FUNCTION_HISTORY_API = True

TYPE_PLATFORM_MAP = {
    "Boolean": "switch",
    "Integer": "number",
    "Enum": "select",
    "String": "text",
}


def format_group_label(group_id: str) -> str:
    """Return a human-readable group label."""
    if group_id.startswith("group") and group_id[5:].isdigit():
        return f"Group {group_id[5:]}"
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


def _get_mock_function_groups_path() -> Path:
    """Return the bundled mock file path inside this integration."""
    return Path(__file__).parent / PANEL_FUNCTIONS_MOCK_DIR / PANEL_FUNCTIONS_MOCK_FILE


def _read_mock_function_groups_file(
    mock_path: Path, category: str
) -> dict[str, Any] | None:
    """Read grouped functions from the local mock file (executor-safe)."""
    if not mock_path.is_file():
        return None

    try:
        data = json.loads(mock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    if data.get("category") and data["category"] != category:
        return None

    grouped_functions = data.get("functions", {})
    if not isinstance(grouped_functions, dict):
        return None
    return grouped_functions


async def _load_mock_function_groups(
    hass: HomeAssistant, device: CustomerDevice
) -> dict[str, Any] | None:
    """Load grouped functions from the local mock file."""
    mock_path = _get_mock_function_groups_path()
    grouped_functions = await hass.async_add_executor_job(
        _read_mock_function_groups_file, mock_path, device.category
    )
    if grouped_functions is None:
        LOGGER.debug(
            "No grouped panel functions for device %s (mock file missing or invalid: %s)",
            device.id,
            mock_path,
        )
        return None
    return grouped_functions


async def fetch_function_groups_from_api(
    hass: HomeAssistant, manager: Manager, device: CustomerDevice
) -> dict[str, Any] | None:
    """Fetch grouped panel schema from cloud API (mocked until backend is ready)."""
    api_path = FUNCTION_GROUPS_API_PATH.format(device_id=device.id)
    LOGGER.debug("Fetching function groups from %s", api_path)

    if USE_MOCK_FUNCTION_GROUPS_API:
        return await _load_mock_function_groups(hass, device)

    response = await hass.async_add_executor_job(
        manager.customer_api.get, api_path
    )
    if not response.get("success"):
        return None
    result = response.get("result")
    if isinstance(result, dict) and "functions" in result:
        grouped_functions = result["functions"]
    elif isinstance(result, dict):
        grouped_functions = result
    else:
        return None
    return grouped_functions if isinstance(grouped_functions, dict) else None


async def ensure_device_function_groups(
    hass: HomeAssistant, manager: Manager, device: CustomerDevice
) -> None:
    """Load grouped panel functions from API for xnyjcn devices."""
    if device.category not in DYNAMIC_PANEL_CATEGORIES:
        return

    grouped_functions = await fetch_function_groups_from_api(hass, manager, device)
    if not grouped_functions:
        device.function_groups = {}
        return

    _apply_function_groups(device, grouped_functions)
    LOGGER.debug(
        "Loaded grouped panel functions for device %s from %s",
        device.id,
        FUNCTION_GROUPS_API_PATH.format(device_id=device.id),
    )


async def preload_panel_devices(hass: HomeAssistant, manager: Manager) -> None:
    """Load panel function groups for all supported devices before entity setup."""
    for device in manager.device_map.values():
        await ensure_device_function_groups(hass, manager, device)


async def apply_function_group_via_api(
    hass: HomeAssistant,
    manager: Manager,
    device: CustomerDevice,
    group_id: str,
    commands: list[dict[str, Any]],
) -> None:
    """Apply a grouped panel configuration via thing commands API."""
    api_path = THING_COMMANDS_API_PATH.format(device_id=device.id)
    body = {"commands": commands}
    LOGGER.debug(
        "Applying function group %s on device %s via %s: %s",
        group_id,
        device.id,
        api_path,
        commands,
    )

    response = await hass.async_add_executor_job(
        manager.customer_api.post, api_path, None, body
    )
    if not response.get("success"):
        raise HomeAssistantError(
            f"Failed to apply function group {group_id} on device {device.id}"
        )


def _build_mock_function_history(device: CustomerDevice, code: str) -> list[dict[str, Any]]:
    """Build mock history entries until the cloud API is available."""
    current_value = device.status.get(code)
    now = datetime.now(UTC)
    return [
        {
            "value": current_value,
            "timestamp": now.isoformat(),
            "source": "device",
        },
        {
            "value": current_value,
            "timestamp": (now - timedelta(hours=1)).isoformat(),
            "source": "cloud",
        },
        {
            "value": current_value,
            "timestamp": (now - timedelta(days=1)).isoformat(),
            "source": "user",
        },
    ]


async def fetch_function_history_from_api(
    hass: HomeAssistant,
    manager: Manager,
    device: CustomerDevice,
    code: str,
) -> list[dict[str, Any]]:
    """Fetch function change history from cloud API (mocked until backend is ready)."""
    api_path = FUNCTION_HISTORY_API_PATH.format(device_id=device.id, code=code)
    LOGGER.debug("Fetching function history from %s", api_path)

    if USE_MOCK_FUNCTION_HISTORY_API:
        return _build_mock_function_history(device, code)

    response = await hass.async_add_executor_job(
        manager.customer_api.get, api_path
    )
    if not response.get("success"):
        return []
    result = response.get("result", [])
    return result if isinstance(result, list) else []


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

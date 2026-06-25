"""Dynamic entity discovery for Tuya panel function groups."""

from __future__ import annotations

from collections.abc import Iterator

from tuya_sharing import CustomerDevice
from tuya_sharing.device import DeviceFunction

from homeassistant.components.number import NumberEntityDescription
from homeassistant.components.select import SelectEntityDescription
from homeassistant.components.switch import SwitchEntityDescription
from homeassistant.components.time import TimeEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import RegistryEntryHider

from .const import DOMAIN
from .panel_functions import DYNAMIC_PANEL_CATEGORIES, format_function_label

HARDCODED_PANEL_DPCODES: dict[str, frozenset[str]] = {
    "xnyjcn": frozenset(
        {
            "backup_reserve",
            "output_power_limit",
            "work_mode",
            "feedin_power_limit_enable",
        }
    ),
}

PANEL_TYPE_TO_PLATFORM: dict[str, str] = {
    "Boolean": "switch",
    "Integer": "number",
    "Enum": "select",
    "hourmin": "time",
}

CONFIG_PLATFORMS = ("number", "select", "switch", "time")


def is_panel_device(device: CustomerDevice) -> bool:
    """Return whether the device supports dynamic panel entities."""
    return device.category in DYNAMIC_PANEL_CATEGORIES


def panel_unique_id(tuya_device_id: str, code: str) -> str:
    """Return the unique id used by Tuya config entities."""
    return f"tuya.{tuya_device_id}{code}"


def iter_panel_functions(
    device: CustomerDevice,
) -> Iterator[tuple[DeviceFunction, str]]:
    """Yield panel functions that should become dynamic entities."""
    if not is_panel_device(device):
        return

    hardcoded = HARDCODED_PANEL_DPCODES.get(device.category, frozenset())
    seen: set[str] = set()
    for functions in getattr(device, "function_groups", {}).values():
        for function in functions:
            code = function.code
            if code in hardcoded or code in seen:
                continue
            platform = PANEL_TYPE_TO_PLATFORM.get(function.type)
            if platform is None:
                continue
            seen.add(code)
            yield function, platform


def build_number_description(function: DeviceFunction) -> NumberEntityDescription:
    """Build a number entity description for a panel function."""
    return NumberEntityDescription(
        key=function.code,
        name=format_function_label(function),
        entity_category=None,
    )


def build_select_description(function: DeviceFunction) -> SelectEntityDescription:
    """Build a select entity description for a panel function."""
    return SelectEntityDescription(
        key=function.code,
        name=format_function_label(function),
        translation_key=function.code,
        entity_category=None,
    )


def build_switch_description(function: DeviceFunction) -> SwitchEntityDescription:
    """Build a switch entity description for a panel function."""
    return SwitchEntityDescription(
        key=function.code,
        name=format_function_label(function),
        entity_category=None,
    )


def build_time_description(function: DeviceFunction) -> TimeEntityDescription:
    """Build a time entity description for an hourmin panel function."""
    return TimeEntityDescription(
        key=function.code,
        name=format_function_label(function),
        translation_key=function.code,
        entity_category=None,
    )


def resolve_entity_id(
    hass: HomeAssistant, tuya_device_id: str, code: str, platform: str
) -> str | None:
    """Resolve entity_id from platform and unique_id."""
    entity_registry = er.async_get(hass)
    return entity_registry.async_get_entity_id(
        platform, DOMAIN, panel_unique_id(tuya_device_id, code)
    )


def get_panel_grouped_codes(device: CustomerDevice) -> set[str]:
    """Return all DP codes that belong to panel function groups."""
    grouped_codes: set[str] = set()
    for functions in getattr(device, "function_groups", {}).values():
        grouped_codes.update(function.code for function in functions)
    return grouped_codes


def is_panel_grouped_code(device: CustomerDevice, code: str) -> bool:
    """Return whether a DP code belongs to panel function groups."""
    return code in get_panel_grouped_codes(device)


def is_panel_dynamic_code(device: CustomerDevice, code: str) -> bool:
    """Return whether a DP code is a dynamically discovered panel function entity."""
    if not is_panel_grouped_code(device, code):
        return False
    hardcoded = HARDCODED_PANEL_DPCODES.get(device.category, frozenset())
    return code not in hardcoded


def restore_panel_entity_visibility(hass: HomeAssistant, device: CustomerDevice) -> None:
    """Clear integration-hidden state for panel function entities."""
    if not is_panel_device(device):
        return

    entity_registry = er.async_get(hass)
    hardcoded = HARDCODED_PANEL_DPCODES.get(device.category, frozenset())
    grouped_codes = get_panel_grouped_codes(device)
    for platform in CONFIG_PLATFORMS:
        for code in grouped_codes:
            entity_id = resolve_entity_id(hass, device.id, code, platform)
            if not entity_id:
                continue
            entry = entity_registry.async_get(entity_id)
            if not entry:
                continue
            updates: dict[str, object] = {}
            if entry.hidden_by == RegistryEntryHider.INTEGRATION:
                updates["hidden_by"] = None
            if code not in hardcoded and entry.entity_category is not None:
                updates["entity_category"] = None
            if updates:
                entity_registry.async_update_entity(entity_id, **updates)


def resolve_entity_id_for_function(
    hass: HomeAssistant, device: CustomerDevice, function: DeviceFunction
) -> str | None:
    """Resolve entity_id for a panel function, including hardcoded entities."""
    if function.type == "hourmin":
        return resolve_entity_id(hass, device.id, function.code, "time")

    platform = PANEL_TYPE_TO_PLATFORM.get(function.type)
    if platform and (
        entity_id := resolve_entity_id(hass, device.id, function.code, platform)
    ):
        return entity_id

    for fallback_platform in CONFIG_PLATFORMS:
        if entity_id := resolve_entity_id(
            hass, device.id, function.code, fallback_platform
        ):
            return entity_id
    return None

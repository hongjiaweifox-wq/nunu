"""Dynamic entity discovery for Tuya panel function groups."""

from __future__ import annotations

from collections.abc import Iterator

from tuya_sharing import CustomerDevice
from tuya_sharing.device import DeviceFunction

from homeassistant.components.number import NumberEntityDescription
from homeassistant.components.select import SelectEntityDescription
from homeassistant.components.switch import SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

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
}

CONFIG_PLATFORMS = ("number", "select", "switch")


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
        entity_category=EntityCategory.CONFIG,
    )


def build_select_description(function: DeviceFunction) -> SelectEntityDescription:
    """Build a select entity description for a panel function."""
    return SelectEntityDescription(
        key=function.code,
        name=format_function_label(function),
        entity_category=EntityCategory.CONFIG,
    )


def build_switch_description(function: DeviceFunction) -> SwitchEntityDescription:
    """Build a switch entity description for a panel function."""
    return SwitchEntityDescription(
        key=function.code,
        name=format_function_label(function),
        entity_category=EntityCategory.CONFIG,
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
    """Return whether a DP code is controlled via panel group apply."""
    return code in get_panel_grouped_codes(device)


def resolve_entity_id_for_function(
    hass: HomeAssistant, device: CustomerDevice, function: DeviceFunction
) -> str | None:
    """Resolve entity_id for a panel function, including hardcoded entities."""
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

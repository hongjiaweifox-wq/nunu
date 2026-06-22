"""WebSocket API and custom panel for Tuya xnyjcn device configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.components import panel_custom, websocket_api
from homeassistant.components.frontend import add_extra_js_url, async_panel_exists
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import dispatcher_send

from .const import DOMAIN, LOGGER, TUYA_HA_SIGNAL_UPDATE_ENTITY
from .panel_functions import (
    DYNAMIC_PANEL_CATEGORIES,
    apply_function_group_via_api,
    build_panel_state,
    ensure_device_function_groups,
    fetch_function_history_from_api,
    validate_group_commands,
)
from .services import _get_tuya_device

URL_BASE = "/tuya_xnyjcn_panel_static"
PANEL_URL_PATH = "tuya-xnyjcn-panel"
PANEL_WEBCOMPONENT = "tuya-xnyjcn-panel"
EMBED_SCRIPT_URL = f"{URL_BASE}/tuya-xnyjcn-device-embed.js"

COMMAND_SCHEMA = vol.Schema(
    {
        vol.Required("code"): str,
        vol.Required("value"): vol.Any(bool, int, float, str, None),
    }
)


@websocket_api.websocket_command({vol.Required("type"): "tuya/get_panel_devices"})
@websocket_api.async_response
async def websocket_get_panel_devices(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Return xnyjcn devices available for the custom configuration panel."""
    device_registry = dr.async_get(hass)
    devices: list[dict[str, Any]] = []
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        manager = entry.runtime_data.manager
        for device in manager.device_map.values():
            if device.category not in DYNAMIC_PANEL_CATEGORIES:
                continue
            device_entry = device_registry.async_get_device(
                identifiers={(DOMAIN, device.id)}
            )
            if device_entry is None:
                continue
            devices.append(
                {
                    "device_id": device_entry.id,
                    "tuya_device_id": device.id,
                    "name": device.name,
                    "online": device.online,
                }
            )
    connection.send_result(msg["id"], {"devices": devices})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "tuya/get_panel_functions",
        vol.Required("device_id"): str,
    }
)
@websocket_api.async_response
async def websocket_get_panel_functions(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Return grouped panel functions and current values for a device."""
    try:
        device, manager = _get_tuya_device(hass, msg["device_id"])
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "not_found", str(err))
        return

    await ensure_device_function_groups(hass, manager, device)
    connection.send_result(msg["id"], build_panel_state(hass, device))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "tuya/get_panel_function_history",
        vol.Required("device_id"): str,
        vol.Required("code"): str,
    }
)
@websocket_api.async_response
async def websocket_get_panel_function_history(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Return change history for a grouped panel function."""
    try:
        device, manager = _get_tuya_device(hass, msg["device_id"])
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "not_found", str(err))
        return

    code = msg["code"]
    await ensure_device_function_groups(hass, manager, device)
    allowed_codes = {
        function.code
        for functions in device.function_groups.values()
        for function in functions
    }
    if code not in allowed_codes:
        connection.send_error(
            msg["id"], "invalid_code", f"Function {code} is not in the panel schema"
        )
        return

    history = await fetch_function_history_from_api(hass, manager, device, code)
    connection.send_result(
        msg["id"],
        {
            "code": code,
            "label": code.replace("_", " ").title(),
            "history": history,
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "tuya/set_panel_functions",
        vol.Required("device_id"): str,
        vol.Required("group_id"): str,
        vol.Required("commands"): [COMMAND_SCHEMA],
    }
)
@websocket_api.async_response
async def websocket_set_panel_functions(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Set all grouped panel functions in a group on a device."""
    try:
        device, manager = _get_tuya_device(hass, msg["device_id"])
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "not_found", str(err))
        return

    group_id = msg["group_id"]
    commands = msg["commands"]
    await ensure_device_function_groups(hass, manager, device)
    try:
        validate_group_commands(device, group_id, commands)
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_command", str(err))
        return

    try:
        await apply_function_group_via_api(
            hass, manager, device, group_id, commands
        )
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "apply_failed", str(err))
        return

    updated_codes = [command["code"] for command in commands]
    for command in commands:
        device.status[command["code"]] = command["value"]
    dispatcher_send(
        hass,
        f"{TUYA_HA_SIGNAL_UPDATE_ENTITY}_{device.id}",
        updated_codes,
        None,
    )
    connection.send_result(msg["id"], build_panel_state(hass, device))


@callback
def _register_websocket_api(hass: HomeAssistant) -> None:
    """Register panel websocket commands."""
    websocket_api.async_register_command(hass, websocket_get_panel_devices)
    websocket_api.async_register_command(hass, websocket_get_panel_functions)
    websocket_api.async_register_command(hass, websocket_get_panel_function_history)
    websocket_api.async_register_command(hass, websocket_set_panel_functions)


async def async_register_tuya_panel(hass: HomeAssistant) -> None:
    """Register the xnyjcn custom configuration panel and websocket API."""
    if hass.data.get(f"{DOMAIN}_panel_registered"):
        return

    _register_websocket_api(hass)

    frontend_dir = Path(__file__).parent / "panel" / "frontend"
    if not hass.data.get(f"{DOMAIN}_panel_static_registered"):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(URL_BASE, str(frontend_dir), cache_headers=False)]
        )
        add_extra_js_url(hass, EMBED_SCRIPT_URL)
        hass.data[f"{DOMAIN}_panel_static_registered"] = True

    if not async_panel_exists(hass, PANEL_URL_PATH):
        await panel_custom.async_register_panel(
            hass=hass,
            frontend_url_path=PANEL_URL_PATH,
            webcomponent_name=PANEL_WEBCOMPONENT,
            config_panel_domain=DOMAIN,
            module_url=f"{URL_BASE}/tuya-xnyjcn-panel.js",
            sidebar_title="Tuya Device Panel",
            sidebar_icon="mdi:solar-power",
            embed_iframe=False,
            require_admin=False,
        )

    hass.data[f"{DOMAIN}_panel_registered"] = True


def get_panel_configuration_url(ha_device_id: str) -> str:
    """Build the configuration panel URL for a device registry entry."""
    return f"homeassistant://{PANEL_URL_PATH}?device_id={ha_device_id}"

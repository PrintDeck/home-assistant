"""Home Assistant integration for PrintDeck."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PrintDeckApiClient
from .const import CONF_TOKEN, CONF_TRANSPORT, DOMAIN, PLATFORMS, TRANSPORT_MQTT
from .coordinator import PrintDeckCoordinator
from .mqtt_coordinator import PrintDeckMqttCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up PrintDeck from a config entry."""
    if entry.data.get(CONF_TRANSPORT) == TRANSPORT_MQTT:
        coordinator = PrintDeckMqttCoordinator(hass, entry)
        await coordinator.async_start()
        entry.async_on_unload(coordinator.async_stop)
    else:
        client = PrintDeckApiClient(
            async_get_clientsession(hass), entry.data[CONF_HOST], entry.data[CONF_TOKEN]
        )
        coordinator = PrintDeckCoordinator(hass, entry, client)
        await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    info = coordinator.data.info
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, info.device_id)},
        manufacturer="PrintDeck",
        model=info.hardware,
        name=info.name,
        sw_version=info.firmware_version,
        configuration_url=coordinator.configuration_url,
    )

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        if isinstance(coordinator, PrintDeckMqttCoordinator):
            coordinator.async_stop()
        raise
    coordinator.events.enabled = True
    entry.async_on_unload(coordinator.events.cursor.reset)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a PrintDeck config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

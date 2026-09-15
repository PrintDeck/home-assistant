"""Prevent overlapping ownership by standard MQTT and PrintDeck entities."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN


def has_standard_mqtt_entities(hass: HomeAssistant, device_id: str) -> bool:
    """Wait until retained discovery deletion has reached the HA registry."""
    prefix = f"{device_id}_"
    return any(
        entity.platform == "mqtt" and entity.unique_id.startswith(prefix)
        for entity in er.async_get(hass).entities.values()
    )


def update_discovery_issue(hass: HomeAssistant, entry_id: str, conflict: bool) -> None:
    """Explain conflicting entity ownership for either HACS transport."""
    issue_id = f"mqtt_discovery_conflict_{entry_id}"
    if conflict:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="mqtt_discovery_conflict",
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, issue_id)

"""Push coordinator using Home Assistant's configured MQTT connection."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import timedelta
from time import monotonic
from urllib.parse import urlsplit

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PrintDeckApiError, PrintDeckInfo
from .const import CONF_TOPIC_ROOT, DOMAIN
from .coordinator import PrintDeckCoordinator, PrintDeckCoordinatorData
from .mqtt_state import PrintDeckDiscoveryConflict, PrintDeckMqttState
from .ownership import has_standard_mqtt_entities, update_discovery_issue

_LOGGER = logging.getLogger(__name__)
INITIAL_STATE_TIMEOUT = 40


class PrintDeckMqttCoordinator(PrintDeckCoordinator):
    """Fan MQTT state out to the same entity IDs used by the HTTP transport."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        DataUpdateCoordinator.__init__(
            self, hass, _LOGGER, config_entry=entry, name=DOMAIN, always_update=False
        )
        self.state = PrintDeckMqttState(entry.data[CONF_TOPIC_ROOT])
        self.info: PrintDeckInfo | None = None
        self._unsubscribers: list[Callable[[], None]] = []
        self._ready = asyncio.Event()
        self._active = False

    @property
    def configuration_url(self) -> str | None:
        """Use device-announced hostname only when it is a valid local HTTP host."""
        if self.info is None or not self.info.hostname:
            return None
        host = self.info.hostname
        parsed = urlsplit(f"http://{host}")
        if parsed.hostname != host or parsed.username or parsed.password or parsed.path:
            return None
        return f"http://{host}"

    async def _async_setup(self) -> None:
        """MQTT startup is owned by async_start, never an HTTP request."""

    async def _async_update_data(self) -> PrintDeckCoordinatorData:
        """A manual entity refresh reads the push cache without network polling."""
        self._publish_current()
        if not self.last_update_success or self.data is None:
            raise UpdateFailed("Waiting for fresh PrintDeck MQTT state")
        return self.data

    async def async_start(self) -> None:
        """Subscribe once, with bounded startup and complete failure cleanup."""
        try:
            if not await mqtt.async_wait_for_mqtt_client(self.hass):
                raise ConfigEntryNotReady(
                    "Configure the Home Assistant MQTT integration first"
                )
            self._active = True
            self._unsubscribers.append(
                mqtt.async_subscribe_connection_status(
                    self.hass, self._connection_changed
                )
            )
            for suffix in (
                "info",
                "printers",
                "device",
                "printers/+/status",
                "availability",
            ):
                self._unsubscribers.append(
                    await mqtt.async_subscribe(
                        self.hass,
                        f"{self.state.root}/{suffix}",
                        self._message_received,
                        qos=1,
                    )
                )
            self._unsubscribers.append(
                async_track_time_interval(
                    self.hass, self._check_freshness, timedelta(seconds=10)
                )
            )
            async with asyncio.timeout(INITIAL_STATE_TIMEOUT):
                await self._ready.wait()
            if not self.last_update_success or self.data is None:
                raise ConfigEntryNotReady(
                    "Disable PrintDeck automatic MQTT Discovery and wait for its entities to be removed"
                )
        except TimeoutError as err:
            self.async_stop()
            raise ConfigEntryNotReady("Waiting for fresh PrintDeck MQTT state") from err
        except ConfigEntryNotReady:
            self.async_stop()
            raise
        except HomeAssistantError as err:
            self.async_stop()
            raise ConfigEntryNotReady(
                "Waiting for the Home Assistant MQTT connection"
            ) from err
        except BaseException:
            self.async_stop()
            raise

    @callback
    def async_stop(self) -> None:
        """Remove every callback before unloading or retrying setup."""
        self._active = False
        while self._unsubscribers:
            self._unsubscribers.pop()()

    @callback
    def _connection_changed(self, connected: bool) -> None:
        if not self._active:
            return
        self.state.invalidate_live()
        self.async_set_update_error(
            UpdateFailed("Waiting for fresh PrintDeck MQTT state")
        )

    @callback
    def _message_received(self, message: mqtt.ReceiveMessage) -> None:
        if not self._active:
            return
        try:
            changed = self.state.ingest(
                message.topic, message.payload, message.retain, monotonic()
            )
        except PrintDeckDiscoveryConflict:
            self._publish_current()
        except (PrintDeckApiError, ValueError, TypeError):
            # Never log incoming payloads, which can contain names and network addresses.
            _LOGGER.debug("Ignored invalid PrintDeck MQTT message")
        else:
            if changed:
                self._publish_current()

    @callback
    def _check_freshness(self, _now: object) -> None:
        self._publish_current()

    @callback
    def _publish_current(self) -> None:
        if not self._active:
            return
        self.info = self.state.info
        registry_conflict = self.info is not None and has_standard_mqtt_entities(
            self.hass, self.info.device_id
        )
        if self.state.conflict or registry_conflict:
            update_discovery_issue(self.hass, self.config_entry.entry_id, True)
            self.async_set_update_error(
                UpdateFailed(
                    "Disable automatic MQTT Discovery and wait for its entities to be removed"
                )
            )
            self._ready.set()
            return
        update_discovery_issue(self.hass, self.config_entry.entry_id, False)
        snapshot = self.state.snapshot(monotonic())
        if snapshot is None or self.info is None:
            self.async_set_update_error(
                UpdateFailed("Waiting for fresh PrintDeck MQTT state")
            )
            return
        self._async_remove_missing_printer_devices(snapshot.printers)
        self.async_set_updated_data(
            PrintDeckCoordinatorData(self.info, snapshot.power, snapshot.printers)
        )
        self._ready.set()

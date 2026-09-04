"""Data coordinator for the PrintDeck integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    PrintDeckApiClient,
    PrintDeckApiDisabledError,
    PrintDeckApiError,
    PrintDeckAuthenticationError,
    PrintDeckInfo,
    PrintDeckPower,
    PrintDeckPrinter,
    PrintDeckRateLimitedError,
    PrintDeckUnsupportedError,
)
from .const import DEFAULT_UPDATE_INTERVAL, DOMAIN
from .identity import device_belongs_to_missing_printer

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PrintDeckCoordinatorData:
    """Last complete snapshot received from PrintDeck."""

    info: PrintDeckInfo
    power: PrintDeckPower
    printers: tuple[PrintDeckPrinter, ...]


class PrintDeckCoordinator(DataUpdateCoordinator[PrintDeckCoordinatorData]):
    """Poll one aggregate endpoint and fan its state out to all entities."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: PrintDeckApiClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=DEFAULT_UPDATE_INTERVAL,
            always_update=False,
        )
        self.client = client
        self.info: PrintDeckInfo | None = None

    async def _async_setup(self) -> None:
        try:
            self.info = await self.client.async_get_info()
        except PrintDeckAuthenticationError as err:
            raise ConfigEntryAuthFailed from err
        except PrintDeckApiError as err:
            raise UpdateFailed(str(err)) from err

    async def _async_update_data(self) -> PrintDeckCoordinatorData:
        assert self.info is not None
        try:
            snapshot = await self.client.async_get_snapshot()
        except PrintDeckAuthenticationError as err:
            raise ConfigEntryAuthFailed from err
        except PrintDeckRateLimitedError as err:
            raise UpdateFailed("PrintDeck request limit reached") from err
        except PrintDeckApiDisabledError as err:
            raise UpdateFailed("Unified Printer API is disabled") from err
        except PrintDeckUnsupportedError as err:
            raise UpdateFailed("PrintDeck API is not supported") from err
        except PrintDeckApiError as err:
            raise UpdateFailed(str(err)) from err
        self._async_remove_missing_printer_devices(snapshot.printers)
        return PrintDeckCoordinatorData(
            info=self.info, power=snapshot.power, printers=snapshot.printers
        )

    def _async_remove_missing_printer_devices(
        self, printers: tuple[PrintDeckPrinter, ...]
    ) -> None:
        """Remove child devices absent from a complete, successful snapshot."""
        assert self.info is not None
        current_printer_ids = {printer.printer_id for printer in printers}
        device_registry = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(
            device_registry, self.config_entry.entry_id
        ):
            if device_belongs_to_missing_printer(
                self.info.device_id, current_printer_ids, device.identifiers
            ):
                device_registry.async_remove_device(device.id)

    def printer(self, printer_id: str) -> PrintDeckPrinter | None:
        """Return one printer from the in-memory snapshot."""
        if self.data is None:
            return None
        return next(
            (
                printer
                for printer in self.data.printers
                if printer.printer_id == printer_id
            ),
            None,
        )

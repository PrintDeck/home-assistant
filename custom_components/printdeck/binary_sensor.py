"""Binary sensor entities exposed by PrintDeck."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import PrintDeckPower, PrintDeckPrinter
from .coordinator import PrintDeckCoordinator
from .entity import PrintDeckDeviceEntity, PrintDeckEntity


@dataclass(frozen=True, kw_only=True)
class PrintDeckBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describe a PrintDeck binary sensor."""

    value_fn: Callable[[PrintDeckPrinter], bool]
    available_when_stale: bool = True


@dataclass(frozen=True, kw_only=True)
class PrintDeckDeviceBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describe a binary sensor belonging to the PrintDeck device."""

    value_fn: Callable[[PrintDeckPower], bool]


DEVICE_BINARY_SENSORS: tuple[PrintDeckDeviceBinarySensorEntityDescription, ...] = (
    PrintDeckDeviceBinarySensorEntityDescription(
        key="battery_charging",
        translation_key="battery_charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        value_fn=lambda power: power.charging,
    ),
)


BINARY_SENSORS: tuple[PrintDeckBinarySensorEntityDescription, ...] = (
    PrintDeckBinarySensorEntityDescription(
        key="online",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda printer: printer.reachability == "online",
    ),
    PrintDeckBinarySensorEntityDescription(
        key="selected",
        translation_key="selected",
        icon="mdi:star-check",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda printer: printer.selected,
    ),
    PrintDeckBinarySensorEntityDescription(
        key="data_stale",
        translation_key="data_stale",
        icon="mdi:database-clock-outline",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda printer: printer.stale,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up PrintDeck binary sensors and newly added printer profiles."""
    coordinator: PrintDeckCoordinator = entry.runtime_data
    known_printers: set[str] = set()
    power_entities_added = False

    @callback
    def async_add_device_power() -> None:
        nonlocal power_entities_added
        if power_entities_added or not coordinator.data.power.available:
            return
        power_entities_added = True
        async_add_entities(
            [
                PrintDeckDeviceBinarySensor(coordinator, description)
                for description in DEVICE_BINARY_SENSORS
            ]
        )

    @callback
    def async_add_new_printers() -> None:
        current_printer_ids = {
            printer.printer_id for printer in coordinator.data.printers
        }
        known_printers.intersection_update(current_printer_ids)
        entities: list[PrintDeckBinarySensor] = []
        for printer in coordinator.data.printers:
            if printer.printer_id in known_printers:
                continue
            known_printers.add(printer.printer_id)
            entities.extend(
                PrintDeckBinarySensor(coordinator, printer, description)
                for description in BINARY_SENSORS
            )
        if entities:
            async_add_entities(entities)

    async_add_device_power()
    async_add_new_printers()
    entry.async_on_unload(coordinator.async_add_listener(async_add_device_power))
    entry.async_on_unload(coordinator.async_add_listener(async_add_new_printers))


class PrintDeckBinarySensor(PrintDeckEntity, BinarySensorEntity):
    """One normalized PrintDeck binary sensor."""

    entity_description: PrintDeckBinarySensorEntityDescription

    @property
    def is_on(self) -> bool | None:
        """Return the current boolean state without performing I/O."""
        printer = self.printer
        return None if printer is None else self.entity_description.value_fn(printer)


class PrintDeckDeviceBinarySensor(PrintDeckDeviceEntity, BinarySensorEntity):
    """One power state from the PrintDeck device."""

    entity_description: PrintDeckDeviceBinarySensorEntityDescription

    @property
    def available(self) -> bool:
        """Return whether battery charging state is available."""
        power = self.coordinator.data.power
        return super().available and power.available and power.battery_present

    @property
    def is_on(self) -> bool:
        """Return the current charging state without performing I/O."""
        return self.entity_description.value_fn(self.coordinator.data.power)

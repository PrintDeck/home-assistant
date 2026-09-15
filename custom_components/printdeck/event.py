"""PrintDeck print events, alongside the existing state sensors."""
from __future__ import annotations
from time import monotonic
from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.core import callback
from .entity import PrintDeckEntity
from .print_events import EVENT_TYPES

DESCRIPTION = EventEntityDescription(key="print_events", translation_key="print_events", icon="mdi:printer-3d")

async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    known: set[str] = set()
    @callback
    def discover():
        current = {printer.printer_id for printer in coordinator.data.printers}
        known.intersection_update(current)
        entities = []
        for printer in coordinator.data.printers:
            if printer.printer_id not in known:
                known.add(printer.printer_id)
                entities.append(PrintDeckPrintEvent(coordinator, printer))
        if entities:
            async_add_entities(entities)
    discover()
    entry.async_on_unload(coordinator.async_add_listener(discover))

class PrintDeckPrintEvent(PrintDeckEntity, EventEntity):
    _attr_event_types = list(EVENT_TYPES)

    def __init__(self, coordinator, printer):
        super().__init__(coordinator, printer, DESCRIPTION)

    @property
    def available(self):
        state = getattr(self.coordinator, "state", None)
        if state is not None:
            # A valid event must remain usable while another profile or the battery
            # topic is missing. The MQTT state validates its own freshness/ownership.
            return not self.coordinator.events.blocked and any(
                printer.printer_id == self._printer_id and not printer.stale
                and printer.print_events is not None
                for printer in state.event_printers(monotonic())
            )
        return super().available and self.printer.print_events is not None

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.events.listen(self._printer_id, self._receive))

    @callback
    def _receive(self, payload):
        self._trigger_event(payload["event_type"], {key: value for key, value in payload.items() if key != "event_type"})
        self.async_write_ha_state()

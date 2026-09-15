"""Deliver one firmware event to its entity and the PrintDeck automation channel."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import asdict
from homeassistant.helpers import device_registry as dr
from .const import DOMAIN
from .identity import printer_device_identifier
from .print_events import PrintEventCursor


class PrintDeckEventHub:
    def __init__(self, hass) -> None:
        self.hass = hass
        self.cursor = PrintEventCursor()
        self.enabled = False
        self.blocked = False
        self._listeners: dict[str, list[Callable]] = {}

    def listen(self, printer_id: str, listener: Callable) -> Callable:
        self._listeners.setdefault(printer_id, []).append(listener)
        def unsubscribe():
            listeners = self._listeners.get(printer_id, [])
            if listener in listeners:
                listeners.remove(listener)
            if not listeners:
                self._listeners.pop(printer_id, None)
        return unsubscribe

    def consume(self, info, printer) -> None:
        events = self.cursor.consume(printer.printer_id, printer.print_events, printer.stale)
        if not self.enabled:
            return
        registry = dr.async_get(self.hass)
        for event in events:
            device = registry.async_get_device(identifiers={
                (DOMAIN, printer_device_identifier(info.device_id, printer.printer_id))})
            if device is None:
                continue
            payload = asdict(event)
            payload.update(device_id=device.id, printdeck_id=info.device_id,
                           printer_id=printer.printer_id, printer_name=printer.name,
                           stream_id=printer.print_events.stream_id)
            payload["event_id"] = f"{info.device_id}:{printer.printer_id}:{payload['stream_id']}:{event.sequence}"
            payload["routing_key"] = f"{info.device_id}:{printer.printer_id}"
            for listener in tuple(self._listeners.get(printer.printer_id, ())):
                listener(payload)
            self.hass.bus.async_fire("printdeck_event", payload)

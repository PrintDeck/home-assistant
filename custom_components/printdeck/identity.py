"""Stable identities for PrintDeck printer devices and entities."""

from __future__ import annotations

from collections.abc import Collection, Iterable

from .const import DOMAIN


def printer_device_identifier(printdeck_device_id: str, printer_id: str) -> str:
    """Return the stable Home Assistant device identifier for one printer profile."""
    return f"{printdeck_device_id}:{printer_id}"


def printer_entity_unique_id(
    printdeck_device_id: str, printer_id: str, entity_key: str
) -> str:
    """Return the stable Home Assistant unique ID for one printer entity."""
    return f"{printdeck_device_id}_{printer_id}_{entity_key}"


def printer_id_from_device_identifier(
    printdeck_device_id: str, identifier: tuple[str, str]
) -> str | None:
    """Extract a printer profile ID from one PrintDeck child-device identifier."""
    domain, value = identifier
    prefix = f"{printdeck_device_id}:"
    if domain != DOMAIN or not value.startswith(prefix):
        return None
    printer_id = value[len(prefix) :]
    return printer_id or None


def device_belongs_to_missing_printer(
    printdeck_device_id: str,
    current_printer_ids: Collection[str],
    identifiers: Iterable[tuple[str, str]],
) -> bool:
    """Return whether a registered child device no longer exists on PrintDeck."""
    printer_ids = {
        printer_id
        for identifier in identifiers
        if (
            printer_id := printer_id_from_device_identifier(
                printdeck_device_id, identifier
            )
        )
        is not None
    }
    return bool(printer_ids) and printer_ids.isdisjoint(current_printer_ids)

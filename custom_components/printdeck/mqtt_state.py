"""Bounded, transport-neutral assembly of PrintDeck MQTT v1 messages."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from .api import (
    PrintDeckInfo,
    PrintDeckInvalidResponseError,
    PrintDeckPower,
    PrintDeckSnapshot,
    parse_info,
    parse_power,
    parse_printer,
)

MAX_PAYLOAD_BYTES = 65536
MAX_PRINTERS = 10
STATE_EXPIRY_SECONDS = 90
GENERATION_PATTERN = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{8}")
TOPIC_ROOT_PATTERN = re.compile(r"printdeck/(printdeck-[a-zA-Z0-9_-]{1,64})/v1")


class PrintDeckDiscoveryConflict(PrintDeckInvalidResponseError):
    """Standard discovery must be disabled before using PrintDeck entities."""


def validate_topic_root(value: str) -> str:
    """Accept only a single PrintDeck namespace, never MQTT wildcards."""
    value = value.strip().rstrip("/")
    if not TOPIC_ROOT_PATTERN.fullmatch(value):
        raise ValueError("Invalid PrintDeck topic root")
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError("Non-finite JSON constant")


def decode_payload(value: str | bytes) -> Mapping[str, Any]:
    """Reject oversized, non-object and non-standard JSON payloads."""
    if len(value) > MAX_PAYLOAD_BYTES or (
        isinstance(value, str) and len(value.encode("utf-8")) > MAX_PAYLOAD_BYTES
    ):
        raise PrintDeckInvalidResponseError("MQTT payload exceeds the limit")
    try:
        payload = json.loads(value, parse_constant=_reject_json_constant)
    except (ValueError, UnicodeError, RecursionError) as err:
        raise PrintDeckInvalidResponseError("Invalid MQTT JSON") from err
    if not isinstance(payload, Mapping) or payload.get("api_version") != "v1":
        raise PrintDeckInvalidResponseError("Unsupported MQTT payload")
    return payload


def parse_mqtt_info(root: str, payload: Mapping[str, Any]) -> PrintDeckInfo:
    """Verify stable identity and exclusive entity ownership."""
    root = validate_topic_root(root)
    info = parse_info(payload)
    if root != f"printdeck/{info.device_id}/v1":
        raise PrintDeckInvalidResponseError("MQTT device identity mismatch")
    options = payload.get("mqtt")
    if not isinstance(options, Mapping) or any(
        not isinstance(options.get(key), bool)
        for key in ("home_assistant_discovery", "discovery_cleanup_pending")
    ):
        raise PrintDeckInvalidResponseError("Missing MQTT discovery ownership")
    if options["home_assistant_discovery"] or options["discovery_cleanup_pending"]:
        raise PrintDeckDiscoveryConflict(
            "Disable automatic MQTT Discovery in PrintDeck and wait for cleanup"
        )
    return info


class PrintDeckMqttState:
    """Keep at most one validated status per configured printer."""

    def __init__(self, root: str) -> None:
        self.root = validate_topic_root(root)
        self.generation: str | None = None
        self.info: PrintDeckInfo | None = None
        self.profiles: dict[str, Mapping[str, Any]] | None = None
        self.statuses: dict[str, tuple[Mapping[str, Any], float]] = {}
        self.power = PrintDeckPower(False, False, None, False, False)
        self.power_updated: float | None = None
        self.online = False
        self.conflict = False

    def invalidate_live(self) -> None:
        """Require fresh state after an offline period or broker reconnect."""
        self.online = False
        self.statuses.clear()
        self.power_updated = None

    def ingest(
        self, topic: str, value: str | bytes, retained: bool, now: float
    ) -> bool:
        """Accept one known topic; rejected messages never mutate valid state."""
        prefix = self.root + "/"
        if not topic.startswith(prefix):
            return False
        suffix = topic[len(prefix) :]
        if suffix == "availability":
            if value in ("offline", b"offline"):
                self.invalidate_live()
            elif value in ("online", b"online"):
                self.online = True
            else:
                raise PrintDeckInvalidResponseError("Invalid MQTT availability")
            return True
        if suffix == "info":
            payload = decode_payload(value)
            try:
                info = parse_mqtt_info(self.root, payload)
            except PrintDeckDiscoveryConflict:
                self.conflict = True
                self.statuses.clear()
                self.power_updated = None
                raise
            self.info = info
            self.conflict = False
            return True
        if suffix == "printers":
            payload = decode_payload(value)
            generation = payload.get("_mqtt_generation")
            if not isinstance(generation, str) or not GENERATION_PATTERN.fullmatch(
                generation
            ):
                raise PrintDeckInvalidResponseError("Invalid MQTT catalog generation")
            values = payload.get("printers")
            if not isinstance(values, list) or len(values) > MAX_PRINTERS:
                raise PrintDeckInvalidResponseError("Invalid MQTT printer catalog")
            profiles = {}
            for profile in values:
                if not isinstance(profile, Mapping):
                    raise PrintDeckInvalidResponseError("Invalid MQTT printer profile")
                pid = profile.get("id")
                if (
                    type(pid) is not int
                    or not 0 < pid <= 0xFFFFFFFF
                    or str(pid) in profiles
                ):
                    raise PrintDeckInvalidResponseError("Invalid MQTT printer identity")
                # Validate metadata using an empty summary before committing a catalog.
                parse_printer(profile, {"printer_id": pid, "connection": {}, "job": {}})
                profiles[str(pid)] = profile
            if generation != self.generation:
                self.statuses.clear()
                self.power_updated = None
                self.generation = generation
            old_profiles = self.profiles or {}
            self.statuses = {
                pid: state
                for pid, state in self.statuses.items()
                if pid in profiles and old_profiles.get(pid) == profiles[pid]
            }
            self.profiles = profiles
            return True
        # Broker-retained telemetry may describe an old job; wait for a live refresh.
        if retained or self.conflict:
            return False
        if suffix == "device":
            payload = decode_payload(value)
            if (
                self.generation is None
                or payload.get("_mqtt_generation") != self.generation
            ):
                return False
            power = parse_power(payload)
            self.power, self.power_updated = power, now
            return True
        parts = suffix.split("/")
        if len(parts) != 3 or parts[0] != "printers" or parts[2] != "status":
            return False
        pid = parts[1]
        if self.profiles is None or pid not in self.profiles:
            return False
        payload = decode_payload(value)
        if (
            self.generation is None
            or payload.get("_mqtt_generation") != self.generation
        ):
            return False
        status = payload.get("status")
        parse_printer(self.profiles[pid], status)
        self.statuses[pid] = (status, now)
        return True

    def snapshot(self, now: float) -> PrintDeckSnapshot | None:
        """Return only a complete, live snapshot; absence never deletes devices."""
        if (
            self.conflict
            or not self.online
            or self.info is None
            or self.profiles is None
            or self.power_updated is None
            or now - self.power_updated >= STATE_EXPIRY_SECONDS
            or any(
                pid not in self.statuses
                or now - self.statuses[pid][1] >= STATE_EXPIRY_SECONDS
                for pid in self.profiles
            )
        ):
            return None
        return PrintDeckSnapshot(
            self.power,
            tuple(
                parse_printer(profile, self.statuses[pid][0])
                for pid, profile in self.profiles.items()
            ),
        )

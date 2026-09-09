"""Focused tests for the public PrintDeck Home Assistant API client."""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import ModuleType
from typing import Self

PUBLIC_COMPONENT = (
    Path(__file__).resolve().parents[1] / "custom_components" / "printdeck"
)


class FakeClientError(Exception):
    """Stand in for aiohttp.ClientError."""


class FakeResponse:
    """Minimal asynchronous aiohttp response."""

    def __init__(
        self, status: int, payload: object, headers: dict[str, str] | None = None
    ) -> None:
        self.status = status
        self._payload = payload
        self.headers = headers or {}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def text(self) -> str:
        if isinstance(self._payload, str):
            return self._payload
        return json.dumps(self._payload)


class FakeSession:
    """Return queued responses and record requested URLs."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, dict[str, str], int]] = []

    def get(
        self, url: str, *, headers: dict[str, str], timeout: int, allow_redirects: bool
    ) -> FakeResponse:
        assert allow_redirects is False
        self.requests.append((url, headers, timeout))
        return self.responses.pop(0)


def load_api_module() -> ModuleType:
    """Load only the client and constants, without importing Home Assistant."""
    aiohttp = ModuleType("aiohttp")
    aiohttp.ClientError = FakeClientError
    aiohttp.ClientSession = FakeSession
    sys.modules["aiohttp"] = aiohttp

    package = ModuleType("printdeck")
    package.__path__ = [str(PUBLIC_COMPONENT)]
    sys.modules["printdeck"] = package

    for name in ("const", "identity", "api"):
        qualified_name = f"printdeck.{name}"
        spec = importlib.util.spec_from_file_location(
            qualified_name, PUBLIC_COMPONENT / f"{name}.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified_name] = module
        spec.loader.exec_module(module)
    return sys.modules["printdeck.api"]


API = load_api_module()
CONST = sys.modules["printdeck.const"]
IDENTITY = sys.modules["printdeck.identity"]


def info_payload() -> dict[str, object]:
    """Return representative integration-ready device information."""
    return {
        "api_version": "v1",
        "product": "PrintDeck",
        "firmware_version": "1.2.3",
        "hardware": "amoled_1_75",
        "device_id": "printdeck-a1b2c3d4e5f6",
        "name": "Workshop PrintDeck",
        "capabilities": {"snapshot": True, "home_assistant": True},
        "network": {
            "hostname": "printdeck-a1b2c3.local",
            "friendly_hostname": "printdeck-workshop.local",
        },
        "read_only": True,
    }


def printer_payload(
    *,
    printer_id: int = 4278190081,
    name: str = "Workshop printer",
    protocol: str = "moonraker",
    endpoint: str = "192.0.2.40:7125",
    phase: str = "printing",
    activity: str = "printing",
    network_address: str = "192.0.2.40",
    network_port: int = 7125,
) -> dict[str, object]:
    """Return one complete normalized printer item."""
    return {
        "printer": {
            "id": printer_id,
            "name": name,
            "protocol": protocol,
            "endpoint": endpoint,
            "network": {"address": network_address, "port": network_port},
            "manufacturer": "Voron",
            "model": "2.4",
            "selected": True,
        },
        "status": {
            "printer_id": printer_id,
            "connection": {
                "state": "online",
                "reachability": "online",
                "detail_level": "full",
                "stale": False,
            },
            "job": {
                "phase": phase,
                "activity": activity,
                "kind": "print",
                "name": "private-job-name.gcode",
                "progress_percent": 52.5,
                "elapsed_seconds": 800,
                "remaining_seconds": 700,
                "current_layer": 40,
                "total_layers": 90,
            },
            "temperatures": {
                "nozzle_current_c": 220.1,
                "nozzle_target_c": 220.0,
                "bed_current_c": 59.9,
                "bed_target_c": 60.0,
                "chamber_current_c": None,
            },
        },
    }


def mixed_printer_payloads() -> list[dict[str, object]]:
    """Use ordinary and future protocols with API-owned diagnostics."""
    printers = []
    for printer_id, (protocol, address, port) in enumerate(
        (
            ("moonraker", "192.0.2.40", 7125),
            ("bambu_lan", "192.0.2.41", 8883),
            ("prusalink", "192.0.2.42", 80),
            ("elegoo_sdcp", "192.0.2.43", 3030),
            ("elegoo_cc2", "192.0.2.44", 1883),
            ("uniformation_sdcp", "192.0.2.45", 3030),
            ("future_transport", "Tekst z API, nie adres IP.\nDruga linia.", 43210),
        ),
        start=1,
    ):
        item = printer_payload(
            printer_id=printer_id, protocol=protocol, endpoint="opaque:legacy:value",
            network_address=address, network_port=port,
        )
        if printer_id != 1:
            item["printer"]["selected"] = False
            item["status"]["connection"].update(
                state="offline", reachability="offline", detail_level="summary", stale=True
            )
            item["status"]["temperatures"] = {}
        printers.append(item)
    return printers


class ParserTests(unittest.TestCase):
    """Validate the normalized payload contract."""

    def test_info_requires_stable_device_identity(self) -> None:
        payload = info_payload()
        parsed = API.parse_info(payload)
        self.assertEqual(parsed.device_id, "printdeck-a1b2c3d4e5f6")
        self.assertEqual(parsed.name, "Workshop PrintDeck")
        self.assertEqual(parsed.hostname, "printdeck-a1b2c3.local")
        self.assertEqual(parsed.friendly_hostname, "printdeck-workshop.local")
        self.assertTrue(parsed.snapshot_supported)

        payload.pop("device_id")
        with self.assertRaises(API.PrintDeckUnsupportedError):
            API.parse_info(payload)

    def test_snapshot_preserves_measurements_and_identity(self) -> None:
        printer = API.parse_snapshot(
            {"api_version": "v1", "printers": [printer_payload()]}
        ).printers[0]

        self.assertEqual(printer.printer_id, "4278190081")
        self.assertEqual(printer.phase, "printing")
        self.assertEqual(printer.activity, "printing")
        self.assertEqual(printer.progress_percent, 52.5)
        self.assertEqual(printer.nozzle_current_c, 220.1)
        self.assertIsNone(printer.chamber_current_c)
        self.assertEqual(printer.network_address, "192.0.2.40")
        self.assertEqual(printer.network_port, 7125)

    def test_snapshot_exposes_device_battery_and_charging_state(self) -> None:
        snapshot = API.parse_snapshot(
            {
                "api_version": "v1",
                "device": {
                    "power": {
                        "available": True,
                        "battery_present": True,
                        "battery_percent": 39,
                        "charging": False,
                        "external_power": False,
                    }
                },
                "printers": [],
            }
        )

        self.assertTrue(snapshot.power.available)
        self.assertTrue(snapshot.power.battery_present)
        self.assertEqual(snapshot.power.battery_percent, 39)
        self.assertFalse(snapshot.power.charging)

    def test_network_diagnostics_are_used_exactly_as_supplied_by_the_api(self) -> None:
        for protocol in ("bambu_lan", "moonraker", "future_transport", ""):
            with self.subTest(protocol=protocol):
                item = printer_payload(
                    protocol=protocol, endpoint="http://ignored.local:1234",
                    network_address="Dowolny tekst z API.\nDruga linia.",
                    network_port=43210,
                )
                parsed = API.parse_printer(item["printer"], item["status"])
                self.assertEqual(parsed.protocol, protocol)
                self.assertEqual(parsed.network_address, "Dowolny tekst z API.\nDruga linia.")
                self.assertEqual(parsed.network_port, 43210)

    def test_missing_network_metadata_does_not_require_firmware_update(self) -> None:
        items = mixed_printer_payloads()
        for item in items:
            item["printer"].pop("network")
        snapshot = API.parse_snapshot({"api_version": "v1", "printers": items})
        self.assertEqual(len(snapshot.printers), len(items))
        for printer in snapshot.printers:
            self.assertIsNone(printer.network_address)
            self.assertIsNone(printer.network_port)
            self.assertEqual(printer.progress_percent, 52.5)

    def test_optional_network_metadata_does_not_block_printer_state(self) -> None:
        for network, address, port in (
            (None, None, None),
            ({}, None, None),
            ({"address": None, "port": None}, None, None),
            ({"address": "API text", "port": None}, "API text", None),
            ({"port": 43210}, None, 43210),
            ({"address": 42, "port": True}, None, None),
            ({"address": "API text", "port": "unavailable"}, "API text", None),
            ([], None, None),
        ):
            with self.subTest(network=network):
                item = printer_payload(protocol="future_transport")
                item["printer"]["network"] = network
                item["printer"].pop("endpoint")
                parsed = API.parse_printer(item["printer"], item["status"])
                self.assertEqual(parsed.network_address, address)
                self.assertEqual(parsed.network_port, port)
                self.assertEqual(parsed.progress_percent, 52.5)

    def test_identical_printer_names_keep_distinct_stable_identities(self) -> None:
        device_id = "printdeck-a1b2c3d4e5f6"
        printer_ids = [str(1000 + index) for index in range(10)]
        unique_ids = {
            IDENTITY.printer_entity_unique_id(device_id, printer_id, "progress")
            for printer_id in printer_ids
        }

        self.assertEqual(len(unique_ids), 10)

    def test_mixed_protocols_preserve_all_printers_in_both_response_formats(self) -> None:
        items = mixed_printer_payloads()
        snapshot = API.parse_snapshot({"api_version": "v1", "printers": items})
        legacy = API.parse_legacy_snapshot(
            {"printers": [item["printer"] for item in items]},
            {"statuses": [item["status"] for item in reversed(items)]},
        )
        self.assertEqual(snapshot.printers, legacy)
        self.assertEqual(len(snapshot.printers), 7)
        self.assertEqual(
            [printer.network_port for printer in snapshot.printers],
            [7125, 8883, 80, 3030, 1883, 3030, 43210],
        )
        self.assertEqual(snapshot.printers[0].progress_percent, 52.5)
        for index, printer in enumerate(snapshot.printers, start=1):
            self.assertEqual(printer.printer_id, str(index))
            if index != 1:
                self.assertFalse(printer.selected)
                self.assertEqual(printer.connection_state, "offline")
                self.assertIsNone(printer.nozzle_current_c)

    def test_endpoint_change_does_not_change_entity_identity(self) -> None:
        before = API.parse_snapshot(
            {
                "api_version": "v1",
                "printers": [printer_payload(endpoint="192.0.2.40:7125")],
            }
        ).printers[0]
        after = API.parse_snapshot(
            {
                "api_version": "v1",
                "printers": [printer_payload(endpoint="192.0.2.99:8125", network_address="192.0.2.99", network_port=8125)],
            }
        ).printers[0]

        before_unique_id = IDENTITY.printer_entity_unique_id(
            "printdeck-a1b2c3d4e5f6", before.printer_id, "progress"
        )
        after_unique_id = IDENTITY.printer_entity_unique_id(
            "printdeck-a1b2c3d4e5f6", after.printer_id, "progress"
        )
        self.assertEqual(before_unique_id, after_unique_id)
        self.assertNotEqual(before.network_address, after.network_address)
        self.assertNotEqual(before.network_port, after.network_port)

    def test_only_missing_child_devices_are_marked_for_removal(self) -> None:
        device_id = "printdeck-a1b2c3d4e5f6"
        current_printer_ids = {"101", "102"}

        self.assertFalse(
            IDENTITY.device_belongs_to_missing_printer(
                device_id, current_printer_ids, {("printdeck", device_id)}
            )
        )
        self.assertFalse(
            IDENTITY.device_belongs_to_missing_printer(
                device_id,
                current_printer_ids,
                {("printdeck", f"{device_id}:101")},
            )
        )
        self.assertTrue(
            IDENTITY.device_belongs_to_missing_printer(
                device_id,
                current_printer_ids,
                {("printdeck", f"{device_id}:103")},
            )
        )

    def test_every_published_phase_and_activity_is_accepted(self) -> None:
        for phase in CONST.PHASE_OPTIONS:
            parsed = API.parse_snapshot(
                {
                    "api_version": "v1",
                    "printers": [printer_payload(phase=phase)],
                }
            ).printers[0]
            self.assertEqual(parsed.phase, phase)

        for activity in CONST.ACTIVITY_OPTIONS:
            parsed = API.parse_snapshot(
                {
                    "api_version": "v1",
                    "printers": [printer_payload(activity=activity)],
                }
            ).printers[0]
            self.assertEqual(parsed.activity, activity)

    def test_snapshot_rejects_mismatched_printer_identity(self) -> None:
        item = printer_payload()
        item["status"]["printer_id"] = 7
        with self.assertRaises(API.PrintDeckInvalidResponseError):
            API.parse_snapshot({"api_version": "v1", "printers": [item]})

    def test_legacy_endpoints_join_by_printer_id(self) -> None:
        item = printer_payload()
        parsed = API.parse_legacy_snapshot(
            {"printers": [item["printer"]]},
            {"statuses": [item["status"]]},
        )
        self.assertEqual(parsed[0].name, "Workshop printer")


class ClientTests(unittest.IsolatedAsyncioTestCase):
    """Validate request aggregation and HTTP errors."""

    async def asyncSetUp(self) -> None:
        API.MINIMUM_REQUEST_INTERVAL_SECONDS = 0.0

    async def test_aggregate_snapshot_uses_one_status_request(self) -> None:
        session = FakeSession(
            [
                FakeResponse(200, info_payload()),
                FakeResponse(
                    200,
                    {"api_version": "v1", "printers": mixed_printer_payloads()},
                ),
            ]
        )
        client = API.PrintDeckApiClient(session, "printdeck.local", "pd_secret")

        await client.async_get_info()
        snapshot = await client.async_get_snapshot()

        self.assertEqual(len(snapshot.printers), 7)
        self.assertEqual(
            [request[0] for request in session.requests],
            [
                "http://printdeck.local/v1/info",
                "http://printdeck.local/v1/snapshot",
            ],
        )
        self.assertEqual(session.requests[0][1]["Authorization"], "Bearer pd_secret")

    async def test_redirect_never_contacts_a_printer_or_another_host(self) -> None:
        session = FakeSession([
            FakeResponse(302, {}, {"Location": "http://printer.local/status"}),
        ])
        client = API.PrintDeckApiClient(session, "printdeck.local", "pd_secret")
        with self.assertRaises(API.PrintDeckCannotConnectError):
            await client.async_get_snapshot()
        self.assertEqual(len(session.requests), 1)
        self.assertEqual(session.requests[0][0], "http://printdeck.local/v1/snapshot")

    async def test_missing_snapshot_falls_back_to_legacy_endpoints(self) -> None:
        item = printer_payload()
        session = FakeSession(
            [
                FakeResponse(404, {"error": "not_found"}),
                FakeResponse(200, {"printers": [item["printer"]]}),
                FakeResponse(200, {"statuses": [item["status"]]}),
            ]
        )
        client = API.PrintDeckApiClient(session, "192.0.2.170", "pd_secret")

        snapshot = await client.async_get_snapshot()

        self.assertEqual(snapshot.printers[0].phase, "printing")
        self.assertEqual(
            [request[0].rsplit("/", 1)[-1] for request in session.requests],
            ["snapshot", "printers", "status"],
        )

    async def test_authentication_error_is_specific(self) -> None:
        client = API.PrintDeckApiClient(
            FakeSession([FakeResponse(401, {"error": "unauthorized"})]),
            "printdeck.local",
            "wrong",
        )
        with self.assertRaises(API.PrintDeckAuthenticationError):
            await client.async_get_info()

    async def test_rate_limit_retries_once(self) -> None:
        session = FakeSession(
            [
                FakeResponse(429, {"error": "rate_limited"}, {"Retry-After": "0"}),
                FakeResponse(200, info_payload()),
            ]
        )
        client = API.PrintDeckApiClient(session, "printdeck.local", "pd_secret")

        parsed = await client.async_get_info()

        self.assertEqual(parsed.firmware_version, "1.2.3")
        self.assertEqual(len(session.requests), 2)


if __name__ == "__main__":
    unittest.main()

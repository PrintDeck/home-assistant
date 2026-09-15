"""Contract and recovery tests for MQTT without a running broker."""

from __future__ import annotations

import importlib
import json
import unittest
from copy import deepcopy

from test_api import API, IDENTITY, info_payload, printer_payload

MQTT = importlib.import_module("printdeck.mqtt_state")
ROOT = "printdeck/printdeck-a1b2c3d4e5f6/v1"


def mqtt_info(**options: bool) -> dict:
    value = info_payload()
    value["mqtt"] = {
        "home_assistant_discovery": False,
        "discovery_cleanup_pending": False,
        **options,
    }
    return value


class MqttContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = MQTT.PrintDeckMqttState(ROOT)
        self.item = printer_payload()

    def send(
        self, topic: str, value: object, retained: bool = False, now: float = 100
    ) -> bool:
        if isinstance(value, dict) and topic != "info":
            value = {"_mqtt_generation": "a1b2c3d4-00000001", **value}
        text = value if isinstance(value, str) else json.dumps(value)
        return self.state.ingest(f"{ROOT}/{topic}", text, retained, now)

    def initialize(self) -> None:
        self.send("info", mqtt_info(), True)
        self.send(
            "printers", {"api_version": "v1", "printers": [self.item["printer"]]}, True
        )
        self.send("availability", "online", True)
        self.send("device", {"api_version": "v1", "device": {"power": {}}})
        self.send(
            f"printers/{self.item['printer']['id']}/status",
            {"api_version": "v1", "status": self.item["status"]},
        )

    def test_generation_change_requires_matching_fresh_status_and_power(self) -> None:
        self.initialize()
        next_generation = "a1b2c3d4-00000002"
        self.send(
            "printers",
            {
                "api_version": "v1",
                "_mqtt_generation": next_generation,
                "printers": [self.item["printer"]],
            },
            True,
        )
        self.assertIsNone(self.state.snapshot(100))
        self.assertFalse(self.send("device", {"api_version": "v1", "device": {}}))
        self.assertFalse(
            self.send(
                f"printers/{self.item['printer']['id']}/status",
                {"api_version": "v1", "status": self.item["status"]},
            )
        )
        self.assertIsNone(self.state.snapshot(100))
        self.send(
            "device",
            {"api_version": "v1", "_mqtt_generation": next_generation, "device": {}},
        )
        self.send(
            f"printers/{self.item['printer']['id']}/status",
            {
                "api_version": "v1",
                "_mqtt_generation": next_generation,
                "status": self.item["status"],
            },
        )
        self.assertIsNotNone(self.state.snapshot(100))
        for generation in (None, 1, "wrong", "a" * 100):
            with self.assertRaises(API.PrintDeckInvalidResponseError):
                self.send(
                    "printers",
                    {
                        "api_version": "v1",
                        "_mqtt_generation": generation,
                        "printers": [],
                    },
                )
        self.assertIsNotNone(self.state.snapshot(100))

    def test_same_contract_and_identity_as_http(self) -> None:
        self.initialize()
        actual = self.state.snapshot(100)
        expected = API.parse_snapshot({"api_version": "v1", "printers": [self.item]})
        self.assertEqual(actual, expected)
        self.assertEqual(
            IDENTITY.printer_entity_unique_id(
                self.state.info.device_id, actual.printers[0].printer_id, "progress"
            ),
            "printdeck-a1b2c3d4e5f6_4278190081_progress",
        )

    def test_null_measurements_remain_unknown_in_both_transports(self) -> None:
        fields = (
            "progress_percent",
            "remaining_seconds",
            "elapsed_seconds",
            "current_layer",
            "total_layers",
        )
        for field in fields:
            self.item["status"]["job"][field] = None
        self.initialize()
        for field in fields:
            self.assertIsNone(getattr(self.state.snapshot(100).printers[0], field))
        for field in fields:
            self.item["status"]["job"].pop(field)
        parsed = API.parse_printer(self.item["printer"], self.item["status"])
        for field in fields:
            self.assertIsNone(getattr(parsed, field))

    def test_retained_identity_does_not_make_stale_telemetry_live(self) -> None:
        self.send("info", mqtt_info(), True)
        self.send(
            "printers", {"api_version": "v1", "printers": [self.item["printer"]]}, True
        )
        self.send("availability", "online", True)
        self.assertFalse(self.send("device", {"api_version": "v1", "device": {}}, True))
        self.assertFalse(
            self.send(
                f"printers/{self.item['printer']['id']}/status",
                {"api_version": "v1", "status": self.item["status"]},
                True,
            )
        )
        self.assertIsNone(self.state.snapshot(100))
        self.assertEqual(self.state.statuses, {})

    def test_offline_and_broker_reconnect_require_new_complete_state(self) -> None:
        self.initialize()
        self.assertIsNotNone(self.state.snapshot(100))
        self.send("availability", "offline")
        self.assertIsNone(self.state.snapshot(100))
        self.send("availability", "online")
        self.assertIsNone(self.state.snapshot(100))
        self.assertEqual(self.state.statuses, {})
        self.initialize()
        self.assertIsNotNone(self.state.snapshot(100))
        self.state.invalidate_live()
        self.assertIsNone(self.state.snapshot(100))

    def test_each_telemetry_topic_has_its_own_expiry(self) -> None:
        self.initialize()
        self.assertIsNotNone(self.state.snapshot(189))
        self.send("device", {"api_version": "v1", "device": {}}, now=189)
        self.assertIsNone(self.state.snapshot(190))
        self.send(
            f"printers/{self.item['printer']['id']}/status",
            {"api_version": "v1", "status": self.item["status"]},
            now=191,
        )
        self.assertIsNotNone(self.state.snapshot(191))
        self.assertIsNone(self.state.snapshot(279))

    def test_conflict_and_cleanup_pending_block_setup_and_recover(self) -> None:
        for option in ("home_assistant_discovery", "discovery_cleanup_pending"):
            self.initialize()
            with self.assertRaises(MQTT.PrintDeckDiscoveryConflict):
                self.send("info", mqtt_info(**{option: True}), True)
            self.assertIsNone(self.state.snapshot(100))
            self.assertTrue(self.state.conflict)
            self.initialize()
            self.assertIsNotNone(self.state.snapshot(100))

    def test_catalog_removal_is_authoritative_only_after_valid_complete_catalog(
        self,
    ) -> None:
        self.initialize()
        with self.assertRaises(API.PrintDeckInvalidResponseError):
            self.send("printers", {"api_version": "v1", "printers": [None]})
        self.assertEqual(len(self.state.snapshot(100).printers), 1)
        self.send("printers", {"api_version": "v1", "printers": []}, True)
        self.assertEqual(self.state.snapshot(100).printers, ())
        self.assertEqual(self.state.statuses, {})

    def test_new_profile_waits_for_its_live_status_before_complete_snapshot(
        self,
    ) -> None:
        self.initialize()
        added = printer_payload(printer_id=2)
        self.send(
            "printers",
            {"api_version": "v1", "printers": [self.item["printer"], added["printer"]]},
        )
        self.assertIsNone(self.state.snapshot(100))
        self.send("printers/2/status", {"api_version": "v1", "status": added["status"]})
        self.assertEqual(len(self.state.snapshot(100).printers), 2)

    def test_ten_printers_and_unconfigured_statuses_do_not_grow_cache(self) -> None:
        items = [printer_payload(printer_id=i + 1) for i in range(11)]
        self.send("info", mqtt_info())
        self.send(
            "printers",
            {"api_version": "v1", "printers": [i["printer"] for i in items[:10]]},
        )
        for item in items[:10]:
            self.send(
                f"printers/{item['printer']['id']}/status",
                {"api_version": "v1", "status": item["status"]},
            )
        for pid in range(11, 1000):
            self.assertFalse(
                self.send(
                    f"printers/{pid}/status",
                    {"api_version": "v1", "status": items[-1]["status"]},
                )
            )
        self.assertEqual(len(self.state.statuses), 10)
        with self.assertRaises(API.PrintDeckInvalidResponseError):
            self.send(
                "printers",
                {"api_version": "v1", "printers": [i["printer"] for i in items]},
            )
        self.assertEqual(len(self.state.profiles), 10)

    def test_wrong_identity_and_status_wrapper_rejected_without_mutation(self) -> None:
        self.initialize()
        info = mqtt_info()
        info["device_id"] = "printdeck-other"
        with self.assertRaises(API.PrintDeckInvalidResponseError):
            self.send("info", info)
        status = deepcopy(self.item["status"])
        status["printer_id"] = 2
        with self.assertRaises(API.PrintDeckInvalidResponseError):
            self.send(
                f"printers/{self.item['printer']['id']}/status",
                {"api_version": "v1", "status": status},
            )
        self.assertEqual(self.state.snapshot(100).printers[0].progress_percent, 52.5)
        for invalid in (
            "printdeck/+/v1",
            "printdeck/#",
            "a/b",
            ROOT + "/info",
            ROOT + "\0",
        ):
            with self.assertRaises(ValueError):
                MQTT.validate_topic_root(invalid)

    def test_payload_size_utf8_nonfinite_and_duplicate_ids_are_bounded(self) -> None:
        for raw in (
            "[]",
            '{"api_version":"v1","x":NaN}',
            '{"api_version":"v1","x":Infinity}',
            '{"api_version":"v1","x":"' + "界" * 22000 + '"}',
            '{"api_version":"v2"}',
            b"\xff",
        ):
            with self.assertRaises(API.PrintDeckInvalidResponseError):
                MQTT.decode_payload(raw)
        self.initialize()
        with self.assertRaises(API.PrintDeckInvalidResponseError):
            self.send(
                "printers",
                {"api_version": "v1", "printers": [self.item["printer"]] * 2},
            )
        self.item["status"]["job"]["progress_percent"] = float("inf")
        with self.assertRaises(API.PrintDeckInvalidResponseError):
            API.parse_printer(self.item["printer"], self.item["status"])


if __name__ == "__main__":
    unittest.main()

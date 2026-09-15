"""Focused lifecycle tests with a controlled HA event loop and MQTT callbacks.

These tests exercise PrintDeck code, not a substitute Home Assistant runtime.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_api import API
from test_mqtt import ROOT, mqtt_info, printer_payload


class UpdateFailed(Exception):
    pass


class ConfigEntryNotReady(Exception):
    pass


class AbortFlow(Exception):
    pass


class Coordinator:
    def __class_getitem__(cls, _item):
        return cls

    def __init__(self, hass, logger, **kwargs):
        self.hass = hass
        self.config_entry = kwargs["config_entry"]
        self.data = None
        self.last_update_success = True
        self.removed = []

    def async_set_update_error(self, error):
        self.last_update_success = False

    def async_set_updated_data(self, data):
        self.data = data
        self.last_update_success = True


class ConfigFlow:
    def __init_subclass__(cls, **kwargs):
        pass

    async def async_set_unique_id(self, unique_id):
        self.unique_id = unique_id

    def async_show_menu(self, **kwargs):
        return {"type": "menu", **kwargs}

    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}

    def _get_reconfigure_entry(self):
        return self.entry

    def _abort_if_unique_id_mismatch(self):
        if self.unique_id != self.entry.unique_id:
            raise AbortFlow("wrong_device")

    def _async_current_entries(self):
        return getattr(self, "entries", [])

    def _abort_if_unique_id_configured(self, **kwargs):
        self.last_updates = kwargs.get("updates")
        if getattr(self, "configured_id", None) == self.unique_id:
            raise AbortFlow("already_configured")

    def async_update_reload_and_abort(self, entry, **kwargs):
        return {
            "type": "abort",
            "reason": "reconfigure_successful",
            "entry": entry,
            **kwargs,
        }


def install_stubs():
    def module(name, **kwargs):
        mod = ModuleType(name)
        mod.__path__ = []
        for key, value in kwargs.items():
            setattr(mod, key, value)
        sys.modules[name] = mod
        if "." in name:
            parent, child = name.rsplit(".", 1)
            if parent in sys.modules:
                setattr(sys.modules[parent], child, mod)
        return mod

    module("homeassistant")
    module("homeassistant.components")
    module("homeassistant.components.mqtt", ReceiveMessage=SimpleNamespace)
    module(
        "homeassistant.config_entries",
        ConfigEntry=SimpleNamespace,
        ConfigFlow=ConfigFlow,
        ConfigFlowResult=dict,
    )
    module("homeassistant.core", HomeAssistant=SimpleNamespace, callback=lambda f: f)
    module("homeassistant.const", CONF_HOST="host")
    module(
        "homeassistant.exceptions",
        ConfigEntryNotReady=ConfigEntryNotReady,
        ConfigEntryAuthFailed=Exception,
        HomeAssistantError=type("HomeAssistantError", (Exception,), {}),
    )
    module("homeassistant.helpers")
    module(
        "homeassistant.helpers.device_registry",
        async_get=lambda _: None,
        async_entries_for_config_entry=lambda *_: [],
    )
    module(
        "homeassistant.helpers.entity_registry", async_get=lambda hass: hass.registry
    )
    module(
        "homeassistant.helpers.issue_registry",
        IssueSeverity=SimpleNamespace(WARNING="warning"),
        async_create_issue=lambda hass, domain, issue, **kwargs: hass.issues.add(issue),
        async_delete_issue=lambda hass, domain, issue: hass.issues.discard(issue),
    )
    module(
        "homeassistant.helpers.event",
        async_track_time_interval=lambda hass, cb, interval: lambda: None,
    )
    module(
        "homeassistant.helpers.update_coordinator",
        DataUpdateCoordinator=Coordinator,
        UpdateFailed=UpdateFailed,
    )
    module(
        "homeassistant.helpers.aiohttp_client",
        async_get_clientsession=lambda hass: None,
    )
    module(
        "homeassistant.helpers.selector",
        TextSelector=lambda *a, **kw: str,
        TextSelectorConfig=lambda **kw: kw,
        TextSelectorType=SimpleNamespace(TEXT="text", PASSWORD="password"),
    )
    module("homeassistant.helpers.service_info")
    module(
        "homeassistant.helpers.service_info.zeroconf",
        ZeroconfServiceInfo=SimpleNamespace,
    )
    # Form rendering is covered structurally; the selector implementation belongs to HA.
    module("voluptuous", Schema=lambda v: v, Required=lambda key, **kw: key)


install_stubs()
COORD = importlib.import_module("printdeck.mqtt_coordinator")
FLOW = importlib.import_module("printdeck.config_flow")


class MqttHarness:
    def __init__(self, hass):
        self.hass = hass
        self.callbacks = {}
        self.cancelled = 0
        self.connection_callback = None
        self.fail_at = None

    async def subscribe(self, hass, topic, callback, qos=0):
        if self.fail_at == topic:
            raise RuntimeError("Subscribe failed")
        self.callbacks[topic] = callback

        def remove():
            self.cancelled += 1
            self.callbacks.pop(topic, None)

        return remove

    def connection(self, hass, callback):
        self.connection_callback = callback

        def remove():
            self.cancelled += 1
            self.connection_callback = None

        return remove

    def emit(self, suffix, payload, retain=False):
        topic = ROOT + "/" + suffix
        if isinstance(payload, dict) and suffix != "info":
            payload = {"_mqtt_generation": "a1b2c3d4-00000001", **payload}
        value = payload if isinstance(payload, str) else json.dumps(payload)
        message = SimpleNamespace(topic=topic, payload=value, retain=retain)
        callback = self.callbacks.get(topic) or self.callbacks.get(
            ROOT + "/printers/+/status"
        )
        assert callback is not None, topic
        callback(message)

    def complete(self):
        item = printer_payload()
        self.emit("info", mqtt_info(), True)
        self.emit(
            "printers", {"api_version": "v1", "printers": [item["printer"]]}, True
        )
        self.emit("device", {"api_version": "v1", "device": {}})
        self.emit(
            f"printers/{item['printer']['id']}/status",
            {"api_version": "v1", "status": item["status"]},
        )
        self.emit("availability", "online", True)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hass = SimpleNamespace(
            loop=asyncio.get_running_loop(),
            registry=SimpleNamespace(entities={}),
            issues=set(),
        )
        self.entry = SimpleNamespace(
            entry_id="test",
            unique_id="printdeck-a1b2c3d4e5f6",
            data={"transport": "mqtt", "topic_root": ROOT},
        )
        self.bus = MqttHarness(self.hass)
        self.patches = [
            patch.object(
                COORD.mqtt,
                "async_wait_for_mqtt_client",
                AsyncMock(return_value=True),
                create=True,
            ),
            patch.object(
                COORD.mqtt, "async_subscribe", self.bus.subscribe, create=True
            ),
            patch.object(
                COORD.mqtt,
                "async_subscribe_connection_status",
                self.bus.connection,
                create=True,
            ),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    async def start_pending(self):
        coordinator = COORD.PrintDeckMqttCoordinator(self.hass, self.entry)
        self.addCleanup(coordinator.async_stop)
        task = asyncio.create_task(coordinator.async_start())
        await asyncio.sleep(0)
        return coordinator, task

    async def test_setup_runtime_disconnect_recovery_and_unload(self):
        coordinator, task = await self.start_pending()
        self.assertFalse(task.done())
        self.assertEqual(len(self.bus.callbacks), 5)
        self.bus.complete()
        await task
        self.assertTrue(coordinator.last_update_success)
        self.assertEqual(coordinator.configuration_url, "http://printdeck-a1b2c3.local")
        self.bus.connection_callback(False)
        self.assertFalse(coordinator.last_update_success)
        self.bus.connection_callback(True)
        self.assertFalse(coordinator.last_update_success)
        self.bus.complete()
        self.assertTrue(coordinator.last_update_success)
        coordinator.async_stop()
        self.assertEqual(self.bus.callbacks, {})
        self.assertIsNone(self.bus.connection_callback)
        self.assertEqual(self.bus.cancelled, 6)

    async def test_failed_and_cancelled_setup_removes_all_subscriptions(self):
        self.bus.fail_at = ROOT + "/device"
        _coordinator, task = await self.start_pending()
        with self.assertRaises(RuntimeError):
            await task
        self.assertEqual(self.bus.callbacks, {})
        self.assertIsNone(self.bus.connection_callback)
        self.bus.fail_at = None
        _coordinator, task = await self.start_pending()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.bus.callbacks, {})
        self.assertIsNone(self.bus.connection_callback)

    async def test_startup_timeout_is_retryable_and_cleans_up(self):
        with patch.object(COORD, "INITIAL_STATE_TIMEOUT", 0.001):
            _coordinator, task = await self.start_pending()
            with self.assertRaises(ConfigEntryNotReady):
                await task
        self.assertEqual(self.bus.callbacks, {})

    async def test_registry_conflict_blocks_setup_even_with_firmware_flag_off(self):
        self.hass.registry.entities["sensor.old"] = SimpleNamespace(
            platform="mqtt", unique_id="printdeck-a1b2c3d4e5f6_1_phase"
        )
        _coordinator, task = await self.start_pending()
        self.bus.complete()
        with self.assertRaises(ConfigEntryNotReady):
            await task
        self.assertTrue(self.hass.issues)
        self.assertEqual(self.bus.callbacks, {})
        self.hass.registry.entities.clear()
        _coordinator, task = await self.start_pending()
        self.bus.complete()
        await task
        self.assertFalse(self.hass.issues)

    async def test_runtime_discovery_conflict_creates_repair_then_recovers(self):
        coordinator, task = await self.start_pending()
        self.bus.complete()
        await task
        self.bus.emit("info", mqtt_info(home_assistant_discovery=True), True)
        self.assertFalse(coordinator.last_update_success)
        self.assertTrue(self.hass.issues)
        self.bus.complete()
        self.assertTrue(coordinator.last_update_success)
        self.assertFalse(self.hass.issues)

    async def test_mqtt_flow_preserves_entry_identity_and_removes_http_credentials(
        self,
    ):
        flow = FLOW.PrintDeckConfigFlow()
        flow.hass = self.hass
        flow.entry = self.entry
        result = await flow.async_step_user()
        self.assertEqual(result["menu_options"], ["http", "mqtt"])
        await flow.async_step_reconfigure()
        task = asyncio.create_task(flow.async_step_mqtt({"topic_root": ROOT}))
        await asyncio.sleep(0)
        self.bus.emit("info", mqtt_info(), True)
        result = await task
        self.assertEqual(result["data"], {"transport": "mqtt", "topic_root": ROOT})
        self.assertEqual(result["reason"], "reconfigure_successful")
        self.assertIs(result["entry"], self.entry)
        self.assertEqual(self.bus.callbacks, {})
        flow._async_validate = AsyncMock(return_value=API.parse_info(mqtt_info()))
        result = await flow.async_step_http(
            {"host": "printdeck.local", "token": "test-token"}
        )
        self.assertEqual(
            result["data"],
            {"host": "printdeck.local", "token": "test-token", "transport": "http"},
        )
        self.assertNotIn("topic_root", result["data"])

    async def test_flow_conflict_invalid_root_duplicate_and_wrong_device(self):
        flow = FLOW.PrintDeckConfigFlow()
        flow.hass = self.hass
        result = await flow.async_step_mqtt({"topic_root": "printdeck/#"})
        self.assertEqual(result["errors"]["base"], "invalid_topic_root")
        task = asyncio.create_task(flow.async_step_mqtt({"topic_root": ROOT}))
        await asyncio.sleep(0)
        self.bus.emit("info", mqtt_info(discovery_cleanup_pending=True), True)
        result = await task
        self.assertEqual(result["errors"]["base"], "mqtt_discovery_conflict")
        flow._async_mqtt_info = AsyncMock(return_value=API.parse_info(mqtt_info()))
        flow.configured_id = self.entry.unique_id
        with self.assertRaises(AbortFlow):
            await flow.async_step_mqtt({"topic_root": ROOT})
        flow.entry = SimpleNamespace(unique_id="printdeck-other", data={})
        await flow.async_step_reconfigure()
        with self.assertRaises(AbortFlow):
            await flow.async_step_mqtt({"topic_root": ROOT})

    async def discovered_flow(self):
        flow = FLOW.PrintDeckConfigFlow()
        flow.hass = self.hass
        flow.context = {}
        result = await flow.async_step_zeroconf(SimpleNamespace(
            host="printdeck.local.", properties={"id": self.entry.unique_id}
        ))
        self.assertEqual(result["menu_options"], ["http", "mqtt"])
        return flow

    async def test_discovered_mqtt_connects_without_a_topic_form(self):
        flow = await self.discovered_flow()
        flow._async_mqtt_info = AsyncMock(return_value=API.parse_info(mqtt_info()))
        result = await flow.async_step_mqtt()
        flow._async_mqtt_info.assert_awaited_once_with(ROOT)
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"], {"transport": "mqtt", "topic_root": ROOT})

    async def test_discovered_mqtt_failure_retries_without_asking_for_topic(self):
        flow = await self.discovered_flow()
        for error, expected in [
            (FLOW.PrintDeckCannotConnectError(), "mqtt_not_ready"),
            (TimeoutError(), "mqtt_no_device"),
            (FLOW.PrintDeckDiscoveryConflict(), "mqtt_discovery_conflict"),
        ]:
            flow._async_mqtt_info = AsyncMock(side_effect=error)
            result = await flow.async_step_mqtt()
            self.assertEqual(result["step_id"], "mqtt_auto")
            self.assertEqual(result["data_schema"], {})
            self.assertEqual(result["errors"], {"base": expected})
        flow._async_mqtt_info = AsyncMock(return_value=API.parse_info(mqtt_info()))
        result = await flow.async_step_mqtt_auto({})
        self.assertEqual(result["type"], "create_entry")
        flow._async_mqtt_info.assert_awaited_once_with(ROOT)

    async def test_http_reconfigure_automatically_uses_existing_identity(self):
        flow = FLOW.PrintDeckConfigFlow()
        flow.hass = self.hass
        flow.entry = SimpleNamespace(
            unique_id=self.entry.unique_id,
            data={"host": "printdeck.local", "token": "test-token"},
        )
        flow._async_mqtt_info = AsyncMock(return_value=API.parse_info(mqtt_info()))
        await flow.async_step_reconfigure()
        result = await flow.async_step_mqtt()
        flow._async_mqtt_info.assert_awaited_once_with(ROOT)
        self.assertEqual(result["reason"], "reconfigure_successful")
        self.assertEqual(result["data"], {"transport": "mqtt", "topic_root": ROOT})

    async def test_manual_mqtt_keeps_field_and_preserves_input_on_error(self):
        flow = FLOW.PrintDeckConfigFlow()
        flow.hass = self.hass
        flow._async_mqtt_info = AsyncMock(side_effect=TimeoutError())
        with patch.object(FLOW.vol, "Required", return_value="topic_root") as required:
            result = await flow.async_step_mqtt()
            self.assertEqual(result["step_id"], "mqtt")
            required.assert_called_with("topic_root", default="")
            flow._async_mqtt_info.assert_not_awaited()
            result = await flow.async_step_mqtt({"topic_root": ROOT})
            self.assertEqual(result["step_id"], "mqtt")
            self.assertEqual(result["errors"], {"base": "mqtt_no_device"})
            required.assert_called_with("topic_root", default=ROOT)

    async def test_discovered_mqtt_rejects_another_device_identity(self):
        flow = await self.discovered_flow()
        payload = mqtt_info()
        payload["device_id"] = "printdeck-other"
        flow._async_mqtt_info = AsyncMock(return_value=API.parse_info(payload))
        result = await flow.async_step_mqtt()
        self.assertEqual(result["step_id"], "mqtt_auto")
        self.assertEqual(result["errors"], {"base": "invalid_response"})

    async def test_invalid_discovery_cannot_supply_an_mqtt_subscription(self):
        for identity in (None, 123, "printdeck-", "printdeck-+/v1", "printdeck-#", "other"):
            flow = FLOW.PrintDeckConfigFlow()
            flow.hass = self.hass
            flow.context = {}
            flow.async_abort = lambda **kwargs: kwargs
            result = await flow.async_step_zeroconf(SimpleNamespace(
                host="printdeck.local", properties={"id": identity}
            ))
            self.assertEqual(result["reason"], "invalid_discovery")
            self.assertIsNone(flow._known_mqtt_root())

    async def test_manual_entity_refresh_reads_cache_without_http(self):
        coordinator, task = await self.start_pending()
        self.bus.complete()
        await task
        self.assertEqual(await coordinator._async_update_data(), coordinator.data)
        self.bus.emit("availability", "offline")
        with self.assertRaises(UpdateFailed):
            await coordinator._async_update_data()

    async def test_mdns_updates_legacy_http_host_without_mutating_mqtt_entry(self):
        for data, expected in [
            ({"host": "old.local", "token": "secret"}, {"host": "new.local"}),
            ({"transport": "mqtt", "topic_root": ROOT}, None),
        ]:
            flow = FLOW.PrintDeckConfigFlow()
            flow.hass = self.hass
            flow.entries = [SimpleNamespace(unique_id=self.entry.unique_id, data=data)]
            flow.configured_id = self.entry.unique_id
            discovery = SimpleNamespace(
                host="new.local", properties={"id": self.entry.unique_id}
            )
            with self.assertRaises(AbortFlow):
                await flow.async_step_zeroconf(discovery)
            self.assertEqual(flow.last_updates, expected)

    async def test_http_add_and_reconfigure_are_blocked_by_generic_mqtt_entities(self):
        flow = FLOW.PrintDeckConfigFlow()
        flow.hass = self.hass
        flow.entry = self.entry
        flow._async_validate = AsyncMock(return_value=API.parse_info(mqtt_info()))
        self.hass.registry.entities["sensor.old"] = SimpleNamespace(
            platform="mqtt", unique_id="printdeck-a1b2c3d4e5f6_1_phase"
        )
        for reconfigure in (False, True):
            flow._reconfiguring = reconfigure
            result = await flow.async_step_http(
                {"host": "printdeck.local", "token": "test-token"}
            )
            self.assertEqual(result["errors"]["base"], "mqtt_discovery_conflict")

    async def test_http_setup_and_poll_do_not_overlap_generic_mqtt_entities(self):
        http_module = importlib.import_module("printdeck.coordinator")
        client = SimpleNamespace(
            async_get_info=AsyncMock(return_value=API.parse_info(mqtt_info())),
            async_get_snapshot=AsyncMock(
                return_value=API.PrintDeckSnapshot(
                    API.PrintDeckPower(False, False, None, False, False), ()
                )
            ),
        )
        coordinator = http_module.PrintDeckCoordinator(self.hass, self.entry, client)
        self.hass.registry.entities["sensor.old"] = SimpleNamespace(
            platform="mqtt", unique_id="printdeck-a1b2c3d4e5f6_1_phase"
        )
        with self.assertRaises(UpdateFailed):
            await coordinator._async_setup()
        self.assertTrue(self.hass.issues)
        with self.assertRaises(UpdateFailed):
            await coordinator._async_update_data()
        client.async_get_snapshot.assert_not_awaited()
        self.hass.registry.entities.clear()
        await coordinator._async_setup()
        await coordinator._async_update_data()
        client.async_get_snapshot.assert_awaited_once()
        self.assertFalse(self.hass.issues)

    async def test_no_mqtt_client_does_not_create_subscriptions(self):
        COORD.mqtt.async_wait_for_mqtt_client.return_value = False
        _coordinator, task = await self.start_pending()
        with self.assertRaises(ConfigEntryNotReady):
            await task
        self.assertEqual(self.bus.callbacks, {})


if __name__ == "__main__":
    unittest.main()

"""Exercise sensor availability for periodic printer observations."""
from __future__ import annotations

from dataclasses import dataclass, replace
import importlib.util
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from test_api import API, PUBLIC_COMPONENT, printer_payload


def load_sensors():
    """Load the real entity classes with a minimal HA framework boundary."""
    modules = {}

    def module(name, **attrs):
        value = ModuleType(name)
        value.__dict__.update(attrs)
        modules[name] = value
        return value

    @dataclass(frozen=True, kw_only=True)
    class Description:
        key: str
        translation_key: str = ""
        icon: str = ""
        device_class: str | None = None
        native_unit_of_measurement: str | None = None
        state_class: str | None = None
        suggested_display_precision: int | None = None
        entity_category: str | None = None
        options: list | None = None

    class CoordinatorEntity:
        def __class_getitem__(cls, _item):
            return cls

        def __init__(self, coordinator):
            self.coordinator = coordinator

        @property
        def available(self):
            return self.coordinator.last_update_success

    module("homeassistant")
    module("homeassistant.components")
    module("homeassistant.components.sensor", SensorEntity=type("SensorEntity", (), {}),
           SensorEntityDescription=Description,
           SensorDeviceClass=SimpleNamespace(BATTERY="battery", ENUM="enum", TEMPERATURE="temperature", DURATION="duration"),
           SensorStateClass=SimpleNamespace(MEASUREMENT="measurement"))
    module("homeassistant.config_entries", ConfigEntry=object)
    module("homeassistant.const", PERCENTAGE="%", EntityCategory=SimpleNamespace(DIAGNOSTIC="diagnostic"),
           UnitOfTemperature=SimpleNamespace(CELSIUS="°C"), UnitOfTime=SimpleNamespace(SECONDS="s"))
    module("homeassistant.core", HomeAssistant=object, callback=lambda fn: fn)
    module("homeassistant.helpers")
    module("homeassistant.helpers.entity_platform", AddConfigEntryEntitiesCallback=object)
    module("homeassistant.helpers.device_registry", DeviceInfo=dict)
    module("homeassistant.helpers.entity", EntityDescription=Description)
    module("homeassistant.helpers.update_coordinator", CoordinatorEntity=CoordinatorEntity)
    module("printdeck.coordinator", PrintDeckCoordinator=object)
    with patch.dict(sys.modules, modules):
        for name in ("entity", "sensor"):
            spec = importlib.util.spec_from_file_location(f"printdeck.{name}", PUBLIC_COMPONENT / f"{name}.py")
            value = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules, {f"printdeck.{name}": value}):
                spec.loader.exec_module(value)
            modules[f"printdeck.{name}"] = value
            sys.modules[f"printdeck.{name}"] = value
    return value


class SummarySensorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sensor_module = load_sensors()

    def setUp(self):
        item = printer_payload()
        item["printer"]["selected"] = False
        item["status"]["connection"]["detail_level"] = "summary"
        item["status"]["temperatures"]["chamber_current_c"] = 35
        self.printer = API.parse_printer(item["printer"], item["status"])
        self.coordinator = SimpleNamespace(last_update_success=True,
            data=SimpleNamespace(info=SimpleNamespace(device_id="printdeck-test")),
            printer=lambda _id: self.printer)

    def entity(self, key):
        description = next(d for d in self.sensor_module.SENSORS if d.key == key)
        return self.sensor_module.PrintDeckSensor(self.coordinator, self.printer, description)

    def test_summary_measurements_are_available_in_same_entities(self):
        for key in ("current_layer", "total_layers", "nozzle_temperature", "nozzle_target_temperature",
                    "bed_temperature", "bed_target_temperature", "chamber_temperature"):
            with self.subTest(key=key):
                entity = self.entity(key)
                self.assertTrue(entity.available)
                self.assertIsNotNone(entity.native_value)
                self.assertTrue(entity._attr_unique_id.endswith("_" + key))

    def test_unknown_is_unavailable_but_zero_target_is_valid(self):
        self.printer = replace(self.printer, nozzle_target_c=0, chamber_current_c=None)
        self.assertTrue(self.entity("nozzle_target_temperature").available)
        self.assertEqual(self.entity("nozzle_target_temperature").native_value, 0)
        self.assertFalse(self.entity("chamber_temperature").available)

    def test_unknown_layers_are_unavailable_including_older_firmware_zero(self):
        for missing in (None, 0):
            self.printer = replace(self.printer, current_layer=missing, total_layers=missing)
            self.assertFalse(self.entity("current_layer").available)
            self.assertFalse(self.entity("total_layers").available)
        self.printer = replace(self.printer, current_layer=0, total_layers=75)
        self.assertFalse(self.entity("current_layer").available)
        self.assertIsNone(self.entity("current_layer").native_value)
        self.assertTrue(self.entity("total_layers").available)

    def test_stale_or_failed_update_never_exposes_measurements(self):
        entity = self.entity("bed_temperature")
        self.printer = replace(self.printer, stale=True)
        self.assertFalse(entity.available)
        self.printer = replace(self.printer, stale=False)
        self.coordinator.last_update_success = False
        self.assertFalse(entity.available)


if __name__ == "__main__":
    unittest.main()

"""Tests für die Entity-Verdrahtung (Sensor + Binary-Sensor).

Geprüft wird das Zusammenspiel Entity <-> Coordinator: native_value/is_on lesen
das richtige State-Feld, available spiegelt is_available, der Push-Listener wird
beim Hinzufügen/Entfernen korrekt an-/abgemeldet und feuert async_write_ha_state.
Die HA-Entity-Basisklassen kommen aus den conftest-Stubs (bzw. echtem HA).
"""
from unittest.mock import MagicMock

from custom_components.jeelink_lacrosse.const import DOMAIN
from custom_components.jeelink_lacrosse.coordinator import SensorState
from custom_components.jeelink_lacrosse.sensor import (
    JeeLinkHumiditySensor,
    JeeLinkTemperatureSensor,
)
from custom_components.jeelink_lacrosse.binary_sensor import (
    JeeLinkLowBatterySensor,
    JeeLinkNewBatterySensor,
)


class FakeCoordinator:
    """Minimaler Coordinator-Ersatz für Entity-Tests."""

    def __init__(self) -> None:
        self.sensors = {"bad": SensorState(56, "Badezimmer")}
        self.listeners: dict[str, list] = {}
        self.available = True

    def is_available(self, slug: str) -> bool:
        return self.available

    def register_listener(self, slug: str, cb) -> None:
        self.listeners.setdefault(slug, []).append(cb)

    def unregister_listener(self, slug: str, cb) -> None:
        lst = self.listeners.get(slug)
        if lst and cb in lst:
            lst.remove(cb)


def _entry():
    entry = MagicMock()
    entry.entry_id = "e1"
    return entry


def test_temperature_sensor_value_unit_and_id():
    coord = FakeCoordinator()
    coord.sensors["bad"].temperature = 21.5
    ent = JeeLinkTemperatureSensor(coord, _entry(), "bad")

    assert ent.native_value == 21.5
    assert ent.device_class == "temperature"
    assert ent.native_unit_of_measurement == "°C"
    assert ent.unique_id == "e1_bad_temperature"
    # Gerät: identifiers gruppieren alle Entities eines Sensors
    assert ent.device_info["identifiers"] == {(DOMAIN, "e1_bad")}
    assert ent.device_info["name"] == "Badezimmer"


def test_humidity_sensor_value_none_without_sensor():
    coord = FakeCoordinator()
    coord.sensors["bad"].humidity = None
    ent = JeeLinkHumiditySensor(coord, _entry(), "bad")
    assert ent.native_value is None
    assert ent.device_class == "humidity"
    assert ent.native_unit_of_measurement == "%"

    coord.sensors["bad"].humidity = 48
    assert ent.native_value == 48


def test_low_battery_binary_sensor():
    coord = FakeCoordinator()
    ent = JeeLinkLowBatterySensor(coord, _entry(), "bad")
    assert ent.device_class == "battery"

    coord.sensors["bad"].low_battery = False
    assert ent.is_on is False
    coord.sensors["bad"].low_battery = True
    assert ent.is_on is True


def test_new_battery_diagnostic_sensor():
    coord = FakeCoordinator()
    ent = JeeLinkNewBatterySensor(coord, _entry(), "bad")
    assert ent.entity_category == "diagnostic"
    coord.sensors["bad"].new_battery = True
    assert ent.is_on is True


def test_availability_follows_coordinator():
    coord = FakeCoordinator()
    ent = JeeLinkTemperatureSensor(coord, _entry(), "bad")
    coord.available = True
    assert ent.available is True
    coord.available = False
    assert ent.available is False


async def test_listener_lifecycle_and_state_write():
    coord = FakeCoordinator()
    ent = JeeLinkTemperatureSensor(coord, _entry(), "bad")

    await ent.async_added_to_hass()
    assert coord.listeners["bad"] == [ent.async_write_ha_state]

    # Coordinator "feuert" -> async_write_ha_state wird aufgerufen
    for cb in coord.listeners["bad"]:
        cb()
    assert ent._ha_state_writes == 1

    await ent.async_will_remove_from_hass()
    assert coord.listeners["bad"] == []

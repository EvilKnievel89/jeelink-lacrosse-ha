"""Pytest-Setup für die Testumgebung.

1. Stellt sicher, dass das Repo-Root auf sys.path liegt ("custom_components" als
   Namespace-Paket).
2. Installiert schlanke Home-Assistant-Import-Stubs – ABER nur, wenn kein echter
   HA-Core installiert ist. So laufen die reinen Modul-Tests auch ohne HA, während
   ein echtes HA-Dev-Setup unangetastet bleibt.

Die Stubs decken nur die Symbole ab, die die Integrationsmodule beim *Import*
brauchen. Das konkrete Laufzeitverhalten (Store, Timer, Entity-State) patchen bzw.
prüfen die einzelnen Tests selbst.
"""
import os
import sys
import types
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(__file__))


def _install_ha_import_stubs() -> None:
    try:
        import homeassistant  # noqa: F401
        return  # echter HA-Core vorhanden -> keine Stubs
    except ImportError:
        pass

    ha = types.ModuleType("homeassistant")
    ha._jeelink_stub = True
    core = types.ModuleType("homeassistant.core")
    const = types.ModuleType("homeassistant.const")
    config_entries = types.ModuleType("homeassistant.config_entries")
    helpers = types.ModuleType("homeassistant.helpers")
    event = types.ModuleType("homeassistant.helpers.event")
    storage = types.ModuleType("homeassistant.helpers.storage")
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    components = types.ModuleType("homeassistant.components")
    sensor = types.ModuleType("homeassistant.components.sensor")
    binary_sensor = types.ModuleType("homeassistant.components.binary_sensor")

    class HomeAssistant:  # nur als Typname (PEP 563: Annotations bleiben Strings)
        ...

    class ConfigEntry:
        ...

    class _Platform:
        SENSOR = "sensor"
        BINARY_SENSOR = "binary_sensor"

    class _UnitOfTemperature:
        CELSIUS = "°C"

    class _EntityCategory:
        DIAGNOSTIC = "diagnostic"

    class _Store:  # Platzhalter; Tests patchen coordinator.Store mit eigenem Fake
        def __init__(self, *args, **kwargs):
            ...

    class _EntityBase:
        """Minimaler Entity-Ersatz.

        Bildet das HA-Verhalten nach, Properties wie device_class/unique_id auf
        _attr_<name> abzubilden, und zählt async_write_ha_state-Aufrufe.
        """

        def async_write_ha_state(self) -> None:
            self._ha_state_writes = getattr(self, "_ha_state_writes", 0) + 1

        def __getattr__(self, name: str):
            # Nur für nicht gefundene Public-Namen: auf _attr_<name> zurückfallen.
            if not name.startswith("_attr_"):
                try:
                    return object.__getattribute__(self, f"_attr_{name}")
                except AttributeError:
                    pass
            raise AttributeError(name)

    class _SensorDeviceClass:
        TEMPERATURE = "temperature"
        HUMIDITY = "humidity"

    class _SensorStateClass:
        MEASUREMENT = "measurement"

    class _BinarySensorDeviceClass:
        BATTERY = "battery"

    core.HomeAssistant = HomeAssistant
    core.callback = lambda fn: fn
    const.Platform = _Platform
    const.UnitOfTemperature = _UnitOfTemperature
    const.PERCENTAGE = "%"
    const.EntityCategory = _EntityCategory
    config_entries.ConfigEntry = ConfigEntry
    event.async_track_time_interval = lambda *a, **k: MagicMock()
    storage.Store = _Store
    device_registry.DeviceInfo = dict     # DeviceInfo(**kwargs) -> dict
    sensor.SensorEntity = type("SensorEntity", (_EntityBase,), {})
    sensor.SensorDeviceClass = _SensorDeviceClass
    sensor.SensorStateClass = _SensorStateClass
    binary_sensor.BinarySensorEntity = type("BinarySensorEntity", (_EntityBase,), {})
    binary_sensor.BinarySensorDeviceClass = _BinarySensorDeviceClass

    ha.core = core
    ha.const = const
    ha.config_entries = config_entries
    ha.helpers = helpers
    ha.components = components
    helpers.event = event
    helpers.storage = storage
    helpers.device_registry = device_registry
    components.sensor = sensor
    components.binary_sensor = binary_sensor

    sys.modules.update(
        {
            "homeassistant": ha,
            "homeassistant.core": core,
            "homeassistant.const": const,
            "homeassistant.config_entries": config_entries,
            "homeassistant.helpers": helpers,
            "homeassistant.helpers.event": event,
            "homeassistant.helpers.storage": storage,
            "homeassistant.helpers.device_registry": device_registry,
            "homeassistant.components": components,
            "homeassistant.components.sensor": sensor,
            "homeassistant.components.binary_sensor": binary_sensor,
        }
    )


_install_ha_import_stubs()

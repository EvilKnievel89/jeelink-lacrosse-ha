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


def _install_voluptuous_stub() -> None:
    """Minimaler voluptuous-Ersatz (nur, wenn nicht echt installiert).

    repairs.py/config_flow.py bauen Schemata mit Schema/Required/In. Hier reicht
    ein Stub, der die Schema-Struktur durchreichbar/inspizierbar macht – Tests
    prüfen Verhalten, keine echte voluptuous-Validierung.
    """
    try:
        import voluptuous  # noqa: F401
        return
    except ImportError:
        pass

    vol = types.ModuleType("voluptuous")

    class _Marker:
        def __init__(self, schema, default=None, **kwargs):
            self.schema = schema
            self.default = default

        def __hash__(self):
            return hash(self.schema)

        def __eq__(self, other):
            return self.schema == getattr(other, "schema", other)

        def __call__(self, value):
            return value

    class _In:
        def __init__(self, container, **kwargs):
            self.container = container

        def __call__(self, value):
            return value

    class _Schema:
        def __init__(self, schema, **kwargs):
            self.schema = schema

        def __call__(self, data):
            return data

    vol.Schema = _Schema
    vol.Required = type("Required", (_Marker,), {})
    vol.Optional = type("Optional", (_Marker,), {})
    vol.In = _In
    sys.modules["voluptuous"] = vol


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
    issue_registry = types.ModuleType("homeassistant.helpers.issue_registry")
    selector = types.ModuleType("homeassistant.helpers.selector")
    components = types.ModuleType("homeassistant.components")
    sensor = types.ModuleType("homeassistant.components.sensor")
    binary_sensor = types.ModuleType("homeassistant.components.binary_sensor")
    repairs = types.ModuleType("homeassistant.components.repairs")

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

    class _IssueSeverity:
        CRITICAL = "critical"
        ERROR = "error"
        WARNING = "warning"

    class _SelectSelectorMode:
        DROPDOWN = "dropdown"
        LIST = "list"

    def _SelectOptionDict(value=None, label=None):
        return {"value": value, "label": label}

    class _SelectSelectorConfig(dict):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)

    class _SelectSelector:
        """Inspizierbarer SelectSelector-Ersatz; Tests lesen .config."""

        def __init__(self, config=None):
            self.config = config or {}

        def __call__(self, value):
            return value

    class _RepairsFlow:
        """Minimaler RepairsFlow-Ersatz: Step-Resultate als dicts.

        hass wird in echtem HA vom Flow-Manager gesetzt; in Tests setzen wir es
        direkt auf der Instanz.
        """

        hass = None

        def async_show_form(
            self,
            *,
            step_id,
            data_schema=None,
            errors=None,
            description_placeholders=None,
            last_step=None,
        ):
            return {
                "type": "form",
                "step_id": step_id,
                "data_schema": data_schema,
                "errors": errors,
                "description_placeholders": description_placeholders,
            }

        def async_create_entry(self, *, title="", data=None):
            return {"type": "create_entry", "title": title, "data": data or {}}

        def async_abort(self, *, reason, description_placeholders=None):
            return {"type": "abort", "reason": reason}

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
    issue_registry.IssueSeverity = _IssueSeverity
    issue_registry.async_create_issue = lambda *a, **k: None
    issue_registry.async_delete_issue = lambda *a, **k: None
    selector.SelectSelector = _SelectSelector
    selector.SelectSelectorConfig = _SelectSelectorConfig
    selector.SelectSelectorMode = _SelectSelectorMode
    selector.SelectOptionDict = _SelectOptionDict
    sensor.SensorEntity = type("SensorEntity", (_EntityBase,), {})
    sensor.SensorDeviceClass = _SensorDeviceClass
    sensor.SensorStateClass = _SensorStateClass
    binary_sensor.BinarySensorEntity = type("BinarySensorEntity", (_EntityBase,), {})
    binary_sensor.BinarySensorDeviceClass = _BinarySensorDeviceClass
    repairs.RepairsFlow = _RepairsFlow

    ha.core = core
    ha.const = const
    ha.config_entries = config_entries
    ha.helpers = helpers
    ha.components = components
    helpers.event = event
    helpers.storage = storage
    helpers.device_registry = device_registry
    helpers.issue_registry = issue_registry
    helpers.selector = selector
    components.sensor = sensor
    components.binary_sensor = binary_sensor
    components.repairs = repairs

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
            "homeassistant.helpers.issue_registry": issue_registry,
            "homeassistant.helpers.selector": selector,
            "homeassistant.components": components,
            "homeassistant.components.sensor": sensor,
            "homeassistant.components.binary_sensor": binary_sensor,
            "homeassistant.components.repairs": repairs,
        }
    )


_install_voluptuous_stub()
_install_ha_import_stubs()

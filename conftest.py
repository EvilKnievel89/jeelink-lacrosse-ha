"""Pytest-Setup für die Testumgebung.

1. Stellt sicher, dass das Repo-Root auf sys.path liegt ("custom_components" als
   Namespace-Paket).
2. Installiert schlanke Home-Assistant-Import-Stubs – ABER nur, wenn kein echter
   HA-Core installiert ist. So laufen die reinen protocol/serial_reader/coordinator-
   Tests auch ohne HA, während ein echtes HA-Dev-Setup unangetastet bleibt.

Die Stubs decken nur die Symbole ab, die __init__.py und coordinator.py beim
*Import* brauchen. Das konkrete Laufzeitverhalten (Store, Timer) patchen die
einzelnen Tests selbst.
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

    class HomeAssistant:  # nur als Typname (PEP 563: Annotations bleiben Strings)
        ...

    class ConfigEntry:
        ...

    class _Platform:
        SENSOR = "sensor"
        BINARY_SENSOR = "binary_sensor"

    class _Store:  # Platzhalter; Tests patchen coordinator.Store mit eigenem Fake
        def __init__(self, *args, **kwargs):
            ...

    core.HomeAssistant = HomeAssistant
    core.callback = lambda fn: fn
    const.Platform = _Platform
    config_entries.ConfigEntry = ConfigEntry
    event.async_track_time_interval = lambda *a, **k: MagicMock()
    storage.Store = _Store

    ha.core = core
    ha.const = const
    ha.config_entries = config_entries
    ha.helpers = helpers
    helpers.event = event
    helpers.storage = storage

    sys.modules.update(
        {
            "homeassistant": ha,
            "homeassistant.core": core,
            "homeassistant.const": const,
            "homeassistant.config_entries": config_entries,
            "homeassistant.helpers": helpers,
            "homeassistant.helpers.event": event,
            "homeassistant.helpers.storage": storage,
        }
    )


_install_ha_import_stubs()

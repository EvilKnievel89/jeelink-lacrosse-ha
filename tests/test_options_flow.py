"""Options-Flow-Tests (Sensoren hinzufügen/bearbeiten/entfernen).

Wie die Config-Flow-Tests brauchen sie den echten HA-Core (Flow-Manager,
voluptuous, config_validation) und `pytest-homeassistant-custom-component`
(hass-Fixture, MockConfigEntry). In der HA-losen Umgebung werden sie übersprungen;
die reine Transformationslogik ist separat in tests/test_sensor_config.py geprüft.
"""
import pytest

ce = pytest.importorskip("homeassistant.config_entries")
if not hasattr(ce, "OptionsFlow"):
    pytest.skip(
        "Echter Home-Assistant-Core nötig (hier nur Import-Stubs vorhanden).",
        allow_module_level=True,
    )

from custom_components.jeelink_lacrosse.const import (  # noqa: E402
    CONF_DEVICE, CONF_BAUD, CONF_LACROSSE_ID, CONF_SENSORS, DOMAIN,
)

_DATA = {CONF_DEVICE: "/dev/ttyUSB0", CONF_BAUD: 57600}


def _entry(hass, sensors):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN, data=_DATA, options={CONF_SENSORS: sensors}
    )
    entry.add_to_hass(hass)
    return entry


async def test_add_sensor_flow_creates_entry(hass):
    entry = _entry(hass, {})
    options = hass.config_entries.options

    result = await options.async_init(entry.entry_id)
    assert result["type"] == "menu"

    result = await options.async_configure(
        result["flow_id"], {"next_step_id": "add_sensor"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "add_sensor"

    # Das ID-Feld ist jetzt ein Dropdown (SelectSelector, custom_value) -> String
    result = await options.async_configure(
        result["flow_id"], {CONF_LACROSSE_ID: "56", "friendly_name": "Badezimmer"}
    )
    assert result["type"] == "create_entry"
    assert entry.options[CONF_SENSORS]["badezimmer"][CONF_LACROSSE_ID] == 56


async def test_add_sensor_duplicate_id_shows_error(hass):
    entry = _entry(hass, {"bad": {CONF_LACROSSE_ID: 56, "friendly_name": "Bad"}})
    options = hass.config_entries.options

    result = await options.async_init(entry.entry_id)
    result = await options.async_configure(
        result["flow_id"], {"next_step_id": "add_sensor"}
    )
    result = await options.async_configure(
        result["flow_id"], {CONF_LACROSSE_ID: "56", "friendly_name": "Zweitname"}
    )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "id_in_use"}


async def test_add_sensor_out_of_range_id_shows_error(hass):
    entry = _entry(hass, {})
    options = hass.config_entries.options

    result = await options.async_init(entry.entry_id)
    result = await options.async_configure(
        result["flow_id"], {"next_step_id": "add_sensor"}
    )
    # custom_value lässt Freitext durch -> Bereichsprüfung im Handler (0..255)
    result = await options.async_configure(
        result["flow_id"], {CONF_LACROSSE_ID: "999", "friendly_name": "Zuviel"}
    )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_id"}


async def test_edit_sensor_flow(hass):
    entry = _entry(hass, {"bad": {CONF_LACROSSE_ID: 56, "friendly_name": "Bad"}})
    options = hass.config_entries.options

    result = await options.async_init(entry.entry_id)
    result = await options.async_configure(
        result["flow_id"], {"next_step_id": "edit_sensor"}
    )
    assert result["step_id"] == "edit_sensor"
    result = await options.async_configure(result["flow_id"], {"sensor": "bad"})
    assert result["step_id"] == "edit_details"
    # ID-Feld ist auch hier ein Dropdown (SelectSelector, custom_value) -> String
    result = await options.async_configure(
        result["flow_id"], {CONF_LACROSSE_ID: "60", "friendly_name": "Badezimmer"}
    )
    assert result["type"] == "create_entry"
    cfg = entry.options[CONF_SENSORS]["bad"]
    assert cfg[CONF_LACROSSE_ID] == 60
    assert cfg["friendly_name"] == "Badezimmer"


async def test_remove_sensor_flow(hass):
    entry = _entry(
        hass,
        {
            "bad": {CONF_LACROSSE_ID: 56, "friendly_name": "Bad"},
            "kel": {CONF_LACROSSE_ID: 12, "friendly_name": "Keller"},
        },
    )
    options = hass.config_entries.options

    result = await options.async_init(entry.entry_id)
    result = await options.async_configure(
        result["flow_id"], {"next_step_id": "remove_sensor"}
    )
    assert result["step_id"] == "remove_sensor"
    result = await options.async_configure(result["flow_id"], {"sensor": "bad"})
    assert result["type"] == "create_entry"
    assert "bad" not in entry.options[CONF_SENSORS]
    assert "kel" in entry.options[CONF_SENSORS]

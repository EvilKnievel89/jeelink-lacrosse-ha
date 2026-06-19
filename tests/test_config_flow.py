"""Config-Flow-Tests.

Diese Tests brauchen den echten Home-Assistant-Core (Flow-Manager, voluptuous,
config_validation) und das pytest-Plugin `pytest-homeassistant-custom-component`
(stellt die `hass`-Fixture bereit). In der hiesigen, HA-losen Umgebung werden sie
sauber übersprungen; in einem echten HA-Dev-Setup laufen sie.

Lokal ausführen:
    pip install pytest-homeassistant-custom-component
    pytest tests/test_config_flow.py
"""
import pytest

# Echten HA-Core verlangen: in der Stub-Umgebung fehlt config_entries.ConfigFlow.
ce = pytest.importorskip("homeassistant.config_entries")
if not hasattr(ce, "ConfigFlow"):
    pytest.skip(
        "Echter Home-Assistant-Core nötig (hier nur Import-Stubs vorhanden).",
        allow_module_level=True,
    )

from unittest.mock import patch  # noqa: E402

from custom_components.jeelink_lacrosse.const import (  # noqa: E402
    DOMAIN, CONF_DEVICE, CONF_BAUD, CONF_SENSORS, CONF_LACROSSE_ID,
)
from custom_components.jeelink_lacrosse.config_flow import MANUAL_PATH  # noqa: E402

_PORTS = {"/dev/ttyUSB0": "/dev/ttyUSB0 - FT232R USB UART"}
_LIST = "custom_components.jeelink_lacrosse.config_flow.list_serial_ports"
_TEST_CONN = (
    "custom_components.jeelink_lacrosse.serial_reader."
    "JeeLinkSerialReader.test_connection"
)


async def test_user_flow_creates_entry(hass):
    """Port aus dem Dropdown + erfolgreicher Verbindungstest -> Eintrag."""
    with patch(_LIST, return_value=_PORTS), \
         patch(_TEST_CONN, return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        assert result["type"] == "form"
        assert result["step_id"] == "user"

        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DEVICE: "/dev/ttyUSB0", CONF_BAUD: 57600}
        )

    assert result2["type"] == "create_entry"
    assert result2["data"] == {CONF_DEVICE: "/dev/ttyUSB0", CONF_BAUD: 57600}


async def test_user_flow_cannot_connect_shows_error(hass):
    """Fehlgeschlagener Verbindungstest -> Formular mit cannot_connect."""
    with patch(_LIST, return_value=_PORTS), \
         patch(_TEST_CONN, return_value=False):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DEVICE: "/dev/ttyUSB0", CONF_BAUD: 57600}
        )

    assert result2["type"] == "form"
    assert result2["errors"] == {"base": "cannot_connect"}


async def test_manual_path_flow(hass):
    """Manuelle Pfadeingabe über den Dropdown-Sentinel."""
    with patch(_LIST, return_value=_PORTS), \
         patch(_TEST_CONN, return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DEVICE: MANUAL_PATH, CONF_BAUD: 57600}
        )
        assert result2["type"] == "form"
        assert result2["step_id"] == "manual"

        path = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_ABC-if00-port0"
        result3 = await hass.config_entries.flow.async_configure(
            result2["flow_id"], {CONF_DEVICE: path, CONF_BAUD: 57600}
        )

    assert result3["type"] == "create_entry"
    assert result3["data"][CONF_DEVICE] == path


async def test_duplicate_device_aborts(hass):
    """Gleiches Gerät zweimal -> already_configured."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    MockConfigEntry(
        domain=DOMAIN,
        unique_id="/dev/ttyUSB0",
        data={CONF_DEVICE: "/dev/ttyUSB0", CONF_BAUD: 57600},
    ).add_to_hass(hass)

    with patch(_LIST, return_value=_PORTS), \
         patch(_TEST_CONN, return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DEVICE: "/dev/ttyUSB0", CONF_BAUD: 57600}
        )

    assert result2["type"] == "abort"
    assert result2["reason"] == "already_configured"


async def test_reconfigure_updates_connection(hass):
    """Reconfigure ändert Port/Baud in entry.data und behält Sensoren/Verlauf."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="/dev/ttyUSB0",
        data={CONF_DEVICE: "/dev/ttyUSB0", CONF_BAUD: 57600},
        options={CONF_SENSORS: {"bad": {CONF_LACROSSE_ID: 56, "friendly_name": "Bad"}}},
    )
    entry.add_to_hass(hass)

    new_path = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_ABC-if00-port0"
    with patch(_LIST, return_value=_PORTS), \
         patch(_TEST_CONN, return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reconfigure", "entry_id": entry.entry_id},
        )
        assert result["type"] == "form"
        assert result["step_id"] == "reconfigure"

        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DEVICE: new_path, CONF_BAUD: 38400}
        )

    assert result2["type"] == "abort"
    assert result2["reason"] == "reconfigure_successful"
    assert entry.data[CONF_DEVICE] == new_path
    assert entry.data[CONF_BAUD] == 38400
    # Sensor-Konfiguration bleibt erhalten
    assert entry.options[CONF_SENSORS]["bad"][CONF_LACROSSE_ID] == 56


async def test_reconfigure_cannot_connect_shows_error(hass):
    """Fehlgeschlagener Verbindungstest -> Formular mit cannot_connect."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="/dev/ttyUSB0",
        data={CONF_DEVICE: "/dev/ttyUSB0", CONF_BAUD: 57600},
    )
    entry.add_to_hass(hass)

    with patch(_LIST, return_value=_PORTS), \
         patch(_TEST_CONN, return_value=False):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reconfigure", "entry_id": entry.entry_id},
        )
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DEVICE: "/dev/ttyUSB1", CONF_BAUD: 57600}
        )

    assert result2["type"] == "form"
    assert result2["errors"] == {"base": "cannot_connect"}

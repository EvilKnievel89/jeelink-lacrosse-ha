"""Tests für den Serial-Reader (pyserial-asyncio-fast wird gemockt).

Kein laufendes Home Assistant nötig: der Reader kennt HA nicht. Gemockt wird
das modul-lokale Alias `serial_asyncio` (= serial_asyncio_fast).
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.jeelink_lacrosse.serial_reader import JeeLinkSerialReader

SERIAL_PATCH = "custom_components.jeelink_lacrosse.serial_reader.serial_asyncio"


def _mock_writer() -> MagicMock:
    writer = MagicMock()
    writer.wait_closed = AsyncMock()
    return writer


async def test_measurement_callback_called():
    """serial_reader ruft Callback auf, wenn gültige Zeile empfangen."""
    measurements = []

    async def mock_callback(m):
        measurements.append(m)

    mock_reader = AsyncMock()
    mock_reader.readline.side_effect = [
        b"OK 9 56 1 4 156 37\r\n",
        b"OK 9 23 1 4 200 55\r\n",
        asyncio.CancelledError(),
    ]
    mock_writer = _mock_writer()

    with patch(f"{SERIAL_PATCH}.open_serial_connection",
               return_value=(mock_reader, mock_writer)):
        reader = JeeLinkSerialReader("/dev/ttyUSB0", 57600, mock_callback)
        await reader.async_start()
        await asyncio.sleep(0.1)
        await reader.async_stop()

    assert len(measurements) == 2
    assert measurements[0].sensor_id == 56
    assert measurements[0].temperature == 18.0
    assert measurements[1].sensor_id == 23
    mock_writer.close.assert_called()      # Transport wurde geschlossen


async def test_reconnect_on_disconnect():
    """serial_reader reconnectet nach Verbindungsabbruch."""
    connect_count = 0

    async def mock_connect(url, baudrate):
        nonlocal connect_count
        connect_count += 1
        mock_reader = AsyncMock()
        if connect_count == 1:
            mock_reader.readline.side_effect = OSError("USB disconnect")
        else:
            mock_reader.readline.side_effect = asyncio.CancelledError()
        return mock_reader, _mock_writer()

    with patch(f"{SERIAL_PATCH}.open_serial_connection", side_effect=mock_connect):
        with patch("custom_components.jeelink_lacrosse.serial_reader.RECONNECT_DELAY", 0):
            reader = JeeLinkSerialReader("/dev/ttyUSB0", 57600, AsyncMock())
            await reader.async_start()
            await asyncio.sleep(0.1)
            await reader.async_stop()

    assert connect_count >= 2


async def test_invalid_line_does_not_disconnect():
    """ValueError aus readline() wird übersprungen, nicht als Disconnect gewertet."""
    connect_count = 0
    measurements = []

    async def mock_callback(m):
        measurements.append(m)

    async def mock_connect(url, baudrate):
        nonlocal connect_count
        connect_count += 1
        mock_reader = AsyncMock()
        mock_reader.readline.side_effect = [
            ValueError("Separator is not found, and chunk exceed the limit"),
            b"OK 9 56 1 4 156 37\r\n",
            asyncio.CancelledError(),
        ]
        return mock_reader, _mock_writer()

    with patch(f"{SERIAL_PATCH}.open_serial_connection", side_effect=mock_connect):
        with patch("custom_components.jeelink_lacrosse.serial_reader.RECONNECT_DELAY", 0):
            reader = JeeLinkSerialReader("/dev/ttyUSB0", 57600, mock_callback)
            await reader.async_start()
            await asyncio.sleep(0.1)
            await reader.async_stop()

    # Kein Reconnect: dieselbe Verbindung blieb bestehen, die Zeile wurde geparst.
    assert connect_count == 1
    assert len(measurements) == 1
    assert measurements[0].sensor_id == 56


async def test_async_stop_cancels_running_loop():
    """Deckt den echten Cancel-Pfad in async_stop ab (Fix ggü. v2)."""
    block = asyncio.Event()

    async def _blocking_readline(*args, **kwargs):
        await block.wait()   # blockiert, bis die Task gecancelt wird
        return b""

    mock_reader = AsyncMock()
    mock_reader.readline = _blocking_readline
    mock_writer = _mock_writer()

    with patch(f"{SERIAL_PATCH}.open_serial_connection",
               return_value=(mock_reader, mock_writer)):
        reader = JeeLinkSerialReader("/dev/ttyUSB0", 57600, AsyncMock())
        await reader.async_start()
        await asyncio.sleep(0.05)          # Loop hängt in readline()
        assert not reader._task.done()
        await reader.async_stop()          # muss task.cancel() + await ausführen

    assert reader._task.done()
    mock_writer.close.assert_called()      # finally schließt auch bei Cancel


async def test_test_connection_success():
    """test_connection gibt True zurück und schließt den Transport."""
    mock_reader = AsyncMock()
    mock_reader.readline.return_value = b"OK 9 56 1 4 156 37\r\n"
    mock_writer = _mock_writer()

    with patch(f"{SERIAL_PATCH}.open_serial_connection",
               return_value=(mock_reader, mock_writer)):
        ok = await JeeLinkSerialReader.test_connection("/dev/ttyUSB0", 57600)

    assert ok is True
    mock_writer.close.assert_called_once()


async def test_test_connection_failure_on_oserror():
    """test_connection fängt OSError ab und gibt False zurück."""
    with patch(f"{SERIAL_PATCH}.open_serial_connection",
               side_effect=OSError("no such device")):
        ok = await JeeLinkSerialReader.test_connection("/dev/ttyNOPE", 57600)

    assert ok is False

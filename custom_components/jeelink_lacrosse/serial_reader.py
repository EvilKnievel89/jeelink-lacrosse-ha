"""
Asynchroner JeeLink Serial-Reader.
Nutzt pyserial-asyncio-fast, kein Threading.
"""
from __future__ import annotations
import asyncio
import logging
from collections.abc import Callable
from typing import Any

import serial                          # pyserial – für SerialException
import serial_asyncio_fast as serial_asyncio

from .protocol import LaCrosseMeasurement, parse_line

_LOGGER = logging.getLogger(__name__)

RECONNECT_DELAY = 30  # Sekunden vor erneutem Verbindungsversuch


class JeeLinkSerialReader:
    """
    Öffnet den JeeLink-Port, liest Zeilen, parsed sie und ruft
    den `on_measurement`-Callback auf. Reconnectet bei Disconnect.
    """

    def __init__(
        self,
        device: str,
        baud: int,
        on_measurement: Callable[[LaCrosseMeasurement], Any],
    ) -> None:
        self._device = device
        self._baud = baud
        self._on_measurement = on_measurement
        self._running = False
        self._task: asyncio.Task | None = None

    async def async_start(self) -> None:
        """Startet den Lese-Loop als Hintergrund-Task."""
        self._running = True
        self._task = asyncio.create_task(
            self._read_loop(), name="jeelink_read_loop"
        )

    async def async_stop(self) -> None:
        """Beendet den Lese-Loop sauber."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        _LOGGER.debug("JeeLink Serial-Reader gestoppt")

    async def _read_loop(self) -> None:
        """Äußerer Loop: verbinden, lesen, bei Fehler reconnecten."""
        while self._running:
            try:
                await self._connect_and_read()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 - bewusst breit für Reconnect
                if self._running:
                    _LOGGER.warning(
                        "JeeLink Verbindung unterbrochen (%s). Reconnect in %ds...",
                        exc, RECONNECT_DELAY,
                    )
                    await asyncio.sleep(RECONNECT_DELAY)

    async def _connect_and_read(self) -> None:
        """Öffnet die Serial-Verbindung und liest Zeilen bis zur Exception."""
        _LOGGER.info(
            "Verbinde mit JeeLink auf %s (Baud: %d)", self._device, self._baud
        )

        reader, writer = await serial_asyncio.open_serial_connection(
            url=self._device,
            baudrate=self._baud,
        )
        _LOGGER.info("JeeLink verbunden – empfange Daten")

        try:
            while self._running:
                try:
                    raw_line = await reader.readline()
                except ValueError:
                    # readline() wirft ValueError, wenn in 64 KiB kein '\n' kommt
                    # (Müll/Partial). Überspringen, NICHT als Disconnect behandeln.
                    _LOGGER.debug("Überlange/ungültige Zeile verworfen")
                    continue

                if not raw_line:
                    raise ConnectionResetError("JeeLink hat Verbindung getrennt")

                line = raw_line.decode("ascii", errors="ignore")
                measurement = parse_line(line)
                if measurement is None:
                    continue

                _LOGGER.debug(
                    "Sensor %d: %.1f°C %s%% new_batt=%s low_batt=%s status=%d",
                    measurement.sensor_id,
                    measurement.temperature,
                    measurement.humidity if measurement.humidity is not None else "n/a",
                    measurement.new_battery,
                    measurement.low_battery,
                    measurement.raw_status,
                )
                result = self._on_measurement(measurement)
                if asyncio.iscoroutine(result):
                    await result
        finally:
            # Transport in jedem Fall schließen (Reconnect, Cancel, Fehler)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 - close() kann beim Disconnect selbst werfen
                pass

    @staticmethod
    async def test_connection(device: str, baud: int) -> bool:
        """
        Schneller Verbindungstest für den Config Flow.
        Öffnet den Port und wartet kurz auf irgendeinen Input.
        """
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                serial_asyncio.open_serial_connection(url=device, baudrate=baud),
                timeout=5.0,
            )
            await asyncio.wait_for(reader.readline(), timeout=3.0)
            return True
        except (OSError, asyncio.TimeoutError, serial.SerialException) as exc:
            _LOGGER.debug("Verbindungstest fehlgeschlagen: %s", exc)
            return False
        finally:
            if writer is not None:
                writer.close()

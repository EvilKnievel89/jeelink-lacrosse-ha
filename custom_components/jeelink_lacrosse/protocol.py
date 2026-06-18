"""
LaCrosse IT+ Protokoll-Parser.
Kein I/O – nur String-Parsing.
Verifiziert gegen LaCrosseITPlusReader-Firmware und pylacrosse.
"""
from __future__ import annotations
from dataclasses import dataclass
import logging

_LOGGER = logging.getLogger(__name__)

TEMP_MIN = -40.0
TEMP_MAX = 60.0
HUM_MAX = 100      # maskierte Werte > 100 (z. B. 106) = kein Feuchtesensor


@dataclass
class LaCrosseMeasurement:
    sensor_id: int
    temperature: float
    humidity: int | None      # None wenn kein Feuchtesensor
    new_battery: bool         # STATUS-Bit 7: frische Batterie gerade eingelegt
    low_battery: bool         # HUM-Bit 7: Batterie schwach (echte Warnung)
    raw_status: int           # Original-Status-Byte für Diagnose


def parse_line(line: str) -> LaCrosseMeasurement | None:
    """
    Parst eine LaCrosse IT+ Zeile vom JeeLink.

    Gültiges Format:  'OK 9 <ID> <STATUS> <T_H> <T_L> <HUM>'
    Ungültig/ignoriert: Leerzeilen, Versionszeilen (start mit '['), '#' etc.

    Gibt None zurück, wenn die Zeile kein gültiges LaCrosse-IT+-Paket ist
    oder die Temperatur außerhalb des physikalisch sinnvollen Bereichs liegt.
    """
    line = line.strip()   # entfernt auch das CRLF-'\r' (Firmware sendet \r\n)

    # Ignoriere Leerzeilen, Versionsstring, sonstige Nicht-Daten
    if not line or line[0] != "O":
        return None

    parts = line.split()

    if len(parts) != 7 or parts[0] != "OK" or parts[1] != "9":
        return None

    try:
        sensor_id = int(parts[2])
        status = int(parts[3])
        t_h = int(parts[4])
        t_l = int(parts[5])
        hum_raw = int(parts[6])
    except ValueError:
        _LOGGER.debug("Konnte Zeile nicht parsen: %s", line)
        return None

    # Temperatur (verifiziert: T_H=4, T_L=156 -> 18,0 °C)
    temperature = (t_h * 256 + t_l - 1000) / 10.0

    # Sanity-Check – spiegelt den Firmware-internen Filter [-40, 60]
    if not TEMP_MIN <= temperature <= TEMP_MAX:
        _LOGGER.debug(
            "Sensor %d: Temperatur %.1f°C außerhalb Bereich [%.1f, %.1f] – ignoriert",
            sensor_id, temperature, TEMP_MIN, TEMP_MAX,
        )
        return None

    # HUM-Byte: Bit 7 = Schwachbatterie, untere 7 Bit = Feuchte
    low_battery = bool(hum_raw & 0x80)
    hum = hum_raw & 0x7F
    humidity = hum if hum <= HUM_MAX else None   # 106 = kein Feuchtesensor

    # STATUS-Byte: Bit 7 = frisch eingelegte Batterie (NICHT Schwachbatterie!)
    new_battery = bool(status & 0x80)

    return LaCrosseMeasurement(
        sensor_id=sensor_id,
        temperature=temperature,
        humidity=humidity,
        new_battery=new_battery,
        low_battery=low_battery,
        raw_status=status,
    )

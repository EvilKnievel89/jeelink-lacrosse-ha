"""Unit-Tests für den reinen Protokoll-Parser (kein Hardware-/HA-Bedarf)."""
from custom_components.jeelink_lacrosse.protocol import parse_line


class TestParseLineValid:
    """Verifiziert anhand bekannter Firmware-Beispiele."""

    def test_firmware_reference_18_0_degrees(self):
        """OK 9 56 1 4 156 37 -> T=18.0, H=37, keine Batterie-Flags."""
        r = parse_line("OK 9 56 1 4 156 37")
        assert r is not None
        assert r.sensor_id == 56
        assert r.temperature == 18.0
        assert r.humidity == 37
        assert r.new_battery is False
        assert r.low_battery is False
        assert r.raw_status == 1

    def test_new_battery_flag_status_bit7(self):
        """STATUS=129 (0x81): frisch eingelegte Batterie, Messwert bleibt gültig."""
        r = parse_line("OK 9 56 129 4 156 37")
        assert r is not None
        assert r.temperature == 18.0
        assert r.new_battery is True     # STATUS-Bit 7
        assert r.low_battery is False
        assert r.humidity == 37
        assert r.raw_status == 129

    def test_low_battery_flag_in_humidity_byte(self):
        """HUM=165 (37 | 0x80): Schwachbatterie + gültige 37% Feuchte."""
        r = parse_line("OK 9 56 1 4 156 165")
        assert r is not None
        assert r.temperature == 18.0
        assert r.low_battery is True      # HUM-Bit 7
        assert r.new_battery is False
        assert r.humidity == 37           # 165 & 0x7f

    def test_no_humidity_sensor(self):
        """HUM=106 (maskiert > 100) -> kein Feuchtesensor -> None."""
        r = parse_line("OK 9 55 1 4 124 106")
        assert r is not None
        assert r.humidity is None
        assert r.low_battery is False
        assert abs(r.temperature - 14.8) < 0.1

    def test_no_humidity_sensor_with_low_battery(self):
        """HUM=234 (106 | 0x80) -> kein Sensor (None) + Schwachbatterie True."""
        r = parse_line("OK 9 55 1 4 124 234")
        assert r is not None
        assert r.humidity is None         # 234 & 0x7f = 106 > 100
        assert r.low_battery is True

    def test_multiple_sensors(self):
        lines = [
            ("OK 9 58 1 4 174 62", 19.8, 62),
            ("OK 9 38 1 4 209 42", 23.3, 42),
            ("OK 9 30 1 4 198 34", 22.2, 34),
        ]
        for line, expected_temp, expected_hum in lines:
            r = parse_line(line)
            assert r is not None
            assert abs(r.temperature - expected_temp) < 0.1
            assert r.humidity == expected_hum


class TestParseLineInvalid:
    """Parser muss robust gegen fehlerhafte Eingaben sein."""

    def test_version_line_ignored(self):
        assert parse_line("[LaCrosseITPlusReader.10.1s (RFM69CW)]") is None

    def test_empty_line_ignored(self):
        assert parse_line("") is None
        assert parse_line("   ") is None

    def test_wrong_sensor_type(self):
        assert parse_line("OK 6 23 1 4 156 37") is None

    def test_wrong_prefix(self):
        assert parse_line("ERR 9 56 1 4 156 37") is None

    def test_too_few_fields(self):
        assert parse_line("OK 9 56 1 4 156") is None

    def test_non_numeric_fields(self):
        assert parse_line("OK 9 XX 1 4 156 37") is None

    def test_temperature_out_of_range_discarded(self):
        # T_H=0, T_L=0 -> (0-1000)/10 = -100°C -> außerhalb [-40, +60]
        assert parse_line("OK 9 56 1 0 0 50") is None

    def test_crlf_is_stripped(self):
        # Firmware sendet \r\n; strip() muss das CR entfernen.
        r = parse_line("OK 9 56 1 4 156 37\r\n")
        assert r is not None
        assert r.temperature == 18.0

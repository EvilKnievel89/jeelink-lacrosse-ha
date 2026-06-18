"""Konstanten der JeeLink-LaCrosse-Integration."""
from __future__ import annotations

DOMAIN = "jeelink_lacrosse"

# Serial-Verbindung
DEFAULT_BAUD = 57600

# Config-/Options-Schlüssel
CONF_DEVICE = "device"
CONF_BAUD = "baud"
CONF_SENSORS = "sensors"
CONF_LACROSSE_ID = "lacrosse_id"
CONF_OFFLINE_THRESHOLD = "offline_threshold"

# Zeitschwellen (in Minuten)
# Default-Schwelle; pro Eintrag über den Options-Flow überschreibbar
# (entry.options[CONF_OFFLINE_THRESHOLD]).
DEFAULT_OFFLINE_THRESHOLD_MINUTES = 30   # Sensor gilt nach dieser Stille als offline/unavailable
CHECK_INTERVAL_MINUTES = 5               # Takt der periodischen Offline-Prüfung

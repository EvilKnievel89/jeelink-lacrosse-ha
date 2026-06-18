"""JeeLink LaCrosse Integration.

Phase 3 liefert die Serial-/Protokoll-Schicht (protocol.py, serial_reader.py,
coordinator.py). Das eigentliche Setup (async_setup_entry / Platform-Forwarding,
Update-Listener-Registrierung) folgt in Phase 2/4 und wird hier bewusst noch nicht
implementiert, damit protocol.py weiterhin ohne Home-Assistant-Abhängigkeit
importier- und testbar bleibt.
"""

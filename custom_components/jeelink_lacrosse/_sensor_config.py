"""Reine, HA-freie Hilfen für die Sensor-Verwaltung im Options-Flow.

Arbeiten ausschließlich auf dem options-dict (dem CONF_SENSORS-Mapping) und sind
damit ohne Home Assistant test- und wiederverwendbar. Der Options-Flow kümmert
sich nur um Formulare, Fehleranzeige und Coordinator-Zugriff; die eigentlichen
Transformationen (slug erzeugen, hinzufügen/ändern/entfernen, Validierung) liegen
hier.

Jeder Sensor-Eintrag hat die Form::

    options[CONF_SENSORS][<slug>] = {CONF_LACROSSE_ID: int, "friendly_name": str}

Alle Funktionen sind nebenwirkungsfrei: sie liefern ein NEUES options-dict zurück
und lassen die Eingabe unangetastet.
"""
from __future__ import annotations

import copy
import re

from .const import CONF_LACROSSE_ID, CONF_SENSORS

FRIENDLY_NAME = "friendly_name"


class SensorConfigError(ValueError):
    """Basis für Validierungsfehler. ``error_key`` landet im Formular (errors[base])."""

    error_key = "unknown"


class DuplicateIdError(SensorConfigError):
    error_key = "id_in_use"


class EmptyNameError(SensorConfigError):
    error_key = "name_required"


class UnknownSlugError(SensorConfigError):
    error_key = "unknown_sensor"


def _slugify(name: str) -> str:
    """Schlanker, HA-unabhängiger Slug: klein, [a-z0-9_], keine Randstriche."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or "sensor"


def make_slug(friendly_name: str, existing) -> str:
    """Eindeutigen Slug aus dem Namen ableiten; bei Kollision _2, _3, … anhängen."""
    base = _slugify(friendly_name)
    existing_set = set(existing)
    if base not in existing_set:
        return base
    i = 2
    while f"{base}_{i}" in existing_set:
        i += 1
    return f"{base}_{i}"


def _sensors(options: dict) -> dict:
    return options.get(CONF_SENSORS, {})


def id_in_use(options: dict, lacrosse_id: int, *, ignore_slug: str | None = None) -> bool:
    """Ist die LaCrosse-ID bereits einem (anderen) Sensor zugeordnet?"""
    for slug, cfg in _sensors(options).items():
        if slug == ignore_slug:
            continue
        if cfg.get(CONF_LACROSSE_ID) == lacrosse_id:
            return True
    return False


def add_sensor(options: dict, lacrosse_id: int, friendly_name: str) -> dict:
    """Neuen Sensor anlegen. Wirft bei leerem Namen / belegter ID."""
    friendly_name = (friendly_name or "").strip()
    if not friendly_name:
        raise EmptyNameError
    if id_in_use(options, int(lacrosse_id)):
        raise DuplicateIdError

    new_options = copy.deepcopy(dict(options))
    sensors = new_options.setdefault(CONF_SENSORS, {})
    slug = make_slug(friendly_name, sensors.keys())
    sensors[slug] = {CONF_LACROSSE_ID: int(lacrosse_id), FRIENDLY_NAME: friendly_name}
    return new_options


def update_sensor(
    options: dict,
    slug: str,
    *,
    lacrosse_id: int | None = None,
    friendly_name: str | None = None,
) -> dict:
    """Namen und/oder ID eines bestehenden Sensors ändern (Slug bleibt stabil)."""
    if slug not in _sensors(options):
        raise UnknownSlugError

    new_options = copy.deepcopy(dict(options))
    cfg = new_options[CONF_SENSORS][slug]

    if friendly_name is not None:
        friendly_name = friendly_name.strip()
        if not friendly_name:
            raise EmptyNameError
        cfg[FRIENDLY_NAME] = friendly_name

    if lacrosse_id is not None:
        if id_in_use(new_options, int(lacrosse_id), ignore_slug=slug):
            raise DuplicateIdError
        cfg[CONF_LACROSSE_ID] = int(lacrosse_id)

    return new_options


def remove_sensor(options: dict, slug: str) -> dict:
    """Sensor entfernen."""
    if slug not in _sensors(options):
        raise UnknownSlugError
    new_options = copy.deepcopy(dict(options))
    del new_options[CONF_SENSORS][slug]
    return new_options


def sensor_labels(options: dict) -> dict:
    """slug -> Anzeigelabel ("Name (ID 56)") für Auswahl-Dropdowns."""
    return {
        slug: f"{cfg.get(FRIENDLY_NAME, slug)} (ID {cfg.get(CONF_LACROSSE_ID)})"
        for slug, cfg in _sensors(options).items()
    }

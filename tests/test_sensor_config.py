"""Tests für die HA-freien Sensor-Verwaltungs-Helfer (_sensor_config).

Diese Funktionen tragen die eigentliche Options-Flow-Logik (Slug-Erzeugung,
Hinzufügen/Ändern/Entfernen, ID-/Namens-Validierung) und sind ohne Home Assistant
testbar. Geprüft wird außerdem die Nebenwirkungsfreiheit (Eingabe bleibt unberührt).
"""
import pytest

from custom_components.jeelink_lacrosse import _sensor_config as sc
from custom_components.jeelink_lacrosse.const import CONF_LACROSSE_ID, CONF_SENSORS

FN = sc.FRIENDLY_NAME


def _opts(sensors=None):
    return {CONF_SENSORS: sensors or {}}


# --- Slug --------------------------------------------------------------------

@pytest.mark.parametrize(
    "name,expected",
    [
        ("Badezimmer", "badezimmer"),
        ("Wohnzimmer Nord", "wohnzimmer_nord"),
        ("  Küche!! ", "k_che"),       # nicht-ASCII fällt weg, Ränder getrimmt
        ("***", "sensor"),               # leerer Slug -> Fallback
    ],
)
def test_slugify_examples(name, expected):
    assert sc._slugify(name) == expected


def test_make_slug_resolves_collisions():
    existing = {"badezimmer", "badezimmer_2"}
    assert sc.make_slug("Badezimmer", existing) == "badezimmer_3"
    assert sc.make_slug("Keller", existing) == "keller"


# --- add_sensor --------------------------------------------------------------

def test_add_sensor_creates_entry_with_slug():
    options = _opts()
    new = sc.add_sensor(options, 56, "Badezimmer")

    assert new[CONF_SENSORS] == {
        "badezimmer": {CONF_LACROSSE_ID: 56, FN: "Badezimmer"}
    }
    # Eingabe unverändert (Nebenwirkungsfreiheit)
    assert options == _opts()


def test_add_sensor_coerces_id_to_int():
    new = sc.add_sensor(_opts(), "56", "Bad")
    assert new[CONF_SENSORS]["bad"][CONF_LACROSSE_ID] == 56


def test_add_sensor_rejects_empty_name():
    with pytest.raises(sc.EmptyNameError) as exc:
        sc.add_sensor(_opts(), 56, "   ")
    assert exc.value.error_key == "name_required"


def test_add_sensor_rejects_duplicate_id():
    options = _opts({"bad": {CONF_LACROSSE_ID: 56, FN: "Bad"}})
    with pytest.raises(sc.DuplicateIdError) as exc:
        sc.add_sensor(options, 56, "Zweitname")
    assert exc.value.error_key == "id_in_use"


def test_add_two_sensors_same_name_get_distinct_slugs():
    options = sc.add_sensor(_opts(), 56, "Bad")
    options = sc.add_sensor(options, 57, "Bad")
    assert set(options[CONF_SENSORS]) == {"bad", "bad_2"}


# --- update_sensor -----------------------------------------------------------

def test_update_sensor_changes_name_and_id():
    options = _opts({"bad": {CONF_LACROSSE_ID: 56, FN: "Bad"}})
    new = sc.update_sensor(options, "bad", lacrosse_id=60, friendly_name="Badezimmer")
    cfg = new[CONF_SENSORS]["bad"]
    assert cfg[CONF_LACROSSE_ID] == 60
    assert cfg[FN] == "Badezimmer"
    # Slug bleibt stabil, Eingabe unberührt
    assert options[CONF_SENSORS]["bad"][CONF_LACROSSE_ID] == 56


def test_update_sensor_keeps_own_id_allowed():
    """Gleiche ID für denselben Sensor ist erlaubt (nur Name geändert)."""
    options = _opts({"bad": {CONF_LACROSSE_ID: 56, FN: "Bad"}})
    new = sc.update_sensor(options, "bad", lacrosse_id=56, friendly_name="Bad neu")
    assert new[CONF_SENSORS]["bad"][FN] == "Bad neu"


def test_update_sensor_rejects_id_used_by_other():
    options = _opts(
        {
            "bad": {CONF_LACROSSE_ID: 56, FN: "Bad"},
            "kel": {CONF_LACROSSE_ID: 12, FN: "Keller"},
        }
    )
    with pytest.raises(sc.DuplicateIdError):
        sc.update_sensor(options, "bad", lacrosse_id=12)


def test_update_sensor_unknown_slug_raises():
    with pytest.raises(sc.UnknownSlugError) as exc:
        sc.update_sensor(_opts(), "nope", friendly_name="X")
    assert exc.value.error_key == "unknown_sensor"


# --- remove_sensor -----------------------------------------------------------

def test_remove_sensor():
    options = _opts(
        {
            "bad": {CONF_LACROSSE_ID: 56, FN: "Bad"},
            "kel": {CONF_LACROSSE_ID: 12, FN: "Keller"},
        }
    )
    new = sc.remove_sensor(options, "bad")
    assert set(new[CONF_SENSORS]) == {"kel"}
    assert set(options[CONF_SENSORS]) == {"bad", "kel"}   # Original unberührt


def test_remove_sensor_unknown_slug_raises():
    with pytest.raises(sc.UnknownSlugError):
        sc.remove_sensor(_opts(), "nope")


# --- sensor_labels / id_in_use ----------------------------------------------

def test_sensor_labels():
    options = _opts({"bad": {CONF_LACROSSE_ID: 56, FN: "Badezimmer"}})
    assert sc.sensor_labels(options) == {"bad": "Badezimmer (ID 56)"}


def test_id_in_use_ignore_slug():
    options = _opts({"bad": {CONF_LACROSSE_ID: 56, FN: "Bad"}})
    assert sc.id_in_use(options, 56) is True
    assert sc.id_in_use(options, 56, ignore_slug="bad") is False
    assert sc.id_in_use(options, 99) is False

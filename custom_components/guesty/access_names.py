"""Localized door names for the public guest access portal."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

ACCESS_DOOR_LANGUAGES: Final = ("de", "en", "es", "fr")

DEFAULT_FIRST_DOOR_NAMES: Final = {
    "de": "Haustür",
    "en": "Front door",
    "es": "Puerta principal",
    "fr": "Porte d’entrée",
}

DEFAULT_SECOND_DOOR_NAMES: Final = {
    "de": "Wohnungstür",
    "en": "Apartment door",
    "es": "Puerta del apartamento",
    "fr": "Porte de l’appartement",
}

_STANDARD_DOOR_NAMES: Final = (
    DEFAULT_FIRST_DOOR_NAMES,
    DEFAULT_SECOND_DOOR_NAMES,
    {
        "de": "Hintertür",
        "en": "Back door",
        "es": "Puerta trasera",
        "fr": "Porte arrière",
    },
    {
        "de": "Garagentor",
        "en": "Garage door",
        "es": "Puerta del garaje",
        "fr": "Porte de garage",
    },
    {
        "de": "Gartentor",
        "en": "Garden gate",
        "es": "Puerta del jardín",
        "fr": "Portail du jardin",
    },
)


def _normalized(value: str) -> str:
    """Normalize a label for the small built-in translation glossary."""
    return " ".join(value.strip().casefold().replace("’", "'").split())


_STANDARD_NAME_LOOKUP: Final = {
    _normalized(label): names
    for names in _STANDARD_DOOR_NAMES
    for label in names.values()
}


def suggest_door_name(name: str, language: str) -> str:
    """Translate known generic door names locally or preserve a custom label."""
    names = _STANDARD_NAME_LOOKUP.get(_normalized(name))
    if names is None:
        return name
    return names.get(language, name)


def localized_door_names(
    item: Mapping[str, Any], defaults: Mapping[str, str]
) -> dict[str, str]:
    """Return complete localized labels from new or legacy mapping data."""
    legacy = str(item.get("name") or item.get("name_de") or defaults["de"]).strip()
    legacy = (legacy or defaults["de"])[:80]
    names: dict[str, str] = {}
    for language in ACCESS_DOOR_LANGUAGES:
        configured = item.get(f"name_{language}")
        if isinstance(configured, str) and configured.strip():
            names[language] = configured.strip()[:80]
        else:
            names[language] = suggest_door_name(legacy, language)[:80]
    return names


def localized_door_name(door: Mapping[str, str], language: str) -> str:
    """Select one configured portal label with a legacy fallback."""
    value = door.get(f"name_{language}") or door.get("name") or "Door"
    return value.strip() or "Door"

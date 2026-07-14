"""Tests for local, privacy-preserving door label translations."""

from custom_components.guesty.access_names import (
    DEFAULT_FIRST_DOOR_NAMES,
    localized_door_names,
)


def test_legacy_generic_name_receives_local_suggestions() -> None:
    """Existing German setups work in every supported portal language."""
    assert localized_door_names({"name": "Haustür"}, DEFAULT_FIRST_DOOR_NAMES) == {
        "de": "Haustür",
        "en": "Front door",
        "es": "Puerta principal",
        "fr": "Porte d’entrée",
    }


def test_known_name_is_recognized_from_another_language() -> None:
    """The glossary is bidirectional for its known generic names."""
    assert localized_door_names(
        {"name": "Porte d'entrée"}, DEFAULT_FIRST_DOOR_NAMES
    ) == {
        "de": "Haustür",
        "en": "Front door",
        "es": "Puerta principal",
        "fr": "Porte d’entrée",
    }


def test_custom_labels_override_unknown_name_fallback() -> None:
    """Unknown private names stay local and explicit translations win."""
    assert localized_door_names(
        {
            "name": "Poolhaus Nord",
            "name_en": "North pool house",
            "name_es": "Casa de piscina norte",
            "name_fr": "Pavillon de piscine nord",
        },
        DEFAULT_FIRST_DOOR_NAMES,
    ) == {
        "de": "Poolhaus Nord",
        "en": "North pool house",
        "es": "Casa de piscina norte",
        "fr": "Pavillon de piscine nord",
    }

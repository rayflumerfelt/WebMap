"""Tests for the JSON document upgrade chain. `02-data-model.md` §6."""

from collections.abc import Iterator

import pytest

from webmap_core.versioning import _UPGRADES, Document, upgrade, upgrader


@pytest.fixture(autouse=True)
def _isolate_registry() -> Iterator[None]:
    """The registry is module-global; keep tests from leaking into each other."""
    saved = dict(_UPGRADES)
    _UPGRADES.clear()
    yield
    _UPGRADES.clear()
    _UPGRADES.update(saved)


def test_chains_upgrades_one_version_at_a_time() -> None:
    @upgrader("palette", 1)
    def _v1_to_v2(doc: Document) -> Document:
        return {**doc, "interpolation": "linear"}

    @upgrader("palette", 2)
    def _v2_to_v3(doc: Document) -> Document:
        return {**doc, "is_continuous": True}

    result = upgrade("palette", {"schema_version": 1, "name": "viridis"}, target=3)

    assert result == {
        "schema_version": 3,
        "name": "viridis",
        "interpolation": "linear",
        "is_continuous": True,
    }


def test_a_document_already_at_target_is_untouched() -> None:
    doc = {"schema_version": 3, "name": "viridis"}

    assert upgrade("palette", dict(doc), target=3) == doc


def test_missing_schema_version_is_treated_as_v1() -> None:
    """Documents written before versioning existed carry no marker."""

    @upgrader("session", 1)
    def _v1_to_v2(doc: Document) -> Document:
        return {**doc, "view": {"zoom": 6}}

    assert upgrade("session", {"layers": []}, target=2)["schema_version"] == 2


def test_a_gap_in_the_chain_names_the_missing_step() -> None:
    """Upgrade paths are never deleted. When one is missing, say which."""

    @upgrader("session", 1)
    def _v1_to_v2(doc: Document) -> Document:
        return doc

    with pytest.raises(ValueError, match=r"session v2 -> v3"):
        upgrade("session", {"schema_version": 1}, target=3)


def test_a_document_from_the_future_is_refused_clearly() -> None:
    """A newer WebMap wrote it. Downgrading in place would lose fields."""
    with pytest.raises(ValueError, match="upgrade this deployment"):
        upgrade("session", {"schema_version": 5}, target=3)


def test_two_upgrades_from_one_version_is_a_registration_error() -> None:
    """An ambiguous chain silently picks one. Fail at import time instead."""

    @upgrader("palette", 1)
    def _first(doc: Document) -> Document:
        return doc

    with pytest.raises(ValueError, match="already registered"):

        @upgrader("palette", 1)
        def _second(doc: Document) -> Document:
            return doc

"""Explicit subtree assignments must take precedence over root ownership."""

import pytest

from josh_room.composition_lifecycle import _outcome


@pytest.mark.parametrize("outcome", ["local-only", "child", "parent"])
def test_single_character_assignment_overrides_root(outcome):
    assignments = [{"path": ".", "outcome": "parent"}, {"path": "a", "outcome": outcome}]
    assert _outcome("a", assignments) == outcome
    assert _outcome("a/payload", assignments) == outcome
    assert _outcome("another/payload", assignments) == "parent"


def test_deepest_subtree_wins_independently_of_outcome_text():
    assignments = [
        {"path": ".", "outcome": "parent"},
        {"path": "a", "outcome": "local-only"},
        {"path": "a/nested", "outcome": "child"},
    ]
    assert _outcome("a/nested/payload", assignments) == "child"
    assert _outcome("a/other", assignments) == "local-only"

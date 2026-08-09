"""Characterization tests for enforced_limits_from_config (src/database.py).

Locks down the one rule that costs the most to get wrong: ALLOWED_WEEKDAYS and
LIMITS_PER_WEEKDAYS are POSITIONAL against each other on the host, not indexed
by Monday..Sunday (CLAUDE.md invariant 4). This function's behavior must be
preserved verbatim in the rewritten model (src/models.py).
"""
from src.database import enforced_limits_from_config


def test_positional_pairing_not_weekday_indexed():
    """Host reports days out of order -- pairing must follow list position,
    not day number."""
    config = {
        'ALLOWED_WEEKDAYS': [3, 1, 5],
        'LIMITS_PER_WEEKDAYS': [111, 222, 333],
    }
    result = enforced_limits_from_config(config)
    assert result[3] == 111
    assert result[1] == 222
    assert result[5] == 333
    # Days absent from ALLOWED_WEEKDAYS are 0 (blocked), not missing.
    assert result[2] == 0
    assert result[4] == 0
    assert result[6] == 0
    assert result[7] == 0


def test_explicit_empty_means_every_day_blocked():
    config = {'ALLOWED_WEEKDAYS': '', 'LIMITS_PER_WEEKDAYS': ''}
    result = enforced_limits_from_config(config)
    assert result == {d: 0 for d in range(1, 8)}


def test_real_fixture_full_week():
    """From the live capture: every day allowed, limits are positional 1:1
    since ALLOWED_WEEKDAYS is already 1..7 in order here."""
    config = {
        'ALLOWED_WEEKDAYS': [1, 2, 3, 4, 5, 6, 7],
        'LIMITS_PER_WEEKDAYS': [10800, 900, 7200, 7200, 7200, 10800, 5400],
    }
    result = enforced_limits_from_config(config)
    assert result == {1: 10800, 2: 900, 3: 7200, 4: 7200, 5: 7200, 6: 10800, 7: 5400}


def test_unparseable_config_returns_none_not_empty_dict():
    """An unparseable host reply must not be mistaken for 'every day blocked' --
    that distinction is what keeps a bad read from showing false drift."""
    assert enforced_limits_from_config({'ALLOWED_WEEKDAYS': 'garbage'}) is None
    assert enforced_limits_from_config(None) is None
    assert enforced_limits_from_config({}) is None


def test_mismatched_list_lengths_returns_none():
    config = {'ALLOWED_WEEKDAYS': [1, 2], 'LIMITS_PER_WEEKDAYS': [100]}
    assert enforced_limits_from_config(config) is None

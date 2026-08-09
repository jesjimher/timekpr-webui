"""Characterization tests for UserDailyTimeInterval.to_timekpr_format
(src/database.py) -- converts a start/end time into timekpr's hour-token
format, including partial-hour minute specs (e.g. '9[30-59]'). This must be
preserved verbatim in the rewritten model.
"""
from src.database import UserDailyTimeInterval


def _interval(start_hour, start_minute, end_hour, end_minute, enabled=True):
    return UserDailyTimeInterval(
        user_id=1, day_of_week=1,
        start_hour=start_hour, start_minute=start_minute,
        end_hour=end_hour, end_minute=end_minute,
        is_enabled=enabled,
    )


def test_disabled_interval_returns_none():
    assert _interval(9, 0, 17, 0, enabled=False).to_timekpr_format() is None


def test_full_hours_are_plain_numbers():
    result = _interval(9, 0, 12, 0).to_timekpr_format()
    assert result == ['9', '10', '11']


def test_same_hour_partial_range():
    result = _interval(9, 15, 9, 45).to_timekpr_format()
    assert result == ['9[15-45]']


def test_partial_start_full_middle_partial_end():
    result = _interval(9, 30, 12, 15).to_timekpr_format()
    assert result == ['9[30-59]', '10', '11', '12[0-15]']


def test_partial_start_no_partial_end():
    result = _interval(9, 30, 11, 0).to_timekpr_format()
    assert result == ['9[30-59]', '10']

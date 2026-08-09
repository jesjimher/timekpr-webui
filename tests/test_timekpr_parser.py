"""Characterization tests for the timekpra output parser (src/ssh_helper.py).

These lock down the exact semantics the rest of the app depends on -- most of
which cost real bugs to discover (see CLAUDE.md). They must keep passing
unchanged through the data-model rewrite: SSHClient._parse_timekpr_output is
one of the few pieces of code the rewrite does not touch.
"""
import os

from src.ssh_helper import SSHClient

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures')


def _client():
    return SSHClient(hostname='unused', key_path='/nonexistent')


def test_parses_real_userinfo_capture():
    """Real capture from a live host (see tests/fixtures/userinfo_athlon.txt)."""
    with open(os.path.join(FIXTURES, 'userinfo_athlon.txt')) as f:
        output = f.read()
    config = _client()._parse_timekpr_output(output)

    assert config['TIME_LEFT_DAY'] == 4523
    assert config['ALLOWED_WEEKDAYS'] == [1, 2, 3, 4, 5, 6, 7]
    assert config['LIMITS_PER_WEEKDAYS'] == [10800, 900, 7200, 7200, 7200, 10800, 5400]
    # Full day (Monday, no restriction) parses as 24 integers, not a bare string.
    assert config['ALLOWED_HOURS_1'] == list(range(24))
    # A restricted day (Tuesday: 15 min budget) parses to its actual hour list.
    assert config['ALLOWED_HOURS_2'] == [9, 10, 11, 12, 13]
    assert config['TRACK_INACTIVE'] is False
    assert config['PLAYTIME_ACTIVITIES'] == '.*Steam.*'


def test_numeric_keys_with_digits_are_matched():
    """The key regex must match keys containing digits (ALLOWED_HOURS_1..7),
    not just [A-Z_]+ -- a prior bug silently dropped these keys entirely."""
    output = "ALLOWED_HOURS_7: 9;10;11\nLIMIT_PER_WEEK: 604800\n"
    config = _client()._parse_timekpr_output(output)
    assert config['ALLOWED_HOURS_7'] == [9, 10, 11]
    assert config['LIMIT_PER_WEEK'] == 604800


def test_negative_values_parse_as_int_not_string():
    """TIME_LEFT_DAY can go negative when a user is over budget; str.isdigit()
    rejects the leading '-', which used to leave it as a string."""
    output = "TIME_LEFT_DAY: -120\n"
    config = _client()._parse_timekpr_output(output)
    assert config['TIME_LEFT_DAY'] == -120
    assert isinstance(config['TIME_LEFT_DAY'], int)


def test_empty_value_stays_empty_string():
    """An explicitly empty ALLOWED_WEEKDAYS (host blocks every day) must not be
    coerced into None or a list -- callers rely on '' meaning 'reported, and empty'."""
    output = "ALLOWED_WEEKDAYS: \n"
    config = _client()._parse_timekpr_output(output)
    assert config['ALLOWED_WEEKDAYS'] == ''


def test_semicolon_list_of_non_ints_kept_as_strings():
    output = "PLAYTIME_ACTIVITIES: .*Steam.*;.*Lutris.*\n"
    config = _client()._parse_timekpr_output(output)
    assert config['PLAYTIME_ACTIVITIES'] == ['.*Steam.*', '.*Lutris.*']


def test_bool_values():
    output = "HIDE_TRAY_ICON: false\nTRACK_INACTIVE: true\n"
    config = _client()._parse_timekpr_output(output)
    assert config['HIDE_TRAY_ICON'] is False
    assert config['TRACK_INACTIVE'] is True

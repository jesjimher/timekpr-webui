import json
from datetime import date, timedelta

from src.models import (
    db, User, Host, Account, DayLimit, Usage, TimeBonus,
    config_mismatch_detail, enforced_limits_from_config,
)


def _make_user_with_two_hosts(username='guillem'):
    user = User(username=username)
    db.session.add(user)
    db.session.commit()

    h1 = Host(ip='athlon.local')
    h2 = Host(ip='menjador.local')
    db.session.add_all([h1, h2])
    db.session.commit()

    a1 = Account(user_id=user.id, host_id=h1.id)
    a2 = Account(user_id=user.id, host_id=h2.id)
    db.session.add_all([a1, a2])
    db.session.commit()
    return user, h1, h2, a1, a2


# ---------------------------------------------------------------- DayLimit.hour_tokens


def test_hour_tokens_disabled_means_full_day(app):
    dl = DayLimit(user_id=1, day_of_week=1, hours_enabled=False)
    assert dl.hour_tokens() == [str(h) for h in range(24)]


def test_hour_tokens_full_hours(app):
    dl = DayLimit(user_id=1, day_of_week=1, hours_enabled=True,
                   start_hour=9, start_minute=0, end_hour=12, end_minute=0)
    assert dl.hour_tokens() == ['9', '10', '11']


def test_hour_tokens_partial_start_and_end(app):
    dl = DayLimit(user_id=1, day_of_week=1, hours_enabled=True,
                   start_hour=9, start_minute=30, end_hour=12, end_minute=15)
    assert dl.hour_tokens() == ['9[30-59]', '10', '11', '12[0-15]']


def test_hour_tokens_same_hour_partial_range(app):
    dl = DayLimit(user_id=1, day_of_week=1, hours_enabled=True,
                   start_hour=9, start_minute=15, end_hour=9, end_minute=45)
    assert dl.hour_tokens() == ['9[15-45]']


def test_hour_tokens_invalid_interval_falls_back_to_full_day(app):
    dl = DayLimit(user_id=1, day_of_week=1, hours_enabled=True,
                   start_hour=17, start_minute=0, end_hour=9, end_minute=0)
    assert dl.hour_tokens() == [str(h) for h in range(24)]


# ---------------------------------------------------------------- User schedule


def test_expected_limits_none_when_never_saved(app):
    user = User(username='guillem')
    db.session.add(user)
    db.session.commit()
    assert user.expected_limits() is None


def test_expected_limits_after_ensure(app):
    user = User(username='guillem')
    db.session.add(user)
    db.session.commit()
    user.ensure_day_limits()
    limits = user.expected_limits()
    assert limits == {d: 0 for d in range(1, 8)}


def test_expected_limits_reflects_saved_schedule(app):
    user = User(username='guillem')
    db.session.add(user)
    db.session.commit()
    db.session.add(DayLimit(user_id=user.id, day_of_week=2, limit_seconds=900))
    db.session.add(DayLimit(user_id=user.id, day_of_week=6, limit_seconds=10800))
    db.session.commit()
    limits = user.expected_limits()
    assert limits[2] == 900
    assert limits[6] == 10800
    assert limits[1] == 0  # day with no row is implicitly blocked


# ---------------------------------------------------------------- pool

def test_pool_target_none_without_schedule(app):
    user, h1, h2, a1, a2 = _make_user_with_two_hosts()
    assert user.pool_target_seconds() is None


def test_pool_target_combines_limit_bonus_and_spent(app):
    user, h1, h2, a1, a2 = _make_user_with_two_hosts()
    for d in range(1, 8):
        db.session.add(DayLimit(user_id=user.id, day_of_week=d, limit_seconds=7200))
    db.session.commit()

    today = date.today()
    db.session.add(Usage(account_id=a1.id, date=today, seconds=1200))
    db.session.add(Usage(account_id=a2.id, date=today, seconds=300))
    user.add_bonus(600)
    db.session.commit()

    # limit 7200 + bonus 600 - spent (1200+300) = 6300
    assert user.pool_target_seconds() == 6300


def test_pool_target_never_negative(app):
    user, h1, h2, a1, a2 = _make_user_with_two_hosts()
    for d in range(1, 8):
        db.session.add(DayLimit(user_id=user.id, day_of_week=d, limit_seconds=60))
    db.session.commit()
    db.session.add(Usage(account_id=a1.id, date=date.today(), seconds=5000))
    db.session.commit()
    assert user.pool_target_seconds() == 0


# ---------------------------------------------------------------- config_mismatch_detail

def test_mismatch_detail_ok_when_nothing_configured(app):
    user, h1, h2, a1, a2 = _make_user_with_two_hosts()
    a1.last_config = json.dumps({'ALLOWED_WEEKDAYS': [1], 'LIMITS_PER_WEEKDAYS': [100]})
    ok, detail = config_mismatch_detail(user, a1)
    assert ok is True and detail is None


def test_mismatch_detail_ok_when_no_config_read_yet(app):
    user, h1, h2, a1, a2 = _make_user_with_two_hosts()
    for d in range(1, 8):
        db.session.add(DayLimit(user_id=user.id, day_of_week=d, limit_seconds=7200))
    db.session.commit()
    ok, detail = config_mismatch_detail(user, a1)  # last_config still None
    assert ok is True and detail is None


def test_mismatch_detail_flags_limit_mismatch(app):
    user, h1, h2, a1, a2 = _make_user_with_two_hosts()
    for d in range(1, 8):
        db.session.add(DayLimit(user_id=user.id, day_of_week=d, limit_seconds=900))
    db.session.commit()

    a1.last_config = json.dumps({
        'ALLOWED_WEEKDAYS': [1, 2, 3, 4, 5, 6, 7],
        'LIMITS_PER_WEEKDAYS': [10800, 900, 7200, 7200, 7200, 10800, 5400],
    })
    ok, detail = config_mismatch_detail(user, a1)
    assert ok is False
    assert 'Mon' in detail  # Monday: host 3h vs configured 15m


def test_mismatch_detail_matches_real_fixture_when_schedule_agrees(app):
    """Uses the exact real-world numbers captured in tests/fixtures --
    schedule that matches must report no drift."""
    user, h1, h2, a1, a2 = _make_user_with_two_hosts()
    limits = [10800, 900, 7200, 7200, 7200, 10800, 5400]
    for d, seconds in zip(range(1, 8), limits):
        db.session.add(DayLimit(user_id=user.id, day_of_week=d, limit_seconds=seconds))
    db.session.commit()

    a1.last_config = json.dumps({
        'ALLOWED_WEEKDAYS': [1, 2, 3, 4, 5, 6, 7],
        'LIMITS_PER_WEEKDAYS': limits,
    })
    ok, detail = config_mismatch_detail(user, a1)
    assert ok is True and detail is None


def test_mismatch_detail_flags_hours_mismatch(app):
    user, h1, h2, a1, a2 = _make_user_with_two_hosts()
    for d in range(1, 8):
        limit_seconds = 900 if d == 2 else 0
        db.session.add(DayLimit(
            user_id=user.id, day_of_week=d, limit_seconds=limit_seconds,
            hours_enabled=(d == 2), start_hour=9, start_minute=0, end_hour=13, end_minute=0,
        ))
    db.session.commit()

    a1.last_config = json.dumps({
        'ALLOWED_WEEKDAYS': [1, 2, 3, 4, 5, 6, 7],
        'LIMITS_PER_WEEKDAYS': [0, 900, 0, 0, 0, 0, 0],
        'ALLOWED_HOURS_2': [9, 10, 11],  # host has 9-11, we want 9-12
    })
    ok, detail = config_mismatch_detail(user, a1)
    assert ok is False
    assert 'Tue' in detail


def _blocked_saturday_user():
    user, h1, h2, a1, a2 = _make_user_with_two_hosts()
    for d in range(1, 8):
        db.session.add(DayLimit(user_id=user.id, day_of_week=d,
                                limit_seconds=0 if d == 6 else 8100))
    db.session.commit()
    return user, a1


def test_mismatch_detail_flags_blocked_day_left_out_of_weekday_list(app):
    """A blocked day omitted from ALLOWED_WEEKDAYS reads as a 0 limit, but
    timekpr's --settimeleft indexes LIMITS_PER_WEEKDAYS by Mon..Sun position
    and fails on a short list -- so it must still count as a mismatch."""
    user, a1 = _blocked_saturday_user()
    a1.last_config = json.dumps({
        'ALLOWED_WEEKDAYS': [1, 2, 3, 4, 5, 7],
        'LIMITS_PER_WEEKDAYS': [8100] * 6,
    })
    ok, detail = config_mismatch_detail(user, a1)
    assert ok is False
    assert 'weekday list incomplete' in detail


def test_mismatch_detail_ok_with_blocked_day_sent_as_zero(app):
    user, a1 = _blocked_saturday_user()
    a1.last_config = json.dumps({
        'ALLOWED_WEEKDAYS': [1, 2, 3, 4, 5, 6, 7],
        'LIMITS_PER_WEEKDAYS': [8100, 8100, 8100, 8100, 8100, 0, 8100],
    })
    ok, detail = config_mismatch_detail(user, a1)
    assert ok is True and detail is None


# ---------------------------------------------------------------- allowed-hours cap on time left

from datetime import datetime

from src.models import allowed_seconds_remaining, allowed_window_end

# 2026-11-17 is a Tuesday (isoweekday 2).
TUESDAY_1930 = datetime(2026, 11, 17, 19, 30)
UNTIL_20 = [str(h) for h in range(9, 20)]  # 09:00-20:00


def test_allowed_remaining_full_day():
    assert allowed_seconds_remaining([str(h) for h in range(24)], TUESDAY_1930) == 4.5 * 3600


def test_allowed_remaining_window_ending_at_20():
    assert allowed_seconds_remaining(UNTIL_20, TUESDAY_1930) == 1800


def test_allowed_remaining_partial_end_hour():
    assert allowed_seconds_remaining(['18', '19[0-45]'], TUESDAY_1930) == 15 * 60


def test_allowed_remaining_after_window_is_zero():
    assert allowed_seconds_remaining(['9', '10'], TUESDAY_1930) == 0


def test_allowed_remaining_before_window_counts_whole_window():
    now = datetime(2026, 11, 17, 7, 0)
    assert allowed_seconds_remaining(['9[30-59]', '10', '11[0-15]'], now) == (30 + 60 + 15) * 60


def test_allowed_remaining_scalar_and_garbage():
    assert allowed_seconds_remaining('19', TUESDAY_1930) == 1800
    assert allowed_seconds_remaining(['19', 'x'], TUESDAY_1930) is None
    assert allowed_seconds_remaining(None, TUESDAY_1930) is None


def test_allowed_window_end():
    assert allowed_window_end(UNTIL_20, TUESDAY_1930) == 20 * 3600
    assert allowed_window_end(['9', '10'], TUESDAY_1930) is None
    assert allowed_window_end(['9', '21[0-30]'], TUESDAY_1930) == 21 * 3600 + 30 * 60


def _account_with_config(cfg):
    _, _, _, a1, _ = _make_user_with_two_hosts()
    a1.last_config = json.dumps(cfg)
    db.session.commit()
    return a1


def test_effective_time_left_capped_by_window(app):
    a = _account_with_config({'TIME_LEFT_DAY': 3600, 'ALLOWED_HOURS_2': UNTIL_20})
    assert a.time_left() == 3600
    assert a.effective_time_left(TUESDAY_1930) == 1800


def test_effective_time_left_without_hours_key(app):
    a = _account_with_config({'TIME_LEFT_DAY': 3600})
    assert a.effective_time_left(TUESDAY_1930) == 3600


def test_effective_time_left_window_larger_than_budget(app):
    a = _account_with_config({'TIME_LEFT_DAY': 3600, 'ALLOWED_HOURS_2': [str(h) for h in range(24)]})
    assert a.effective_time_left(TUESDAY_1930) == 3600

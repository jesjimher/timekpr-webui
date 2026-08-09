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
        'ALLOWED_WEEKDAYS': [2],
        'LIMITS_PER_WEEKDAYS': [900],
        'ALLOWED_HOURS_2': [9, 10, 11],  # host has 9-11, we want 9-12
    })
    ok, detail = config_mismatch_detail(user, a1)
    assert ok is False
    assert 'Tue' in detail

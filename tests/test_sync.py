"""Tests for the convergence decisions in src/sync.py, against a fake
SSHClient (no network). This loop decides whether a child's computer is
allowed to keep running -- it replaced ~550 lines of the old push/read/
reconcile machinery with no equivalent test coverage of its own, so it gets
exercised directly here rather than trusting manual observation against real
hosts to catch a bad decision.
"""
from datetime import date, datetime, timedelta

import src.sync as sync
from src.models import db, User, Host, Account, DayLimit, Usage


class FakeSSHClient:
    """configs: {ip: config_dict}. An ip missing from configs simulates an
    unreachable host (raises on connect, like the real client would)."""
    configs = {}
    calls = []

    def __init__(self, hostname, **kwargs):
        self.hostname = hostname

    def __enter__(self):
        if self.hostname not in FakeSSHClient.configs:
            raise ConnectionError(f"{self.hostname} unreachable")
        return self

    def __exit__(self, *a):
        return False

    def validate_user(self, username):
        return True, 'ok', dict(FakeSSHClient.configs[self.hostname])

    def modify_time_left(self, username, operation, seconds):
        FakeSSHClient.calls.append(('modify_time_left', self.hostname, operation, seconds))
        return True, 'ok'

    def set_day_limits(self, username, day_limits, current_config):
        FakeSSHClient.calls.append(('set_day_limits', self.hostname))
        return True, 'ok'


def _setup(monkeypatch, configs):
    FakeSSHClient.configs = configs
    FakeSSHClient.calls = []
    monkeypatch.setattr(sync, 'SSHClient', FakeSSHClient)


def _matching_config(limit_seconds_by_day, time_left, spent=0):
    return {
        'ALLOWED_WEEKDAYS': list(range(1, 8)),
        'LIMITS_PER_WEEKDAYS': [limit_seconds_by_day.get(d, 0) for d in range(1, 8)],
        'TIME_LEFT_DAY': time_left,
        'TIME_SPENT_DAY': spent,
    }


def _make_user(username='guillem', daily_limit=7200):
    user = User(username=username)
    db.session.add(user)
    db.session.commit()
    for d in range(1, 8):
        db.session.add(DayLimit(user_id=user.id, day_of_week=d, limit_seconds=daily_limit))
    db.session.commit()
    return user


def _add_account(user, ip):
    host = Host(ip=ip)
    db.session.add(host)
    db.session.commit()
    account = Account(user_id=user.id, host_id=host.id)
    db.session.add(account)
    db.session.commit()
    return account


# ---------------------------------------------------------------- no-op cases

def test_no_schedule_no_writes(app, monkeypatch):
    """A user with no day_limit rows at all: nothing to pool-enforce."""
    user = User(username='guillem')
    db.session.add(user)
    db.session.commit()
    a1 = _add_account(user, 'host-a')
    _setup(monkeypatch, {'host-a': {'ALLOWED_WEEKDAYS': '', 'LIMITS_PER_WEEKDAYS': '',
                                     'TIME_LEFT_DAY': 999, 'TIME_SPENT_DAY': 0}})

    manager = sync.SyncManager()
    manager._sync_user(user, threshold=300)

    assert FakeSSHClient.calls == []


def test_idle_day_everything_already_matching_no_writes(app, monkeypatch):
    """Config matches the schedule and TIME_LEFT_DAY already equals the pool
    target: the whole point of differential writes is zero traffic here."""
    user = _make_user(daily_limit=7200)
    a1 = _add_account(user, 'host-a')
    a2 = _add_account(user, 'host-b')
    limits = {d: 7200 for d in range(1, 8)}
    cfg = _matching_config(limits, time_left=7200)  # nobody has spent anything
    _setup(monkeypatch, {'host-a': cfg, 'host-b': cfg})

    manager = sync.SyncManager()
    manager._sync_user(user, threshold=300)

    assert FakeSSHClient.calls == []
    assert a1.drift_since is None
    assert a2.drift_since is None


# ---------------------------------------------------------------- pool convergence

def test_inactive_host_corrected_active_host_left_alone(app, monkeypatch):
    """Usage happened on host-a (its own TIME_LEFT_DAY already reflects it, and
    host-a's own current_left already equals the new pool target -- that's what
    'the host in use counts down on its own' means). host-b hasn't moved, so it
    is now far from the shared pool and must be corrected down."""
    user = _make_user(daily_limit=7200)
    a1 = _add_account(user, 'host-a')  # active: spent 1800s
    a2 = _add_account(user, 'host-b')  # inactive: spent 0s, stale TIME_LEFT_DAY

    limits = {d: 7200 for d in range(1, 8)}
    # host-a itself reports having spent 1800s -- that's what the read step
    # turns into today's Usage row, which is what pool_target_seconds() sums.
    cfg_active = _matching_config(limits, time_left=5400, spent=1800)   # 7200 - 1800
    cfg_inactive = _matching_config(limits, time_left=7200, spent=0)    # hasn't moved
    _setup(monkeypatch, {'host-a': cfg_active, 'host-b': cfg_inactive})

    manager = sync.SyncManager()
    manager._sync_user(user, threshold=300)

    # pool target = 7200 - 1800 = 5400
    # host-a: delta = 5400 - 5400 = 0 -> no write
    # host-b: delta = 5400 - 7200 = -1800 -> beyond threshold -> corrected
    kinds = [c for c in FakeSSHClient.calls if c[0] == 'modify_time_left']
    assert len(kinds) == 1
    assert kinds[0] == ('modify_time_left', 'host-b', '-', 1800)


def test_small_delta_below_threshold_is_ignored(app, monkeypatch):
    user = _make_user(daily_limit=7200)
    a1 = _add_account(user, 'host-a')
    limits = {d: 7200 for d in range(1, 8)}
    # delta will be 7200 - 7150 = 50s, well under the 300s threshold
    _setup(monkeypatch, {'host-a': _matching_config(limits, time_left=7150)})

    manager = sync.SyncManager()
    manager._sync_user(user, threshold=300)

    assert FakeSSHClient.calls == []


def test_bonus_reaches_every_host_including_active(app, monkeypatch):
    """A +30min bonus is a big enough jump that it must propagate everywhere,
    even to the host whose own reads look 'active'. This is why there is no
    explicit 'skip the active host' rule: the propagation threshold already
    keeps a genuinely active host quiet on its own (timekpr counts down there
    in step with the pool, so its delta from read-cycle jitter alone stays
    under the threshold) -- a rule that also blocked bonuses would mean a
    bonus granted while a kid is playing never reaches the machine they're
    playing on, which is exactly where it should land."""
    user = _make_user(daily_limit=7200)
    a1 = _add_account(user, 'host-a')
    a2 = _add_account(user, 'host-b')
    limits = {d: 7200 for d in range(1, 8)}
    # both hosts currently at the old (pre-bonus) target
    _setup(monkeypatch, {
        'host-a': _matching_config(limits, time_left=7200),
        'host-b': _matching_config(limits, time_left=7200),
    })
    user.add_bonus(1800)

    manager = sync.SyncManager()
    manager._sync_user(user, threshold=300)

    kinds = {(c[1], c[2], c[3]) for c in FakeSSHClient.calls if c[0] == 'modify_time_left'}
    assert kinds == {('host-a', '+', 1800), ('host-b', '+', 1800)}


# ---------------------------------------------------------------- guardrail

def test_unreadable_sibling_blocks_grants_but_not_cuts(app, monkeypatch):
    """host-b can't be read this cycle. Its usage is frozen, so the computed
    pool looks bigger than it really is -- granting host-a time on that basis
    would be wrong. A cut is still safe and must still happen."""
    user = _make_user(daily_limit=7200)
    a1 = _add_account(user, 'host-a')
    _add_account(user, 'host-b')  # will be "offline" -- absent from configs

    limits = {d: 7200 for d in range(1, 8)}
    # host-a is far *below* the (inflated) pool target -- would normally grant
    _setup(monkeypatch, {'host-a': _matching_config(limits, time_left=1000)})

    manager = sync.SyncManager()
    manager._sync_user(user, threshold=300)

    assert FakeSSHClient.calls == []


def test_unreadable_sibling_still_allows_a_cut(app, monkeypatch):
    user = _make_user(daily_limit=7200)
    a1 = _add_account(user, 'host-a')
    _add_account(user, 'host-b')

    limits = {d: 7200 for d in range(1, 8)}
    # host-a is far *above* the pool target -- a cut is safe even with an
    # unreadable sibling, since usage can only be undercounted, never over.
    _setup(monkeypatch, {'host-a': _matching_config(limits, time_left=9000)})

    manager = sync.SyncManager()
    manager._sync_user(user, threshold=300)

    kinds = [c for c in FakeSSHClient.calls if c[0] == 'modify_time_left']
    assert len(kinds) == 1
    assert kinds[0][2] == '-'


# ---------------------------------------------------------------- config convergence

def test_config_mismatch_triggers_push_and_sets_drift_since(app, monkeypatch):
    user = _make_user(daily_limit=900)  # 15 min, like the incident schedule
    a1 = _add_account(user, 'host-a')
    # host reports a much bigger limit than configured
    cfg = _matching_config({d: 7200 for d in range(1, 8)}, time_left=7200)
    _setup(monkeypatch, {'host-a': cfg})

    manager = sync.SyncManager()
    manager._sync_user(user, threshold=300)

    assert ('set_day_limits', 'host-a') in FakeSSHClient.calls
    assert a1.drift_since is not None


def test_config_match_clears_drift_since(app, monkeypatch):
    user = _make_user(daily_limit=7200)
    a1 = _add_account(user, 'host-a')
    a1.drift_since = datetime.utcnow() - timedelta(hours=1)
    db.session.commit()

    cfg = _matching_config({d: 7200 for d in range(1, 8)}, time_left=7200)
    _setup(monkeypatch, {'host-a': cfg})

    manager = sync.SyncManager()
    manager._sync_user(user, threshold=300)

    assert a1.drift_since is None
    assert ('set_day_limits', 'host-a') not in FakeSSHClient.calls

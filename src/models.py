"""Data model for the rewritten TimeKpr WebUI.

Replaces src/database.py. Key structural change: the weekly schedule and
allowed-hours belong to the *user* (one child, one schedule), not to the
(user, host) pair. What's per-host is only ever observation -- what a host
last reported, and whether it currently matches the user's intent.

There is no stored "is_synced" flag anywhere. Sync state is derived by
comparing DayLimit (intent) against Account.last_config (the host's own last
report) at read time -- see config_mismatch_detail(). The only persisted piece
of verification state is Account.drift_since, kept purely to debounce the UI
alarm against the normal few-seconds window after an edit, before the next
sync cycle has had a chance to converge (see verification_state()).
"""
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta, date
import json
import bcrypt

db = SQLAlchemy()

DAY_ABBR = ('', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')

# A configuration mismatch must persist this long before the UI shows it as an
# alarm. Absorbs the normal convergence window right after an edit (up to one
# sync cycle) without absorbing a real stuck host.
DRIFT_ALARM_DELAY = 300  # seconds

# How long since the last successful read before a host counts as "unverified"
# rather than "ok" (offline/unreachable, not necessarily wrong).
STALE_AFTER = 180  # seconds


def coerce_int(value, default=None):
    """Best-effort int coercion, tolerating strings, lists, None and bools --
    timekpr's own textual replies need this even though our stored values are
    now always ints from the start."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple)):
        return coerce_int(value[0], default) if value else default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def coerce_time_spent_day(value):
    """Normalize TIME_SPENT_DAY from a timekpra reply into an int."""
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple)):
        return coerce_time_spent_day(value[0]) if value else 0
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def enforced_limits_from_config(config):
    """Return {day_num 1-7: seconds} actually enforced by the host, from its
    ALLOWED_WEEKDAYS/LIMITS_PER_WEEKDAYS (which are positional against each
    other, not against Monday..Sunday). Days absent from ALLOWED_WEEKDAYS are
    0 (blocked).

    Returns None -- never {} -- when the config can't be interpreted, so an
    unparseable host reply can't be mistaken for "every day blocked".
    """
    if not isinstance(config, dict):
        return None
    days_raw = config.get('ALLOWED_WEEKDAYS')
    limits_raw = config.get('LIMITS_PER_WEEKDAYS')

    if days_raw == '' and (limits_raw is None or limits_raw == ''):
        return {d: 0 for d in range(1, 8)}
    if not isinstance(days_raw, list) or not isinstance(limits_raw, list):
        return None
    if len(days_raw) != len(limits_raw):
        return None

    pairs = [(coerce_int(d), coerce_int(v)) for d, v in zip(days_raw, limits_raw)]
    if any(d is None or v is None for d, v in pairs):
        return None

    result = {d: 0 for d in range(1, 8)}
    for day_num, seconds in pairs:
        if 1 <= day_num <= 7:
            result[day_num] = seconds
    return result


def _format_seconds(seconds):
    h, rem = divmod(max(0, int(seconds)), 3600)
    m = rem // 60
    return f"{h}h{m:02d}m" if m else f"{h}h"


def _format_limit_mismatches(expected, enforced, mismatches):
    """Tooltip-ready string, compressing consecutive days with an identical
    (expected, enforced) pair, e.g.:
    'Mon: host 2h30m vs configured 15m; Tue-Sun: host 1h30m vs configured 15m'
    """
    parts = []
    i = 0
    while i < len(mismatches):
        start = mismatches[i]
        pair = (expected[start], enforced.get(start, 0))
        j = i
        while (j + 1 < len(mismatches) and mismatches[j + 1] == mismatches[j] + 1
               and (expected[mismatches[j + 1]], enforced.get(mismatches[j + 1], 0)) == pair):
            j += 1
        end = mismatches[j]
        label = DAY_ABBR[start] if start == end else f"{DAY_ABBR[start]}-{DAY_ABBR[end]}"
        parts.append(f"{label}: host {_format_seconds(pair[1])} vs configured {_format_seconds(pair[0])}")
        i = j + 1
    return "; ".join(parts)


class Settings(db.Model):
    __tablename__ = 'settings'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.Text, nullable=False)

    @classmethod
    def get_value(cls, key, default=None):
        setting = cls.query.filter_by(key=key).first()
        return setting.value if setting else default

    @classmethod
    def set_value(cls, key, value):
        setting = cls.query.filter_by(key=key).first()
        if setting:
            setting.value = value
        else:
            setting = cls(key=key, value=value)
            db.session.add(setting)
        db.session.commit()
        return setting

    @classmethod
    def get_int(cls, key, default):
        try:
            return int(cls.get_value(key, default))
        except (TypeError, ValueError):
            return default

    @classmethod
    def hash_password(cls, password):
        salt = bcrypt.gensalt()
        return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

    @classmethod
    def check_password(cls, password, hashed_password):
        return bcrypt.checkpw(password.encode('utf-8'), hashed_password.encode('utf-8'))

    @classmethod
    def set_admin_password(cls, password):
        cls.set_value('admin_password_hash', cls.hash_password(password))

    @classmethod
    def check_admin_password(cls, password):
        hashed_password = cls.get_value('admin_password_hash')
        if not hashed_password:
            cls.set_admin_password('admin')
            return password == 'admin'
        return cls.check_password(password, hashed_password)


class User(db.Model):
    """The child. One row per managed username -- the level at which the
    schedule, the shared time pool and the bonus/penalty all live."""
    __tablename__ = 'user'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)

    accounts = db.relationship('Account', backref='user', lazy=True,
                                cascade='all, delete-orphan')
    day_limits = db.relationship('DayLimit', backref='user', lazy=True,
                                  cascade='all, delete-orphan',
                                  order_by='DayLimit.day_of_week')

    def __repr__(self):
        return f'<User {self.username}>'

    # ---------------------------------------------------------- schedule

    def ensure_day_limits(self):
        """Create the 7 DayLimit rows (all-zero, hours disabled) if this user
        has never had a schedule saved. Mirrors the old 'create schedule row
        on first visit to the editor' behavior."""
        if self.day_limits:
            return
        for day in range(1, 8):
            db.session.add(DayLimit(user_id=self.id, day_of_week=day))
        db.session.commit()

    def expected_limits(self):
        """{day_num 1-7: seconds} this user's schedule intends to enforce, or
        None if no schedule has ever been saved for this user at all (distinct
        from an explicit all-zero schedule)."""
        if not self.day_limits:
            return None
        by_day = {dl.day_of_week: dl.limit_seconds for dl in self.day_limits}
        return {d: by_day.get(d, 0) for d in range(1, 8)}

    def today_limit_seconds(self):
        expected = self.expected_limits()
        return None if expected is None else expected.get(date.today().isoweekday())

    # ---------------------------------------------------------- pool

    def today_bonus_seconds(self):
        b = TimeBonus.query.filter_by(user_id=self.id, date=date.today()).first()
        return b.seconds if b else 0

    def spent_today(self):
        """Total seconds spent today, summed across every account (host) of
        this user."""
        today = date.today()
        account_ids = [a.id for a in self.accounts]
        if not account_ids:
            return 0
        rows = Usage.query.filter(Usage.account_id.in_(account_ids), Usage.date == today).all()
        return sum(r.seconds for r in rows)

    def pool_target_seconds(self):
        """The TIME_LEFT_DAY every account should converge towards right now,
        or None if this user has no schedule configured at all."""
        limit = self.today_limit_seconds()
        if limit is None:
            return None
        return max(0, limit + self.today_bonus_seconds() - self.spent_today())

    def add_bonus(self, signed_seconds):
        today = date.today()
        b = TimeBonus.query.filter_by(user_id=self.id, date=today).first()
        if b:
            b.seconds += signed_seconds
        else:
            b = TimeBonus(user_id=self.id, date=today, seconds=signed_seconds)
            db.session.add(b)
        db.session.commit()
        return b

    # ---------------------------------------------------------- usage history

    def _usage_by_date(self):
        account_ids = [a.id for a in self.accounts]
        if not account_ids:
            return {}
        totals: dict = {}
        for r in Usage.query.filter(Usage.account_id.in_(account_ids)).all():
            totals[r.date] = totals.get(r.date, 0) + r.seconds
        return totals

    def get_recent_usage(self, days=7):
        today = date.today()
        start_date = today - timedelta(days=days - 1)
        by_date = self._usage_by_date()
        result = {}
        for i in range(days):
            d = start_date + timedelta(days=i)
            result[d.strftime('%Y-%m-%d')] = by_date.get(d, 0)
        return result

    def get_usage_weekly_grouped(self, weeks=13):
        today = date.today()
        days_since_monday = today.weekday()
        current_monday = today - timedelta(days=days_since_monday)
        start_date = current_monday - timedelta(weeks=weeks - 1)
        by_date = self._usage_by_date()

        result = []
        for i in range(weeks):
            week_start = start_date + timedelta(weeks=i)
            week_end = week_start + timedelta(days=6)
            total = sum(s for d, s in by_date.items() if week_start <= d <= week_end)
            result.append({
                'label': week_start.strftime('%d %b'),
                'week_start': week_start.strftime('%Y-%m-%d'),
                'total': total,
            })
        return result

    def get_usage_monthly_grouped(self, months=12):
        today = date.today()
        by_date = self._usage_by_date()
        result = []
        for i in range(months - 1, -1, -1):
            month = today.month - i
            year = today.year
            while month <= 0:
                month += 12
                year -= 1
            month_start = today.replace(year=year, month=month, day=1)
            if month == 12:
                month_end = today.replace(year=year + 1, month=1, day=1) - timedelta(days=1)
            else:
                month_end = today.replace(year=year, month=month + 1, day=1) - timedelta(days=1)
            total = sum(s for d, s in by_date.items() if month_start <= d <= month_end)
            result.append({
                'label': month_start.strftime('%b %Y'),
                'month': month_start.strftime('%Y-%m'),
                'total': total,
            })
        return result

    def get_all_usage_monthly(self):
        by_date = self._usage_by_date()
        if not by_date:
            return []
        from collections import defaultdict
        buckets = defaultdict(int)
        for d, s in by_date.items():
            buckets[d.strftime('%Y-%m')] += s
        result = []
        for key in sorted(buckets):
            year, month = int(key[:4]), int(key[5:])
            label = datetime(year, month, 1).strftime('%b %Y')
            result.append({'label': label, 'month': key, 'total': buckets[key]})
        return result


class Host(db.Model):
    """A machine. Identity is the IP -- timekpr is reached by SSHing there."""
    __tablename__ = 'host'
    id = db.Column(db.Integer, primary_key=True)
    ip = db.Column(db.String(50), unique=True, nullable=False)
    label = db.Column(db.String(100), nullable=True)

    accounts = db.relationship('Account', backref='host', lazy=True,
                                cascade='all, delete-orphan')

    def __repr__(self):
        return f'<Host {self.label or self.ip}>'

    @property
    def display_name(self):
        return self.label or self.ip


class Account(db.Model):
    """One user's session on one host -- the unit SSH actually talks to, and
    the unit that owns observation (what the host last reported) as opposed
    to intent (which lives on User)."""
    __tablename__ = 'account'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    host_id = db.Column(db.Integer, db.ForeignKey('host.id'), nullable=False)

    date_added = db.Column(db.DateTime, default=datetime.utcnow)
    last_synced = db.Column(db.DateTime, nullable=True)   # last successful read
    last_config = db.Column(db.Text, nullable=True)       # raw --userinfo JSON
    last_error = db.Column(db.Text, nullable=True)        # cleared on next success
    drift_since = db.Column(db.DateTime, nullable=True)   # see DRIFT_ALARM_DELAY

    usage_records = db.relationship('Usage', backref='account', lazy=True,
                                     cascade='all, delete-orphan')

    __table_args__ = (
        db.UniqueConstraint('user_id', 'host_id', name='account_user_host_uc'),
    )

    def __repr__(self):
        return f'<Account {self.user.username}@{self.host.ip}>'

    @property
    def is_valid(self):
        """Whether the last read produced usable config -- replaces the old
        stored ManagedUser.is_valid flag."""
        return self.last_config is not None

    def _config_dict(self):
        if not self.last_config:
            return None
        try:
            return json.loads(self.last_config)
        except (TypeError, ValueError):
            return None

    def config_value(self, key):
        config = self._config_dict()
        return config.get(key) if config else None

    def enforced_limits(self):
        return enforced_limits_from_config(self._config_dict())

    def enforced_today_seconds(self):
        limits = self.enforced_limits()
        return None if limits is None else limits.get(date.today().isoweekday())

    def time_left(self):
        return coerce_int(self.config_value('TIME_LEFT_DAY'))

    def is_stale(self, max_age=STALE_AFTER):
        if not self.last_synced:
            return True
        return (datetime.utcnow() - self.last_synced).total_seconds() > max_age

    def get_recent_usage(self, days=7):
        """This account's own daily usage for the last *days* days -- what the
        dashboard's per-host stacked chart needs (User.get_recent_usage sums
        across accounts instead, for the history page)."""
        today = date.today()
        start_date = today - timedelta(days=days - 1)
        rows = Usage.query.filter(
            Usage.account_id == self.id, Usage.date >= start_date, Usage.date <= today
        ).all()
        by_date = {r.date: r.seconds for r in rows}
        result = {}
        for i in range(days):
            d = start_date + timedelta(days=i)
            result[d.strftime('%Y-%m-%d')] = by_date.get(d, 0)
        return result

    def verification_state(self):
        """'drift' | 'unverified' | 'ok' -- single source of truth for the UI.

        Nothing here is stored except drift_since (set/cleared by the sync
        loop in src/sync.py as it detects/clears a mismatch each cycle).
        is_stale() alone can't catch every failure: a host that answers SSH
        perfectly but silently isn't applying the configured limit still
        looks fresh by that measure, since the read succeeds even though the
        earlier write didn't take -- drift_since is what actually catches
        that. And a fresh mismatch is kept at 'ok' for DRIFT_ALARM_DELAY
        seconds so that saving a schedule doesn't flash the dashboard red
        while the next sync cycle is still converging; without that grace
        window, every edit would trip the alarm and train the parent to
        ignore it.
        """
        if self.drift_since and (datetime.utcnow() - self.drift_since).total_seconds() > DRIFT_ALARM_DELAY:
            return 'drift'
        if self.is_stale():
            return 'unverified'
        return 'ok'


def config_mismatch_detail(user, account):
    """Compare `user`'s intended schedule/hours against what `account` itself
    last reported. Pure function of data already in the database -- no SSH.

    Returns (ok, detail): ok is True if everything checked matches (or there
    was nothing to check yet); detail is a human-readable mismatch string, or
    None.
    """
    config = account._config_dict()
    if config is None:
        return True, None
    expected = user.expected_limits()
    if expected is None:
        return True, None

    limits_detail = None
    enforced = enforced_limits_from_config(config)
    if enforced is None:
        limits_detail = ("Cannot read enforced limits from host "
                          "(ALLOWED_WEEKDAYS/LIMITS_PER_WEEKDAYS missing or unparseable)")
    else:
        mismatches = [d for d in range(1, 8) if expected[d] != enforced.get(d, 0)]
        if mismatches:
            limits_detail = _format_limit_mismatches(expected, enforced, mismatches)

    hours_drifted_days = []
    for dl in user.day_limits:
        host_raw = config.get(f'ALLOWED_HOURS_{dl.day_of_week}')
        if host_raw is None:
            continue  # host doesn't report this key -- can't verify, don't invent drift
        wanted = set(dl.hour_tokens())
        host_tokens = (set(str(t) for t in host_raw) if isinstance(host_raw, list)
                       else {str(host_raw)})
        if wanted != host_tokens:
            hours_drifted_days.append(dl.day_of_week)

    hours_detail = None
    if hours_drifted_days:
        days = ', '.join(DAY_ABBR[d] for d in sorted(hours_drifted_days))
        hours_detail = f"Allowed-hours mismatch on {days}"

    detail = "; ".join(p for p in (limits_detail, hours_detail) if p) or None
    return detail is None, detail


class DayLimit(db.Model):
    """One day of one user's schedule: the daily time budget and the allowed
    hours window, together -- they're the same parental decision for the same
    day. Exactly one row per (user, day_of_week); a user with no rows at all
    has never had a schedule saved."""
    __tablename__ = 'day_limit'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    day_of_week = db.Column(db.Integer, nullable=False)  # 1=Monday..7=Sunday (ISO)

    limit_seconds = db.Column(db.Integer, nullable=False, default=0)  # 0 = blocked

    hours_enabled = db.Column(db.Boolean, nullable=False, default=False)  # False = all 24h allowed
    start_hour = db.Column(db.Integer, nullable=False, default=9)
    start_minute = db.Column(db.Integer, nullable=False, default=0)
    end_hour = db.Column(db.Integer, nullable=False, default=17)
    end_minute = db.Column(db.Integer, nullable=False, default=0)

    __table_args__ = (
        db.UniqueConstraint('user_id', 'day_of_week', name='day_limit_user_day_uc'),
    )

    def __repr__(self):
        return f'<DayLimit {self.user.username} {DAY_ABBR[self.day_of_week]}>'

    def day_name(self):
        names = ('', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
        return names[self.day_of_week]

    def is_valid_interval(self):
        start = self.start_hour * 60 + self.start_minute
        end = self.end_hour * 60 + self.end_minute
        return start < end and 0 <= start < 1440 and 0 <= end <= 1440

    def time_range_string(self):
        return f"{self.start_hour:02d}:{self.start_minute:02d}-{self.end_hour:02d}:{self.end_minute:02d}"

    def hour_tokens(self):
        """timekpr hour-token list this day should enforce. Full 24h whenever
        hours restriction is off or the interval is malformed -- there's only
        one caller (the sync loop, needing a concrete list to compare/send),
        so unlike the old to_timekpr_format() this never returns None."""
        full_day = [str(h) for h in range(24)]
        if not self.hours_enabled or not self.is_valid_interval():
            return full_day

        if self.start_minute == 0 and self.end_minute == 0:
            return [str(h) for h in range(self.start_hour, self.end_hour)]

        result = []
        current_hour = self.start_hour
        if current_hour == self.end_hour:
            return [f"{current_hour}[{self.start_minute}-{self.end_minute}]"]

        if self.start_minute == 0:
            result.append(str(current_hour))
        else:
            result.append(f"{current_hour}[{self.start_minute}-59]")
        current_hour += 1

        while current_hour < self.end_hour:
            result.append(str(current_hour))
            current_hour += 1

        if self.end_minute > 0:
            result.append(f"{self.end_hour}[0-{self.end_minute}]")

        return result


class Usage(db.Model):
    """Daily seconds spent, per account. The only table with irreplaceable
    history -- everything else can be re-derived from the hosts."""
    __tablename__ = 'usage'
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    seconds = db.Column(db.Integer, nullable=False, default=0)

    __table_args__ = (
        db.UniqueConstraint('account_id', 'date', name='usage_account_date_uc'),
    )

    def __repr__(self):
        return f'<Usage {self.account.user.username}@{self.account.host.ip} {self.date}: {self.seconds}>'


class TimeBonus(db.Model):
    """A one-off +/- adjustment to today's pool for a user, from the 'Adjust
    Time' button. Replaces GroupTimeAdjustment; no per-host split and no
    reconciled_at flag needed since the sync loop just re-evaluates the pool
    target every cycle instead of tracking whether this row was 'applied'."""
    __tablename__ = 'time_bonus'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    seconds = db.Column(db.Integer, nullable=False, default=0)  # signed

    __table_args__ = (
        db.UniqueConstraint('user_id', 'date', name='time_bonus_user_date_uc'),
    )

    def __repr__(self):
        return f'<TimeBonus {self.user.username} {self.date}: {self.seconds:+d}s>'

from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta, date
import json
import bcrypt

db = SQLAlchemy()


def coerce_time_spent_day(value):
    """Normalise TIME_SPENT_DAY (sortie timekpra) en entier pour la colonne time_spent."""
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


def coerce_int(value, default=None):
    """Best-effort int coercion, tolerating legacy string values from before the
    ssh_helper parser fix (e.g. '-120' cached in an older last_config blob), lists,
    None, and bools. Returns *default* rather than raising."""
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


DAY_ORDER = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')


def enforced_limits_from_config(config):
    """Return {day_num 1-7: seconds} actually enforced by the host, from its
    ALLOWED_WEEKDAYS/LIMITS_PER_WEEKDAYS (which are positional against each other,
    not against Monday..Sunday). Days absent from ALLOWED_WEEKDAYS are 0 (blocked).

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


class Settings(db.Model):
    __tablename__ = 'settings'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.Text, nullable=False)
    
    @classmethod
    def get_value(cls, key, default=None):
        """Get a setting value by key"""
        setting = cls.query.filter_by(key=key).first()
        return setting.value if setting else default
    
    @classmethod
    def set_value(cls, key, value):
        """Set a setting value by key"""
        setting = cls.query.filter_by(key=key).first()
        if setting:
            setting.value = value
        else:
            setting = cls(key=key, value=value)
            db.session.add(setting)
        db.session.commit()
        return setting
    
    @classmethod
    def hash_password(cls, password):
        """Hash a password using bcrypt"""
        salt = bcrypt.gensalt()
        hashed = bcrypt.hashpw(password.encode('utf-8'), salt)
        return hashed.decode('utf-8')
    
    @classmethod
    def check_password(cls, password, hashed_password):
        """Check if password matches the stored hash"""
        return bcrypt.checkpw(password.encode('utf-8'), hashed_password.encode('utf-8'))
    
    @classmethod
    def set_admin_password(cls, password):
        """Set admin password with hashing"""
        hashed = cls.hash_password(password)
        cls.set_value('admin_password_hash', hashed)
        # Remove old plain text password if it exists
        old_password = cls.query.filter_by(key='admin_password').first()
        if old_password:
            db.session.delete(old_password)
            db.session.commit()
    
    @classmethod
    def get_int(cls, key, default):
        """Get a setting value as an integer, falling back to *default* on missing or invalid data."""
        try:
            return int(cls.get_value(key, default))
        except (TypeError, ValueError):
            return default

    @classmethod
    def check_admin_password(cls, password):
        """Check admin password against stored hash"""
        hashed_password = cls.get_value('admin_password_hash')
        if not hashed_password:
            # Check if we have old plain text password for migration
            old_password = cls.get_value('admin_password')
            if old_password:
                # Migrate old password to hashed format
                cls.set_admin_password(old_password)
                return password == old_password
            else:
                # No password set, initialize with default
                cls.set_admin_password('admin')
                return password == 'admin'
        return cls.check_password(password, hashed_password)

class ManagedUser(db.Model):
    __tablename__ = 'managed_user'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), nullable=False)
    system_ip = db.Column(db.String(50), nullable=False)
    is_valid = db.Column(db.Boolean, default=False)
    date_added = db.Column(db.DateTime, default=datetime.utcnow)
    last_checked = db.Column(db.DateTime, nullable=True)
    last_config = db.Column(db.Text, nullable=True) # Store the full config JSON
    pending_time_adjustment = db.Column(db.Integer, nullable=True) # Pending time adjustment in seconds
    pending_time_operation = db.Column(db.String(1), nullable=True) # + or -
    last_sync_error = db.Column(db.Text, nullable=True) # Message from the last failed push, if any
    last_sync_error_at = db.Column(db.DateTime, nullable=True) # When that failure was recorded

    # Verification: is_synced (above/on the schedule/interval rows) only means
    # "we sent it"; these columns track whether the host's own reported config
    # actually matches what was intended. See CLAUDE.md "Data flow".
    last_read_ok_at = db.Column(db.DateTime, nullable=True)      # last *successful* SSH read
    limits_verified_at = db.Column(db.DateTime, nullable=True)   # last read that matched the intent
    drift_detail = db.Column(db.Text, nullable=True)             # human-readable mismatch, or NULL
    drift_since = db.Column(db.DateTime, nullable=True)          # first observation of current drift
    drift_repush_at = db.Column(db.DateTime, nullable=True)      # when the one-shot auto-repair fired

    # Relationship with usage data and weekly schedules
    usage_data = db.relationship('UserTimeUsage', backref='user', lazy=True, cascade="all, delete-orphan")
    weekly_schedule = db.relationship('UserWeeklySchedule', backref='user', uselist=False, cascade="all, delete-orphan")
    
    def __repr__(self):
        return f'<ManagedUser {self.username}@{self.system_ip}>'
    
    def get_recent_usage(self, days=7):
        """Get usage data for the last n days"""
        today = datetime.utcnow().date()
        start_date = today - timedelta(days=days-1)
        
        # Get the usage records for the specified period
        records = UserTimeUsage.query.filter_by(user_id=self.id).filter(
            UserTimeUsage.date >= start_date,
            UserTimeUsage.date <= today
        ).order_by(UserTimeUsage.date).all()
        
        # Create a dict with all days in the period
        usage_dict = {}
        for i in range(days):
            date = start_date + timedelta(days=i)
            usage_dict[date.strftime('%Y-%m-%d')] = 0
        
        # Fill in the actual data
        for record in records:
            date_str = record.date.strftime('%Y-%m-%d')
            usage_dict[date_str] = record.time_spent
        
        return usage_dict
    
    def get_usage_weekly_grouped(self, weeks=13):
        """Get usage totals grouped by week (Monday-Sunday) for the last N weeks"""
        today = datetime.utcnow().date()
        # Start from Monday of the week N-1 weeks ago
        days_since_monday = today.weekday()  # 0=Monday
        current_monday = today - timedelta(days=days_since_monday)
        start_date = current_monday - timedelta(weeks=weeks - 1)

        records = UserTimeUsage.query.filter_by(user_id=self.id).filter(
            UserTimeUsage.date >= start_date,
            UserTimeUsage.date <= today
        ).order_by(UserTimeUsage.date).all()

        result = []
        for i in range(weeks):
            week_start = start_date + timedelta(weeks=i)
            week_end = week_start + timedelta(days=6)
            total = sum(r.time_spent for r in records if week_start <= r.date <= week_end)
            result.append({
                'label': week_start.strftime('%d %b'),
                'week_start': week_start.strftime('%Y-%m-%d'),
                'total': total,
            })
        return result

    def get_usage_monthly_grouped(self, months=12):
        """Get usage totals grouped by calendar month for the last N months"""
        today = datetime.utcnow().date()

        result = []
        for i in range(months - 1, -1, -1):
            # Walk back i months from current month
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

            records = UserTimeUsage.query.filter_by(user_id=self.id).filter(
                UserTimeUsage.date >= month_start,
                UserTimeUsage.date <= month_end
            ).all()
            total = sum(r.time_spent for r in records)
            result.append({
                'label': month_start.strftime('%b %Y'),
                'month': month_start.strftime('%Y-%m'),
                'total': total,
            })
        return result

    def get_all_usage_monthly(self):
        """Get all recorded usage grouped by calendar month, oldest first"""
        records = UserTimeUsage.query.filter_by(user_id=self.id).order_by(UserTimeUsage.date).all()
        if not records:
            return []

        from collections import defaultdict
        buckets = defaultdict(int)
        for r in records:
            key = r.date.strftime('%Y-%m')
            buckets[key] += r.time_spent

        result = []
        for key in sorted(buckets):
            year, month = int(key[:4]), int(key[5:])
            label = datetime(year, month, 1).strftime('%b %Y')
            result.append({'label': label, 'month': key, 'total': buckets[key]})
        return result

    def get_config_value(self, key):
        """Extract a specific value from the stored config"""
        if not self.last_config:
            return None
        try:
            config = json.loads(self.last_config)
            return config.get(key)
        except:
            return None

    def _config_dict(self):
        if not self.last_config:
            return None
        try:
            return json.loads(self.last_config)
        except (TypeError, ValueError):
            return None

    def expected_limits(self):
        """{day_num 1-7: seconds} this host's weekly schedule is meant to enforce,
        or None if no schedule row exists for this host at all."""
        if not self.weekly_schedule:
            return None
        schedule_dict = self.weekly_schedule.get_schedule_dict()
        return {
            i + 1: int(round((schedule_dict.get(day, 0) or 0) * 3600))
            for i, day in enumerate(DAY_ORDER)
        }

    def enforced_limits(self):
        """{day_num 1-7: seconds} actually enforced according to the last successful
        read, or None if unreadable/never read."""
        return enforced_limits_from_config(self._config_dict())

    def enforced_today_seconds(self):
        """Seconds enforced today according to the host's own reported config."""
        limits = self.enforced_limits()
        if limits is None:
            return None
        return limits.get(date.today().isoweekday())

    def is_stale(self, max_age=180):
        """Whether the last successful read is older than *max_age* seconds (or missing)."""
        if not self.last_read_ok_at:
            return True
        return (datetime.utcnow() - self.last_read_ok_at).total_seconds() > max_age

    def verification_state(self):
        """'drift' | 'unverified' | 'ok' -- single source of truth for the UI.

        'drift': the last read found a mismatch between intent and host config.
        'unverified': no confirmed-matching read has landed yet, or it's stale.
        'ok': the host's own config matched the intent as of the last read.
        """
        if self.drift_detail:
            return 'drift'
        if not self.limits_verified_at or self.is_stale():
            return 'unverified'
        return 'ok'


class GroupTimeAdjustment(db.Model):
    """Per-day manual pool adjustment for a username group (signed seconds).

    Created when the user clicks "Adjust Time" on a multi-host group card.
    The reconciliation loop reads this and adds extra_seconds to the computed
    remaining pool before enforcing it on every host.
    """
    __tablename__ = 'group_time_adjustment'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), nullable=False)
    date = db.Column(db.Date, nullable=False)
    extra_seconds = db.Column(db.Integer, default=0)  # positive = bonus, negative = penalty
    reconciled_at = db.Column(db.DateTime, nullable=True)  # set after first successful reconciliation

    __table_args__ = (
        db.UniqueConstraint('username', 'date', name='group_user_date_uc'),
    )

    def __repr__(self):
        return f'<GroupTimeAdjustment {self.username} {self.date}: {self.extra_seconds:+d}s>'


def get_user_groups():
    """Return {username: [ManagedUser, ...]} for all valid users, grouped by username."""
    users = ManagedUser.query.filter_by(is_valid=True).all()
    groups: dict = {}
    for u in users:
        groups.setdefault(u.username, []).append(u)
    return groups


def group_today_limit(username):
    """Return today's global time limit in seconds for *username* group.

    Returns None when no member has a weekly schedule configured at all (nothing
    to enforce, distinct from a deliberate 0 = "blocked today"). Includes any
    GroupTimeAdjustment bonus/penalty recorded for today.
    """
    today = date.today()
    day_key = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'][today.weekday()]

    members = ManagedUser.query.filter_by(username=username).all()
    base_hours = None
    for m in members:
        if m.weekly_schedule:
            # Take the first member that HAS a schedule row, even if its value is
            # 0 -- skipping it in favour of a sibling host risks showing a stale
            # or differently-configured host's limit instead of this one's.
            base_hours = float(m.weekly_schedule.get_schedule_dict().get(day_key, 0) or 0)
            break

    if base_hours is None:
        return None

    base_seconds = int(round(base_hours * 3600))
    adj = GroupTimeAdjustment.query.filter_by(username=username, date=today).first()
    extra = adj.extra_seconds if adj else 0
    return max(0, base_seconds + extra)


class UserTimeUsage(db.Model):
    __tablename__ = 'user_time_usage'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('managed_user.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    time_spent = db.Column(db.Integer, default=0) # Time spent in seconds
    
    __table_args__ = (
        db.UniqueConstraint('user_id', 'date', name='user_date_uc'),
    )
    
    def __repr__(self):
        return f'<UserTimeUsage {self.user.username} {self.date}: {self.time_spent}>'

class UserWeeklySchedule(db.Model):
    __tablename__ = 'user_weekly_schedule'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('managed_user.id'), nullable=False)
    
    # Time limits per day in hours (0 = no limit/disabled)
    monday_hours = db.Column(db.Float, default=0)
    tuesday_hours = db.Column(db.Float, default=0)
    wednesday_hours = db.Column(db.Float, default=0)
    thursday_hours = db.Column(db.Float, default=0)
    friday_hours = db.Column(db.Float, default=0)
    saturday_hours = db.Column(db.Float, default=0)
    sunday_hours = db.Column(db.Float, default=0)
    
    # Sync status and timestamps
    is_synced = db.Column(db.Boolean, default=False)
    last_synced = db.Column(db.DateTime, nullable=True)
    last_modified = db.Column(db.DateTime, default=datetime.utcnow)
    
    def __repr__(self):
        return f'<UserWeeklySchedule {self.user.username}>'
    
    def get_schedule_dict(self):
        """Get schedule as a dictionary for easy template rendering"""
        return {
            'monday': self.monday_hours,
            'tuesday': self.tuesday_hours,
            'wednesday': self.wednesday_hours,
            'thursday': self.thursday_hours,
            'friday': self.friday_hours,
            'saturday': self.saturday_hours,
            'sunday': self.sunday_hours
        }
    
    def set_schedule_from_dict(self, schedule_dict):
        """Set schedule from a dictionary"""
        self.monday_hours = schedule_dict.get('monday', 0)
        self.tuesday_hours = schedule_dict.get('tuesday', 0)
        self.wednesday_hours = schedule_dict.get('wednesday', 0)
        self.thursday_hours = schedule_dict.get('thursday', 0)
        self.friday_hours = schedule_dict.get('friday', 0)
        self.saturday_hours = schedule_dict.get('saturday', 0)
        self.sunday_hours = schedule_dict.get('sunday', 0)
        self.last_modified = datetime.utcnow()
        self.is_synced = False
    
    def set_weekdays_hours(self, hours):
        """Set the same hours for all weekdays (Monday to Friday)"""
        self.monday_hours = hours
        self.tuesday_hours = hours
        self.wednesday_hours = hours
        self.thursday_hours = hours
        self.friday_hours = hours
        self.last_modified = datetime.utcnow()
        self.is_synced = False
    
    def has_pending_changes(self):
        """Check if there are unsynced changes"""
        return not self.is_synced
    
    def mark_synced(self):
        """Mark the schedule as synced with the remote system"""
        self.is_synced = True
        self.last_synced = datetime.utcnow()

class UserDailyTimeInterval(db.Model):
    __tablename__ = 'user_daily_time_interval'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('managed_user.id'), nullable=False)
    
    # Day of week (1=Monday, 7=Sunday, matching ISO 8601)
    day_of_week = db.Column(db.Integer, nullable=False)  # 1-7
    
    # Time interval (24-hour format)
    start_hour = db.Column(db.Integer, nullable=False)   # 0-23
    start_minute = db.Column(db.Integer, default=0)      # 0-59
    end_hour = db.Column(db.Integer, nullable=False)     # 0-23
    end_minute = db.Column(db.Integer, default=0)        # 0-59
    
    # Whether this interval is enabled
    is_enabled = db.Column(db.Boolean, default=True)
    
    # Sync status and timestamps
    is_synced = db.Column(db.Boolean, default=False)
    last_synced = db.Column(db.DateTime, nullable=True)
    last_modified = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationship back to user
    user = db.relationship('ManagedUser', backref=db.backref('time_intervals', cascade='all, delete-orphan'))
    
    # Constraint to ensure only one interval per user per day
    __table_args__ = (
        db.UniqueConstraint('user_id', 'day_of_week', name='user_day_interval_uc'),
    )
    
    def __repr__(self):
        return f'<UserDailyTimeInterval {self.user.username} Day{self.day_of_week} {self.start_hour:02d}:{self.start_minute:02d}-{self.end_hour:02d}:{self.end_minute:02d}>'
    
    def get_time_range_string(self):
        """Get formatted time range string (e.g., '09:00-17:30')"""
        return f"{self.start_hour:02d}:{self.start_minute:02d}-{self.end_hour:02d}:{self.end_minute:02d}"
    
    def get_day_name(self):
        """Get day name from day_of_week number"""
        days = ['', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        return days[self.day_of_week] if 1 <= self.day_of_week <= 7 else 'Unknown'
    
    def is_valid_interval(self):
        """Check if the time interval is valid (start < end, within 24h)"""
        start_minutes = self.start_hour * 60 + self.start_minute
        end_minutes = self.end_hour * 60 + self.end_minute
        return start_minutes < end_minutes and 0 <= start_minutes < 1440 and 0 <= end_minutes < 1440
    
    def mark_synced(self):
        """Mark the interval as synced with the remote system"""
        self.is_synced = True
        self.last_synced = datetime.utcnow()
    
    def mark_modified(self):
        """Mark the interval as modified (needs sync)"""
        self.is_synced = False
        self.last_modified = datetime.utcnow()
    
    def to_timekpr_format(self):
        """Convert interval to timekpr hour specification format"""
        if not self.is_enabled:
            return None
        
        # If full hour intervals, just return the hour numbers
        if self.start_minute == 0 and self.end_minute == 0:
            hours = list(range(self.start_hour, self.end_hour))
            return [str(h) for h in hours]
        
        # If partial hours, include minute specifications
        result = []
        current_hour = self.start_hour
        
        # First hour (potentially partial)
        if current_hour == self.end_hour:
            # Same hour, use minute range
            result.append(f"{current_hour}[{self.start_minute}-{self.end_minute}]")
        else:
            # Multiple hours
            if self.start_minute == 0:
                result.append(str(current_hour))
            else:
                result.append(f"{current_hour}[{self.start_minute}-59]")
            
            current_hour += 1
            
            # Full hours in between
            while current_hour < self.end_hour:
                result.append(str(current_hour))
                current_hour += 1
            
            # Last hour (potentially partial)
            if self.end_minute > 0:
                result.append(f"{self.end_hour}[0-{self.end_minute}]")
        
        return result
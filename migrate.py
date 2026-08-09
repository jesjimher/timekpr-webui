#!/usr/bin/env python3
"""One-time migration from the old per-host schema (src/database.py) to the
new per-user schema (src/models.py): schedules and allowed-hours move from
being stored per (user, host) pair to being stored once per user, since a
child has one schedule, not one per machine.

Usage:
    python migrate.py <source.db> <dest.db>

Read-only against the source database -- never writes to it. Refuses to
overwrite an existing dest.db (delete it yourself if you want to re-run).
Disposes its engines before returning, so callers that immediately swap
files on disk (see src/auto_migrate.py, which runs this automatically at
container startup) don't do so under an open connection to the old inode.

What gets migrated:
  - managed_user (grouped by username) -> user + host + account
  - user_weekly_schedule + user_daily_time_interval -> day_limit
    (one row per user, taken from whichever host was edited most recently;
    if hosts disagree, this prints a warning instead of silently picking one)
  - user_time_usage -> usage
  - settings, except the four RECONCILE_*/SKIP_ACTIVE_HOST tunables, which
    have no equivalent under the new single-threshold sync loop

What does NOT get migrated:
  - group_time_adjustment: expires daily, nothing to carry over
  - is_synced / drift_* / pending_* / last_sync_error*: all derived or
    re-observed by the new sync loop within one read cycle
"""
import sys
import os
from datetime import datetime

from flask import Flask

from src.database import (
    db as old_db,
    ManagedUser as OldManagedUser,
    UserDailyTimeInterval as OldInterval,
    UserTimeUsage as OldUsage,
    Settings as OldSettings,
)
from src.models import db as new_db, User, Host, Account, DayLimit, Usage, Settings

_SKIP_SETTINGS = {
    'RECONCILE_ACTIVE_INTERVAL', 'RECONCILE_IDLE_INTERVAL',
    'RECONCILE_THRESHOLD', 'SKIP_ACTIVE_HOST',
}
_DAY_NAMES = ['', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']


def _make_app(db_path, db_obj):
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{os.path.abspath(db_path)}'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db_obj.init_app(app)
    return app


def _load_old_data(source_path):
    """Read everything from the old schema into plain data structures, fully
    materialized -- so nothing here depends on staying inside this app's
    context once we move on to writing the new one."""
    app = _make_app(source_path, old_db)
    with app.app_context():
        settings = [(s.key, s.value) for s in OldSettings.query.all()]

        by_username = {}
        for m in OldManagedUser.query.order_by(OldManagedUser.id).all():
            schedule = None
            schedule_modified = None
            if m.weekly_schedule:
                schedule = dict(m.weekly_schedule.get_schedule_dict())
                schedule_modified = m.weekly_schedule.last_modified

            intervals = {}
            for iv in OldInterval.query.filter_by(user_id=m.id).all():
                intervals[iv.day_of_week] = {
                    'start_hour': iv.start_hour, 'start_minute': iv.start_minute,
                    'end_hour': iv.end_hour, 'end_minute': iv.end_minute,
                    'is_enabled': iv.is_enabled, 'is_valid': iv.is_valid_interval(),
                }

            usage = [(u.date, u.time_spent) for u in
                     OldUsage.query.filter_by(user_id=m.id).all()]

            by_username.setdefault(m.username, []).append({
                'system_ip': m.system_ip,
                'is_valid': m.is_valid,
                'date_added': m.date_added,
                'last_checked': m.last_checked,
                'last_config': m.last_config,
                'schedule': schedule,
                'schedule_modified': schedule_modified,
                'intervals': intervals,
                'usage': usage,
            })

        old_db.session.remove()
        old_db.engine.dispose()

    return settings, by_username


def migrate(source_path, dest_path):
    if os.path.exists(dest_path):
        raise SystemExit(f"Refusing to overwrite existing {dest_path} -- delete it first")

    settings, by_username = _load_old_data(source_path)

    new_app = _make_app(dest_path, new_db)
    report = []

    with new_app.app_context():
        new_db.create_all()

        for key, value in settings:
            if key in _SKIP_SETTINGS:
                continue
            Settings.set_value(key, value)

        for username, members in sorted(by_username.items()):
            user = User(username=username)
            new_db.session.add(user)
            new_db.session.commit()

            # --- schedule: take the most recently edited member; warn if
            # another member disagrees instead of silently discarding it ---
            scheduled = [m for m in members if m['schedule'] is not None]
            chosen = None
            if scheduled:
                chosen = max(scheduled, key=lambda m: m['schedule_modified'] or datetime.min)
                for other in scheduled:
                    if other is not chosen and other['schedule'] != chosen['schedule']:
                        report.append(
                            f"WARNING {username}: schedule differs between "
                            f"{chosen['system_ip']} (used) and {other['system_ip']} (ignored)"
                        )

            if chosen is not None:
                for day in range(1, 8):
                    hours = chosen['schedule'].get(_DAY_NAMES[day], 0) or 0
                    iv = chosen['intervals'].get(day)
                    hours_enabled = bool(iv and iv['is_enabled'] and iv['is_valid'])
                    new_db.session.add(DayLimit(
                        user_id=user.id, day_of_week=day,
                        limit_seconds=int(round(hours * 3600)),
                        hours_enabled=hours_enabled,
                        start_hour=iv['start_hour'] if iv else 9,
                        start_minute=iv['start_minute'] if iv else 0,
                        end_hour=iv['end_hour'] if iv else 17,
                        end_minute=iv['end_minute'] if iv else 0,
                    ))

            # --- accounts + usage history ---
            for m in members:
                host = Host.query.filter_by(ip=m['system_ip']).first()
                if not host:
                    host = Host(ip=m['system_ip'])
                    new_db.session.add(host)
                    new_db.session.commit()

                account = Account(
                    user_id=user.id, host_id=host.id,
                    date_added=m['date_added'],
                    last_synced=m['last_checked'] if m['is_valid'] else None,
                    last_config=m['last_config'],
                )
                new_db.session.add(account)
                new_db.session.commit()

                for record_date, seconds in m['usage']:
                    new_db.session.add(Usage(account_id=account.id, date=record_date, seconds=seconds))

            new_db.session.commit()
            report.append(
                f"OK {username}: {len(members)} host(s), "
                f"schedule {'migrated' if chosen is not None else 'absent (never configured)'}"
            )

        new_db.session.remove()
        new_db.engine.dispose()

    return report


if __name__ == '__main__':
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <source.db> <dest.db>")
        sys.exit(1)
    for line in migrate(sys.argv[1], sys.argv[2]):
        print(line)

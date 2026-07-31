#!/usr/bin/env python3
"""
Read-only drift check: for every managed user, print the configured weekly
schedule next to what the host itself last reported enforcing, plus the
verification state the dashboard would show.

Reads the database only -- no SSH. Reflects the last background read cycle,
not live host state.
"""

from datetime import date
from flask import Flask

from src.database import db, ManagedUser, coerce_int


def create_app():
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///timekpr.db'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    return app


def _fmt_seconds(seconds):
    if seconds is None:
        return '?'
    h, rem = divmod(max(0, seconds), 3600)
    return f"{h}h{rem // 60:02d}m"


def check_drift():
    users = ManagedUser.query.order_by(ManagedUser.username, ManagedUser.system_ip).all()
    if not users:
        print("No managed users found.")
        return

    today_name = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][date.today().weekday()]

    for user in users:
        print(f"\n{user.username} @ {user.system_ip}")
        print(f"  valid: {user.is_valid}   stale: {user.is_stale()}")
        print(f"  last attempt:      {user.last_checked or '-'}")
        print(f"  last successful:   {user.last_read_ok_at or '-'}")
        print(f"  verified at:       {user.limits_verified_at or '-'}")

        expected = user.expected_limits()
        enforced = user.enforced_limits()
        if expected is None:
            print("  no weekly schedule configured for this host")
        else:
            print(f"  today ({today_name}) configured: {_fmt_seconds(expected[date.today().isoweekday()])}"
                  f"   enforced: {_fmt_seconds(enforced.get(date.today().isoweekday()) if enforced else None)}")
            if enforced is None:
                print("  cannot read host's enforced limits (ALLOWED_WEEKDAYS/LIMITS_PER_WEEKDAYS missing)")
            else:
                mismatched = [d for d in range(1, 8)
                              if abs(expected[d] - enforced.get(d, 0)) > 60]
                if mismatched:
                    days = ['', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
                    for d in mismatched:
                        print(f"    {days[d]}: configured {_fmt_seconds(expected[d])}"
                              f" vs host {_fmt_seconds(enforced.get(d, 0))}")

        time_left = coerce_int(user.get_config_value('TIME_LEFT_DAY'))
        print(f"  TIME_LEFT_DAY (host): {_fmt_seconds(time_left) if time_left is not None else '?'}")

        state = user.verification_state()
        marker = {'drift': '/!\\ DRIFT', 'unverified': '? UNVERIFIED', 'ok': 'OK'}[state]
        print(f"  state: {marker}")
        if user.drift_detail:
            print(f"  detail: {user.drift_detail}")


if __name__ == '__main__':
    app = create_app()
    with app.app_context():
        db.create_all()
        check_drift()

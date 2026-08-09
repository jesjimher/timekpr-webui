"""Tests for the automatic in-place migration (src/auto_migrate.py) that runs
at app startup so a `git pull` + Docker rebuild picks up the new schema
without a manual step. Uses real files on disk (not the in-memory fixture)
since the whole point is file-swap behavior.
"""
import os
import sqlite3
from datetime import date

from flask import Flask

from src.auto_migrate import ensure_migrated, resolve_sqlite_path, SCHEMA_VERSION


def _old_schema_file(path):
    """Build a real old-schema sqlite file with one user, matching what a
    live production database looks like before migration."""
    from src.database import (
        db, ManagedUser, UserWeeklySchedule, UserDailyTimeInterval,
        UserTimeUsage, Settings,
    )
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{os.path.abspath(path)}'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
        Settings.set_admin_password('hunter2')
        Settings.set_value('AUTH_MODE', 'local')

        user = ManagedUser(username='guillem', system_ip='athlon.local', is_valid=True)
        db.session.add(user)
        db.session.commit()

        sched = UserWeeklySchedule(user_id=user.id)
        sched.set_schedule_from_dict({'monday': 2.0, 'tuesday': 2.0, 'wednesday': 2.0,
                                       'thursday': 2.0, 'friday': 2.0, 'saturday': 3.0, 'sunday': 3.0})
        db.session.add(sched)
        db.session.add(UserTimeUsage(user_id=user.id, date=date.today(), time_spent=1200))
        db.session.commit()

        db.session.remove()
        db.engine.dispose()


def _new_schema_file(path):
    from src.models import db, User
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{os.path.abspath(path)}'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
        db.session.add(User(username='guillem'))
        db.session.commit()
        db.session.remove()
        db.engine.dispose()


# ---------------------------------------------------------------- resolve_sqlite_path

def test_resolve_relative_sqlite_uri():
    assert resolve_sqlite_path('sqlite:///timekpr.db', '/app/instance') == '/app/instance/timekpr.db'


def test_resolve_absolute_sqlite_uri():
    assert resolve_sqlite_path('sqlite:////data/timekpr.db', '/app/instance') == '/data/timekpr.db'


def test_resolve_non_sqlite_uri_returns_none():
    assert resolve_sqlite_path('postgresql://localhost/timekpr', '/app/instance') is None


# ---------------------------------------------------------------- ensure_migrated

def test_fresh_install_is_a_noop(tmp_path):
    db_path = str(tmp_path / 'timekpr.db')
    ensure_migrated(db_path)
    assert not os.path.exists(db_path)  # left for the caller's own create_all()


def test_already_new_schema_gets_stamped_without_touching_data(tmp_path):
    db_path = str(tmp_path / 'timekpr.db')
    _new_schema_file(db_path)

    ensure_migrated(db_path)

    conn = sqlite3.connect(db_path)
    version = conn.execute("SELECT value FROM settings WHERE key='SCHEMA_VERSION'").fetchone()[0]
    users = conn.execute("SELECT username FROM user").fetchall()
    conn.close()
    assert version == SCHEMA_VERSION
    assert users == [('guillem',)]
    assert not any(f.name.startswith('timekpr.db.pre-migration-') for f in tmp_path.iterdir())


def test_old_schema_migrates_in_place_and_backs_up(tmp_path):
    db_path = str(tmp_path / 'timekpr.db')
    _old_schema_file(db_path)

    ensure_migrated(db_path)

    # A backup of the untouched pre-migration file must exist.
    backups = [f for f in tmp_path.iterdir() if f.name.startswith('timekpr.db.pre-migration-')]
    assert len(backups) == 1
    backup_conn = sqlite3.connect(str(backups[0]))
    assert backup_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='managed_user'"
    ).fetchone() is not None
    backup_conn.close()

    # No leftover temp file.
    assert not os.path.exists(db_path + '.migrating')

    # The original path now serves the new schema, with data carried over.
    conn = sqlite3.connect(db_path)
    version = conn.execute("SELECT value FROM settings WHERE key='SCHEMA_VERSION'").fetchone()[0]
    assert version == SCHEMA_VERSION
    users = conn.execute("SELECT username FROM user").fetchall()
    assert users == [('guillem',)]
    day_limits = conn.execute(
        "SELECT day_of_week, limit_seconds FROM day_limit ORDER BY day_of_week"
    ).fetchall()
    assert day_limits == [(1, 7200), (2, 7200), (3, 7200), (4, 7200), (5, 7200), (6, 10800), (7, 10800)]
    usage = conn.execute("SELECT seconds FROM usage").fetchall()
    assert usage == [(1200,)]
    # Settings (admin password hash) carried over unchanged.
    assert conn.execute(
        "SELECT value FROM settings WHERE key='admin_password_hash'"
    ).fetchone() is not None
    conn.close()


def test_migration_is_idempotent(tmp_path):
    db_path = str(tmp_path / 'timekpr.db')
    _old_schema_file(db_path)

    ensure_migrated(db_path)
    first_backups = {f.name for f in tmp_path.iterdir() if 'pre-migration' in f.name}

    ensure_migrated(db_path)  # simulate a second container start
    second_backups = {f.name for f in tmp_path.iterdir() if 'pre-migration' in f.name}

    assert first_backups == second_backups  # no second backup, no re-migration

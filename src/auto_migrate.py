"""Automatic, idempotent migration from the old per-host schema to the new
per-user schema (see migrate.py), run once at process startup so `git pull`
+ a Docker rebuild picks up the new model without a manual step.

Safe on every start: a sentinel row (settings.SCHEMA_VERSION) marks a file as
already migrated, so every run after the first is a fast no-op. The original
file is always backed up before anything is rewritten, and this must run
before the main app creates its own SQLAlchemy engine on the same path --
see the ordering note in app.py -- because the migration ends by replacing
the file on disk, which an already-open connection would not see.
"""
import os
import sqlite3
import shutil
import logging
from datetime import datetime

from flask import Flask

logger = logging.getLogger(__name__)

SCHEMA_VERSION = '2'


def resolve_sqlite_path(uri, instance_path):
    """Mirror Flask-SQLAlchemy's own resolution of a `sqlite:///` URI, so this
    checks the exact same file the app is about to open. Returns None for any
    other backend -- this migration only ever applies to the sqlite file in
    the Docker volume."""
    prefix = 'sqlite:///'
    if not uri.startswith(prefix):
        return None
    rest = uri[len(prefix):]
    return rest if os.path.isabs(rest) else os.path.join(instance_path, rest)


def _make_app(db_path, db_obj):
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{os.path.abspath(db_path)}'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db_obj.init_app(app)
    return app


def _table_exists(db_path, table_name):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _schema_version(db_path):
    """Read the sentinel with a raw connection -- no SQLAlchemy model bound,
    so this works even before the 'settings' table has the shape either
    schema expects."""
    if not _table_exists(db_path, 'settings'):
        return None
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = 'SCHEMA_VERSION'").fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _stamp_schema_version(db_path, version):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('SCHEMA_VERSION', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (version,),
        )
        conn.commit()
    finally:
        conn.close()


def ensure_migrated(db_path):
    """Idempotent: migrates *db_path* in place if it still holds the old
    schema, backing up the original first. No-op if already migrated or if
    there's nothing to migrate yet (fresh install -- the caller's own
    db.create_all() handles that case)."""
    if not os.path.exists(db_path):
        return

    if _schema_version(db_path) == SCHEMA_VERSION:
        return

    if not _table_exists(db_path, 'managed_user'):
        # Already new-schema (or a brand new empty file created by a previous
        # partial start) with no sentinel yet -- nothing to migrate, just
        # stamp it so future starts don't re-check.
        from src.models import db as new_db, Settings
        app = _make_app(db_path, new_db)
        with app.app_context():
            new_db.create_all()
            Settings.set_value('SCHEMA_VERSION', SCHEMA_VERSION)
            new_db.session.remove()
            new_db.engine.dispose()
        return

    logger.warning("Old schema detected in %s -- migrating to the new per-user "
                    "schema (this runs once, then never again).", db_path)

    backup_path = f"{db_path}.pre-migration-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    shutil.copy2(db_path, backup_path)
    logger.info("Backed up pre-migration database to %s", backup_path)

    tmp_path = f"{db_path}.migrating"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    from migrate import migrate as run_migration
    for line in run_migration(db_path, tmp_path):
        logger.info("migrate: %s", line)

    _stamp_schema_version(tmp_path, SCHEMA_VERSION)

    os.replace(tmp_path, db_path)
    logger.warning("Migration complete -- %s now uses the new schema. "
                    "The pre-migration copy is kept at %s.", db_path, backup_path)

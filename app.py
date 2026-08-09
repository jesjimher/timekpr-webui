from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
import os
from datetime import datetime, date
import json
import logging
import pytz

from src.models import (
    db, User, Host, Account, Usage, Settings,
    coerce_time_spent_day, config_mismatch_detail,
)
from src.timekpr import SSHClient
from src.sync import SyncManager
from src.auth import get_authenticated_user, get_auth_mode, set_auth_mode, login_required
from src.auto_migrate import ensure_migrated, resolve_sqlite_path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

TIMEZONE_STR = os.environ.get('TZ', 'UTC')
try:
    LOCAL_TIMEZONE = pytz.timezone(TIMEZONE_STR)
    logging.info(f"Using timezone: {TIMEZONE_STR}")
except pytz.exceptions.UnknownTimeZoneError:
    logging.warning(f"Unknown timezone '{TIMEZONE_STR}', falling back to UTC")
    LOCAL_TIMEZONE = pytz.UTC
    TIMEZONE_STR = 'UTC'

app = Flask(__name__)
app.secret_key = os.urandom(24)
DATABASE_URI = os.environ.get('DATABASE_URL', 'sqlite:///timekpr.db')
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URI
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Migrate the old per-host schema in place, if this file still has it, before
# any engine for this path exists in this process -- see src/auto_migrate.py.
# Must run before db.init_app(app) below: the migration ends by replacing the
# file on disk, which an already-open connection would not see.
_sqlite_path = resolve_sqlite_path(DATABASE_URI, app.instance_path)
if _sqlite_path:
    ensure_migrated(_sqlite_path)

db.init_app(app)

sync_manager = SyncManager()
sync_manager.init_app(app)

ADMIN_USERNAME = 'admin'


@app.template_filter('localtime')
def localtime_filter(dt):
    """Convert UTC datetime to local timezone"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = pytz.UTC.localize(dt)
    return dt.astimezone(LOCAL_TIMEZONE)


@app.template_filter('synctime')
def synctime_filter(dt):
    """Format a UTC datetime as HH:MM, or HH:MM DD/MM if it isn't today"""
    if dt is None:
        return None
    local_dt = localtime_filter(dt)
    now_local = datetime.now(LOCAL_TIMEZONE)
    if local_dt.date() == now_local.date():
        return local_dt.strftime('%H:%M')
    return local_dt.strftime('%H:%M %d/%m')


@app.context_processor
def inject_timezone():
    return {'timezone': TIMEZONE_STR}


def _format_hm(seconds):
    """Format a non-negative second count as '{h}h {m}m'."""
    h, rem = divmod(max(0, seconds), 3600)
    return f"{h}h {rem // 60}m"


# ---------------------------------------------------------------- auth

@app.route('/', methods=['GET', 'POST'])
def login():
    auth_mode = get_auth_mode()

    if auth_mode == 'external' and get_authenticated_user():
        return redirect(url_for('dashboard'))

    error = None
    if request.method == 'POST':
        if auth_mode != 'local':
            flash('Login form is disabled in this mode', 'danger')
            return render_template('login.html', error='Login form is disabled', auth_mode=auth_mode)

        username = request.form.get('username')
        password = request.form.get('password')
        if username == ADMIN_USERNAME and Settings.check_admin_password(password):
            session['logged_in'] = True
            flash('Login successful!', 'success')
            return redirect(url_for('dashboard'))
        error = 'Invalid credentials. Please try again.'
        flash(error, 'danger')

    return render_template('login.html', error=error, auth_mode=auth_mode)


@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    flash('You have been logged out', 'info')
    return redirect(url_for('login'))


# ---------------------------------------------------------------- dashboard

@app.route('/dashboard')
@login_required
def dashboard():
    db.session.expire_all()
    users = [u for u in User.query.order_by(User.username).all() if u.accounts]

    offline = sync_manager.get_offline_hosts()
    active_accounts = sync_manager.get_active_accounts()
    groups = []

    for user in users:
        accounts = user.accounts

        dates = None
        per_host_values = []
        for a in accounts:
            usage = a.get_recent_usage(days=7)
            if dates is None:
                dates = list(usage.keys())
            per_host_values.append({'ip': a.host.ip, 'hours': [v / 3600.0 for v in usage.values()]})

        # Ground truth: what the hosts themselves report. Excluded: a stale
        # account (no successful read in the last 3 min) and one currently
        # offline in the sync loop's own backoff tracking -- that second check
        # matters because a host that *just* went offline is still "fresh" by
        # the 3-minute clock, so without it, a machine that's now off keeps
        # contributing its last (real, but frozen) reading to the number below
        # even though nobody can possibly be using it right now.
        #
        # Take the MIN, not the max, across what's left: a multi-host user
        # shares one pool, so the lowest figure is always the freshest -- an
        # idle sibling that hasn't been corrected yet still shows the full
        # day's budget, and picking the max would flash that stale, optimistic
        # number instead of what the host actually in use is really counting
        # down to.
        fresh_accounts = [a for a in accounts if not a.is_stale() and a.host.ip not in offline]
        left_values = [v for v in (a.time_left() for a in fresh_accounts) if v is not None]
        enforced_time_left = min(left_values) if left_values else None
        global_time_left = _format_hm(enforced_time_left) if enforced_time_left is not None else "Unknown"

        # Feeds only the chart's "remaining today" bar segment -- not shown as
        # its own number, since it mostly restates global_time_left above.
        pool_target = user.pool_target_seconds()

        # Only a genuine, persistent mismatch on a reachable host is an alarm.
        # A host that's merely offline is normal -- it catches up once it's
        # back on -- and must not read as "something is wrong".
        verification = 'drift' if any(a.verification_state() == 'drift' for a in accounts) else 'ok'

        drift_details = []
        for a in accounts:
            if a.verification_state() == 'drift':
                _, detail = config_mismatch_detail(user, a)
                if detail:
                    drift_details.append(f"{a.host.ip}: {detail}")

        groups.append({
            'username': user.username,
            'hosts': [{
                'id': a.id,
                'ip': a.host.ip,
                'offline': a.host.ip in offline,
                'in_use': a.id in active_accounts and a.host.ip not in offline,
                'last_synced': a.last_synced,
            } for a in accounts],
            'dates': dates or [],
            'per_host_values': per_host_values,
            'remaining_today_hours': (pool_target / 3600.0) if pool_target is not None else 0.0,
            'global_time_left': global_time_left,
            'verification': verification,
            'drift_details': drift_details,
            'has_bonus': user.today_bonus_seconds() != 0,
        })

    any_alarm = any(g['verification'] == 'drift' for g in groups)
    return render_template('dashboard.html', groups=groups, any_alarm=any_alarm)


# ---------------------------------------------------------------- management

@app.route('/admin')
@login_required
def admin():
    users = User.query.order_by(User.username).all()
    return render_template('admin.html', users=users)


@app.route('/users/add', methods=['POST'])
@login_required
def add_user():
    username = (request.form.get('username') or '').strip()
    system_ip = (request.form.get('system_ip') or '').strip()

    if not username or not system_ip:
        flash('Both username and system IP are required', 'danger')
        return redirect(url_for('admin'))

    user = User.query.filter_by(username=username).first()
    if not user:
        user = User(username=username)
        db.session.add(user)
        db.session.commit()

    host = Host.query.filter_by(ip=system_ip).first()
    if not host:
        host = Host(ip=system_ip)
        db.session.add(host)
        db.session.commit()

    if Account.query.filter_by(user_id=user.id, host_id=host.id).first():
        flash(f'{username} on {system_ip} already exists', 'warning')
        return redirect(url_for('admin'))

    account = Account(user_id=user.id, host_id=host.id)
    db.session.add(account)
    db.session.commit()

    ssh_client = SSHClient(hostname=system_ip)
    is_valid, message, config = ssh_client.validate_user(username)
    if is_valid and config:
        account.last_config = json.dumps(config)
        account.last_synced = datetime.utcnow()
        db.session.add(Usage(
            account_id=account.id, date=date.today(),
            seconds=coerce_time_spent_day(config.get('TIME_SPENT_DAY', 0)),
        ))
        db.session.commit()
        flash(f'{username} added and validated on {system_ip}', 'success')
    else:
        account.last_error = message
        db.session.commit()
        flash(f'{username} added on {system_ip} but validation failed: {message}', 'warning')

    return redirect(url_for('admin'))


@app.route('/users/validate/<int:account_id>')
@login_required
def validate_user(account_id):
    account = Account.query.get_or_404(account_id)

    ssh_client = SSHClient(hostname=account.host.ip)
    is_valid, message, config = ssh_client.validate_user(account.user.username)

    if is_valid and config:
        account.last_config = json.dumps(config)
        account.last_synced = datetime.utcnow()
        account.last_error = None
        today = date.today()
        seconds = coerce_time_spent_day(config.get('TIME_SPENT_DAY', 0))
        usage = Usage.query.filter_by(account_id=account.id, date=today).first()
        if usage:
            usage.seconds = seconds
        else:
            db.session.add(Usage(account_id=account.id, date=today, seconds=seconds))
        db.session.commit()
        flash(f'{account.user.username} validated successfully on {account.host.ip}', 'success')
    else:
        account.last_error = message
        db.session.commit()
        flash(f'Validation failed: {message}', 'danger')

    return redirect(url_for('admin'))


@app.route('/users/delete/<int:account_id>', methods=['POST'])
@login_required
def delete_user(account_id):
    account = Account.query.get_or_404(account_id)
    user = account.user
    host = account.host
    username = user.username
    ip = host.ip

    db.session.delete(account)
    db.session.commit()

    # An orphaned user (no hosts left) or host (no users left) is just clutter.
    if not user.accounts:
        db.session.delete(user)
    if not host.accounts:
        db.session.delete(host)
    db.session.commit()

    flash(f'{username} removed from {ip}', 'success')
    return redirect(url_for('admin'))


# ---------------------------------------------------------------- schedule

@app.route('/weekly-schedule/<username>')
@login_required
def weekly_schedule(username):
    user = User.query.filter_by(username=username).first()
    if not user:
        flash(f'No user found: {username}', 'danger')
        return redirect(url_for('dashboard'))

    user.ensure_day_limits()
    day_limits = {dl.day_of_week: dl for dl in user.day_limits}
    hosts_str = ', '.join(a.host.ip for a in user.accounts)
    return render_template('weekly_schedule.html', user=user, day_limits=day_limits, hosts_str=hosts_str)


@app.route('/weekly-schedule/update', methods=['POST'])
@login_required
def update_weekly_schedule():
    username = (request.form.get('username') or '').strip()
    user = User.query.filter_by(username=username).first()
    if not user:
        flash('User not found', 'danger')
        return redirect(url_for('dashboard'))

    user.ensure_day_limits()
    by_day = {dl.day_of_week: dl for dl in user.day_limits}

    for day in range(1, 8):
        dl = by_day[day]
        try:
            hours = max(0.0, min(24.0, float(request.form.get(f'hours_{day}', '0'))))
        except (TypeError, ValueError):
            hours = 0.0
        dl.limit_seconds = int(round(hours * 3600))
        dl.hours_enabled = request.form.get(f'hours_enabled_{day}') == 'on'
        # Hour-only picker (no minute UI): end_hour may be 24 ("23:59" in the
        # select), which hour_tokens()'s range(start, end) already turns into
        # hours 0..23 correctly -- no need for the old 23h59m conversion.
        try:
            dl.start_hour = int(request.form.get(f'start_hour_{day}', dl.start_hour))
            dl.end_hour = int(request.form.get(f'end_hour_{day}', dl.end_hour))
        except (TypeError, ValueError):
            pass
        dl.start_minute = 0
        dl.end_minute = 0
        if dl.hours_enabled and not dl.is_valid_interval():
            db.session.rollback()
            flash(f'Invalid time interval for {dl.day_name()}: start time must be before end time', 'danger')
            return redirect(url_for('weekly_schedule', username=username))

    # A manual edit is a fresh intent -- let the next sync cycle re-evaluate
    # drift from scratch instead of carrying over an alarm from before the edit.
    for a in user.accounts:
        a.drift_since = None

    db.session.commit()
    flash(f'Weekly schedule updated for {username}', 'success')
    return redirect(url_for('weekly_schedule', username=username))


# ---------------------------------------------------------------- history

@app.route('/stats/<username>')
@login_required
def user_stats(username):
    user = User.query.filter_by(username=username).first()
    if not user:
        flash(f'No user found: {username}', 'danger')
        return redirect(url_for('dashboard'))

    return render_template(
        'stats.html',
        user=user,
        hosts_str=', '.join(a.host.ip for a in user.accounts),
        daily_30=user.get_recent_usage(days=30),
        weekly_13=user.get_usage_weekly_grouped(weeks=13),
        monthly_12=user.get_usage_monthly_grouped(months=12),
        all_monthly=user.get_all_usage_monthly(),
    )


# ---------------------------------------------------------------- time adjustment

@app.route('/api/user/<username>/adjust-time', methods=['POST'])
@login_required
def adjust_time(username):
    user = User.query.filter_by(username=username).first()
    if not user:
        return jsonify({'success': False, 'message': 'User not found'}), 404

    operation = request.form.get('operation')
    seconds_str = request.form.get('seconds')
    if operation not in ('+', '-') or not seconds_str:
        return jsonify({'success': False, 'message': 'Missing or invalid parameters'}), 400
    try:
        seconds = int(seconds_str)
    except ValueError:
        return jsonify({'success': False, 'message': 'Invalid seconds value'}), 400

    signed = seconds if operation == '+' else -seconds
    bonus = user.add_bonus(signed)
    total_min = bonus.seconds // 60
    sign_str = '+' if total_min >= 0 else ''

    return jsonify({
        'success': True,
        'message': (f"Adjusted {operation}{seconds // 60}m "
                    f"(total today: {sign_str}{total_min}m). "
                    "Applies to every host within about a minute."),
        'refresh': True,
    })


# ---------------------------------------------------------------- live status (single endpoint)

@app.route('/api/status')
@login_required
def api_status():
    offline = sync_manager.get_offline_hosts()
    active_accounts = sync_manager.get_active_accounts()

    users_payload = {}
    for user in User.query.all():
        accounts = user.accounts
        if not accounts:
            continue

        # Only a genuine, persistent mismatch on a reachable host counts as an
        # alarm. A host that's merely offline/unreachable is normal (it'll
        # catch up once it's back) and must not read as "something is wrong".
        verification = 'drift' if any(a.verification_state() == 'drift' for a in accounts) else 'ok'

        drift_details = []
        for a in accounts:
            if a.verification_state() == 'drift':
                _, detail = config_mismatch_detail(user, a)
                if detail:
                    drift_details.append(f"{a.host.ip}: {detail}")

        # MIN across non-stale, non-offline accounts -- see dashboard() above.
        fresh = [a for a in accounts if not a.is_stale() and a.host.ip not in offline]
        left_values = [v for v in (a.time_left() for a in fresh) if v is not None]
        time_left = min(left_values) if left_values else None

        users_payload[user.username] = {
            'verification': verification,
            'drift_details': drift_details,
            'time_left_str': _format_hm(time_left) if time_left is not None else 'Unknown',
            'accounts': {
                a.id: {
                    'ip': a.host.ip,
                    'offline': a.host.ip in offline,
                    'in_use': a.id in active_accounts and a.host.ip not in offline,
                    'last_synced_iso': a.last_synced.isoformat() + 'Z' if a.last_synced else None,
                    'last_error': a.last_error,
                } for a in accounts
            },
        }

    return jsonify({'success': True, 'sync': sync_manager.get_status(), 'users': users_payload})


@app.route('/restart-tasks')
@login_required
def restart_tasks():
    sync_manager.restart()
    flash('Sync loop restarted', 'success')
    return redirect(request.referrer or url_for('dashboard'))


# ---------------------------------------------------------------- settings

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'change_password':
            current_password = request.form.get('current_password')
            new_password = request.form.get('new_password')
            confirm_password = request.form.get('confirm_password')

            if not current_password or not new_password or not confirm_password:
                flash('All fields are required', 'danger')
            elif not Settings.check_admin_password(current_password):
                flash('Current password is incorrect', 'danger')
            elif new_password != confirm_password:
                flash('New passwords do not match', 'danger')
            elif len(new_password) < 4:
                flash('New password must be at least 4 characters long', 'danger')
            else:
                Settings.set_admin_password(new_password)
                flash('Password updated successfully', 'success')
                return redirect(url_for('settings'))

        elif action == 'change_auth_mode':
            auth_mode = request.form.get('auth_mode')
            try:
                set_auth_mode(auth_mode)
                flash(f'Authentication mode changed to: {auth_mode}', 'success')
                return redirect(url_for('settings'))
            except ValueError as e:
                flash(f'Error: {str(e)}', 'danger')

        elif action == 'change_sync_settings':
            raw = request.form.get('propagation_threshold', '')
            try:
                val = int(raw)
                if val <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                flash('Invalid value for propagation threshold: must be a positive integer.', 'danger')
            else:
                Settings.set_value('PROPAGATION_THRESHOLD', str(val))
                flash('Synchronization settings updated', 'success')
                return redirect(url_for('settings'))

    return render_template(
        'settings.html',
        current_auth_mode=get_auth_mode(),
        propagation_threshold=Settings.get_int('PROPAGATION_THRESHOLD', SyncManager.PROPAGATION_THRESHOLD_DEFAULT),
    )


# With app context: create schema, seed defaults, start the sync loop.
with app.app_context():
    db.create_all()
    print("Database tables verified")

    if not Settings.get_value('admin_password_hash', None):
        Settings.set_admin_password('admin')
        print("Admin password initialized")

    if not Settings.get_value('AUTH_MODE', None):
        Settings.set_value('AUTH_MODE', 'local')
        print("Authentication mode initialized to: local (username/password)")

    sync_manager.start()
    print("Sync loop started automatically")

if __name__ == '__main__':
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    app.run(host='0.0.0.0', port=5000, debug=debug, use_reloader=debug)

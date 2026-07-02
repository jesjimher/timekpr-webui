from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
import os
from datetime import datetime, date, timedelta
import json
import logging
import pytz

from src.database import (
    db,
    ManagedUser,
    UserTimeUsage,
    Settings,
    UserWeeklySchedule,
    UserDailyTimeInterval,
    coerce_time_spent_day,
    GroupTimeAdjustment,
    group_today_limit,
)
from src.ssh_helper import SSHClient
from src.task_manager import BackgroundTaskManager
from src.auth import get_authenticated_user, get_auth_mode, set_auth_mode

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Get timezone from environment variable or default to UTC
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
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///timekpr.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Initialize the database
db.init_app(app)

# Initialize background task manager
task_manager = BackgroundTaskManager()
task_manager.init_app(app)

# Admin username remains hardcoded
ADMIN_USERNAME = 'admin'

# Jinja2 filter to convert UTC datetime to local timezone
@app.template_filter('localtime')
def localtime_filter(dt):
    """Convert UTC datetime to local timezone"""
    if dt is None:
        return None

    # If datetime is naive (no timezone info), assume it's UTC
    if dt.tzinfo is None:
        dt = pytz.UTC.localize(dt)

    # Convert to local timezone
    local_dt = dt.astimezone(LOCAL_TIMEZONE)
    return local_dt

# Make timezone string available to templates
@app.context_processor
def inject_timezone():
    """Inject timezone info into all templates"""
    return {'timezone': TIMEZONE_STR}

@app.route('/', methods=['GET', 'POST'])
def login():
    auth_mode = get_auth_mode()

    # If using external mode, check if already authenticated
    if auth_mode == 'external':
        if get_authenticated_user():
            return redirect(url_for('dashboard'))

    error = None

    if request.method == 'POST':
        # Only process login form in 'local' mode
        if auth_mode != 'local':
            flash('Login form is disabled in this mode', 'danger')
            return render_template('login.html', error='Login form is disabled', auth_mode=auth_mode)

        username = request.form.get('username')
        password = request.form.get('password')

        # Check admin password using hash comparison
        if username == ADMIN_USERNAME and Settings.check_admin_password(password):
            session['logged_in'] = True
            flash('Login successful!', 'success')
            return redirect(url_for('dashboard'))
        else:
            error = 'Invalid credentials. Please try again.'
            flash(error, 'danger')

    return render_template('login.html', error=error, auth_mode=auth_mode)

@app.route('/dashboard')
def dashboard():
    user = get_authenticated_user()
    if not user:
        flash('Please login first', 'warning')
        return redirect(url_for('login'))

    # Bust SQLAlchemy cache so we see freshly-committed usage data
    db.session.expire_all()
    all_users = ManagedUser.query.filter_by(is_valid=True).all()

    # Group users by username (same name on multiple hosts → one card)
    username_groups: dict = {}
    for u in all_users:
        username_groups.setdefault(u.username, []).append(u)

    today = date.today()
    offline = task_manager.get_offline_hosts()
    logged_in_users = task_manager.get_logged_in_users()
    groups = []

    for username, members in sorted(username_groups.items()):
        # --- Usage data per host (7 days) ---
        host_usage = {}  # ip → {date_str: seconds}
        for m in members:
            host_usage[m.system_ip] = m.get_recent_usage(days=7)

        # Ordered date strings (all members share the same 7-day window)
        dates = list(list(host_usage.values())[0].keys()) if host_usage else []

        # Per-host hour arrays aligned to `dates`
        per_host_values = [
            {
                'ip': m.system_ip,
                'hours': [host_usage.get(m.system_ip, {}).get(d, 0) / 3600.0 for d in dates],
            }
            for m in members
        ]

        # --- Global time-left for the group ---
        limit_seconds = group_today_limit(username)
        total_spent_today = sum(
            (UserTimeUsage.query.filter_by(user_id=m.id, date=today).first() or
             type('_', (), {'time_spent': 0})()).time_spent
            for m in members
        )

        if limit_seconds > 0:
            remaining = max(0, limit_seconds - total_spent_today)
            h, m_rem = divmod(remaining, 3600)
            global_time_left = f"{h}h {m_rem // 60}m"
            remaining_today_hours = remaining / 3600.0
        elif len(members) == 1:
            # Single-host, no group limit — fall back to host's own TIME_LEFT_DAY
            tl = members[0].get_config_value('TIME_LEFT_DAY')
            if tl is not None:
                h, m_rem = divmod(tl, 3600)
                global_time_left = f"{h}h {m_rem // 60}m"
            else:
                global_time_left = "Unknown"
            remaining_today_hours = 0.0
        else:
            global_time_left = "No limit"
            remaining_today_hours = 0.0

        # --- Most-recent update time across all hosts ---
        checked_times = [m.last_checked for m in members if m.last_checked]
        last_checked = max(checked_times) if checked_times else None

        # --- Pending pool adjustment flag ---
        pool_adj = GroupTimeAdjustment.query.filter_by(username=username, date=today).first()
        has_pending = bool(pool_adj and pool_adj.extra_seconds != 0 and pool_adj.reconciled_at is None)
        if not has_pending and len(members) == 1:
            has_pending = bool(
                members[0].pending_time_adjustment is not None and
                members[0].pending_time_operation is not None
            )

        # --- Sync status (initial, updated by JS polling) ---
        any_unsynced = False
        for m in members:
            if m.weekly_schedule and not m.weekly_schedule.is_synced:
                any_unsynced = True
                break
            if UserDailyTimeInterval.query.filter_by(user_id=m.id, is_synced=False).first():
                any_unsynced = True
                break

        groups.append({
            'username': username,
            'hosts': [{'id': m.id, 'ip': m.system_ip, 'offline': m.system_ip in offline, 'in_use': m.id in logged_in_users, 'last_checked': m.last_checked} for m in members],
            'ids': [m.id for m in members],
            'primary_user_id': members[0].id,
            'dates': dates,
            'per_host_values': per_host_values,
            'remaining_today_hours': remaining_today_hours,
            'global_time_left': global_time_left,
            'last_checked': last_checked,
            'has_pending': has_pending,
            'any_unsynced': any_unsynced,
            'is_multi_host': len(members) > 1,
        })

    return render_template('dashboard.html', groups=groups)

@app.route('/admin')
def admin():
    user = get_authenticated_user()
    if not user:
        flash('Please login first', 'warning')
        return redirect(url_for('login'))
    
    # Get all managed users
    users = ManagedUser.query.all()
    return render_template('admin.html', users=users)

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if not get_authenticated_user():
        flash('Please login first', 'warning')
        return redirect(url_for('login'))

    # Handle auth mode change
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'change_password':
            current_password = request.form.get('current_password')
            new_password = request.form.get('new_password')
            confirm_password = request.form.get('confirm_password')

            # Validate inputs
            if not current_password or not new_password or not confirm_password:
                flash('All fields are required', 'danger')
            elif not Settings.check_admin_password(current_password):
                flash('Current password is incorrect', 'danger')
            elif new_password != confirm_password:
                flash('New passwords do not match', 'danger')
            elif len(new_password) < 4:
                flash('New password must be at least 4 characters long', 'danger')
            else:
                # Update the password with hashing
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
            errors = []
            int_fields = {
                'RECONCILE_THRESHOLD':       request.form.get('reconcile_threshold', ''),
                'RECONCILE_ACTIVE_INTERVAL': request.form.get('reconcile_active_interval', ''),
                'RECONCILE_IDLE_INTERVAL':   request.form.get('reconcile_idle_interval', ''),
            }
            for key, raw in int_fields.items():
                try:
                    val = int(raw)
                    if val <= 0:
                        raise ValueError
                except (ValueError, TypeError):
                    errors.append(f'Invalid value for {key}: must be a positive integer.')
            if errors:
                for msg in errors:
                    flash(msg, 'danger')
            else:
                for key, raw in int_fields.items():
                    Settings.set_value(key, str(int(raw)))
                skip = 'true' if request.form.get('skip_active_host') == 'on' else 'false'
                Settings.set_value('SKIP_ACTIVE_HOST', skip)
                flash('Synchronization settings updated', 'success')
                return redirect(url_for('settings'))

    # Get current auth settings
    current_auth_mode = get_auth_mode()

    return render_template('settings.html',
                         current_auth_mode=current_auth_mode,
                         reconcile_threshold=Settings.get_int('RECONCILE_THRESHOLD', 300),
                         reconcile_active_interval=Settings.get_int('RECONCILE_ACTIVE_INTERVAL', 120),
                         reconcile_idle_interval=Settings.get_int('RECONCILE_IDLE_INTERVAL', 180),
                         skip_active_host=Settings.get_value('SKIP_ACTIVE_HOST', 'true') == 'true')

@app.route('/api/task-status')
def get_task_status():
    """Get the status of the background task manager"""
    user = get_authenticated_user()
    if not user:
        return jsonify({'success': False, 'message': 'Not authenticated'}), 401
    
    status = task_manager.get_status()
    return jsonify({
        'success': True,
        'status': status
    })

@app.route('/api/host-status')
def host_status():
    """Return which host IPs are currently offline (SSH failing / in backoff)."""
    user = get_authenticated_user()
    if not user:
        return jsonify({'success': False, 'message': 'Not authenticated'}), 401
    return jsonify({'success': True, 'offline_ips': sorted(task_manager.get_offline_hosts())})

@app.route('/restart-tasks')
def restart_tasks():
    """Restart the background task manager"""
    user = get_authenticated_user()
    if not user:
        flash('Please login first', 'warning')
        return redirect(url_for('login'))
    
    task_manager.restart()
    flash('Background tasks restarted', 'success')
    
    # Redirect back to the referring page
    referrer = request.referrer
    if referrer:
        return redirect(referrer)
    else:
        return redirect(url_for('dashboard'))

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    flash('You have been logged out', 'info')
    return redirect(url_for('login'))

@app.route('/users/add', methods=['GET', 'POST'])
def add_user():
    if not session.get('logged_in'):
        if request.method == 'GET':
            flash('Please login first', 'warning')
            return redirect(url_for('login'))
        return jsonify({'success': False, 'message': 'Not authenticated'}), 401

    if request.method == 'GET':
        return redirect(url_for('admin'))
    
    username = request.form.get('username')
    system_ip = request.form.get('system_ip')
    
    if not username or not system_ip:
        flash('Both username and system IP are required', 'danger')
        return redirect(url_for('admin'))
    
    # Check if user already exists
    existing_user = ManagedUser.query.filter_by(username=username, system_ip=system_ip).first()
    
    if existing_user:
        flash(f'User {username} on {system_ip} already exists', 'warning')
        return redirect(url_for('admin'))
    
    # Create new user
    new_user = ManagedUser(username=username, system_ip=system_ip)
    
    # Validate with timekpr
    ssh_client = SSHClient(hostname=system_ip)
    is_valid, message, config_dict = ssh_client.validate_user(username)
    
    new_user.is_valid = is_valid
    new_user.last_checked = datetime.utcnow()
    
    if is_valid and config_dict:
        new_user.last_config = json.dumps(config_dict)

        # Add the user to get an ID first
        db.session.add(new_user)
        db.session.commit()

        # Add today's usage data
        today = date.today()
        time_spent = coerce_time_spent_day(config_dict.get('TIME_SPENT_DAY', 0))
        usage = UserTimeUsage(user_id=new_user.id, date=today, time_spent=time_spent)
        db.session.add(usage)

        # Inherit group schedule/intervals if other hosts already exist for this username
        existing_peers = ManagedUser.query.filter(
            ManagedUser.username == username,
            ManagedUser.id != new_user.id,
        ).all()
        for peer in existing_peers:
            if peer.weekly_schedule:
                sched = UserWeeklySchedule(user_id=new_user.id)
                sched.set_schedule_from_dict(peer.weekly_schedule.get_schedule_dict())
                db.session.add(sched)
                for interval in peer.time_intervals:
                    new_ivl = UserDailyTimeInterval(
                        user_id=new_user.id,
                        day_of_week=interval.day_of_week,
                        start_hour=interval.start_hour,
                        start_minute=interval.start_minute,
                        end_hour=interval.end_hour,
                        end_minute=interval.end_minute,
                        is_enabled=interval.is_enabled,
                        is_synced=False,
                    )
                    db.session.add(new_ivl)
                break  # one peer's schedule is enough

        db.session.commit()
        flash(f'User {username} added and validated successfully', 'success')
    else:
        db.session.add(new_user)
        db.session.commit()
        flash(f'User {username} added but validation failed: {message}', 'warning')

    return redirect(url_for('admin'))

@app.route('/users/validate/<int:user_id>')
def validate_user(user_id):
    user = get_authenticated_user()
    if not user:
        return jsonify({'success': False, 'message': 'Not authenticated'}), 401
    
    user = ManagedUser.query.get_or_404(user_id)
    
    # Validate with timekpr
    ssh_client = SSHClient(hostname=user.system_ip)
    is_valid, message, config_dict = ssh_client.validate_user(user.username)
    
    user.is_valid = is_valid
    user.last_checked = datetime.utcnow()
    
    if is_valid and config_dict:
        user.last_config = json.dumps(config_dict)
        
        # Update today's usage data
        today = date.today()
        time_spent = coerce_time_spent_day(config_dict.get('TIME_SPENT_DAY', 0))
        
        # Look for an existing record for today
        usage = UserTimeUsage.query.filter_by(
            user_id=user.id,
            date=today
        ).first()
        
        if usage:
            usage.time_spent = time_spent
        else:
            # Create a new record
            usage = UserTimeUsage(
                user_id=user.id,
                date=today,
                time_spent=time_spent
            )
            db.session.add(usage)
        
        db.session.commit()
        flash(f'User {user.username} validated successfully', 'success')
    else:
        db.session.commit()
        flash(f'User validation failed: {message}', 'danger')
    
    return redirect(url_for('admin'))

@app.route('/users/delete/<int:user_id>', methods=['POST'])
def delete_user(user_id):
    user = get_authenticated_user()
    if not user:
        return jsonify({'success': False, 'message': 'Not authenticated'}), 401
    
    user = ManagedUser.query.get_or_404(user_id)
    username = user.username
    
    db.session.delete(user)
    db.session.commit()
    
    flash(f'User {username} removed successfully', 'success')
    return redirect(url_for('admin'))

@app.route('/api/user/<int:user_id>/usage')
def get_user_usage(user_id):
    """API endpoint to get user usage data"""
    user = get_authenticated_user()
    if not user:
        return jsonify({'success': False, 'message': 'Not authenticated'}), 401
    
    user = ManagedUser.query.get_or_404(user_id)
    days = request.args.get('days', 7, type=int)
    
    usage_data = user.get_recent_usage(days=days)
    
    # Format for chart.js
    labels = list(usage_data.keys())
    values = list(usage_data.values())
    
    # Convert seconds to hours for better readability
    values_hours = [round(v / 3600, 1) for v in values]
    
    return jsonify({
        'success': True,
        'labels': labels,
        'values': values_hours,
        'username': user.username
    })

@app.route('/weekly-schedule/<int:user_id>')
def weekly_schedule_user(user_id):
    """Display weekly schedule management page for a single host (legacy / admin use)."""
    user = get_authenticated_user()
    if not user:
        flash('Please login first', 'warning')
        return redirect(url_for('login'))

    user = ManagedUser.query.get_or_404(user_id)

    if not user.weekly_schedule:
        schedule = UserWeeklySchedule(user_id=user.id)
        db.session.add(schedule)
        db.session.commit()

    return render_template(
        'weekly_schedule_single.html',
        user=user,
        group_username='',
        member_ids=[user.id],
        hosts_str=user.system_ip,
    )


@app.route('/weekly-schedule/group/<username>')
def weekly_schedule_group(username):
    """Display weekly schedule management page for a username group."""
    user = get_authenticated_user()
    if not user:
        flash('Please login first', 'warning')
        return redirect(url_for('login'))

    members = ManagedUser.query.filter_by(username=username).all()
    if not members:
        flash(f'No users found with username {username}', 'danger')
        return redirect(url_for('dashboard'))

    # Use first member as the representative (form values, interval loading)
    user = members[0]
    if not user.weekly_schedule:
        schedule = UserWeeklySchedule(user_id=user.id)
        db.session.add(schedule)
        db.session.commit()

    member_ids = [m.id for m in members]
    hosts_str = ', '.join(m.system_ip for m in members)

    return render_template(
        'weekly_schedule_single.html',
        user=user,
        group_username=username,
        member_ids=member_ids,
        hosts_str=hosts_str,
    )

@app.route('/weekly-schedule/update', methods=['POST'])
def update_weekly_schedule():
    """Update weekly schedule — fans out to all group members when group_username is set."""
    user = get_authenticated_user()
    if not user:
        return jsonify({'success': False, 'message': 'Not authenticated'}), 401

    days = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
    schedule_data = {}
    for day in days:
        try:
            hours = float(request.form.get(day, '0'))
            hours = max(0.0, min(24.0, hours))
        except (ValueError, TypeError):
            hours = 0.0
        schedule_data[day] = hours

    group_username = request.form.get('group_username', '').strip()

    if group_username:
        # Fan out to every host with this username
        members = ManagedUser.query.filter_by(username=group_username).all()
        if not members:
            flash(f'No users found for {group_username}', 'danger')
            return redirect(url_for('dashboard'))
        try:
            for m in members:
                if not m.weekly_schedule:
                    sched = UserWeeklySchedule(user_id=m.id)
                    db.session.add(sched)
                    db.session.flush()
                    m.weekly_schedule = sched
                m.weekly_schedule.set_schedule_from_dict(schedule_data)
            db.session.commit()
            task_manager.trigger_sync()
            flash(f'Weekly schedule updated for all hosts of {group_username}', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Error updating schedule: {str(e)}', 'danger')
        return redirect(url_for('weekly_schedule_group', username=group_username))

    # Single-host fallback
    user_id = request.form.get('user_id')
    if not user_id:
        flash('User ID is required', 'danger')
        return redirect(url_for('dashboard'))
    try:
        user_id = int(user_id)
    except ValueError:
        flash('Invalid user ID', 'danger')
        return redirect(url_for('dashboard'))

    user = ManagedUser.query.get_or_404(user_id)
    if not user.weekly_schedule:
        schedule = UserWeeklySchedule(user_id=user.id)
        db.session.add(schedule)
        db.session.flush()
        user.weekly_schedule = schedule
    else:
        schedule = user.weekly_schedule

    schedule.set_schedule_from_dict(schedule_data)
    try:
        db.session.commit()
        task_manager.trigger_sync()
        flash(f'Weekly schedule updated for {user.username}', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error updating schedule: {str(e)}', 'danger')
    return redirect(url_for('weekly_schedule_user', user_id=user.id))

@app.route('/api/user/<int:user_id>/intervals')
def get_user_intervals(user_id):
    """API endpoint to get user time intervals"""
    user = get_authenticated_user()
    if not user:
        return jsonify({'success': False, 'message': 'Not authenticated'}), 401
    
    user = ManagedUser.query.get_or_404(user_id)
    
    # Get all intervals for this user
    intervals = UserDailyTimeInterval.query.filter_by(user_id=user.id).all()
    
    # Format intervals by day
    intervals_dict = {}
    for interval in intervals:
        intervals_dict[interval.day_of_week] = {
            'id': interval.id,
            'day_name': interval.get_day_name(),
            'start_hour': interval.start_hour,
            'start_minute': interval.start_minute,
            'end_hour': interval.end_hour,
            'end_minute': interval.end_minute,
            'is_enabled': interval.is_enabled,
            'is_synced': interval.is_synced,
            'time_range': interval.get_time_range_string(),
            'last_synced': interval.last_synced.strftime('%Y-%m-%d %H:%M') if interval.last_synced else None
        }
    
    return jsonify({
        'success': True,
        'intervals': intervals_dict,
        'username': user.username
    })

@app.route('/api/user/<int:user_id>/intervals/update', methods=['POST'])
def update_user_intervals(user_id):
    """API endpoint to update user time intervals"""
    user = get_authenticated_user()
    if not user:
        return jsonify({'success': False, 'message': 'Not authenticated'}), 401
    
    user = ManagedUser.query.get_or_404(user_id)
    
    try:
        # Get interval data from request
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'message': 'No data provided'}), 400
        
        intervals_data = data.get('intervals', {})
        
        for day_str, interval_data in intervals_data.items():
            try:
                day_of_week = int(day_str)
                if not (1 <= day_of_week <= 7):
                    continue
                
                # Get or create interval for this day
                interval = UserDailyTimeInterval.query.filter_by(
                    user_id=user.id,
                    day_of_week=day_of_week
                ).first()
                
                if not interval:
                    interval = UserDailyTimeInterval(
                        user_id=user.id,
                        day_of_week=day_of_week
                    )
                    db.session.add(interval)
                
                # Update interval properties
                interval.start_hour = int(interval_data.get('start_hour', 9))
                interval.start_minute = int(interval_data.get('start_minute', 0))
                interval.end_hour = int(interval_data.get('end_hour', 17))
                interval.end_minute = int(interval_data.get('end_minute', 0))
                interval.is_enabled = bool(interval_data.get('is_enabled', False))
                
                # Validate the interval
                if not interval.is_valid_interval():
                    return jsonify({
                        'success': False,
                        'message': f'Invalid time interval for {interval.get_day_name()}: start time must be before end time'
                    }), 400
                
                # Mark as modified (needs sync)
                interval.mark_modified()
                
            except (ValueError, KeyError) as e:
                return jsonify({
                    'success': False,
                    'message': f'Invalid data format: {str(e)}'
                }), 400
        
        db.session.commit()
        task_manager.trigger_sync()

        return jsonify({
            'success': True,
            'message': f'Time intervals updated for {user.username}',
            'username': user.username
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({
            'success': False,
            'message': f'Error updating intervals: {str(e)}'
        }), 500

@app.route('/api/user/<int:user_id>/intervals/sync-status')
def get_intervals_sync_status(user_id):
    """Get sync status of user's time intervals"""
    user = get_authenticated_user()
    if not user:
        return jsonify({'success': False, 'message': 'Not authenticated'}), 401
    
    user = ManagedUser.query.get_or_404(user_id)
    
    # Get all intervals for this user
    intervals = UserDailyTimeInterval.query.filter_by(user_id=user.id).all()
    
    # Check if any intervals need sync
    needs_sync = any(not interval.is_synced for interval in intervals)
    
    # Get last sync time (most recent among all intervals)
    last_synced = None
    if intervals:
        synced_intervals = [i for i in intervals if i.last_synced]
        if synced_intervals:
            last_synced = max(i.last_synced for i in synced_intervals)
            last_synced = last_synced.strftime('%Y-%m-%d %H:%M')
    
    # Count enabled vs total intervals
    enabled_count = sum(1 for i in intervals if i.is_enabled)
    total_count = len(intervals)
    
    return jsonify({
        'success': True,
        'needs_sync': needs_sync,
        'last_synced': last_synced,
        'enabled_intervals': enabled_count,
        'total_intervals': total_count,
        'username': user.username
    })

@app.route('/api/schedule-sync-status/<int:user_id>')
def get_schedule_sync_status(user_id):
    """Get the sync status of a user's weekly schedule"""
    user = get_authenticated_user()
    if not user:
        return jsonify({'success': False, 'message': 'Not authenticated'}), 401
    
    user = ManagedUser.query.get_or_404(user_id)
    
    if user.weekly_schedule:
        schedule_dict = user.weekly_schedule.get_schedule_dict()
        last_synced = None
        if user.weekly_schedule.last_synced:
            last_synced = user.weekly_schedule.last_synced.strftime('%Y-%m-%d %H:%M')
        
        return jsonify({
            'success': True,
            'is_synced': user.weekly_schedule.is_synced,
            'schedule': schedule_dict,
            'last_synced': last_synced,
            'last_modified': user.weekly_schedule.last_modified.strftime('%Y-%m-%d %H:%M') if user.weekly_schedule.last_modified else None
        })
    else:
        return jsonify({
            'success': True,
            'is_synced': True,  # No schedule means no sync needed
            'schedule': None,
            'last_synced': None,
            'last_modified': None
        })

@app.route('/stats/<int:user_id>')
def user_stats(user_id):
    """Display extended usage history for a single host."""
    user = get_authenticated_user()
    if not user:
        flash('Please login first', 'warning')
        return redirect(url_for('login'))

    user = ManagedUser.query.get_or_404(user_id)
    return render_template('stats.html',
        user=user,
        daily_30=user.get_recent_usage(days=30),
        weekly_13=user.get_usage_weekly_grouped(weeks=13),
        monthly_12=user.get_usage_monthly_grouped(months=12),
        all_monthly=user.get_all_usage_monthly(),
    )


@app.route('/stats/group/<username>')
def group_stats(username):
    """Display aggregated usage history across all hosts of a username group."""
    user = get_authenticated_user()
    if not user:
        flash('Please login first', 'warning')
        return redirect(url_for('login'))

    members = ManagedUser.query.filter_by(username=username).all()
    if not members:
        flash(f'No users found with username {username}', 'danger')
        return redirect(url_for('dashboard'))

    from collections import defaultdict

    # daily_30: sum seconds per date across all hosts
    daily_30: dict = {}
    for m in members:
        for d, s in m.get_recent_usage(days=30).items():
            daily_30[d] = daily_30.get(d, 0) + s
    daily_30 = dict(sorted(daily_30.items()))

    # weekly_13
    weekly_map: dict = {}
    for m in members:
        for w in m.get_usage_weekly_grouped(weeks=13):
            k = w['week_start']
            if k not in weekly_map:
                weekly_map[k] = {'label': w['label'], 'week_start': k, 'total': 0}
            weekly_map[k]['total'] += w['total']
    weekly_13 = [weekly_map[k] for k in sorted(weekly_map)]

    # monthly_12
    monthly_map: dict = {}
    for m in members:
        for mo in m.get_usage_monthly_grouped(months=12):
            k = mo['month']
            if k not in monthly_map:
                monthly_map[k] = {'label': mo['label'], 'month': k, 'total': 0}
            monthly_map[k]['total'] += mo['total']
    monthly_12 = [monthly_map[k] for k in sorted(monthly_map)]

    # all_monthly
    allmo_map: dict = {}
    for m in members:
        for mo in m.get_all_usage_monthly():
            k = mo['month']
            if k not in allmo_map:
                allmo_map[k] = {'label': mo['label'], 'month': k, 'total': 0}
            allmo_map[k]['total'] += mo['total']
    all_monthly = [allmo_map[k] for k in sorted(allmo_map)]

    from types import SimpleNamespace
    stats_user = SimpleNamespace(
        username=username,
        system_ip=', '.join(m.system_ip for m in members),
    )
    return render_template('stats.html',
        user=stats_user,
        daily_30=daily_30,
        weekly_13=weekly_13,
        monthly_12=monthly_12,
        all_monthly=all_monthly,
    )

@app.route('/api/modify-time', methods=['POST'])
def modify_time():
    """Modify time left for a user"""
    user = get_authenticated_user()
    if not user:
        return jsonify({'success': False, 'message': 'Not authenticated'}), 401
    
    # Get parameters from request
    user_id = request.form.get('user_id')
    operation = request.form.get('operation')
    seconds = request.form.get('seconds')
    
    if not user_id or not operation or not seconds:
        return jsonify({'success': False, 'message': 'Missing required parameters'}), 400
    
    try:
        user_id = int(user_id)
        seconds = int(seconds)
    except ValueError:
        return jsonify({'success': False, 'message': 'Invalid parameter format'}), 400
    
    # Validate operation
    if operation not in ['+', '-']:
        return jsonify({'success': False, 'message': "Operation must be '+' or '-'"}), 400
    
    # Get user from database
    user = ManagedUser.query.get_or_404(user_id)
    
    # Create SSH client
    ssh_client = SSHClient(hostname=user.system_ip)
    
    # Execute the command
    success, message = ssh_client.modify_time_left(user.username, operation, seconds)
    
    if success:
        # Update user info to reflect changes
        is_valid, _, config_dict = ssh_client.validate_user(user.username)
        if is_valid and config_dict:
            user.last_checked = datetime.utcnow()
            user.last_config = json.dumps(config_dict)
            # Clear any pending adjustments since we succeeded
            user.pending_time_adjustment = None
            user.pending_time_operation = None
            db.session.commit()
            
        return jsonify({
            'success': True,
            'message': message,
            'username': user.username,
            'refresh': True
        })
    else:
        # Store as pending adjustment if it failed
        user.pending_time_adjustment = seconds
        user.pending_time_operation = operation
        db.session.commit()
        task_manager.trigger_sync()

        return jsonify({
            'success': True,  # We report success since we stored it for later
            'message': f"Computer seems to be offline. Time adjustment of {operation}{seconds} seconds has been queued and will be applied when the computer comes online.",
            'username': user.username,
            'pending': True,
            'refresh': True
        })

@app.route('/api/group/<username>/adjust-pool', methods=['POST'])
def group_adjust_pool(username):
    """Add or subtract time from the shared pool for a username group.

    For single-host groups the request is forwarded immediately to the host
    (with pending-retry on failure).  For multi-host groups a
    GroupTimeAdjustment record is created/updated; the reconciliation loop
    will propagate the change to all hosts within the next 10 s cycle.
    """
    user = get_authenticated_user()
    if not user:
        return jsonify({'success': False, 'message': 'Not authenticated'}), 401

    operation = request.form.get('operation')
    seconds_str = request.form.get('seconds')

    if not operation or not seconds_str:
        return jsonify({'success': False, 'message': 'Missing required parameters'}), 400
    if operation not in ['+', '-']:
        return jsonify({'success': False, 'message': "Operation must be '+' or '-'"}), 400
    try:
        seconds = int(seconds_str)
    except ValueError:
        return jsonify({'success': False, 'message': 'Invalid seconds value'}), 400

    members = ManagedUser.query.filter_by(username=username, is_valid=True).all()
    if not members:
        return jsonify({'success': False, 'message': f'No valid users found for {username}'}), 404

    today = date.today()

    if len(members) == 1:
        # Single-host: use existing direct SSH + pending fallback
        user = members[0]
        ssh_client = SSHClient(hostname=user.system_ip)
        success, message = ssh_client.modify_time_left(username, operation, seconds)
        if success:
            is_valid, _, config_dict = ssh_client.validate_user(username)
            if is_valid and config_dict:
                user.last_checked = datetime.utcnow()
                user.last_config = json.dumps(config_dict)
                user.pending_time_adjustment = None
                user.pending_time_operation = None
            # Update GroupTimeAdjustment so the dashboard limit reflects the extra time
            signed = seconds if operation == '+' else -seconds
            adj = GroupTimeAdjustment.query.filter_by(username=username, date=today).first()
            if adj:
                adj.extra_seconds += signed
                adj.reconciled_at = datetime.utcnow()
            else:
                adj = GroupTimeAdjustment(
                    username=username, date=today,
                    extra_seconds=signed, reconciled_at=datetime.utcnow(),
                )
                db.session.add(adj)
            db.session.commit()
            return jsonify({'success': True, 'message': message, 'refresh': True})
        else:
            user.pending_time_adjustment = seconds
            user.pending_time_operation = operation
            db.session.commit()
            task_manager.trigger_sync()
            return jsonify({
                'success': True,
                'message': (f"Computer offline. Adjustment of {operation}{seconds // 60}m "
                            "queued and will apply when it reconnects."),
                'pending': True,
                'refresh': True,
            })

    # Multi-host: upsert GroupTimeAdjustment
    signed = seconds if operation == '+' else -seconds
    adj = GroupTimeAdjustment.query.filter_by(username=username, date=today).first()
    if adj:
        adj.extra_seconds += signed
        adj.reconciled_at = None  # reset so badge reappears until next reconciliation cycle
    else:
        adj = GroupTimeAdjustment(username=username, date=today, extra_seconds=signed)
        db.session.add(adj)
    db.session.commit()
    task_manager.trigger_sync()

    total_min = adj.extra_seconds // 60
    sign_str = '+' if total_min >= 0 else ''
    return jsonify({
        'success': True,
        'message': (f"Pool adjusted {operation}{seconds // 60}m "
                    f"(total today: {sign_str}{total_min}m). "
                    "All hosts will be updated within 10 seconds."),
        'refresh': True,
    })


# With app context
with app.app_context():
    db.create_all()
    print("Database tables verified")

    # Add reconciled_at column if it doesn't exist (migration for existing DBs)
    with db.engine.connect() as conn:
        cols = [row[1] for row in conn.execute(db.text("PRAGMA table_info(group_time_adjustment)"))]
        if 'reconciled_at' not in cols:
            conn.execute(db.text("ALTER TABLE group_time_adjustment ADD COLUMN reconciled_at DATETIME"))
            conn.commit()
            print("Migrated group_time_adjustment: added reconciled_at column")

    # Initialize admin password if it doesn't exist
    if not Settings.get_value('admin_password_hash', None) and not Settings.get_value('admin_password', None):
        Settings.set_admin_password('admin')
        print("Admin password initialized")

    # Initialize authentication mode if it doesn't exist (default: local)
    if not Settings.get_value('AUTH_MODE', None):
        Settings.set_value('AUTH_MODE', 'local')
        print("Authentication mode initialized to: local (username/password)")

    # Initialize sync/reconciliation settings if they don't exist
    _sync_defaults = {
        'RECONCILE_THRESHOLD':       '300',
        'RECONCILE_ACTIVE_INTERVAL': '120',
        'RECONCILE_IDLE_INTERVAL':   '180',
        'SKIP_ACTIVE_HOST':          'true',
    }
    for _key, _val in _sync_defaults.items():
        if Settings.get_value(_key) is None:
            Settings.set_value(_key, _val)
    print("Sync settings initialized")

    # Start background tasks automatically
    task_manager.start()
    print("Background tasks started automatically")

if __name__ == '__main__':
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    app.run(host='0.0.0.0', port=5000, debug=debug, use_reloader=debug)
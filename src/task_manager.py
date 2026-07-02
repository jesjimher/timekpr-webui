import threading
import time
from datetime import datetime, date, timedelta
import logging
import json
import traceback

from src.database import (
    db,
    ManagedUser,
    UserTimeUsage,
    UserDailyTimeInterval,
    coerce_time_spent_day,
    GroupTimeAdjustment,
    get_user_groups,
    group_today_limit,
    Settings,
)
from src.ssh_helper import SSHClient

logger = logging.getLogger(__name__)


class BackgroundTaskManager:
    # Minimum drift before issuing a settimeleft correction during reconciliation
    _RECONCILE_THRESHOLD = 60   # seconds

    # How often to re-read usage data from each host (SSH --userinfo)
    _READ_INTERVAL = 60         # seconds

    # Offline backoff: delay = min(failures * base, max)
    _BACKOFF_BASE = 30          # seconds per failure
    _BACKOFF_MAX = 300          # 5 minutes maximum (used for usage reads)
    _PUSH_BACKOFF_MAX = 60      # shorter cap for user-initiated pending pushes

    # Reconciliation cadence for multi-host groups
    _RECONCILE_ACTIVE = 30      # seconds when usage is active today
    _RECONCILE_IDLE = 60        # seconds when no usage today

    def __init__(self, app=None):
        self.app = app
        self.running = False
        self.thread = None
        self.last_error = None
        self._task_lock = threading.Lock()
        self._sync_event = threading.Event()   # wakes the loop early on UI save
        self._host_backoff: dict = {}           # {hostname: {failures, last_attempt}}
        self._last_full_read: dict = {}         # {user_id: datetime}
        self._last_reconcile: dict = {}         # {username: datetime}
        self._prev_time_left: dict = {}         # {user_id: seconds} last known TIME_LEFT_DAY per host
        self._logged_in_users: dict = {}        # {user_id: bool} whether user has an active session

    def init_app(self, app):
        self.app = app

    def start(self):
        if self.running:
            logger.info("Task manager already running, not starting again")
            return
        self.running = True
        self.thread = threading.Thread(target=self._run_tasks, daemon=True)
        self.thread.start()
        logger.info("Background task manager started with thread ID: %s", self.thread.ident)

    def stop(self):
        logger.info("Stopping background task manager...")
        self.running = False
        self._sync_event.set()  # wake sleeping thread so it exits quickly
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                logger.warning("Thread did not stop gracefully within timeout")
            else:
                logger.info("Thread stopped successfully")
        logger.info("Background task manager stopped")

    def restart(self):
        logger.info("Restarting background task manager...")
        self.stop()
        time.sleep(1)
        self.start()
        logger.info("Background task manager restarted")

    def trigger_sync(self):
        """Wake the background loop immediately to push pending changes."""
        self._sync_event.set()

    def get_status(self):
        status = {
            'running': self.running,
            'thread_alive': self.thread.is_alive() if self.thread else False,
            'last_error': self.last_error,
            'thread_id': self.thread.ident if self.thread else None,
        }
        logger.info("Task manager status: %s", status)
        return status

    # ------------------------------------------------------------------ backoff

    def _host_ready(self, hostname: str, max_delay: int = None) -> bool:
        """Whether a host is ready to be contacted again after past failures.

        ``max_delay`` caps the backoff for this particular check. Reads use the
        default (long) cap; user-initiated pending pushes pass a short cap so an
        urgent change is retried much sooner without hammering a dead host.
        """
        state = self._host_backoff.get(hostname)
        if not state:
            return True
        cap = self._BACKOFF_MAX if max_delay is None else max_delay
        delay = min(state['failures'] * self._BACKOFF_BASE, cap)
        return datetime.utcnow() >= state['last_attempt'] + timedelta(seconds=delay)

    def _record_success(self, hostname: str):
        if hostname in self._host_backoff:
            logger.info("Host %s recovered, clearing backoff", hostname)
            del self._host_backoff[hostname]

    def _record_failure(self, hostname: str):
        state = self._host_backoff.get(hostname, {'failures': 0})
        failures = state['failures'] + 1
        self._host_backoff[hostname] = {
            'failures': failures,
            'last_attempt': datetime.utcnow(),
        }
        delay = min(failures * self._BACKOFF_BASE, self._BACKOFF_MAX)
        logger.info("Host %s in backoff (read %ds, push %ds; failure #%d)",
                    hostname, delay,
                    min(failures * self._BACKOFF_BASE, self._PUSH_BACKOFF_MAX), failures)

    def get_offline_hosts(self) -> set:
        """Hostnames whose SSH connection is currently failing (in backoff)."""
        return set(self._host_backoff.keys())

    def get_logged_in_users(self) -> set:
        """User IDs where the managed user currently has an active session."""
        return {uid for uid, logged_in in self._logged_in_users.items() if logged_in}

    # ------------------------------------------------------------------ main loop

    def _run_tasks(self):
        logger.info("Task loop started in thread ID: %s", threading.current_thread().ident)
        while self.running:
            try:
                if self._task_lock.acquire(blocking=False):
                    try:
                        logger.info("Starting task execution cycle")
                        if self.app:
                            with self.app.app_context():
                                self._push_pending_changes()
                                self._read_usage_data()
                                self._reconcile_groups()
                        else:
                            logger.error("App is not initialized in task manager")
                        self.last_error = None
                    finally:
                        self._task_lock.release()
                else:
                    logger.info("Task already running, skipping this cycle")
            except Exception as e:
                if self._task_lock.locked():
                    self._task_lock.release()
                error_msg = f"Error in background task: {str(e)}"
                trace = traceback.format_exc()
                logger.error("%s\n%s", error_msg, trace)
                self.last_error = {
                    'message': error_msg,
                    'trace': trace,
                    'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                }

            # Sleep up to 10s, but wake immediately on trigger_sync() or stop()
            logger.info("Task cycle finished, waiting up to 10s")
            self._sync_event.wait(timeout=10)
            self._sync_event.clear()

    # ------------------------------------------------------------------ push (fast path)

    def _push_pending_changes(self):
        """Push unsynced config to remote hosts. Opens SSH only when there's actual work."""
        try:
            users = ManagedUser.query.all()
            for user in users:
                try:
                    has_pending = (
                        (user.pending_time_adjustment is not None and user.pending_time_operation is not None)
                        or (user.weekly_schedule and not user.weekly_schedule.is_synced)
                        or UserDailyTimeInterval.query.filter_by(
                            user_id=user.id, is_synced=False
                        ).first() is not None
                    )
                    if not has_pending:
                        continue

                    # Pending changes are user-initiated (time adjustment, schedule,
                    # intervals) and should be applied ASAP, so they use a short
                    # backoff cap: an online-but-recently-flaky host recovers within
                    # ~60s instead of up to 5 min, while a truly-dead host is still
                    # retried at most every ~60s rather than every cycle.
                    if not self._host_ready(user.system_ip, max_delay=self._PUSH_BACKOFF_MAX):
                        logger.info("Host %s in push backoff, skipping push for %s",
                                    user.system_ip, user.username)
                        continue

                    logger.info("Pushing pending changes for %s @ %s", user.username, user.system_ip)
                    try:
                        with SSHClient(hostname=user.system_ip) as ssh:
                            self._apply_user_changes(user, ssh)
                        self._record_success(user.system_ip)
                    except Exception as e:
                        logger.error("SSH error pushing to %s @ %s: %s",
                                     user.username, user.system_ip, e)
                        self._record_failure(user.system_ip)
                        db.session.rollback()
                except Exception as e:
                    logger.error("Error processing pending for %s: %s\n%s",
                                 user.username, e, traceback.format_exc())
                    db.session.rollback()
        except Exception as e:
            logger.error("Error in _push_pending_changes: %s\n%s", e, traceback.format_exc())
            db.session.rollback()

    def _apply_user_changes(self, user, ssh: SSHClient):
        """Apply all pending changes for one user over an already-open SSH connection."""
        # --- time adjustment ---
        if user.pending_time_adjustment is not None and user.pending_time_operation is not None:
            adjustment = user.pending_time_adjustment
            op = user.pending_time_operation
            logger.info("Applying pending time adjustment for %s: %s%ds",
                        user.username, op, adjustment)
            success, message = ssh.modify_time_left(user.username, op, adjustment)
            if success:
                user.pending_time_adjustment = None
                user.pending_time_operation = None
                signed = adjustment if op == '+' else -adjustment
                today = date.today()
                adj = GroupTimeAdjustment.query.filter_by(
                    username=user.username, date=today
                ).first()
                if adj:
                    adj.extra_seconds += signed
                    adj.reconciled_at = datetime.utcnow()
                else:
                    db.session.add(GroupTimeAdjustment(
                        username=user.username, date=today,
                        extra_seconds=signed, reconciled_at=datetime.utcnow(),
                    ))
                db.session.commit()
                logger.info("Cleared pending time adjustment for %s", user.username)
            else:
                logger.warning("Failed to apply time adjustment for %s: %s", user.username, message)

        # --- weekly schedule ---
        if user.weekly_schedule and not user.weekly_schedule.is_synced:
            schedule_dict = user.weekly_schedule.get_schedule_dict()
            _week_days = ('monday', 'tuesday', 'wednesday', 'thursday',
                          'friday', 'saturday', 'sunday')
            if not any((schedule_dict.get(d, 0) or 0) > 0 for d in _week_days):
                user.weekly_schedule.mark_synced()
                db.session.commit()
            else:
                success, message = ssh.set_weekly_time_limits(user.username, schedule_dict)
                if success:
                    user.weekly_schedule.mark_synced()
                    db.session.commit()
                    logger.info("Synced weekly schedule for %s", user.username)
                else:
                    logger.warning("Failed to sync weekly schedule for %s: %s", user.username, message)

        # --- time intervals ---
        unsynced_intervals = UserDailyTimeInterval.query.filter_by(
            user_id=user.id, is_synced=False
        ).all()
        if unsynced_intervals:
            intervals_dict = {iv.day_of_week: iv for iv in user.time_intervals}
            success, message = ssh.set_allowed_hours(user.username, intervals_dict)
            if success:
                for iv in unsynced_intervals:
                    iv.mark_synced()
                db.session.commit()
                logger.info("Synced %d time intervals for %s", len(unsynced_intervals), user.username)
            else:
                logger.warning("Failed to sync time intervals for %s: %s", user.username, message)

    # ------------------------------------------------------------------ read usage (slow path)

    def _read_usage_data(self):
        """Read current usage from all hosts. Each host polled at most every _READ_INTERVAL seconds."""
        now = datetime.utcnow()
        try:
            users = ManagedUser.query.all()
            for user in users:
                try:
                    last_read = self._last_full_read.get(user.id)
                    if last_read and (now - last_read).total_seconds() < self._READ_INTERVAL:
                        continue  # not due yet

                    if not self._host_ready(user.system_ip):
                        logger.info("Host %s in backoff, skipping read for %s",
                                    user.system_ip, user.username)
                        continue

                    logger.info("Reading usage for %s @ %s", user.username, user.system_ip)
                    try:
                        with SSHClient(hostname=user.system_ip) as ssh:
                            is_valid, result_message, config_dict = ssh.validate_user(user.username)
                            self._logged_in_users[user.id] = ssh.is_user_logged_in(user.username)
                        self._record_success(user.system_ip)
                        self._last_full_read[user.id] = now

                        user.last_checked = now
                        if is_valid and config_dict:
                            user.last_config = json.dumps(config_dict)
                            user.is_valid = True
                            today = date.today()
                            time_spent = coerce_time_spent_day(config_dict.get('TIME_SPENT_DAY', 0))
                            usage = UserTimeUsage.query.filter_by(
                                user_id=user.id, date=today
                            ).first()
                            if usage:
                                usage.time_spent = time_spent
                            else:
                                db.session.add(
                                    UserTimeUsage(user_id=user.id, date=today, time_spent=time_spent)
                                )
                            logger.info("Updated usage for %s: %ds", user.username, time_spent)
                        else:
                            logger.warning("Could not get data for %s: %s", user.username, result_message)
                        db.session.commit()
                    except Exception as e:
                        logger.error("SSH error reading %s @ %s: %s",
                                     user.username, user.system_ip, e)
                        self._record_failure(user.system_ip)
                        user.last_checked = now
                        try:
                            db.session.commit()
                        except Exception:
                            db.session.rollback()
                except Exception as e:
                    logger.error("Error reading usage for %s: %s\n%s",
                                 user.username, e, traceback.format_exc())
                    db.session.rollback()
        except Exception as e:
            logger.error("Error in _read_usage_data: %s\n%s", e, traceback.format_exc())
            db.session.rollback()

    # ------------------------------------------------------------------ group reconciliation

    def _reconcile_groups(self):
        """Enforce shared time pool across multi-host groups with adaptive cadence.

        Polls active groups (usage > 0 today) every RECONCILE_ACTIVE_INTERVAL seconds,
        idle groups every RECONCILE_IDLE_INTERVAL seconds.

        Reconciliation parameters are read from the Settings table each cycle so they
        can be adjusted through the web UI without restarting the server.
        """
        groups = get_user_groups()
        today = date.today()
        now = datetime.utcnow()

        # Read configurable parameters from Settings (fall back to class defaults)
        threshold = Settings.get_int('RECONCILE_THRESHOLD', self._RECONCILE_THRESHOLD)
        active_interval = Settings.get_int('RECONCILE_ACTIVE_INTERVAL', self._RECONCILE_ACTIVE)
        idle_interval = Settings.get_int('RECONCILE_IDLE_INTERVAL', self._RECONCILE_IDLE)
        skip_active_host = Settings.get_value('SKIP_ACTIVE_HOST', 'true') == 'true'

        for username, members in groups.items():
            if len(members) < 2:
                continue  # single-host: handled by per-host pending_time_adjustment

            try:
                total_spent = sum(
                    (UserTimeUsage.query.filter_by(user_id=m.id, date=today).first() or
                     type('_', (), {'time_spent': 0})()).time_spent
                    for m in members
                )

                # Check for pending unreconciled pool adjustment — if present, skip
                # the normal cadence check so the change is applied immediately.
                pending_adj = GroupTimeAdjustment.query.filter_by(
                    username=username, date=today
                ).first()
                has_pending_adj = bool(pending_adj and pending_adj.reconciled_at is None
                                       and pending_adj.extra_seconds != 0)

                interval = active_interval if total_spent > 0 else idle_interval
                last = self._last_reconcile.get(username)
                if not has_pending_adj and last and (now - last).total_seconds() < interval:
                    continue

                limit = group_today_limit(username)
                if limit <= 0:
                    self._last_reconcile[username] = now
                    continue

                desired = max(0, limit - total_spent)
                logger.info(
                    "Group '%s': limit=%ds spent=%ds desired=%ds threshold=%ds",
                    username, limit, total_spent, desired, threshold,
                )

                any_host_reached = False
                for m in members:
                    try:
                        current_left = m.get_config_value('TIME_LEFT_DAY')
                        if current_left is None:
                            continue

                        any_host_reached = True

                        # Skip the host that is actively consuming time to avoid showing
                        # a "time changed" notification on the screen the user is using.
                        if skip_active_host:
                            prev_left = self._prev_time_left.get(m.id)
                            if prev_left is not None and current_left < prev_left:
                                logger.info("%s@%s: skipping active host (left %d→%d)",
                                            username, m.system_ip, prev_left, current_left)
                                self._prev_time_left[m.id] = current_left
                                continue
                        self._prev_time_left[m.id] = current_left

                        delta = desired - current_left
                        if abs(delta) < threshold:
                            logger.info("%s@%s: delta=%ds < threshold=%ds, no adjustment",
                                        username, m.system_ip, delta, threshold)
                            continue

                        # A pending manual pool adjustment uses the short push
                        # backoff so it reaches every host quickly; routine
                        # reconciliation uses the normal (long) cap.
                        cap = self._PUSH_BACKOFF_MAX if has_pending_adj else None
                        if not self._host_ready(m.system_ip, max_delay=cap):
                            logger.info("Host %s in backoff, skipping reconciliation", m.system_ip)
                            continue

                        operation = '+' if delta > 0 else '-'
                        amount = abs(delta)
                        logger.info("Reconciling %s@%s: %s%ds (current=%d desired=%d)",
                                    username, m.system_ip, operation, amount, current_left, desired)
                        try:
                            with SSHClient(hostname=m.system_ip) as ssh:
                                success, msg = ssh.modify_time_left(username, operation, amount)
                            if success:
                                self._record_success(m.system_ip)
                                logger.info("Reconciliation OK for %s@%s", username, m.system_ip)
                            else:
                                logger.warning("Reconciliation failed for %s@%s: %s",
                                               username, m.system_ip, msg)
                        except Exception as e:
                            logger.error("SSH error reconciling %s@%s: %s", username, m.system_ip, e)
                            self._record_failure(m.system_ip)
                    except Exception as e:
                        logger.error("Error reconciling %s@%s: %s", username, m.system_ip, e)

                self._last_reconcile[username] = now

                if any_host_reached:
                    adj = GroupTimeAdjustment.query.filter_by(username=username, date=today).first()
                    if adj and adj.reconciled_at is None:
                        adj.reconciled_at = now
                        db.session.commit()

            except Exception as e:
                logger.error("Error reconciling group '%s': %s\n%s",
                             username, e, traceback.format_exc())

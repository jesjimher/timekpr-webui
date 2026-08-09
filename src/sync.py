"""The single convergence loop. Replaces src/task_manager.py.

Old model: "something changed -> mark it -> push it -> mark it sent", with a
separate read pass, a separate reconciliation pass, and a one-shot drift
repair. New model: every cycle, for every account, look at how it compares to
where it should be and correct it -- offline handling, the sync badge, and
drift repair all fall out of that same idea instead of needing separate
mechanisms.

Every write is differential (see CLAUDE.md's command budget): a command is
only sent when the host's own last-read config doesn't already match the
target, so a converged system sends nothing.

There's no separate detection of "which host is the child actively using" --
the propagation threshold makes that unnecessary. A host actually in use has
timekpr counting down locally in step with the shared pool, so its own delta
from read-cycle jitter alone stays under the threshold and it's simply never
written to. An idle sibling's reported time stays flat while the pool keeps
draining, so it crosses the threshold and gets corrected instead. A one-off
bonus is a big enough jump to cross the threshold everywhere at once,
including the host in use -- which is exactly where a bonus granted mid-play
should land, so there is deliberately no extra rule to exclude it.
"""
import time
import threading
import json
import logging
from datetime import datetime, date, timedelta

from src.models import (
    db, User, Usage, Settings,
    coerce_int, coerce_time_spent_day, config_mismatch_detail,
)
from src.timekpr import SSHClient

logger = logging.getLogger(__name__)


class SyncManager:
    # Seconds between full cycles. The only recurring SSH traffic in the app.
    READ_INTERVAL = 60

    # How far the shared pool may drift from a host's reported TIME_LEFT_DAY
    # before it gets corrected. Also what gives "skip the host in use" for
    # free: its own drift stays under this just from read-cycle jitter, since
    # timekpr is already counting down there in step with the pool.
    PROPAGATION_THRESHOLD_DEFAULT = 300

    # Offline backoff, in-memory only -- it doesn't need to survive a restart,
    # a host that's actually offline will just fail the next cycle again.
    BACKOFF_BASE = 30
    BACKOFF_MAX = 300

    def __init__(self, app=None):
        self.app = app
        self.running = False
        self.thread = None
        self.last_error = None
        self.last_cycle_at = None
        self._host_backoff: dict = {}   # {ip: {'failures': int, 'last_attempt': datetime}}
        self._active: dict = {}         # {account_id: bool} -- display only, see _sync_user

    def init_app(self, app):
        self.app = app

    def start(self):
        if self.running:
            logger.info("Sync loop already running")
            return
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        logger.info("Sync loop started")

    def stop(self):
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)

    def restart(self):
        self.stop()
        time.sleep(1)
        self.start()

    def sync_now(self, username):
        """Run one sync pass for a single user immediately, off the periodic
        clock. Used right after an edit (schedule save, time adjustment) so
        the dashboard doesn't sit on a stale host reading for up to a full
        READ_INTERVAL before the change is visible -- without this, the
        parent has no way to tell a slow-to-converge edit apart from one
        that silently failed."""
        if not self.app:
            return

        def _run():
            try:
                with self.app.app_context():
                    threshold = Settings.get_int('PROPAGATION_THRESHOLD', self.PROPAGATION_THRESHOLD_DEFAULT)
                    user = User.query.filter_by(username=username).first()
                    if user:
                        self._sync_user(user, threshold)
            except Exception:
                logger.exception("On-demand sync failed for %s", username)

        threading.Thread(target=_run, daemon=True).start()

    def get_status(self):
        return {
            'running': self.running,
            'thread_alive': self.thread.is_alive() if self.thread else False,
            'last_error': self.last_error,
            'last_cycle_at': self.last_cycle_at.isoformat() if self.last_cycle_at else None,
        }

    def get_offline_hosts(self):
        """Host IPs currently in SSH backoff."""
        return set(self._host_backoff.keys())

    def get_active_accounts(self):
        """Account ids whose time left dropped on the last read -- i.e. where
        someone is actually using the computer right now, not just logged in."""
        return {aid for aid, v in self._active.items() if v}

    # ------------------------------------------------------------------ backoff

    def _host_ready(self, ip):
        state = self._host_backoff.get(ip)
        if not state:
            return True
        delay = min(state['failures'] * self.BACKOFF_BASE, self.BACKOFF_MAX)
        return datetime.utcnow() >= state['last_attempt'] + timedelta(seconds=delay)

    def _record_success(self, ip):
        self._host_backoff.pop(ip, None)

    def _record_failure(self, ip):
        state = self._host_backoff.get(ip, {'failures': 0})
        self._host_backoff[ip] = {'failures': state['failures'] + 1, 'last_attempt': datetime.utcnow()}

    # ------------------------------------------------------------------ main loop

    def _loop(self):
        while self.running:
            try:
                if self.app:
                    with self.app.app_context():
                        self._run_cycle()
                self.last_error = None
            except Exception as e:
                logger.exception("Sync cycle failed")
                self.last_error = {'message': str(e), 'time': datetime.utcnow().isoformat()}
            self.last_cycle_at = datetime.utcnow()
            for _ in range(self.READ_INTERVAL):
                if not self.running:
                    break
                time.sleep(1)

    def _run_cycle(self):
        threshold = Settings.get_int('PROPAGATION_THRESHOLD', self.PROPAGATION_THRESHOLD_DEFAULT)
        for user in User.query.all():
            self._sync_user(user, threshold)

    def _sync_user(self, user, threshold):
        fresh_this_cycle = set()
        any_unreadable = False

        for account in user.accounts:
            ip = account.host.ip
            if not self._host_ready(ip):
                continue

            # Captured before this read overwrites last_config -- comparing
            # against it is how "in use" is decided below (see _active dict).
            previous_time_left = account.time_left()

            try:
                with SSHClient(hostname=ip) as ssh:
                    is_valid, message, config = ssh.validate_user(user.username)
                self._record_success(ip)
            except Exception as e:
                logger.warning("Read failed for %s@%s: %s", user.username, ip, e)
                self._record_failure(ip)
                account.last_error = str(e)
                db.session.commit()
                any_unreadable = True
                continue

            if not is_valid or not config:
                account.last_error = message
                db.session.commit()
                any_unreadable = True
                continue

            now = datetime.utcnow()
            account.last_config = json.dumps(config)
            account.last_synced = now
            account.last_error = None
            fresh_this_cycle.add(account.id)

            # "In use" means time is actually being spent right now, not that
            # a session is merely open (a locked/idle session stays logged in
            # for hours without that meaning anything). A drop in TIME_LEFT_DAY
            # since the last read is what timekpr itself only does while the
            # user is active, so it's the one signal that means what it says.
            current_time_left = coerce_int(config.get('TIME_LEFT_DAY'))
            self._active[account.id] = (
                previous_time_left is not None and current_time_left is not None
                and current_time_left < previous_time_left
            )

            today = date.today()
            seconds = coerce_time_spent_day(config.get('TIME_SPENT_DAY', 0))
            usage = Usage.query.filter_by(account_id=account.id, date=today).first()
            if usage:
                usage.seconds = seconds
            else:
                db.session.add(Usage(account_id=account.id, date=today, seconds=seconds))
            db.session.commit()

            # A) configuration -- every host, including the one in use
            ok, detail = config_mismatch_detail(user, account)
            if ok:
                account.drift_since = None
            else:
                if not account.drift_since:
                    account.drift_since = now
                logger.info("Config mismatch %s@%s: %s", user.username, ip, detail)
                self._converge_config(user, account, config)
                # Changing the daily budget can make the host recompute its
                # own remaining-time counter as a side effect, so the reading
                # captured above is no longer trustworthy for step B below.
                # Re-read once, right after the write it follows, instead of
                # either trusting a stale value or deferring the pool
                # correction a full cycle (up to 60s) after every schedule
                # edit -- that deferral is what made saving a schedule feel
                # slow even with the immediate on-demand sync in place.
                try:
                    with SSHClient(hostname=ip) as ssh:
                        is_valid, _, fresh_config = ssh.validate_user(user.username)
                    if is_valid and fresh_config:
                        account.last_config = json.dumps(fresh_config)
                        account.last_synced = datetime.utcnow()
                    else:
                        fresh_this_cycle.discard(account.id)
                except Exception as e:
                    logger.warning("Post-config re-read failed for %s@%s: %s", user.username, ip, e)
                    fresh_this_cycle.discard(account.id)
            db.session.commit()

        # B) time left -- only hosts that were read successfully this cycle
        self._converge_pool(user, fresh_this_cycle, any_unreadable, threshold)

    def _converge_config(self, user, account, config):
        if not user.day_limits:
            return
        try:
            with SSHClient(hostname=account.host.ip) as ssh:
                success, message = ssh.set_day_limits(user.username, user.day_limits, config)
            if not success:
                account.last_error = message
                db.session.commit()
        except Exception as e:
            logger.warning("Config push failed for %s@%s: %s", user.username, account.host.ip, e)
            self._record_failure(account.host.ip)

    def _converge_pool(self, user, fresh_this_cycle, any_unreadable, threshold):
        target = user.pool_target_seconds()
        if target is None:
            return  # nothing configured -- nothing to pool-enforce

        for account in user.accounts:
            if account.id not in fresh_this_cycle:
                continue  # never write from a stale or failed read
            current_left = account.time_left()
            if current_left is None:
                continue

            delta = target - current_left

            # A sibling account couldn't be read this cycle, so its usage is
            # frozen and the pool looks bigger than it really is. Cutting is
            # still safe on incomplete data; granting is not.
            if any_unreadable and delta > 0:
                continue
            if abs(delta) < threshold:
                continue

            try:
                with SSHClient(hostname=account.host.ip) as ssh:
                    op = '+' if delta > 0 else '-'
                    success, message = ssh.modify_time_left(user.username, op, abs(delta))
                if success:
                    self._record_success(account.host.ip)
                    cfg = account._config_dict()
                    if cfg is not None:
                        cfg['TIME_LEFT_DAY'] = current_left + delta
                        account.last_config = json.dumps(cfg)
                        db.session.commit()
                else:
                    account.last_error = message
                    db.session.commit()
            except Exception as e:
                logger.warning("Pool push failed for %s@%s: %s", user.username, account.host.ip, e)
                self._record_failure(account.host.ip)

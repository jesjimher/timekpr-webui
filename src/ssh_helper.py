import paramiko
import re
import os
import socket
import logging

logger = logging.getLogger(__name__)


def _maybe_int(value):
    """Best-effort int coercion. Returns None instead of raising or leaving a string.

    timekpra can report negative values (e.g. TIME_LEFT_DAY when over budget) which
    str.isdigit() rejects, silently leaving them as strings for arithmetic done
    elsewhere in the app.
    """
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


class SSHClient:
    # Hard wall-clock limit for each remote command. Without this a host that
    # goes to sleep or drops the network mid-command leaves recv_exit_status()
    # blocked forever, which would freeze the single background sync thread.
    COMMAND_TIMEOUT = 15  # seconds

    # Substrings (case-insensitive) that mark a timekpra command as failed even when
    # it exits 0 -- validate_user's own not-found check is proof some timekpra error
    # paths don't set a non-zero exit code, so exit status alone can't be trusted for
    # mutating commands.
    _ERROR_MARKERS = (
        'traceback', 'error', 'failed', 'invalid', 'not found', 'not supported',
        'permission denied', 'must be', 'usage:', 'cannot',
    )

    # Anchored so a key can contain digits (ALLOWED_HOURS_1..7) -- the previous
    # [A-Z_]+ pattern couldn't match those keys at all, silently dropping them.
    _KEY_RE = re.compile(r'^\s*([A-Z][A-Z0-9_]*)\s*:\s*(.*)$')

    def __init__(self, hostname, username='timekpr-remote', key_path=None, port=22):
        self.hostname = hostname
        self.username = username
        self.port = port
        self._client = None

        if key_path is None:
            project_root = os.path.dirname(os.path.dirname(__file__))
            possible_paths = [
                '/app/ssh/timekpr_ui_key',
                os.path.join(project_root, 'ssh', 'timekpr_ui_key'),
                os.path.join(os.getcwd(), 'ssh', 'timekpr_ui_key'),
                'ssh/timekpr_ui_key',
            ]
            self.key_path = next((p for p in possible_paths if os.path.exists(p)),
                                 os.path.join(project_root, 'ssh', 'timekpr_ui_key'))
        else:
            self.key_path = key_path

    def connect(self):
        """Open SSH connection, reusing an existing active one."""
        if self._client:
            transport = self._client.get_transport()
            if transport and transport.is_active():
                return

        if not os.path.exists(self.key_path):
            raise FileNotFoundError(f"SSH private key not found at {self.key_path}")

        private_key = paramiko.RSAKey.from_private_key_file(self.key_path)
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self.hostname,
            username=self.username,
            pkey=private_key,
            port=self.port,
            timeout=10,
        )
        # Keepalive so a silently-dead peer (suspended laptop, dropped Wi-Fi)
        # tears the transport down instead of leaving reads blocked forever.
        transport = client.get_transport()
        if transport:
            transport.set_keepalive(5)
        self._client = client
        logger.debug("SSH connected to %s", self.hostname)

    def disconnect(self):
        """Close the SSH connection if open."""
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()

    def __del__(self):
        self.disconnect()

    def _exec(self, command, sudo_on_fail=False):
        """Run a command on the open connection; retry with sudo on non-zero exit if requested.

        Bounded by COMMAND_TIMEOUT: if the host stalls, we drop the connection
        and raise so the caller treats it as offline (queue + backoff) instead
        of hanging the background sync thread indefinitely.
        """
        self.connect()
        try:
            stdin, stdout, stderr = self._client.exec_command(
                command, timeout=self.COMMAND_TIMEOUT
            )
            channel = stdout.channel
            channel.settimeout(self.COMMAND_TIMEOUT)
            # Read output first (bounded by the channel timeout), then fetch the
            # exit status, which is delivered once the channel reaches EOF/close.
            out = stdout.read().decode('utf-8')
            err = stderr.read().decode('utf-8')
            exit_status = channel.recv_exit_status()
        except (socket.timeout, EOFError) as e:
            logger.warning("Command on %s timed out/dropped after %ds: %s (%s)",
                           self.hostname, self.COMMAND_TIMEOUT, command, e)
            self.disconnect()  # force a fresh connection on the next attempt
            raise TimeoutError(f"Command timed out on {self.hostname}: {command}")

        # A command can exit 0 while still having failed -- e.g. a permission error
        # printed to stdout/stderr without a non-zero status. Without this check the
        # sudo retry never fires for that case and the failure is recorded as success.
        needs_sudo = exit_status != 0
        if not needs_sudo and sudo_on_fail and self._output_error(out, err):
            needs_sudo = True

        if needs_sudo and sudo_on_fail and not command.startswith('sudo '):
            logger.debug("Retrying with sudo: %s", command)
            return self._exec('sudo ' + command)

        return exit_status, out, err

    def _output_error(self, out, err):
        """Return the first line in stdout/stderr that looks like a timekpra error, or None."""
        for line in (out or '').splitlines() + (err or '').splitlines():
            low = line.strip().lower()
            if low and any(marker in low for marker in self._ERROR_MARKERS):
                return line.strip()
        return None

    def _exec_checked(self, command, description, sudo_on_fail=True):
        """Run a mutating command and treat exit-0-but-error-text output as failure too.

        Used by every command that changes remote state. Read-only commands (like
        --userinfo) keep using _exec directly since they have their own success
        criteria and an over-eager error marker there would just be noise.
        """
        exit_status, out, err = self._exec(command, sudo_on_fail=sudo_on_fail)
        if exit_status != 0:
            detail = (err or out).strip()[:300]
            return False, f"{description}: exit {exit_status}: {detail}"
        bad = self._output_error(out, err)
        if bad:
            logger.warning("Command exited 0 but output looks like an error: %s -> %s", command, bad)
            return False, f"{description}: {bad}"
        return True, out

    def validate_user(self, username):
        """
        Check if a user exists by running timekpra --userinfo.
        Returns: (is_valid, message, config_dict)
        """
        try:
            exit_status, output, error = self._exec(f'timekpra --userinfo {username}')
            not_found = f'User "{username}" configuration is not found'
            if not_found in output or not_found in error:
                return False, f"User '{username}' not found on system", None
            config_dict = self._parse_timekpr_output(output)
            return True, output, config_dict
        except Exception as e:
            return False, f"Connection error: {str(e)}", None

    def _parse_timekpr_output(self, output):
        config_dict = {}
        for line in output.split('\n'):
            match = self._KEY_RE.match(line)
            if match:
                key = match.group(1)
                value = match.group(2).strip()
                as_int = _maybe_int(value)
                if as_int is not None:
                    value = as_int
                elif ';' in value:
                    parts = value.split(';')
                    coerced = [_maybe_int(item) for item in parts]
                    if all(c is not None for c in coerced):
                        value = coerced
                    else:
                        value = parts
                elif value.lower() == 'true':
                    value = True
                elif value.lower() == 'false':
                    value = False
                config_dict[key] = value
        return config_dict

    def is_user_logged_in(self, username):
        """Return True if *username* has an active session according to `who`."""
        _, output, _ = self._exec(f'who | grep -w {username}')
        return bool(output.strip())

    def modify_time_left(self, username, operation, seconds):
        """
        Modify time left using timekpra --settimeleft.
        operation: '+' or '-'
        Returns: (success, message)
        """
        if operation not in ['+', '-']:
            return False, "Invalid operation. Must be '+' or '-'"
        try:
            success, detail = self._exec_checked(
                f'timekpra --settimeleft {username} {operation} {seconds}',
                f"Failed to modify time for {username}",
            )
            if success:
                return True, f"Successfully modified time for {username}: {operation}{seconds} seconds"
            return False, detail
        except Exception as e:
            return False, f"Connection error: {str(e)}"

    def set_weekly_time_limits(self, username, schedule_dict, current_config=None):
        """
        Set daily time limits using timekpra --setalloweddays and --settimelimits.

        schedule_dict: day names -> hour values (Monday..Sunday). A day with 0 (or
        missing) hours is *blocked* for that day -- it is simply left out of the
        allowed-days list, exactly the mechanism timekpr already uses for any
        excluded day; clearing every day just applies that to all seven at once
        instead of leaving the previous limits in force on the host.

        current_config: the last known --userinfo config_dict for this user, if
        available. When given, a command is skipped if the host already reports
        the state it would produce -- every timekpra write raises a desktop
        notification on the logged-in user's screen, so redundant writes are
        avoided rather than re-sent every sync cycle.

        Returns: (success, message)
        """
        try:
            day_order = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']

            allowed_day_nums = [
                i + 1 for i, day in enumerate(day_order)
                if (schedule_dict.get(day, 0) or 0) > 0
            ]
            desired_limits = {
                i + 1: int(round((schedule_dict.get(day, 0) or 0) * 3600))
                for i, day in enumerate(day_order)
                if (schedule_dict.get(day, 0) or 0) > 0
            }

            current_config = current_config or {}
            host_days_raw = current_config.get('ALLOWED_WEEKDAYS')
            host_limits_raw = current_config.get('LIMITS_PER_WEEKDAYS')

            host_days = None
            if isinstance(host_days_raw, list):
                coerced = [_maybe_int(d) for d in host_days_raw]
                if all(c is not None for c in coerced):
                    host_days = set(coerced)
            elif host_days_raw == '':
                host_days = set()  # host explicitly reports no allowed days

            days_match = host_days is not None and host_days == set(allowed_day_nums)

            if not days_match:
                days_arg = ';'.join(str(d) for d in allowed_day_nums)  # may be empty -> blocks every day
                success, detail = self._exec_checked(
                    f"timekpra --setalloweddays {username} '{days_arg}'",
                    "Failed to set allowed days",
                )
                if not success:
                    return False, detail

            # LIMITS_PER_WEEKDAYS is positional against ALLOWED_WEEKDAYS on the host,
            # not against Monday..Sunday -- zip them together rather than assuming order.
            host_limits = None
            if (isinstance(host_days_raw, list) and isinstance(host_limits_raw, list)
                    and len(host_days_raw) == len(host_limits_raw)):
                pairs = [(_maybe_int(d), _maybe_int(v)) for d, v in zip(host_days_raw, host_limits_raw)]
                if all(d is not None and v is not None for d, v in pairs):
                    host_limits = dict(pairs)
            limits_match = host_limits is not None and host_limits == desired_limits

            if desired_limits and not limits_match:
                limits_arg = ';'.join(str(desired_limits[d]) for d in allowed_day_nums)
                success, detail = self._exec_checked(
                    f"timekpra --settimelimits {username} '{limits_arg}'",
                    "Failed to set time limits",
                )
                if not success:
                    return False, detail

            if days_match and limits_match:
                logger.debug("Weekly limits for %s already match host, nothing sent", username)
            else:
                logger.info("Set weekly limits for %s: days=%s limits=%s",
                            username, allowed_day_nums, desired_limits)
            return True, f"Weekly limits confirmed for {username}"

        except Exception as e:
            logger.error("Exception in set_weekly_time_limits: %s", e)
            return False, f"Connection error: {str(e)}"

    def set_allowed_hours(self, username, intervals_dict, current_config=None):
        """
        Set allowed hours using timekpra --setallowedhours.

        intervals_dict: day_of_week (1-7) -> UserDailyTimeInterval. A day absent
        from the dict, disabled, or with an invalid interval means "no restriction"
        for that day (the full 24 hours).

        current_config: the last known --userinfo config_dict, used to skip days
        whose ALLOWED_HOURS_<day> the host already reports as desired.

        Returns: (success, message)
        """
        try:
            day_names = ['', 'monday', 'tuesday', 'wednesday', 'thursday',
                         'friday', 'saturday', 'sunday']
            current_config = current_config or {}
            full_day_tokens = [str(h) for h in range(24)]

            success_count = 0
            skipped_count = 0
            error_messages = []

            for day_num in range(1, 8):
                interval = intervals_dict.get(day_num)

                if interval and interval.is_enabled and interval.is_valid_interval():
                    desired_tokens = interval.to_timekpr_format() or full_day_tokens
                else:
                    desired_tokens = full_day_tokens

                host_tokens_raw = current_config.get(f'ALLOWED_HOURS_{day_num}')
                host_tokens = None
                if isinstance(host_tokens_raw, list):
                    host_tokens = [str(t) for t in host_tokens_raw]
                elif isinstance(host_tokens_raw, str) and host_tokens_raw != '':
                    host_tokens = [host_tokens_raw]

                if host_tokens is not None and set(host_tokens) == set(desired_tokens):
                    skipped_count += 1
                    success_count += 1
                    continue

                success, detail = self._exec_checked(
                    f"timekpra --setallowedhours {username} {day_num} '{';'.join(desired_tokens)}'",
                    day_names[day_num],
                )
                if not success:
                    error_messages.append(detail)
                    continue
                success_count += 1

            if error_messages:
                return False, f"Configured {success_count}/7 days; failures: {'; '.join(error_messages)}"
            logger.info("Allowed hours for %s: %d/7 days sent, %d already matched host",
                        username, success_count - skipped_count, skipped_count)
            return True, f"Configured allowed hours for {username}: 7/7 days"

        except Exception as e:
            logger.error("Exception in set_allowed_hours: %s", e)
            return False, f"Connection error: {str(e)}"

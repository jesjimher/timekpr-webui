import paramiko
import re
import os
import logging

logger = logging.getLogger(__name__)


class SSHClient:
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
        """Run a command on the open connection; retry with sudo on non-zero exit if requested."""
        self.connect()
        stdin, stdout, stderr = self._client.exec_command(command)
        exit_status = stdout.channel.recv_exit_status()
        out = stdout.read().decode('utf-8')
        err = stderr.read().decode('utf-8')

        if exit_status != 0 and sudo_on_fail and not command.startswith('sudo '):
            logger.debug("Retrying with sudo: %s", command)
            return self._exec('sudo ' + command)

        return exit_status, out, err

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
        pattern = r'([A-Z_]+):\s*(.*)'
        for line in output.split('\n'):
            match = re.search(pattern, line)
            if match:
                key = match.group(1)
                value = match.group(2).strip()
                if value.isdigit():
                    value = int(value)
                elif ';' in value:
                    value = value.split(';')
                    if all(item.isdigit() for item in value):
                        value = [int(item) for item in value]
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
            exit_status, output, error = self._exec(
                f'timekpra --settimeleft {username} {operation} {seconds}',
                sudo_on_fail=True,
            )
            if exit_status == 0:
                return True, f"Successfully modified time for {username}: {operation}{seconds} seconds"
            return False, f"Error modifying time: {error}"
        except Exception as e:
            return False, f"Connection error: {str(e)}"

    def set_weekly_time_limits(self, username, schedule_dict):
        """
        Set daily time limits using timekpra --setalloweddays and --settimelimits.
        schedule_dict: day names → hour values
        Returns: (success, message)
        """
        try:
            day_order = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']

            allowed_days = [
                str(i + 1) for i, day in enumerate(day_order)
                if (schedule_dict.get(day, 0) or 0) > 0
            ]
            if not allowed_days:
                logger.warning("No days with time limits > 0 found")
                return False, "No days with time limits configured"

            exit_status, output, error = self._exec(
                f"timekpra --setalloweddays {username} '{';'.join(allowed_days)}'",
                sudo_on_fail=True,
            )
            if exit_status != 0:
                return False, f"Failed to set allowed days: {error or output}"

            time_limits = [
                str(int((schedule_dict.get(day, 0) or 0) * 3600))
                for day in day_order if (schedule_dict.get(day, 0) or 0) > 0
            ]
            if time_limits:
                exit_status, output, error = self._exec(
                    f"timekpra --settimelimits {username} '{';'.join(time_limits)}'",
                    sudo_on_fail=True,
                )
                if exit_status != 0:
                    return False, f"Failed to set time limits: {error or output}"

            logger.info("Set time limits for %s: days=%s limits=%s", username, allowed_days, time_limits)
            return True, f"Successfully configured time limits for {username}"

        except Exception as e:
            logger.error("Exception in set_weekly_time_limits: %s", e)
            return False, f"Connection error: {str(e)}"

    def set_allowed_hours(self, username, intervals_dict):
        """
        Set allowed hours using timekpra --setallowedhours.
        intervals_dict: day_of_week (1-7) → UserDailyTimeInterval
        Returns: (success, message)
        """
        try:
            day_names = ['', 'monday', 'tuesday', 'wednesday', 'thursday',
                         'friday', 'saturday', 'sunday']
            success_count = 0
            error_messages = []

            for day_num in range(1, 8):
                interval = intervals_dict.get(day_num)

                if interval and interval.is_enabled and interval.is_valid_interval():
                    hour_specs = interval.to_timekpr_format()
                    if hour_specs:
                        exit_status, output, error = self._exec(
                            f"timekpra --setallowedhours {username} {day_num} '{';'.join(hour_specs)}'",
                            sudo_on_fail=True,
                        )
                        if exit_status != 0:
                            error_messages.append(f"{day_names[day_num]}: {error or output}")
                            continue
                else:
                    full_day = ';'.join(str(h) for h in range(24))
                    exit_status, output, error = self._exec(
                        f"timekpra --setallowedhours {username} {day_num} '{full_day}'",
                        sudo_on_fail=True,
                    )
                    if exit_status != 0:
                        error_messages.append(
                            f"{day_names[day_num]}: Failed to set full day - {error or output}"
                        )
                        continue

                success_count += 1

            if success_count > 0 or not error_messages:
                return True, f"Configured allowed hours for {username}: {success_count}/7 days"
            return False, f"Failed to configure allowed hours: {'; '.join(error_messages)}"

        except Exception as e:
            logger.error("Exception in set_allowed_hours: %s", e)
            return False, f"Connection error: {str(e)}"

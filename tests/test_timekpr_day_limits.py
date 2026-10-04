"""SSHClient.set_day_limits: what actually gets sent to timekpra, with the SSH
layer replaced by a recorder."""
from src.models import DayLimit
from src.timekpr import SSHClient

FULL_DAY = list(range(24))


class RecordingClient(SSHClient):
    def __init__(self):
        super().__init__(hostname='unused', key_path='/nonexistent')
        self.commands = []

    def _exec_checked(self, command, description, sudo_on_fail=True):
        self.commands.append(command)
        return True, ''


def _schedule(limits_by_day):
    return [DayLimit(day_of_week=d, limit_seconds=limits_by_day[d], hours_enabled=False,
                     start_hour=9, start_minute=0, end_hour=17, end_minute=0)
            for d in range(1, 8)]


def _full_hours_config():
    return {f'ALLOWED_HOURS_{d}': FULL_DAY for d in range(1, 8)}


SATURDAY_BLOCKED = {d: (0 if d == 6 else 8100) for d in range(1, 8)}


def test_blocked_day_sent_as_zero_limit_not_omitted():
    """timekpr's --settimeleft indexes LIMITS_PER_WEEKDAYS by Mon..Sun
    position; leaving a blocked day out shortens the list and makes it fail
    (on Sundays, outright), so all 7 days must always be sent."""
    client = RecordingClient()
    config = _full_hours_config()
    config.update({'ALLOWED_WEEKDAYS': [1, 2, 3, 4, 5, 7], 'LIMITS_PER_WEEKDAYS': [8100] * 6})

    success, _ = client.set_day_limits('kid', _schedule(SATURDAY_BLOCKED), config)

    assert success
    assert client.commands == [
        "timekpra --setalloweddays kid '1;2;3;4;5;6;7'",
        "timekpra --settimelimits kid '8100;8100;8100;8100;8100;0;8100'",
    ]


def test_already_converged_host_gets_no_commands():
    client = RecordingClient()
    config = _full_hours_config()
    config.update({
        'ALLOWED_WEEKDAYS': [1, 2, 3, 4, 5, 6, 7],
        'LIMITS_PER_WEEKDAYS': [8100, 8100, 8100, 8100, 8100, 0, 8100],
    })

    success, _ = client.set_day_limits('kid', _schedule(SATURDAY_BLOCKED), config)

    assert success
    assert client.commands == []

"""The schedule grammar: when a job's scheduled runs happen.

A schedule is one line of text, kept as the job's ``triggers.schedule``: the days it runs
on, then the times it runs at, any of the one with any of the other.

    days:   daily
            weekly DAY[,DAY...]            DAY: mon tue wed thu fri sat sun
            monthly D                      D: 1 to 28
            monthly last
    times:  HH:MM[,HH:MM...]               at most 48 times
            every Nh [HH:MM-HH:MM]         N: 1 to 12

    daily 21:00     weekly mon,thu 08:00     daily 09:00,21:00     monthly last 22:00
    daily every 2h 08:00-23:00               weekly mon,tue,wed,thu,fri every 1h 09:00-18:00

Times are local wall-clock times from 00:00 to 23:59. ``every Nh`` runs at the window's
start and every N hours after it, its end included when the hours land on it; without a
window it runs from 00:00 through the day. Each running day starts the count again. A
window may cross midnight (``every 2h 22:00-06:00``) and then belongs to the day it opens
on: on a weekly or monthly schedule it runs into the morning of the day after.

The 29th to the 31st are left out of ``monthly D`` because Task Scheduler skips a month
that lacks the day chosen; ``monthly last`` serves the end of every month. A list holds at
most 48 times because a Task Scheduler task holds at most 48 triggers (measured on the
development machine: 48 validate, 49 are refused) and each time is a trigger of the job's
one schedule task.

``parse_schedule`` accepts any case, any spacing between the words — none inside a list of
days or times or a window, so ``08:00 - 23:00`` is refused — and any order and repetition
of days and times; ``Schedule.text`` is the one canonical form a stored schedule takes (lower
case, days in week order, ``daily`` for all seven, times in order of the day and each
once, hours and minutes in two digits), so two schedules that mean the same thing are
stored the same way.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, List, Optional, Tuple, Union

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
EVERY_DAY = tuple(range(7))

DAILY, WEEKLY, MONTHLY = "daily", "weekly", "monthly"
LAST = "last"
"""``Schedule.month_day`` of ``monthly last``."""
MONTH_DAYS = range(1, 29)
EVERY_HOURS = range(1, 13)
MAX_TIMES = 48

DAY_MINUTES = 24 * 60

_TIME = re.compile(r"^(\d{1,2}):(\d{2})$")
_EVERY = re.compile(r"^(\d{1,2})h$")
_MONTH_DAY = re.compile(r"^\d{1,2}$")

EXPECTED = ("expected the days ('daily', 'weekly DAY[,DAY...]', 'monthly D' or 'monthly last') and then the "
            "times ('HH:MM[,HH:MM...]' or 'every Nh' with an optional 'HH:MM-HH:MM')")

MonthDay = Union[int, str, None]


class ScheduleError(ValueError):
    """Text that is not a schedule."""


def clock(minute: int) -> str:
    """Minutes after midnight as ``HH:MM`` (past 1440 read as the next day's)."""
    return f"{minute // 60 % 24:02d}:{minute % 60:02d}"


@dataclass(frozen=True)
class Window:
    """The part of a day an ``every Nh`` schedule runs in, as minutes after midnight;
    ``end`` before ``start`` crosses midnight."""

    start: int
    end: int

    def __post_init__(self):
        for minute in (self.start, self.end):
            if isinstance(minute, bool) or not isinstance(minute, int) or not 0 <= minute < DAY_MINUTES:
                raise ScheduleError(f"not a time of day: {minute!r} minutes")
        if self.start == self.end:
            raise ScheduleError(f"a window needs different start and end times: {self.text()!r}")

    @property
    def length(self) -> int:
        return (self.end - self.start) % DAY_MINUTES

    def text(self) -> str:
        return f"{clock(self.start)}-{clock(self.end)}"


@dataclass(frozen=True)
class Schedule:
    """Runs on each of ``weekdays`` (0 Monday ... 6 Sunday; every day when None), or on
    ``month_day`` of every month (1 to 28, or ``LAST``) when that is set, ``weekdays`` then
    None or empty; at each of ``times`` (minutes after midnight), or every ``every`` hours
    inside ``window`` (the whole day when None)."""

    weekdays: Optional[Tuple[int, ...]] = None
    month_day: MonthDay = None
    times: Tuple[int, ...] = ()
    every: Optional[int] = None
    window: Optional[Window] = None

    def __post_init__(self):
        if self.weekdays is None:
            weekdays = EVERY_DAY if self.month_day is None else ()
        else:
            weekdays = tuple(sorted(set(self.weekdays)))
        if any(d not in EVERY_DAY for d in weekdays):
            raise ScheduleError(f"not days of the week: {self.weekdays!r}")
        if self.month_day is None:
            if not weekdays:
                raise ScheduleError("a schedule needs at least one day of the week")
        else:
            if weekdays:
                raise ScheduleError("a monthly schedule names no days of the week")
            if self.month_day != LAST and (isinstance(self.month_day, bool) or self.month_day not in MONTH_DAYS):
                raise ScheduleError(f"a day of the month is 1 to 28, or last: {self.month_day!r}")
        times = tuple(sorted(set(self.times)))
        if any(isinstance(t, bool) or not isinstance(t, int) or not 0 <= t < DAY_MINUTES for t in times):
            raise ScheduleError(f"not times of day: {self.times!r}")
        if self.every is None:
            if not times:
                raise ScheduleError("a schedule needs at least one time of day")
            if len(times) > MAX_TIMES:
                raise ScheduleError(f"a schedule holds at most {MAX_TIMES} times a day; for more, run every N hours")
            if self.window is not None:
                raise ScheduleError("a window belongs to 'every Nh'")
        else:
            if times:
                raise ScheduleError("a schedule runs at times of day or every N hours, not both")
            if isinstance(self.every, bool) or self.every not in EVERY_HOURS:
                raise ScheduleError(f"every N hours takes N from 1 to 12: {self.every!r}")
            if self.window is not None and not isinstance(self.window, Window):
                raise ScheduleError(f"not a window: {self.window!r}")
        object.__setattr__(self, "weekdays", weekdays)
        object.__setattr__(self, "times", times)

    # --- the days ----------------------------------------------------------------------

    @property
    def kind(self) -> str:
        if self.month_day is not None:
            return MONTHLY
        return DAILY if self.weekdays == EVERY_DAY else WEEKLY

    @property
    def is_daily(self) -> bool:
        return self.kind == DAILY

    def runs_on(self, day: date) -> bool:
        """Whether the schedule's runs, or its window, start on ``day``."""
        if self.month_day is None:
            return day.weekday() in self.weekdays
        if self.month_day == LAST:
            return day.day == calendar.monthrange(day.year, day.month)[1]
        return day.day == self.month_day

    # --- the times ---------------------------------------------------------------------

    @property
    def first_minute(self) -> int:
        """When a running day's first run falls, in minutes after its midnight."""
        if self.every is None:
            return self.times[0]
        return self.window.start if self.window is not None else 0

    @property
    def span(self) -> int:
        """Minutes from an ``every Nh`` day's first run to its last (0 for one run)."""
        length = self.window.length if self.window is not None else DAY_MINUTES - 1
        step = self.every * 60
        return length // step * step

    def day_minutes(self) -> Tuple[int, ...]:
        """When a running day's runs fall, in minutes after its midnight; past 1440 for a
        window crossing midnight."""
        if self.every is None:
            return self.times
        return tuple(range(self.first_minute, self.first_minute + self.span + 1, self.every * 60))

    # --- text --------------------------------------------------------------------------

    def days_text(self) -> str:
        if self.kind == MONTHLY:
            return f"monthly {self.month_day}"
        if self.kind == DAILY:
            return "daily"
        return f"weekly {','.join(DAYS[d] for d in self.weekdays)}"

    def times_text(self) -> str:
        if self.every is None:
            return ",".join(clock(t) for t in self.times)
        every = f"every {self.every}h"
        return f"{every} {self.window.text()}" if self.window is not None else every

    def text(self) -> str:
        return f"{self.days_text()} {self.times_text()}"

    def describe(self) -> str:
        if self.kind == MONTHLY:
            days = "Monthly on the last day" if self.month_day == LAST else f"Monthly on day {self.month_day}"
        elif self.kind == DAILY:
            days = "Daily"
        else:
            days = f"Weekly on {', '.join(DAY_NAMES[d] for d in self.weekdays)}"
        if self.every is None:
            clocks = [clock(t) for t in self.times]
            at = clocks[0] if len(clocks) == 1 else f"{', '.join(clocks[:-1])} and {clocks[-1]}"
            return f"{days} at {at}"
        hours = "every hour" if self.every == 1 else f"every {self.every} hours"
        if self.window is None:
            return f"{days} {hours}"
        return f"{days} {hours} from {clock(self.window.start)} to {clock(self.window.end)}"

    # --- occurrences -------------------------------------------------------------------

    def _running_days(self, moment: datetime) -> Iterable[date]:
        """The running days whose runs may fall after ``moment``, in order: from the day
        before (a window crossing midnight) to past the longest gap, a month."""
        first = moment.date() - timedelta(days=1)
        for offset in range(64):
            day = first + timedelta(days=offset)
            if self.runs_on(day):
                yield day

    def next_at(self, minute: int, moment: datetime) -> datetime:
        """The first run at ``minute`` after a running day's midnight strictly after
        ``moment`` (both naive local times)."""
        for day in self._running_days(moment):
            candidate = datetime.combine(day, datetime.min.time()) + timedelta(minutes=minute)
            if candidate > moment:
                return candidate
        raise AssertionError("a schedule with a day always has a next time")

    def next_after(self, moment: datetime) -> datetime:
        """The first scheduled run strictly after ``moment`` (both naive local times)."""
        return min(self.next_at(minute, moment) for minute in self.day_minutes())

    def day_under_way(self, moment: datetime) -> Optional[Tuple[datetime, datetime]]:
        """For ``every Nh``: the first and last run left after ``moment`` of a running day
        whose first run came at or before it, so a day under way keeps its remaining runs;
        None when no day is under way or none of its runs is left."""
        if self.every is None:
            return None
        for day in self._running_days(moment):
            midnight = datetime.combine(day, datetime.min.time())
            if midnight + timedelta(minutes=self.first_minute) > moment:
                break
            left: List[datetime] = [run for run in (midnight + timedelta(minutes=m) for m in self.day_minutes())
                                    if run > moment]
            if left:
                return left[0], left[-1]
        return None


def parse_time(text: str) -> Tuple[int, int]:
    """``HH:MM`` as (hour, minute). Raises ScheduleError."""
    match = _TIME.match(text.strip())
    if not match:
        raise ScheduleError(f"not a time such as 21:00: {text.strip()!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ScheduleError(f"not a time of day: {text.strip()!r}")
    return hour, minute


def parse_minute(text: str) -> int:
    """``HH:MM`` as minutes after midnight. Raises ScheduleError."""
    hour, minute = parse_time(text)
    return hour * 60 + minute


def parse_times(text: str) -> Tuple[int, ...]:
    """``HH:MM[,HH:MM...]`` as minutes after midnight, empty pieces passed over. Raises
    ScheduleError."""
    pieces = [p.strip() for p in text.split(",") if p.strip()]
    if not pieces:
        raise ScheduleError(f"not times such as 09:00,21:00: {text.strip()!r}")
    return tuple(parse_minute(p) for p in pieces)


def parse_window(text: str) -> Window:
    """``HH:MM-HH:MM``. Raises ScheduleError."""
    start, dash, end = text.partition("-")
    if not dash:
        raise ScheduleError(f"not a window such as 08:00-23:00: {text.strip()!r}")
    return Window(parse_minute(start), parse_minute(end))


def _parse_days(words: List[str]) -> Tuple[Tuple[int, ...], MonthDay, List[str]]:
    """The days at the front of ``words``, and the words after them."""
    if words and words[0] == DAILY:
        return EVERY_DAY, None, words[1:]
    if len(words) >= 2 and words[0] == WEEKLY:
        names = [n.strip() for n in words[1].split(",") if n.strip()]
        unknown = [n for n in names if n not in DAYS]
        if unknown or not names:
            raise ScheduleError(f"days are written mon, tue, wed, thu, fri, sat, sun: {words[1]!r}")
        return tuple(DAYS.index(n) for n in names), None, words[2:]
    if len(words) >= 2 and words[0] == MONTHLY:
        if words[1] == LAST:
            return (), LAST, words[2:]
        if not _MONTH_DAY.match(words[1]) or int(words[1]) not in MONTH_DAYS:
            raise ScheduleError(f"a day of the month is 1 to 28, or last: {words[1]!r}")
        return (), int(words[1]), words[2:]
    raise ScheduleError(EXPECTED)


def parse_schedule(text: str) -> Schedule:
    """A schedule from its text. Raises ScheduleError."""
    if not isinstance(text, str):
        raise ScheduleError(f"not a schedule: {text!r}")
    weekdays, month_day, rest = _parse_days(text.strip().lower().split())
    if len(rest) in (2, 3) and rest[0] == "every":
        match = _EVERY.match(rest[1])
        if not match:
            raise ScheduleError(f"every N hours is written like 'every 2h': {rest[1]!r}")
        window = parse_window(rest[2]) if len(rest) == 3 else None
        return Schedule(weekdays, month_day, every=int(match.group(1)), window=window)
    if len(rest) == 1:
        return Schedule(weekdays, month_day, times=parse_times(rest[0]))
    raise ScheduleError(f"{EXPECTED}, got {text.strip()!r}")

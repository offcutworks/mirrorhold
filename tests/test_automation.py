"""Automation and notifications, without Task Scheduler and without the desktop.

The schedule grammar; the tasks a configuration calls for and their Task Scheduler
definitions; reconciliation against a scheduler kept in memory, and the rule that only
an installed copy serving the store gets tasks; which drives arrived and what an arrival
runs; the scheduler's entry (``automatic``) with a runner, a boot time and settling of
its own; what a run's notice says and how it is handed to Windows; the command line's
``when``, ``reconcile`` and ``installation``. The last class runs an arrival through the
engine on temporary folders mapped as volumes.

The real scheduler, a real arrival and a real notification were checked live on the
development machine; nothing here reaches them."""
import ctypes
import os
import subprocess
import time
import unittest
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest import mock

from support import TempDirTest, endpoint, volume
from test_engine_contract import INSTALLED

from mirrorhold import cli
from mirrorhold.automation.arrival import (PresenceState, arrival_selection, arrivals, destination_key,
                                           present_keys, read_state, settled_volumes, source_key, state_path,
                                           write_state)
from mirrorhold.automation.automatic import run_on_arrival, run_scheduled
from mirrorhold.automation.reconcile import AutomationState, apply_automation, reconcile, remove_all
from mirrorhold.automation.scheduler import RegisteredTask, SchedulerError, WindowsScheduler
from mirrorhold.automation.tasks import (ARRIVAL, SCHEDULE, Launcher, desired_tasks, fingerprint_of, task_name,
                                         task_xml)
from mirrorhold.contract_errors import ContractError
from mirrorhold.endpoints.volumes import FixedVolumeSource, VolumeScan
from mirrorhold.installation.manifest import InstalledCopy, write_installation
from mirrorhold.jobs.model import Job, Notifications, Triggers, new_job
from mirrorhold.jobs.schedule import LAST, Schedule, ScheduleError, Window, parse_schedule
from mirrorhold.jobs.store import ConfigStore, StoreContents
from mirrorhold.locations import runtime_layout
from mirrorhold.notifications import announce
from mirrorhold.notifications.message import Notice, failure_notice, notice_for
from mirrorhold.notifications import windows_runtime
from mirrorhold.notifications.toast import (POWERSHELL_APP_ID, SWITCH_VARIABLE, NotificationError, SilentNotifier,
                                            ToastNotifier, default_notifier, toast_xml)
from mirrorhold.installation.shortcut import APP_ID
from mirrorhold.records.record import (DONE_KEYS, LEFT_KEYS, PLANNED_KEYS, Outcome, RunRecord, read_records,
                                       write_record)
from mirrorhold.run.runner import JobRunResult, RunError, RunOptions, preview_job, run_job

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
needs_engine = unittest.skipUnless(INSTALLED, "the pinned engine is not installed")
BOOT = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


class MemoryScheduler:
    """Task Scheduler's folder, in memory: what reconciliation may see and change."""

    def __init__(self, *, fail: bool = False):
        self.folder = "\\MirrorHold"
        self.xml = {}
        self.calls = []
        self.folder_removed = False
        self.fail = fail

    def _check(self, call):
        self.calls.append(call)
        if self.fail:
            raise SchedulerError("Task Scheduler cannot be reached: the service is stopped")

    def tasks(self):
        self._check("tasks")
        out = []
        for name, xml in self.xml.items():
            node = ET.fromstring(xml.split("?>", 1)[1]).find("t:RegistrationInfo/t:Description", NS)
            out.append(RegisteredTask(name, node.text if node is not None else ""))
        return out

    def register(self, name, xml):
        self._check(("register", name))
        self.xml[name] = xml

    def delete(self, name):
        self._check(("delete", name))
        self.xml.pop(name, None)

    def remove_folder(self):
        self._check("remove_folder")
        self.folder_removed = not self.xml


class Untouchable:
    """A scheduler no call may reach."""
    folder = "\\MirrorHold"

    def __getattr__(self, name):
        raise AssertionError(f"the scheduler was used ({name}) where nothing may touch it")


def parse(xml: str) -> ET.Element:
    return ET.fromstring(xml.split("?>", 1)[1])


def a_job(name="Photos", **settings) -> Job:
    return new_job(name, endpoint(volume(1, "S"), "Photos"), [endpoint(volume(2, "D"), "Backup"),
                                                              endpoint(volume(3, "E"), "Backup")], **settings)


def launcher(tmp=Path("X:\\app")) -> Launcher:
    return Launcher(Path("X:\\env\\pythonw.exe"), Path(tmp) / "installed",
                    Path("X:\\MirrorHold source\\Config\\mirrorhold.json"), "PC\\someone")


def a_record(job, index, outcome, *, reasons=(), copied=0, versions=0, trigger="arrival", detail=""):
    done = dict.fromkeys(DONE_KEYS, 0)
    done.update(files_copied=copied, bytes_copied=copied * 1024 * 1024, moved_to_versions=versions)
    return RunRecord("r" * 32, job.job_id, job.name, job.destinations[index - 1].marker_id, index, trigger,
                     "2026-10-06T12:00:00Z", "2026-10-06T12:30:00Z", outcome, tuple(reasons), detail, "v1.75.1",
                     dict.fromkeys(PLANNED_KEYS, 0), done, dict.fromkeys(LEFT_KEYS, 0))


class ScheduleGrammarTest(unittest.TestCase):
    def test_canonical_forms(self):
        self.assertEqual(parse_schedule("Daily 9:05").text(), "daily 09:05")
        self.assertEqual(parse_schedule("  weekly  thu,MON,mon 21:00 ").text(), "weekly mon,thu 21:00")
        self.assertEqual(parse_schedule("weekly mon,tue,wed,thu,fri,sat,sun 06:30").text(), "daily 06:30")
        self.assertEqual(parse_schedule("weekly sat,sun 10:00").describe(), "Weekly on Sat, Sun at 10:00")
        self.assertEqual(parse_schedule("daily 21:00").describe(), "Daily at 21:00")

    def test_what_is_not_a_schedule(self):
        for text in ("", "daily", "daily 24:00", "daily 21:60", "daily 7pm", "weekly 21:00", "weekly xyz 21:00",
                     "hourly 10:00", "weekly mon thu 21:00"):
            with self.assertRaises(ScheduleError, msg=text):
                parse_schedule(text)

    def test_the_next_time(self):
        daily = parse_schedule("daily 21:00")
        self.assertEqual(daily.next_after(datetime(2026, 10, 6, 20, 59)), datetime(2026, 10, 6, 21, 0))
        self.assertEqual(daily.next_after(datetime(2026, 10, 6, 21, 0)), datetime(2026, 10, 7, 21, 0))
        weekly = parse_schedule("weekly mon,thu 08:00")        # 2026-10-06 is a Tuesday
        self.assertEqual(weekly.next_after(datetime(2026, 10, 6, 12, 0)), datetime(2026, 10, 8, 8, 0))
        self.assertEqual(weekly.next_after(datetime(2026, 10, 8, 8, 0)), datetime(2026, 10, 12, 8, 0))

    def test_triggers_keep_the_canonical_text_and_refuse_at_the_field(self):
        self.assertEqual(Triggers(False, "Weekly FRI 7:00").schedule, "weekly fri 07:00")
        self.assertFalse(Triggers().automatic)
        self.assertTrue(Triggers(True).automatic and Triggers(False, "daily 01:00").automatic)
        with self.assertRaises(ContractError) as caught:
            Job.from_document(a_job().to_document() | {"triggers": {"on_arrival": False, "schedule": "at noon"}})
        self.assertIn("triggers.schedule", str(caught.exception))

    def test_a_new_job_runs_only_when_started(self):
        job = a_job()
        self.assertEqual((job.triggers, job.enabled), (Triggers(False, None), True))
        self.assertEqual(desired_tasks([job], launcher()), {})


class ScheduleFormsTest(unittest.TestCase):
    """The days — daily, weekly, monthly — combined with the times — a list, or every N
    hours in an optional window that may cross midnight."""

    def test_canonical_forms(self):
        cases = {"daily 21:00,9:00,09:00": "daily 09:00,21:00",
                 " Monthly  LAST 22:00 ": "monthly last 22:00",
                 "monthly 07 6:05": "monthly 7 06:05",
                 "daily every 2H 8:00-23:00": "daily every 2h 08:00-23:00",
                 "weekly fri,mon,wed,tue,thu every 1h 09:00-18:00": "weekly mon,tue,wed,thu,fri every 1h 09:00-18:00",
                 "weekly sun,sat,mon,tue,wed,thu,fri every 3h": "daily every 3h",
                 "monthly 28 every 12h 22:00-06:00": "monthly 28 every 12h 22:00-06:00"}
        for text, canonical in cases.items():
            self.assertEqual(parse_schedule(text).text(), canonical, text)
            self.assertEqual(parse_schedule(canonical).text(), canonical, canonical)

    def test_what_is_not_a_schedule(self):
        for text in ("monthly 29 22:00", "monthly 0 22:00", "monthly 31 22:00", "monthly first 22:00",
                     "monthly mon 22:00", "monthly 22:00", "daily every 13h", "daily every 0h", "daily every 2",
                     "daily every 2h 08:00", "daily every 2h 08:00-08:00", "daily every 2h 08:00-24:00",
                     "daily every 2h 08:00-23:00 extra", "daily every", "daily 09:00 21:00", "daily ,",
                     "daily 09:00,9pm", "weekly every 2h", "daily " + ",".join(f"{h:02d}:{m:02d}" for h in range(24)
                                                                                 for m in (0, 20, 40))[:-6]):
            with self.assertRaises(ScheduleError, msg=text):
                parse_schedule(text)

    def test_a_list_holds_at_most_48_times(self):
        half_hours = ",".join(f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30))
        self.assertEqual(len(parse_schedule(f"daily {half_hours}").times), 48)
        with self.assertRaises(ScheduleError) as caught:
            parse_schedule(f"daily {half_hours},00:15")
        self.assertIn("every N hours", str(caught.exception))
        self.assertEqual(len(parse_schedule(f"daily {half_hours},00:30").times), 48)    # a repeat is one time

    def test_described_in_words(self):
        cases = {"daily 09:00,21:00": "Daily at 09:00 and 21:00",
                 "weekly sat 08:00,12:00,20:00": "Weekly on Sat at 08:00, 12:00 and 20:00",
                 "monthly 15 22:00": "Monthly on day 15 at 22:00",
                 "monthly last 22:00": "Monthly on the last day at 22:00",
                 "daily every 2h 08:00-23:00": "Daily every 2 hours from 08:00 to 23:00",
                 "weekly mon,fri every 1h": "Weekly on Mon, Fri every hour"}
        for text, words in cases.items():
            self.assertEqual(parse_schedule(text).describe(), words)

    def test_the_runs_of_every_n_hours(self):
        def minutes(text):
            return [f"{m // 60:02d}:{m % 60:02d}" for m in parse_schedule(text).day_minutes()]
        self.assertEqual(minutes("daily every 2h 08:00-23:00"),
                         ["08:00", "10:00", "12:00", "14:00", "16:00", "18:00", "20:00", "22:00"])
        self.assertEqual(minutes("daily every 1h 09:00-12:00"), ["09:00", "10:00", "11:00", "12:00"])  # end included
        self.assertEqual(minutes("daily every 5h"), ["00:00", "05:00", "10:00", "15:00", "20:00"])
        self.assertEqual(minutes("daily every 12h"), ["00:00", "12:00"])
        self.assertEqual(minutes("daily every 3h 22:00-06:00"), ["22:00", "25:00", "28:00"])          # past midnight
        self.assertEqual(minutes("daily every 6h 09:00-12:00"), ["09:00"])

    def test_the_next_time(self):
        # 2026-10-06 is a Tuesday; 2026-10-31 a Saturday, the last day of its month.
        twice = parse_schedule("daily 09:00,21:00")
        self.assertEqual(twice.next_after(datetime(2026, 10, 6, 9, 0)), datetime(2026, 10, 6, 21, 0))
        self.assertEqual(twice.next_after(datetime(2026, 10, 6, 21, 0)), datetime(2026, 10, 7, 9, 0))
        last = parse_schedule("monthly last 22:00")
        self.assertEqual(last.next_after(datetime(2026, 10, 6, 12, 0)), datetime(2026, 10, 31, 22, 0))
        self.assertEqual(last.next_after(datetime(2026, 10, 31, 22, 0)), datetime(2026, 11, 30, 22, 0))
        self.assertEqual(last.next_after(datetime(2028, 2, 1, 0, 0)), datetime(2028, 2, 29, 22, 0))
        fifteenth = parse_schedule("monthly 15 06:30")
        self.assertEqual(fifteenth.next_after(datetime(2026, 10, 15, 6, 30)), datetime(2026, 11, 15, 6, 30))
        hourly = parse_schedule("weekly mon,fri every 1h 09:00-18:00")
        self.assertEqual(hourly.next_after(datetime(2026, 10, 6, 12, 0)), datetime(2026, 10, 9, 9, 0))
        self.assertEqual(hourly.next_after(datetime(2026, 10, 9, 17, 30)), datetime(2026, 10, 9, 18, 0))
        self.assertEqual(hourly.next_after(datetime(2026, 10, 9, 18, 0)), datetime(2026, 10, 12, 9, 0))
        night = parse_schedule("weekly sat every 2h 22:00-06:00")       # Saturday's window runs into Sunday
        self.assertEqual(night.next_after(datetime(2026, 11, 1, 3, 0)), datetime(2026, 11, 1, 4, 0))
        self.assertEqual(night.next_after(datetime(2026, 11, 1, 6, 0)), datetime(2026, 11, 7, 22, 0))

    def test_a_day_under_way_keeps_its_remaining_runs(self):
        hourly = parse_schedule("daily every 2h 08:00-23:00")
        self.assertEqual(hourly.day_under_way(datetime(2026, 10, 6, 10, 7)),
                         (datetime(2026, 10, 6, 12, 0), datetime(2026, 10, 6, 22, 0)))
        self.assertIsNone(hourly.day_under_way(datetime(2026, 10, 6, 7, 0)))     # not opened yet
        self.assertIsNone(hourly.day_under_way(datetime(2026, 10, 6, 22, 0)))    # its last run already come
        night = parse_schedule("monthly last every 2h 22:00-06:00")
        self.assertEqual(night.day_under_way(datetime(2026, 11, 1, 1, 0)),
                         (datetime(2026, 11, 1, 2, 0), datetime(2026, 11, 1, 6, 0)))
        self.assertIsNone(night.day_under_way(datetime(2026, 11, 2, 1, 0)))
        self.assertIsNone(parse_schedule("weekly mon every 2h").day_under_way(datetime(2026, 10, 6, 12, 0)))
        self.assertIsNone(parse_schedule("daily 09:00,21:00").day_under_way(datetime(2026, 10, 6, 12, 0)))

    def test_built_from_its_parts(self):
        self.assertEqual(Schedule(month_day=LAST, times=(22 * 60,)).text(), "monthly last 22:00")
        self.assertEqual(Schedule(times=(9 * 60,)).text(), "daily 09:00")
        self.assertEqual(Schedule(every=2, window=Window(8 * 60, 23 * 60)).text(), "daily every 2h 08:00-23:00")
        for parts in ({"every": 2, "window": (480, 1380)}, {"weekdays": (0,), "month_day": 3, "times": (60,)},
                      {"times": (60,), "every": 2}, {"times": ()}, {"times": (60,), "window": Window(0, 60)}):
            with self.assertRaises(ScheduleError, msg=parts):
                Schedule(**parts)

    def test_the_command_line_takes_every_form(self):
        self.assertEqual(Triggers(False, "Monthly Last 22:00").schedule, "monthly last 22:00")
        with self.assertRaises(ContractError) as caught:
            Triggers(False, "monthly 30 22:00")
        self.assertIn("1 to 28", str(caught.exception))


class ScheduleFormTaskTest(unittest.TestCase):
    """The schedule task of each form: one calendar trigger per time listed, or one
    repeating across its window, and a one-off trigger for a day under way."""

    def task(self, text, now):
        job = a_job("Job", triggers=Triggers(False, text))
        return parse(task_xml(desired_tasks([job], launcher())[task_name(job.job_id, SCHEDULE)], now))

    def test_several_times_a_day_are_one_trigger_each(self):
        task = self.task("daily 09:00,13:00,21:00", datetime(2026, 10, 6, 12, 0))
        starts = [c.find("t:StartBoundary", NS).text for c in task.findall("t:Triggers/t:CalendarTrigger", NS)]
        self.assertEqual(starts, ["2026-10-07T09:00:00", "2026-10-06T13:00:00", "2026-10-06T21:00:00"])
        self.assertIsNone(task.find("t:Triggers/t:CalendarTrigger/t:Repetition", NS))

    def test_monthly_names_its_day_and_every_month(self):
        task = self.task("monthly last 22:00", datetime(2026, 10, 6, 12, 0))
        calendar = task.find("t:Triggers/t:CalendarTrigger", NS)
        self.assertEqual(calendar.find("t:StartBoundary", NS).text, "2026-10-31T22:00:00")
        self.assertEqual(calendar.find("t:ScheduleByMonth/t:DaysOfMonth/t:Day", NS).text, "Last")
        self.assertEqual(len(calendar.find("t:ScheduleByMonth/t:Months", NS)), 12)
        task = self.task("monthly 15 06:30", datetime(2026, 10, 6, 12, 0))
        self.assertEqual(task.find("t:Triggers/t:CalendarTrigger/t:ScheduleByMonth/t:DaysOfMonth/t:Day", NS).text,
                         "15")

    def test_every_n_hours_repeats_across_its_window_and_reaches_its_last_run(self):
        task = self.task("weekly mon,fri every 2h 08:00-23:00", datetime(2026, 10, 6, 12, 0))
        calendar = task.find("t:Triggers/t:CalendarTrigger", NS)
        self.assertEqual(calendar.find("t:StartBoundary", NS).text, "2026-10-09T08:00:00")
        self.assertEqual((calendar.find("t:Repetition/t:Interval", NS).text,
                          calendar.find("t:Repetition/t:Duration", NS).text), ("PT2H", f"PT{14 * 60 + 1}M"))
        days = [child.tag.split("}")[1] for child in calendar.find("t:ScheduleByWeek/t:DaysOfWeek", NS)]
        self.assertEqual(days, ["Monday", "Friday"])
        self.assertIsNone(task.find("t:Triggers/t:TimeTrigger", NS))       # Tuesday: no day under way
        one = self.task("daily every 6h 09:00-12:00", datetime(2026, 10, 6, 12, 0))
        self.assertIsNone(one.find("t:Triggers/t:CalendarTrigger/t:Repetition", NS))

    def test_a_day_under_way_gets_a_one_off_trigger_for_its_remaining_runs(self):
        task = self.task("daily every 2h 08:00-23:00", datetime(2026, 10, 6, 10, 7))
        once = task.find("t:Triggers/t:TimeTrigger", NS)
        self.assertEqual(once.find("t:StartBoundary", NS).text, "2026-10-06T12:00:00")
        self.assertEqual(once.find("t:Repetition/t:Duration", NS).text, f"PT{10 * 60 + 1}M")
        self.assertEqual(task.find("t:Triggers/t:CalendarTrigger/t:StartBoundary", NS).text, "2026-10-07T08:00:00")
        late = self.task("daily every 2h 08:00-23:00", datetime(2026, 10, 6, 21, 0))
        self.assertIsNone(late.find("t:Triggers/t:TimeTrigger/t:Repetition", NS))   # only 22:00 left

    def test_the_daily_and_weekly_forms_keep_their_definition(self):
        """A schedule written before the new forms keeps its text, and so its fingerprint:
        its task is not registered again, which keeps its missed-run catch-up."""
        job = a_job(triggers=Triggers(True, "weekly mon,thu 08:00"))
        spec = desired_tasks([job], launcher())[task_name(job.job_id, SCHEDULE)]
        self.assertEqual(spec.schedule, "weekly mon,thu 08:00")
        task = parse(task_xml(spec, datetime(2026, 10, 6, 12, 0)))
        self.assertEqual(len(task.findall("t:Triggers/*", NS)), 1)


class TaskDefinitionTest(unittest.TestCase):
    def test_the_tasks_a_configuration_calls_for(self):
        manual = a_job("Manual")
        arrival = a_job("Arrival", triggers=Triggers(True))
        both = a_job("Both", triggers=Triggers(True, "daily 21:00"))
        paused = a_job("Paused", triggers=Triggers(True, "daily 21:00"), enabled=False)
        wanted = desired_tasks([manual, arrival, both, paused], launcher())
        self.assertEqual(set(wanted), {task_name(arrival.job_id, ARRIVAL), task_name(both.job_id, ARRIVAL),
                                       task_name(both.job_id, SCHEDULE)})

    def test_an_arrival_task(self):
        job = a_job('Work & "home"', triggers=Triggers(True))
        spec = desired_tasks([job], launcher())[task_name(job.job_id, ARRIVAL)]
        task = parse(task_xml(spec, datetime(2026, 10, 6, 12, 0)))
        subscription = task.find("t:Triggers/t:EventTrigger/t:Subscription", NS).text
        query = ET.fromstring(subscription).find("Query/Select").text
        self.assertIn("Microsoft-Windows-Kernel-PnP/Configuration", subscription)
        self.assertIn("EventID=410 or EventID=420", query)
        self.assertIn("{71a27cdd-812a-11d0-bec7-08002be2092f}", query)
        self.assertEqual(task.find("t:Triggers/t:EventTrigger/t:Delay", NS).text, "PT10S")
        self.assertEqual(task.find("t:Triggers/t:LogonTrigger/t:UserId", NS).text, "PC\\someone")
        self.assertIsNone(task.find("t:Triggers/t:CalendarTrigger", NS))
        principal = task.find("t:Principals/t:Principal", NS)
        self.assertEqual((principal.find("t:UserId", NS).text, principal.find("t:LogonType", NS).text),
                         ("PC\\someone", "InteractiveToken"))
        settings = task.find("t:Settings", NS)
        self.assertEqual({k: settings.find(f"t:{k}", NS).text for k in
                          ("MultipleInstancesPolicy", "StartWhenAvailable", "ExecutionTimeLimit",
                           "DisallowStartIfOnBatteries")},
                         {"MultipleInstancesPolicy": "IgnoreNew", "StartWhenAvailable": "true",
                          "ExecutionTimeLimit": "PT0S", "DisallowStartIfOnBatteries": "false"})
        action = task.find("t:Actions/t:Exec", NS)
        self.assertEqual(action.find("t:Command", NS).text, "X:\\env\\pythonw.exe")
        self.assertEqual(action.find("t:Arguments", NS).text,
                         f'-m mirrorhold --store "X:\\MirrorHold source\\Config\\mirrorhold.json" automatic '
                         f'{job.job_id} --by arrival')
        self.assertEqual(action.find("t:WorkingDirectory", NS).text, "X:\\app\\installed")
        description = task.find("t:RegistrationInfo/t:Description", NS).text
        self.assertIn('"Work & "home""', description)
        self.assertEqual(fingerprint_of(description), spec.fingerprint)

    def test_a_schedule_task_starts_at_its_next_time(self):
        daily = a_job("Daily", triggers=Triggers(False, "daily 21:00"))
        weekly = a_job("Weekly", triggers=Triggers(False, "weekly mon,thu 08:00"))
        wanted = desired_tasks([daily, weekly], launcher())
        task = parse(task_xml(wanted[task_name(daily.job_id, SCHEDULE)], datetime(2026, 10, 6, 21, 30)))
        calendar = task.find("t:Triggers/t:CalendarTrigger", NS)
        self.assertEqual(calendar.find("t:StartBoundary", NS).text, "2026-10-07T21:00:00")
        self.assertEqual(calendar.find("t:ScheduleByDay/t:DaysInterval", NS).text, "1")
        self.assertIsNone(task.find("t:Triggers/t:EventTrigger", NS))
        task = parse(task_xml(wanted[task_name(weekly.job_id, SCHEDULE)], datetime(2026, 10, 6, 12, 0)))
        calendar = task.find("t:Triggers/t:CalendarTrigger", NS)
        self.assertEqual(calendar.find("t:StartBoundary", NS).text, "2026-10-08T08:00:00")
        days = [child.tag.split("}")[1] for child in calendar.find("t:ScheduleByWeek/t:DaysOfWeek", NS)]
        self.assertEqual(days, ["Monday", "Thursday"])
        self.assertIn("automatic", task.find("t:Actions/t:Exec/t:Arguments", NS).text.split())
        self.assertTrue(task.find("t:Actions/t:Exec/t:Arguments", NS).text.endswith("--by schedule"))

    def test_the_fingerprint_follows_what_the_definition_depends_on(self):
        job = a_job(triggers=Triggers(True, "daily 21:00"))
        name = task_name(job.job_id, SCHEDULE)
        base = desired_tasks([job], launcher())[name].fingerprint
        self.assertEqual(desired_tasks([job], launcher())[name].fingerprint, base)
        for changed in (replace(job, triggers=Triggers(True, "daily 22:00")), replace(job, name="Renamed")):
            self.assertNotEqual(desired_tasks([changed], launcher())[name].fingerprint, base)
        self.assertNotEqual(desired_tasks([job], launcher(Path("Y:\\other")))[name].fingerprint, base)


class ReconcileTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.store = self.tmp / "state" / "store.json"
        self.root = self.tmp / "installed"
        self.job = a_job(triggers=Triggers(True, "daily 21:00"))

    def install_record(self, store=None):
        self.root.mkdir(exist_ok=True)
        write_installation(InstalledCopy(self.root, "1.0.0", "2026-10-06T12:00:00+00:00", (store or self.store),
                                         Path("X:\\env\\pythonw.exe"), self.tmp))

    def test_reconcile_registers_keeps_changes_and_removes(self):
        scheduler = MemoryScheduler()
        scheduler.xml["left-over"] = task_xml(desired_tasks([a_job(triggers=Triggers(True))], launcher())
                                              .popitem()[1], datetime(2026, 10, 6))
        wanted = desired_tasks([self.job], launcher())
        first = reconcile(scheduler, wanted, datetime(2026, 10, 6, 12, 0))
        self.assertEqual((set(first.registered), first.removed), (set(wanted), ("left-over",)))
        registrations = [c for c in scheduler.calls if c[0] == "register"]
        second = reconcile(scheduler, wanted, datetime(2026, 10, 7, 12, 0))
        self.assertEqual((second.registered, second.removed, set(second.unchanged)), ((), (), set(wanted)))
        self.assertEqual([c for c in scheduler.calls if c[0] == "register"], registrations)
        changed = desired_tasks([replace(self.job, triggers=Triggers(True, "daily 06:00"))], launcher())
        third = reconcile(scheduler, changed, datetime(2026, 10, 7, 12, 0))
        self.assertEqual(third.registered, (task_name(self.job.job_id, SCHEDULE),))
        paused = reconcile(scheduler, desired_tasks([replace(self.job, enabled=False)], launcher()))
        self.assertEqual(set(paused.removed), set(wanted))
        self.assertEqual(scheduler.xml, {})

    def test_nothing_is_registered_without_an_installed_copy_serving_the_store(self):
        outcome = apply_automation(self.store, [self.job], root=self.root, scheduler=Untouchable())
        self.assertEqual(outcome.state, AutomationState.NOT_INSTALLED)
        self.install_record(store=self.tmp / "elsewhere" / "store.json")
        outcome = apply_automation(self.store, [self.job], root=self.root, scheduler=Untouchable())
        self.assertEqual(outcome.state, AutomationState.OTHER_STORE)

    def test_tasks_point_at_the_installed_copy(self):
        self.install_record()
        scheduler = MemoryScheduler()
        outcome = apply_automation(self.store, [self.job, a_job("Manual")], root=self.root, scheduler=scheduler,
                                   user="PC\\someone")
        self.assertEqual((outcome.state, outcome.result.tasks), (AutomationState.APPLIED, 2))
        for xml in scheduler.xml.values():
            action = parse(xml).find("t:Actions/t:Exec", NS)
            self.assertEqual(action.find("t:WorkingDirectory", NS).text, str(self.root))
            self.assertIn(f"--store {subprocess.list2cmdline([str(self.store)])} automatic {self.job.job_id}",
                          action.find("t:Arguments", NS).text)
        self.assertIn("2 scheduled tasks", outcome.describe())

    def test_a_scheduler_failure_is_reported_never_raised(self):
        self.install_record()
        outcome = apply_automation(self.store, [self.job], root=self.root, scheduler=MemoryScheduler(fail=True))
        self.assertEqual(outcome.state, AutomationState.FAILED)
        self.assertIn("service is stopped", outcome.describe())

    def test_removing_all(self):
        scheduler = MemoryScheduler()
        reconcile(scheduler, desired_tasks([self.job], launcher()))
        self.assertEqual(len(remove_all(scheduler).removed), 2)
        self.assertTrue(scheduler.folder_removed)

    def test_a_missing_folder_is_told_apart_from_a_failure(self):
        class ComError(Exception):
            def __init__(self, hresult, text, excepinfo):
                super().__init__(hresult, text, excepinfo, None)
                self.excepinfo = excepinfo

        missing = ComError(-2147352567, "Exception occurred.", (0, None, None, None, 0, -2147024894))
        denied = ComError(-2147352567, "Exception occurred.", (0, None, "Access is denied.", None, 0, -2147024891))
        folder = mock.Mock()
        service = mock.Mock()
        service.GetFolder.side_effect = missing
        scheduler = WindowsScheduler("\\MirrorHold Test")
        with mock.patch.object(WindowsScheduler, "_service", return_value=service):
            self.assertEqual(scheduler.tasks(), [])                 # no folder yet: no task
            service.GetFolder.side_effect = [missing, folder]
            folder.CreateFolder.return_value = folder
            scheduler.register("t", "<Task />")
            service.GetFolder.side_effect = denied
            with self.assertRaises(SchedulerError) as caught:
                scheduler.tasks()
        self.assertIn("Access is denied. (0x80070005)", str(caught.exception))


class ArrivalLogicTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.job = a_job(triggers=Triggers(True))
        self.s, self.d, self.e = volume(1, "S"), volume(2, "D"), volume(3, "E")
        self.src = source_key(self.job)
        self.dest = [destination_key(d.marker_id) for d in self.job.destinations]

    def test_what_is_present(self):
        self.assertEqual(present_keys(self.job, (self.s, self.e)), {self.src, self.dest[1]})
        self.assertEqual(present_keys(self.job, (self.d, self.e)), {self.dest[0], self.dest[1]})

    def test_what_an_arrival_runs(self):
        everything = frozenset({self.src, *self.dest})
        self.assertEqual(arrival_selection(self.job, everything, frozenset({self.src})), (1, 2))
        self.assertEqual(arrival_selection(self.job, frozenset({self.src, self.dest[1]}), frozenset({self.src})),
                         (2,))
        self.assertEqual(arrival_selection(self.job, everything, frozenset({self.dest[1]})), (2,))
        self.assertIsNone(arrival_selection(self.job, frozenset(self.dest), frozenset(self.dest)))
        self.assertIsNone(arrival_selection(self.job, everything, frozenset()))

    def test_arrivals_within_and_across_sessions(self):
        present = frozenset({self.src, self.dest[0]})
        self.assertEqual(arrivals(None, BOOT, present), present)
        same = PresenceState(BOOT + timedelta(seconds=40), frozenset({self.src}))
        self.assertEqual(arrivals(same, BOOT, present), {self.dest[0]})
        earlier = PresenceState(BOOT - timedelta(hours=20), present)
        self.assertEqual(arrivals(earlier, BOOT, present), present)          # started with them attached

    def test_the_state_is_kept_beside_the_store(self):
        layout = runtime_layout(self.tmp / "state" / "store.json")
        self.assertIsNone(read_state(layout, self.job.job_id))
        write_state(layout, self.job.job_id, PresenceState(BOOT, frozenset({self.src})))
        self.assertEqual(read_state(layout, self.job.job_id), PresenceState(BOOT, frozenset({self.src})))
        self.assertEqual(state_path(layout, self.job.job_id).parent, self.tmp / "state" / "automation")
        state_path(layout, self.job.job_id).write_text("{ not json", encoding="utf-8")
        self.assertIsNone(read_state(layout, self.job.job_id))

    def test_settling_waits_for_two_looks_that_agree(self):
        scans = [VolumeScan((self.s,), (("\\\\?\\Volume{x}\\", "unreadable"),)), VolumeScan((self.s, self.d), ()),
                 VolumeScan((self.s, self.d), ())]
        source = mock.Mock()
        source.scan.side_effect = scans
        waits = []
        volumes = settled_volumes(source, sleep=waits.append, clock=lambda: 0.0)
        self.assertEqual((volumes, len(waits)), ((self.s, self.d), 2))
        endless = mock.Mock()
        endless.scan.side_effect = [VolumeScan((self.s,), ()), VolumeScan((self.d,), ()), VolumeScan((self.s,), ())]
        clock = iter([0.0, 30.0, 61.0])
        self.assertEqual(settled_volumes(endless, sleep=lambda s: None, clock=lambda: next(clock)), (self.s,))


class FakeRunner:
    """Records each run's options and returns records it makes up; ``during`` runs
    inside a run (a drive attached meanwhile)."""

    def __init__(self, outcome=Outcome.COMPLETED, during=None, error=None):
        self.options, self.outcome, self.during, self.error = [], outcome, during, error

    def __call__(self, store, job_id, options, *, volume_source, sync_roots):
        if self.error is not None:
            raise self.error
        self.options.append(options)
        job = store.load().contents.job(job_id)
        if self.during is not None:
            during, self.during = self.during, None
            during()
        indexes = options.destinations or tuple(range(1, len(job.destinations) + 1))
        return JobRunResult("r" * 32, tuple(a_record(job, i, self.outcome, copied=3, trigger=options.trigger)
                                            for i in indexes))


class Shown:
    def __init__(self):
        self.notices = []

    def show(self, notice):
        self.notices.append(notice)


REFUSAL = "the notification could not be shown: showing the toast: error 0x80070490"


class Refusing:
    """A notifier whose every notice Windows refuses."""

    def show(self, notice):
        raise NotificationError(REFUSAL)


class AutomaticEntryTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.s, self.d, self.e = volume(1, "S"), volume(2, "D"), volume(3, "E")
        self.store = ConfigStore(self.tmp / "state" / "store.json")
        self.job = a_job(triggers=Triggers(True, "daily 21:00"))
        self.store.save(StoreContents((self.job,)))
        self.volumes = [self.s, self.d]
        self.boot = BOOT

    def arrive(self, runner, notifier=None):
        source = mock.Mock()
        source.scan.side_effect = lambda: VolumeScan(tuple(self.volumes), ())
        return run_on_arrival(self.store, self.job.job_id, volume_source=source, sync_roots=(), notifier=notifier,
                              runner=runner, boot_time=lambda: self.boot,
                              settle=lambda src: tuple(src.scan().volumes), sleep=lambda s: None)

    def test_the_first_look_of_a_session_runs_every_present_destination(self):
        runner, shown = FakeRunner(), Shown()
        result = self.arrive(runner, shown)
        self.assertEqual([(o.destinations, o.automatic, o.unattended) for o in runner.options],
                         [((1,), "arrival", True)])
        self.assertEqual([r.trigger for r in result.records], ["arrival"])
        self.assertEqual(len(shown.notices), 1)
        self.assertIn("backed up", shown.notices[0].title)

    def test_only_what_arrived_runs_and_a_drive_that_left_arrives_again(self):
        runner = FakeRunner()
        self.arrive(runner)
        result = self.arrive(runner)
        self.assertEqual((len(runner.options), result.nothing_because), (1, "no drive of this job arrived"))
        self.volumes = [self.s, self.d, self.e]
        self.arrive(runner)
        self.assertEqual(runner.options[-1].destinations, (2,))
        self.volumes = [self.s, self.e]
        self.assertEqual(self.arrive(runner).runs, [])                         # a removal runs nothing
        self.volumes = [self.s, self.d, self.e]
        self.arrive(runner)
        self.assertEqual(runner.options[-1].destinations, (1,))

    def test_a_new_session_counts_every_attached_drive_as_arrived(self):
        runner = FakeRunner()
        self.arrive(runner)
        self.boot = BOOT + timedelta(days=1)
        self.arrive(runner)
        self.assertEqual([o.destinations for o in runner.options], [(1,), (1,)])

    def test_a_drive_attached_during_a_run_is_served_by_the_next_round(self):
        runner = FakeRunner(during=lambda: self.volumes.append(self.e))
        result = self.arrive(runner)
        self.assertEqual([o.destinations for o in runner.options], [(1,), (2,)])
        self.assertEqual(len(result.runs), 2)

    def test_nothing_runs_without_the_source_or_for_a_job_that_no_longer_asks(self):
        self.volumes = [self.d]
        self.assertEqual(self.arrive(FakeRunner()).runs, [])
        self.volumes = [self.s, self.d]
        for changed, why in ((replace(self.job, enabled=False), "paused"),
                             (replace(self.job, triggers=Triggers(False, "daily 21:00")), "attached")):
            self.store.save(StoreContents((changed,)))
            result = self.arrive(FakeRunner())
            self.assertEqual(result.runs, [])
            self.assertIn(why, result.nothing_because)
        self.store.save(StoreContents())
        self.assertEqual(self.arrive(FakeRunner()).nothing_because, "the job no longer exists")

    def test_a_scheduled_run_covers_every_destination(self):
        runner, shown = FakeRunner(outcome=Outcome.HELD), Shown()
        result = run_scheduled(self.store, self.job.job_id, volume_source=FixedVolumeSource(()), sync_roots=(),
                               notifier=shown, runner=runner)
        self.assertEqual([(o.destinations, o.automatic) for o in runner.options], [(None, "schedule")])
        self.assertEqual(len(result.records), 2)
        self.assertIn("needs your review", shown.notices[0].title)
        self.store.save(StoreContents((replace(self.job, triggers=Triggers(True)),)))
        result = run_scheduled(self.store, self.job.job_id, volume_source=FixedVolumeSource(()), sync_roots=(),
                               runner=FakeRunner())
        self.assertEqual(result.nothing_because, "the job no longer runs on a schedule")

    def test_an_automatic_run_reminds_of_a_destination_due_a_verification(self):
        old = replace(a_record(self.job, 1, Outcome.COMPLETED), run_id="o" * 32, started="2026-06-01T12:00:00Z",
                      ended="2026-06-01T12:30:00Z")
        write_record(runtime_layout(self.store.path), old)
        shown = Shown()
        run_scheduled(self.store, self.job.job_id, volume_source=FixedVolumeSource(()), sync_roots=(), notifier=shown,
                      runner=FakeRunner())
        self.assertEqual(shown.notices[0].lines[2:], ("Destination 1: a full verification is due (last: never)",))

    def test_a_start_that_fails_says_why_and_keeps_a_log(self):
        shown = Shown()
        result = run_scheduled(self.store, self.job.job_id, volume_source=FixedVolumeSource(()), sync_roots=(),
                               notifier=shown, runner=FakeRunner(error=RunError("the engine is missing")))
        self.assertIn("the engine is missing", result.nothing_because)
        self.assertEqual([n.lines for n in shown.notices], [("the engine is missing",)])
        log = runtime_layout(self.store.path).automation_dir / f"{self.job.job_id}.log"
        self.assertIn("RunError", log.read_text(encoding="utf-8"))
        run_scheduled(self.store, self.job.job_id, volume_source=FixedVolumeSource(()), sync_roots=(),
                      runner=FakeRunner())
        self.assertFalse(log.exists())

    def test_why_windows_refused_a_notice_is_kept_whether_the_run_ran_or_could_not_start(self):
        ran = run_scheduled(self.store, self.job.job_id, volume_source=FixedVolumeSource(()), sync_roots=(),
                            notifier=Refusing(), runner=FakeRunner())
        failed = run_scheduled(self.store, self.job.job_id, volume_source=FixedVolumeSource(()), sync_roots=(),
                               notifier=Refusing(), runner=FakeRunner(error=RunError("the engine is missing")))
        self.assertEqual((ran.problems, failed.problems), ([REFUSAL], [REFUSAL]))
        self.assertEqual(failed.failure, "the engine is missing")

    def test_an_automatic_run_is_unattended(self):
        with self.assertRaises(RunError):
            run_job(self.store, self.job.job_id, RunOptions(automatic="arrival"), volume_source=FixedVolumeSource(()),
                    sync_roots=())
        self.assertEqual(RunOptions(unattended=True, automatic="schedule").trigger, "schedule")
        self.assertEqual((RunOptions().trigger, RunOptions(unattended=True).trigger), ("manual", "unattended"))


class NotificationTest(unittest.TestCase):
    def setUp(self):
        self.job = a_job()

    def test_what_a_notice_says(self):
        records = [a_record(self.job, 1, Outcome.COMPLETED, copied=2230, versions=1840),
                   a_record(self.job, 2, Outcome.HELD, reasons=("above-threshold",), copied=12)]
        notice = notice_for(records, Notifications())
        self.assertEqual(notice.title, "Photos: a backup needs your review")
        self.assertEqual(notice.lines[0],
                         "Destination 1: completed - 2,230 files copied (2.2 GiB), 1,840 into versions")
        self.assertIn("removals held", notice.lines[1])
        first = notice_for([a_record(self.job, 1, Outcome.HELD, reasons=("first-run",))], Notifications())
        self.assertIn("no backup there yet - start the first backup in MirrorHold", first.lines[0])
        failed = notice_for([a_record(self.job, 1, Outcome.STOPPED, detail="the destination is full")],
                            Notifications())
        self.assertEqual((failed.title, failed.lines), ("Photos: a backup did not finish",
                                                        ("Destination 1: stopped - the destination is full",)))

    def test_a_due_verification_is_a_line_of_a_notice_raised_anyway(self):
        records = [a_record(self.job, 1, Outcome.COMPLETED, copied=2, versions=0),
                   a_record(self.job, 2, Outcome.COMPLETED, copied=1, versions=0)]
        notice = notice_for(records, Notifications(), due=((1, "2026-07-01T12:00:00Z"), (2, None)))
        self.assertEqual(notice.title, "Photos: backed up")
        self.assertEqual(notice.lines[2:], ("Destination 1: a full verification is due (last: 2026-07-01)",
                                            "Destination 2: a full verification is due (last: never)"))
        self.assertIsNone(notice_for(records, Notifications(on_completion=False), due=((1, None),)),
                          "a run the user's choices keep quiet stays quiet")

    def test_a_count_of_one_names_one_file(self):
        one = notice_for([a_record(self.job, 1, Outcome.COMPLETED, copied=1, versions=0),
                          a_record(self.job, 2, Outcome.HELD, reasons=("above-threshold",), copied=1)], Notifications())
        self.assertEqual(one.lines[0], "Destination 1: completed - 1 file copied (1.0 MiB), 0 into versions")
        self.assertEqual(one.lines[1], "Destination 2: removals held for your review; 1 new and changed file copied")

    def test_a_waiting_first_run_says_whether_it_needs_a_go_ahead_or_a_review(self):
        nothing = notice_for([a_record(self.job, 1, Outcome.HELD, reasons=("first-run",))], Notifications())
        self.assertEqual((nothing.title, nothing.lines),
                         ("Photos: a backup needs your review",
                          ("Destination 1: no backup there yet - start the first backup in MirrorHold",)))
        holding = notice_for([a_record(self.job, 2, Outcome.HELD, reasons=("first-run", "destination-holds-content"))],
                             Notifications())
        self.assertEqual(holding.lines,
                         ("Destination 2: the folder already holds files - review its first run in MirrorHold",))

    def test_the_users_choices_and_what_is_never_announced(self):
        records = [a_record(self.job, 1, Outcome.COMPLETED)]
        self.assertIsNone(notice_for(records, Notifications(on_completion=False)))
        self.assertIsNone(notice_for([a_record(self.job, 1, Outcome.CANCELLED),
                                      a_record(self.job, 2, Outcome.ABSENT)], Notifications()))
        self.assertIsNone(failure_notice("Photos", "why", Notifications(on_failure=False)))
        shown = Shown()
        self.assertEqual(announce(records, Notifications(), shown), "")
        self.assertEqual(len(shown.notices), 1)
        self.assertEqual(announce(records, Notifications(), None), "")

    def test_the_notice_handed_to_windows(self):
        notice = Notice("Work & <home>: backed up", ("Destination 1: it's done",))
        xml = toast_xml(notice)
        self.assertIn("Work &amp; &lt;home&gt;: backed up", xml)
        handed = []
        ToastNotifier(APP_ID, deliver=lambda content, app_id: handed.append((content, app_id))).show(notice)
        self.assertEqual(handed, [(xml, "MirrorHold.MirrorHold")])
        self.assertIn("it's done", handed[0][0])

    def test_a_notice_that_asks_stays_on_the_screen_until_the_user_closes_it(self):
        toast = ET.fromstring(toast_xml(Notice("Work: a backup needs your review", ("Destination 1: held",), True)))
        self.assertEqual(toast.get("scenario"), "reminder")
        (action,) = toast.findall("actions/action")
        self.assertEqual((action.get("activationType"), action.get("arguments")), ("system", "dismiss"))
        self.assertEqual(action.get("content"), "Dismiss", "named, not left to the language of Windows' shell")
        plain = ET.fromstring(toast_xml(Notice("Work: backed up", ("Destination 1: done",))))
        self.assertIsNone(plain.get("scenario"))
        self.assertIsNone(plain.find("actions"))

    def test_a_file_name_that_is_not_valid_unicode_is_shown_with_a_replacement_character(self):
        notice = Notice("Work: some files are not being backed up",
                        ("Destination 1: completed - 1 file not backed up for 2 runs in a row "
                         "(for example bad\udc80.jpg)",))
        self.assertIn("(for example bad\ufffd.jpg)", toast_xml(notice))
        handed = []
        ToastNotifier(APP_ID, deliver=lambda content, app_id: handed.append(content)).show(notice)
        self.assertIn("bad\ufffd.jpg", handed[0])

    def test_a_notice_windows_refuses_is_reported_in_words(self):
        def refuse(content, app_id):
            raise windows_runtime.ToastRefused("showing the toast: error 0x80070490")

        with self.assertRaises(NotificationError) as caught:
            ToastNotifier(APP_ID, deliver=refuse).show(Notice("Work: backed up", ("Destination 1: done",)))
        self.assertEqual(str(caught.exception),
                         "the notification could not be shown: showing the toast: error 0x80070490")

    def test_which_notifier_a_process_uses(self):
        self.assertIsInstance(default_notifier(), SilentNotifier)        # the suites switch notices off
        with mock.patch.dict(os.environ, {SWITCH_VARIABLE: ""}):
            missing = default_notifier(shortcut=Path(__file__).with_name("no such shortcut.lnk"))
            present = default_notifier(shortcut=Path(__file__))
        self.assertEqual((missing.app_id, present.app_id), (POWERSHELL_APP_ID, APP_ID))
        self.assertIsInstance(present, ToastNotifier)


@unittest.skipUnless(os.name == "nt", "the Windows Runtime is Windows'")
class WindowsRuntimeTest(unittest.TestCase):
    """A toast handed to the Windows Runtime as a notice is, up to the showing, which would
    reach the desktop: its content read, the toast made, the identity's notifier obtained."""

    def test_a_notice_is_taken_up_to_its_showing_under_either_identity(self):
        notice = Notice("Work & <home>: it's backed up \U0001F4F7", ("Destination 1: a name with \U0001F600 and é",))
        for app_id in (APP_ID, POWERSHELL_APP_ID):
            with self.subTest(app_id=app_id):
                windows_runtime.check(toast_xml(notice), app_id)
                windows_runtime.check(toast_xml(Notice(notice.title, notice.lines, asks=True)), app_id)

    def test_content_that_is_not_a_toast_is_refused_at_its_reading(self):
        with self.assertRaises(windows_runtime.ToastRefused) as caught:
            windows_runtime.check("<toast><visual>", APP_ID)
        self.assertTrue(str(caught.exception).startswith("reading the toast's content: "), str(caught.exception))

    def test_an_unforeseen_failure_is_reported_as_itself_not_as_a_wait(self):
        def broken(xml, app_id, deliver, abandoned):
            raise ValueError("a broken string")

        with mock.patch.object(windows_runtime, "_hand_over", broken):
            with self.assertRaises(windows_runtime.ToastRefused) as caught:
                windows_runtime.show("<toast/>", APP_ID, timeout=10)
        self.assertIn("ValueError('a broken string')", str(caught.exception))

    def test_a_hand_over_that_outlasts_its_wait_is_told_not_to_show(self):
        told = []

        def slow(xml, app_id, deliver, abandoned):
            told.append(abandoned.wait(5))

        with mock.patch.object(windows_runtime, "_hand_over", slow):
            with self.assertRaises(windows_runtime.ToastRefused) as caught:
                windows_runtime.show("<toast/>", APP_ID, timeout=0.2)
        self.assertIn("did not take the toast within", str(caught.exception))
        deadline = time.monotonic() + 5
        while not told and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(told, [True])

    def test_a_thread_already_in_another_apartment_still_hands_it_over(self):
        ole32 = ctypes.windll.ole32
        joined = ole32.CoInitializeEx(None, 2)            # this thread single-threaded, as pywin32 leaves it
        try:
            windows_runtime.check(toast_xml(Notice("Work: backed up", ("Destination 1: done",))), APP_ID)
        finally:
            if joined in (0, 1):
                ole32.CoUninitialize()


class LostRecordNoticeTest(unittest.TestCase):
    """A shared destination whose record of what to leave was lost waits for a review in
    the window, and its notice says so (the maintainer's decision, 2026-10-17)."""

    def test_a_lost_record_asks_for_a_review_in_the_window(self):
        job = a_job()
        reasons = ("first-run", "destination-holds-content", "shared-record-lost")
        notice = notice_for([a_record(job, 2, Outcome.HELD, reasons=reasons)], Notifications())
        self.assertEqual((notice.title, notice.lines),
                         ("Photos: a backup needs your review",
                          ("Destination 2: its record of what to leave was lost - review it in MirrorHold",)))


class CommandLineAutomationTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.vol_s, self.vol_d = self.tmp / "volS", self.tmp / "volD"
        (self.vol_s / "Work").mkdir(parents=True)
        self.vol_d.mkdir()
        self.volumes = FixedVolumeSource((volume(1, mounts=(str(self.vol_s) + "\\",)),
                                          volume(2, mounts=(str(self.vol_d) + "\\",))))
        self.store_path = self.tmp / "state" / "store.json"
        self.root = self.tmp / "installed"
        self.scheduler = MemoryScheduler()

    def cli(self, *args):
        out, err = StringIO(), StringIO()
        code = cli.main(["--store", str(self.store_path), *args], volume_source=self.volumes, sync_roots=(),
                        out=out, err=err, scheduler=self.scheduler, install_root=self.root,
                        shortcut=self.tmp / "MirrorHold.lnk")
        return code, out.getvalue() + err.getvalue()

    def test_when_switches_automatic_runs_on_and_off(self):
        self.assertEqual(self.cli("add", "Work", "--source", str(self.vol_s / "Work"), "--dest",
                                  str(self.vol_d / "Work"), "--no-survey")[0], 0)
        code, out = self.cli("jobs")
        self.assertIn("runs         when you start it\n", out)
        code, out = self.cli("when", "Work", "--arrival", "on", "--schedule", "Weekly THU 7:30")
        self.assertEqual(code, 0, out)
        self.assertIn("when one of its drives is attached (also at start-up); also weekly on thu at 07:30", out)
        self.assertIn("not installed, so no job runs by itself", out)
        job = ConfigStore(self.store_path).load().contents.jobs[0]
        self.assertEqual(job.triggers, Triggers(True, "weekly thu 07:30"))
        code, out = self.cli("when", "Work", "--schedule", "at noon")
        self.assertEqual(code, 1)
        self.assertIn("not a schedule", out)
        self.assertEqual(ConfigStore(self.store_path).load().contents.jobs[0], job)
        code, out = self.cli("when", "Work", "--automatic", "off")
        self.assertIn("automatic runs paused", out)
        self.assertEqual(self.scheduler.calls, [])                                # never installed: never touched

    def test_job_changes_reconcile_the_tasks_of_an_installed_copy(self):
        self.root.mkdir()
        write_installation(InstalledCopy(self.root, "1.0.0", "2026-10-06T12:00:00+00:00", self.store_path,
                                         Path("X:\\env\\pythonw.exe"), self.tmp))
        self.cli("add", "Work", "--source", str(self.vol_s / "Work"), "--dest", str(self.vol_d / "Work"),
                 "--no-survey")
        self.assertEqual(self.scheduler.xml, {})
        code, out = self.cli("when", "Work", "--arrival", "on", "--schedule", "daily 21:00")
        self.assertIn("2 scheduled tasks (2 registered, 0 removed)", out)
        self.assertEqual(len(self.scheduler.xml), 2)
        code, out = self.cli("reconcile")
        self.assertEqual(code, 0, out)
        self.assertIn("0 registered", out)
        self.assertIn("installed at", self.cli("installation")[1])
        self.cli("remove", "Work")
        self.assertEqual(self.scheduler.xml, {})


@needs_engine
class ArrivalLivePathTest(TempDirTest):
    """An arrival run through the engine: the destination drive leaves and comes back,
    and the run that follows mirrors onto it, recorded as started by the arrival."""

    def test_an_arrival_runs_the_destination_that_came_back(self):
        vol_s, vol_d = self.tmp / "volS", self.tmp / "volD"
        (vol_s / "Work").mkdir(parents=True)
        (vol_s / "Work" / "a.txt").write_text("a", encoding="utf-8")
        vol_d.mkdir()
        s, d = volume(1, mounts=(str(vol_s) + "\\",)), volume(2, mounts=(str(vol_d) + "\\",))
        store = ConfigStore(self.tmp / "state" / "store.json")
        job = new_job("Work", endpoint(s, "Work"), [endpoint(d, "Backup")], triggers=Triggers(True))
        store.save(StoreContents((job,)))
        attached = [s, d]
        source = mock.Mock()
        source.scan.side_effect = lambda: VolumeScan(tuple(attached), ())

        def arrive():
            return run_on_arrival(store, job.job_id, volume_source=source, sync_roots=(),
                                  boot_time=lambda: BOOT, settle=lambda src: tuple(src.scan().volumes),
                                  sleep=lambda s: None)

        first = arrive()                                     # a first run waits for confirmation
        self.assertEqual([(r.outcome, r.reasons, r.trigger) for r in first.records],
                         [(Outcome.HELD, ("first-run",), "arrival")])
        ticket = preview_job(store, job.job_id, volume_source=FixedVolumeSource((s, d)), sync_roots=()).ticket
        run_job(store, job.job_id, RunOptions(ticket_id=ticket.ticket_id), volume_source=FixedVolumeSource((s, d)),
                sync_roots=())
        (vol_s / "Work" / "b.txt").write_text("b", encoding="utf-8")
        attached.remove(d)
        self.assertEqual(arrive().runs, [])
        attached.append(d)
        again = arrive()
        self.assertEqual([(r.outcome, r.trigger, r.done["files_copied"]) for r in again.records],
                         [(Outcome.COMPLETED, "arrival", 1)])
        self.assertEqual((vol_d / "Backup" / "b.txt").read_text(encoding="utf-8"), "b")
        self.assertEqual(sorted(r.trigger for r in read_records(runtime_layout(store.path), job.job_id)),
                         ["arrival", "arrival", "manual"])


if __name__ == "__main__":
    unittest.main()

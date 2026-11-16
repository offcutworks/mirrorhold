# Test Debt Ledger

Current-state record of reds left standing, one entry per unit. Git carries the
history; a settled entry is **deleted**, not marked resolved.

An entry is written with the change that caused the red, in that same commit. Whoever
made that change never edits the failing test's body — the expectation is re-derived by
someone else, so no change's author can weaken the evidence of the red it caused.

A test that goes red only sometimes, whatever the change, is entered by whoever saw it
red: its commit field says no change caused it, its delta field opens with the mark
`flaky-quarantine.`, exactly so, and its fix is a change of its own — never re-running it
until it passes. A release build accepts a test so marked whether it fails or passes,
while every other test this ledger names must fail; a test named both ways, or a delta
opening with "flaky" spelt any other way, refuses the build.

Fields: **tests · commit · delta · disposition · since**. The **commit** field names the
change that caused the red by its date and a description, never by a commit's
identifier. A **disposition** is `none`; `needs-human`, where the test's new expected
value is the maintainer's to decide; or `origin-flagged`, where the red is a deferred
fix's own regression test, which stands only once the maintainer has accepted it.

---

### `test_exchange.StatusFoundByItsLockTest.test_a_status_that_appears_while_it_is_looked_for_is_read`

- **tests:** this one test.
- **commit:** no change caused it. Seen red once, 2026-10-17, in a whole run of the suite on
  the development machine.
- **delta:** flaky-quarantine. The test holds a run's lock and starts a timer that writes its
  status a quarter of the way into the 0.2 s a look waits for a missing status; in that run
  the look found the run alive with its status unreadable. It has passed in every run on
  record since, among them 18 runs of the test beside 16 or 24 busy processes and 60 rounds
  of its scenario beside 16, on 2026-10-20. Left as it is until it is red again, the
  maintainer's decision of 2026-10-20; a longer wait in the test is the maintainer's to
  decide. The fix is a change of its own.
- **disposition:** none
- **since:** 2026-10-17

### `test_engine_contract.ComparisonStopTest.test_ending_a_graceful_stop_ends_the_comparison`

- **tests:** this one test.
- **commit:** no change caused it. Seen red once, 2026-10-22, in a whole run of the suite
  beside other processes on the development machine, its module taking 673 s.
- **delta:** flaky-quarantine. The test asks the engine to quit mid-comparison and requires it
  gone within 10 s, about 1.5 s when the test was written; in that run it took 11.5 s. Run
  alone straight after, it passed. A longer bound in the test is the maintainer's to decide;
  the fix is a change of its own.
- **disposition:** none
- **since:** 2026-10-22

### `test_engine_contract.TemporaryFileTest.test_the_temporary_name_follows_the_files_name_size_and_time`

- **tests:** this one test.
- **commit:** no change caused it. Seen red once, 2026-10-28, in a whole run of the suite
  beside other processes on the development machine, its module taking 617 s.
- **delta:** flaky-quarantine. The test starts the engine copying an 8 MiB file at 2 MiB/s and
  waits up to 30 s to see its growing temporary file; in that run it saw none. Its class
  passed when run alone straight after (5 tests, 14.8 s), as did the next whole run that day
  (its module 208 s). A longer wait in the test is the maintainer's to decide; the fix is a
  change of its own.
- **disposition:** none
- **since:** 2026-10-28

### `test_exchange.LockLookTest.test_looking_at_a_lock_its_owner_removes_never_raises_and_never_makes_it_again`

- **tests:** this one test.
- **commit:** no change caused it. Seen red once, 2026-10-29, in a whole run of the suite on
  the development machine, a run that held another red under load which passed alone.
- **delta:** flaky-quarantine. The test has an owner take and remove a lock while another
  thread looks at it, and in that run the lock file was there once both had stopped: "a look
  made the lock file again". The test passed when run alone and in the next whole run, and in
  two whole runs on 2026-10-31. Its cause is not known; the fix is a change of its own.
- **disposition:** none
- **since:** 2026-10-29

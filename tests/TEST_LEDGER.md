# Test Debt Ledger

Current-state record of reds left standing, one entry per unit. Git carries the
history; a settled entry is **deleted**, not marked resolved.

An entry is written with the change that caused the red, in that same commit. Whoever
made that change never edits the failing test's body — the expectation is re-derived by
someone else, so no change's author can weaken the evidence of the red it caused.

Fields: **tests · commit · delta · disposition · since**. The **commit** field names the
change that caused the red by its date and a description, never by a commit's
identifier. A **disposition** is `none`; `needs-human`, where the test's new expected
value is the maintainer's to decide; or `origin-flagged`, where the red is a deferred
fix's own regression test, which stands only once the maintainer has accepted it.

---

No entries.

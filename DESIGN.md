# Supervised Driving Log Design

## Goal

Build a phone-first local web app that becomes the authoritative record of
Daniel's supervised driving history for North Carolina Level 2 provisional
license requirements.

The system must:

- Track a 60 hour supervised-driving target.
- Track a separate 10 hour night-driving target.
- Seed itself from the existing records in this repo.
- Allow review, entry, deletion, export, and import of drives.
- Support a live-drive flow with start, end, and cancel.
- Be deployable on this Rocky host as a persistent local service.

## Requirements Grounded In Current Records

### DMV form `DL-4A.pdf`

The North Carolina `DL-4A` form requires these fields per log entry:

- `date`
- `time_of_day`
- `time_of_night`
- `amount_of_driving_time`
- `supervising_driver_printed_name`
- `supervising_driver_dl_number_and_state`

It also requires totals for:

- total day hours driven
- total night hours driven
- grand total

The form text also states:

- minimum 60 hours total
- at least 10 hours at night
- no more than 10 hours per week may count toward the 60 hours

The product should track the 10-hours-per-week rule as an advisory. It must
continue recording and totaling every valid drive, while clearly flagging the
drive that causes a week's total to exceed 10 hours.

### Existing seed records

Current seed sources:

- `records/2026-07-02 Daniel driving log.pdf`
- `records/log.txt`

Observed characteristics:

- The PDF contains many historical entries with a start timestamp and either
  day duration or night duration.
- The PDF already reports aggregate totals: `17h 0m` day and `1h 8m` night.
- `records/log.txt` adds later entries in free-form text.
- `records/log.txt` includes several entries with start/end times, some with
  duration only, and one final line with no explicit duration.
- The sources may contain duplicates or partial overlap.

Implication: import must preserve provenance and warnings. The system must not
silently normalize ambiguous records into "clean" data without keeping an audit
trail.

## Recommended Architecture

Use a small Python application with:

- `Python 3.13`
- `FastAPI` for the HTTP server and JSON/web handlers
- `SQLite` for the authoritative database
- server-rendered HTML templates for the UI
- minimal client-side JavaScript for large tap targets, live updates, and the
  progress gauge

Why this stack:

- Easy to run on Rocky with a user-level `systemd` service
- SQLite is sufficient for a single-family log and easy to back up
- Server-rendered pages reduce complexity and make Tailscale deployment simple
- Python has mature timezone and astronomy libraries

### Supported platform and dependencies

The production baseline is:

- Rocky Linux 9.8
- CPython 3.13.14
- Python `sqlite3` linked against SQLite 3.34.1 or newer
- FastAPI 0.139.2
- Uvicorn 0.51.0
- Jinja2 3.1.6
- python-multipart 0.0.32
- Astral 3.2
- tzdata 2026.3

The implementation must commit separate runtime and test lock files containing
all transitive versions and package hashes. `./driving-log bootstrap` creates a
repo-local `.venv` with `python3.13` and installs only those locked artifacts.
Production must not install from an unbounded version range.

The test baseline is pytest 9.1.1, HTTPX 0.28.1, and Playwright 1.61.0, also
hash-locked. Browser support is Safari and Chrome on iOS 18 and 19, plus the
current and immediately previous desktop Chrome releases. Automated phone
coverage uses Playwright WebKit at representative iPhone viewport sizes. A
release also requires a smoke test in Chrome on an actual iPhone because all
iOS browsers use WebKit but browser chrome and input behavior still differ.

Dependency upgrades are explicit commits that regenerate lock files and run the
full test suite. The application records its release and schema version so
`doctor` can report the exact deployed combination.

## Data Model

### 1. `drives`

Canonical stored record for a completed drive.

Fields:

- `id` UUID primary key, stable across CSV export/import
- `request_id` nullable UUID, unique idempotency key for manual creation
- `live_drive_id` nullable UUID with a unique constraint
- `driver_name`
- `supervisor_name`
- `supervisor_dl_number`
- `supervisor_dl_state`
- `started_at_utc` canonical UTC instant
- `ended_at_utc` canonical UTC instant
- `timezone_name`, normally `America/New_York`
- `started_utc_offset_minutes`
- `ended_utc_offset_minutes`
- `duration_minutes`
- `day_minutes`
- `night_minutes`
- `solar_calculation_version`
- `solar_latitude`
- `solar_longitude`
- `tzdata_version`
- `road_type` enum-like text: `local`, `highway`, `mixed`, `unknown`
- `weather` free text
- `notes` free text
- `source` enum-like text:
  `manual`, `live_drive`, `seed_pdf`, `seed_log_txt`, `microsoft_form`
- `source_reference` original source locator, such as file name plus row/index
- `import_batch_id` nullable foreign key
- `created_at`
- `updated_at`
- `version` integer incremented on every edit
- `deleted_at` nullable soft-delete timestamp

Rules:

- `duration_minutes = ended_at_utc - started_at_utc`
- `duration_minutes = day_minutes + night_minutes`
- only non-deleted rows count toward totals
- retrying a create with the same `request_id` and payload returns the existing
  drive; reusing it with a different payload returns a conflict
- CSV import preserves the original `source` and uses `import_batch_id` to
  record how the row entered this database
- edits preserve `id`, source provenance, and creation time; they use the
  submitted `version` for optimistic concurrency and return a conflict rather
  than overwriting a newer edit

### 2. `drive_warnings`

Stores actionable source-quality findings found during import. They are shown
until the drive is reviewed and successfully saved, at which point its source
warnings are deleted in the same transaction as the edit.

Fields:

- `id`
- `drive_id`
- `warning_code`
- `warning_message`
- `created_at`

Intrinsic warnings such as long duration and crossing midnight, and relational
warnings such as overlaps and weekly overage, are derived from the current
non-deleted drive set. They must not be persisted as authoritative warning rows
because imports and deletions can invalidate them.

Ordinary parsing choices, such as calculating duration from explicit start and
end timestamps, belong only to `import_rows` provenance. Actionable ambiguity
or source conflict appears initially with a link to review and save the drive.
Exact source duplicates use the derived overlap warning instead of a persisted
source warning.

### 3. `live_drives`

Represents the durable lifecycle of a live drive.

Fields:

- `id`
- `driver_name`
- `supervisor_name`
- `started_at_utc`
- `timezone_name`
- `started_utc_offset_minutes`
- `started_from` such as `web`
- `status`: `active`, `ending`, `completed`, or `cancelled`
- `provisional_ended_at_utc` nullable
- `provisional_ended_utc_offset_minutes` nullable
- `end_request_id` nullable unique UUID
- `completed_drive_id` nullable unique foreign key
- `finalization_payload_hash` nullable
- `finalized_at` nullable
- `created_at`

Constraints:

- a partial unique index permits only one row whose status is `active` or
  `ending`
- a completed drive's `live_drive_id` and the live row's
  `completed_drive_id` provide a stable one-to-one identity
- completed and cancelled live rows remain as audit history

Tapping `End drive` is a durable transition performed in one `BEGIN IMMEDIATE`
transaction:

1. Read the live row by its stable UUID.
2. If it is active, record the server's current UTC instant and offset as the
   provisional end, store the request UUID, and change status to `ending`.
3. If it is already ending with the same request UUID, return the same
   provisional end as a successful retry.
4. If it is completed or cancelled, return that terminal state; conflicting
   request data returns a conflict.
5. Commit the transition before showing the completion form.

If the process or host fails before commit, SQLite rolls back both changes. If
it fails after commit but before the phone receives the response, a new session
resumes the completion form with the same provisional end time.

Finalizing an ending drive is a second `BEGIN IMMEDIATE` transaction. It
validates metadata and any corrected end instant, inserts exactly one completed
drive, and marks the live row completed with the drive ID and payload hash. A
retry with the same payload returns that completed drive; different data
returns a conflict. Cancel changes either `active` or `ending` to `cancelled` in
one transaction. A deliberate `Resume drive` action may change `ending` back to
`active` after confirmation and clears the provisional end. Repeated terminal
actions are idempotent. After restart, an active or ending row remains visible
with the appropriate timer or completion interface.

### 4. `import_batches`

Tracks seed, CSV, and future form imports.

Fields:

- `id`
- `source_type`
- `source_name`
- `content_sha256` unique
- `format_version`
- `imported_at`
- `raw_snapshot` compressed blob
- `status`
- `summary_json`

### 5. `import_rows`

Required row-level audit history for every seed, CSV, and future Microsoft
import.

Fields:

- `id`
- `import_batch_id`
- `source_type`
- `source_instance_id`, identifying the particular form, workbook, or seed file
- `source_row_key`
- `raw_text`
- `parsed_payload_json`
- `result_drive_id` nullable
- `status`
- `error_message` nullable

Constraint:

- unique `(source_type, source_instance_id, source_row_key)` across all batches

### 6. `configuration`

Stores non-secret application settings required to reproduce calculations,
including location, timezone, calendar-week convention, driver name, and
optional supervisor defaults. Secrets and Microsoft credentials remain outside
the database.

### 7. `supervisor_profiles`

Stores the private export-time mapping from a supervising driver's normalized
name to their license number and two-letter state. Profiles are managed from
the DMV export page, included in full-state archives, and excluded from CSV
backups and audit metadata. Ordinary drive entry continues to store only the
supervisor name.

### 8. `audit_events`

Records security-neutral mutations and operational events such as drive
creation/deletion, live-drive finalization/cancellation, imports, archive
creation, migrations, and restores. It stores IDs and outcomes but never
supervisor license numbers or credentials.

Every browser mutation carries a generated request UUID stored uniquely with
its audit event in the same transaction. Repeating the same request and payload
returns the prior result; reusing the UUID with a different action or payload
returns a conflict. This covers response-loss retries for edits and deletions
as well as creation.

## Seed Import Strategy

The database starts empty and is seeded from the two current record files.

### Import order

1. Import all PDF-derived records.
2. Import all `log.txt` records.
3. Run duplicate/overlap detection across the combined result.
4. Present a seed review page showing ambiguous or suspicious rows.

### Parsing expectations

For the PDF:

- Parse date/time as the drive start time.
- Parse total duration from whichever source day/night column is populated, but
  treat the source column classification as provenance rather than canonical
  day/night truth.
- Resolve the start to a UTC instant using `America/New_York`, compute the end
  instant from duration, and recompute canonical day/night minutes from the
  Apex solar rule.
- Preserve the source day/night value in `import_rows`; attach a reviewable
  `seed_day_night_mismatch` warning when it differs from the computed split.
- Keep environment text as `road_type` when possible.
- Store `Sean Ahern` as supervisor where present.

For `log.txt`:

- Accept both `start only + duration` and `start-end + duration` formats.
- If a row includes start and end but no duration, resolve both to UTC and
  compute duration.
- If a row includes duration but no end, compute the UTC end from start plus
  duration.
- Recompute every canonical day/night split from the resulting UTC interval,
  regardless of any day/night label in the source text.
- If a row lacks enough information to compute duration, import it as a failed
  row in `import_rows` and require manual completion in the UI.

The last line in `records/log.txt` has no separately written duration:

- `2026-07-24 11:10-11:31: local and highways with wet roads, cloudy conditions`

That line has enough information to be complete: its start and end timestamps
define a 21 minute duration. The importer computes that duration without a
warning and preserves the source row as provenance.

### Authoritative-record rule

After initial seed, the database is authoritative.

- The original source files are historical inputs, not ongoing truth.
- All later edits happen through the app or explicit import.
- Every imported row keeps provenance so disputes can be traced back.

## Day/Night Classification

Night driving must be computed from entered timestamps, not manually toggled.

### Time basis

- Users enter and view local times in `America/New_York`.
- Store canonical start and end as UTC instants, along with timezone name,
  entered offsets, and the pinned tzdata version used to resolve them.
- Use the pinned `tzdata` package rather than host-global timezone files so
  calculations are reproducible after redeployment.
- Reject local times that do not exist during the spring DST transition.
- When a manually entered time is ambiguous during the fall transition, require
  the user to choose the first or second occurrence and show the corresponding
  timezone abbreviation and UTC offset.
- Live-drive timestamps come from server UTC and are therefore unambiguous;
  local values are display conversions.

### Sunrise/sunset rule

For every local date intersected by a drive:

- obtain sunrise and sunset for the local area
- define daytime start as `sunrise - 15 minutes`
- define daytime end as `sunset + 15 minutes`
- any portion outside that daytime window counts as night minutes

Implications:

- A single drive can contribute to both day and night tallies.
- Intersect the UTC drive interval with every relevant local solar boundary;
  do not assign a cross-midnight drive entirely to its start date.
- Store the calculation version, Apex coordinates, timezone, and tzdata version
  on each drive so historical totals remain explainable after configuration or
  dependency changes.

### Location assumption

For version 1, use Apex, North Carolina as the configured home location for
astronomy calculations. Recommended configuration:

- `location_name = Apex, NC`
- `latitude`
- `longitude`
- `timezone = America/New_York`

Resolve and store fixed coordinates for Apex during implementation rather than
calling a geocoding service for every drive. If the family routinely drives far
from home, location can become a per-drive field later, but that is likely
unnecessary for this use case.

### Suggested implementation

Use a maintained astronomy library such as `astral` so sunrise/sunset and DST
are delegated to a standard source rather than custom formulas.

## Validation Rules

### Hard errors

Reject save when:

- the resolved end instant is before the resolved start instant
- duration is zero or negative
- a local timestamp falls in a nonexistent DST interval
- an ambiguous fall-DST timestamp has no selected occurrence/offset
- required fields are missing for a completed drive

### Soft warnings

Allow save, but show prominent warnings when:

- duration exceeds 5 hours
- drive crosses midnight
- drive overlaps another saved drive
- the drive would cause its week's total to exceed 10 hours
- imported data contains an ambiguity or conflicts with its canonical value

### Weekly cap handling

The DMV form states that no more than 10 hours per week may count toward the 60.

Recommended behavior:

- keep `total_minutes` as the sum of every valid drive
- use `total_minutes` for the primary 60-hour progress gauge
- intersect each drive's UTC interval with local calendar-week boundaries and
  allocate minutes on each side; do not assign a cross-boundary drive wholly by
  its start date
- derive over-cap weeks from all current non-deleted drives whenever totals are
  read
- after every create, import, or deletion, return the newly computed week total
  and overage in the mutation response
- show over-cap weeks prominently on the dashboard and drive list
- allow the drive to be saved after the warning is acknowledged

The query returns the week boundaries, total minutes, and
`max(0, total_minutes - 600)` overage. It also identifies all drives in that
week and the first drive in chronological order that moves the cumulative
weekly total above 600 minutes. Because this state is derived rather than
stored, deleting an earlier drive or importing a historical drive immediately
moves or removes the warning correctly.

The supplied form does not define the week boundary. Use a documented calendar
week convention in version 1 and keep that convention configurable. The default
is Sunday 12:00 a.m. through the following Sunday 12:00 a.m. in
`America/New_York`. Before final DMV submission, any week over 10 hours should
be reviewed manually.

## Web Interface

The main usage mode is phone-first while seated in the passenger seat. The UI
should favor large targets, high contrast, and minimal typing.

### Solar theme and system UI

Use the same Apex solar rule as driving classification for the interface theme:

- light theme from 15 minutes before sunrise through 15 minutes after sunset
- dark theme at all other times

Every server-rendered page includes the theme effective at the response's server
UTC time and the next 48 hours of precomputed theme-boundary UTC instants. Set a
root `data-theme` value, CSS `color-scheme`, and the browser `theme-color` so
native form controls and surrounding browser UI match the active light or dark
palette. Use system conventions for safe areas, text sizing, focus indication,
reduced motion, and touch controls while keeping the solar theme authoritative
over `prefers-color-scheme`.

JavaScript schedules each supplied boundary and flips the root theme locally.
If iOS suspends the page and delays a timer, the visibility handler immediately
applies every elapsed boundary before rendering and then refreshes the schedule.
Static pages are correct when loaded; the live-drive interface is required to
remain open and flip at a boundary without a page reload.

### 1. Dashboard

Primary landing page.

Shows:

- circular progress gauge for `total_minutes / 60 hours`
- separate night progress gauge or clear secondary stat for `night / 10 hours`
- numeric totals:
  `total`, `night total`, `remaining`
- advisory card listing any weeks over the 10-hour cap and each overage amount
- active timer or ending-drive completion banner when a live row is in progress
- quick actions:
  `Start a drive`, `Add drive`, `View drives`, `Import`, `Export`

Gauge behavior:

- speedometer-like arc with a clear `60h` target marker
- secondary night stat should be visible without scrolling
- color should indicate `not ready`, `near goal`, `goal met`

### 2. Drive list

Mobile-friendly chronological list with filters.

Capabilities:

- view recent drives first
- filter by month
- filter rows with warnings only
- tap a drive to open details
- edit an existing drive from its details view
- delete an incorrect drive from the details view

Displayed row summary:

- start date
- start-end
- duration
- day/night split
- road type
- warning badge if present

### 3. Manual entry form

Must be large and easy to use quickly.

Form design:

- big touch targets
- one-column layout
- large time pickers
- large save button
- defaults for frequent values such as supervisor name
- radio or segmented buttons for road type
- weather as a short optional text field

Fields:

- start date
- start time
- end date, defaulting to the start date
- end time
- large `Ends next day` control that advances the end date by one day
- supervisor name
- road type
- weather
- notes

Supervisor DL number and state remain nullable and are deliberately omitted
from ordinary drive entry. A later DMV export mapping will associate the
entered supervisor name with license details and warn if that mapping is
missing.

On submit:

- show computed duration
- show computed day/night split
- show both local dates when the drive crosses midnight
- show warnings before final confirm if needed

### 4. Edit drive

The edit form reuses the large manual-entry controls and allows correction of:

- date
- end date or `Ends next day`
- start time
- end time
- supervisor name
- road type
- weather
- notes

Saving an edit occurs in one transaction. It validates the submitted record
version, reruns hard validation, recomputes duration and day/night minutes, and
updates the drive without changing its stable ID or provenance. It also clears
the drive's reviewable import warnings. The response
includes newly derived long-drive, midnight, overlap, and weekly-overage
warnings. An `audit_events` row records the before and after values with private
license data redacted. If another session edited the drive first, return a
conflict and show both versions for deliberate reconciliation.

Repeating a completed edit with the same request UUID returns the prior result
instead of applying the edit twice.

### 5. Live drive

Entry point:

- tap `Start a drive`

At start:

- record the current server UTC timestamp and local display offset
- optionally capture default supervisor immediately
- submit a browser-generated UUID so a repeated tap or retried request returns
  the same live drive

During active drive:

- dashboard prominently shows the fixed local start time, timezone
  abbreviation, live duration as `H:MM:SS`, and `End drive` / `Cancel drive`
- refreshing the page or restarting the service recovers the stored active or
  ending drive rather than starting a new timer
- the active drive is household/server state, not browser-session state; its
  ownership must not depend on a cookie, tab, IP address, Tailscale address, or
  in-memory process object
- every authorized dashboard request queries SQLite for the single active or
  ending row; active elapsed time is reconstructed from `started_at_utc`, while
  ending state resumes the completion form with its provisional end

#### Live client clock

The live state response includes:

- stable live-drive ID and state
- canonical `started_at_utc`
- formatted local start time and timezone abbreviation
- current `server_now_utc`
- current solar theme
- the next 48 hours of solar theme-boundary UTC instants

At response receipt, the client calculates the authoritative elapsed baseline
from `server_now_utc - started_at_utc`, captures both `performance.now()` and
`Date.now()`, and derives a server-to-device wall-clock offset. While visible,
it redraws once per second from the baseline plus monotonic elapsed time; it
never increments a counter that can accumulate timer drift.

On return from iOS suspension, rebase immediately from
`Date.now() + server_offset - started_at_utc` so elapsed duration and solar
boundaries catch up even before the network is available. Mark that projection
as awaiting server confirmation, then replace the offset after a successful
resynchronization. A material disagreement between monotonic and adjusted wall
time also triggers this rebase and resynchronization path.

No per-second network requests are made. Resynchronize live state and server
time only on initial load, return from background/visibility change, explicit
reconnect, exhaustion of the supplied boundary schedule, or a
start/end/resume/cancel/finalize mutation. If the network is unavailable, the
projected timer and supplied theme schedule continue locally and the interface
clearly marks server synchronization as pending. Persisted duration and end time
always come from server-owned UTC timestamps or an explicitly corrected end
time, never from the projected display counter.

End flow:

- tap `End drive`
- durably record the current server UTC timestamp and transition to `ending`
- render the completion form only after that transition commits
- prompt only for remaining metadata:
  `road_type`, `weather`, `notes`, optional supervisor confirmation
- show warnings and computed totals before final save
- submit to the stable live-drive URL; duplicate submissions return the same
  completed drive
- default the end timestamp to the successful end request time, but allow it to
  be corrected before confirmation when reconnection or reauthentication
  delayed the request after the car stopped
- if the browser or network disappears after the tap, a new session resumes
  this same completion form and provisional end time

Cancel flow:

- tap `Cancel drive`
- require confirmation
- mark the live-drive row cancelled without creating a completed drive
- permit cancellation from either active or ending state
- repeat submissions return the already-cancelled result

Required reconnect scenario:

1. Start a live drive from an iPhone.
2. Allow the browser to be reaped and Tailscale to disconnect.
3. Reauthenticate to Tailscale from a new network address and open a new browser
   session.
4. The dashboard must show the same active drive, original start time, and
   reconstructed elapsed timer with working `End drive` and `Cancel drive`
   controls.

The same outcome is required if the web service or Rocky host restarts while
the phone is disconnected. A parallel test ends the drive before disconnection
and must recover the ending-state completion form and provisional end time.

### 6. Import/export pages

Web actions:

- export all non-deleted drives to CSV
- import CSV backup
- create and download a full-state archive
- upload and verify a full-state archive; restoration requires a separate,
  explicit confirmation
- show import summary with created rows, skipped rows, warnings, and failures

## CSV Drive Log Backup

CSV is the human-readable, portable backup of the current completed-drive log.
It is not a complete application archive and does not include deleted drives,
live-drive state, configuration, import audit rows, or audit events.

Recommended columns:

- `format_version`
- `id`
- `driver_name`
- `supervisor_name`
- `supervisor_dl_number`
- `supervisor_dl_state`
- `started_at_utc`
- `ended_at_utc`
- `started_at_local`
- `ended_at_local`
- `timezone_name`
- `started_utc_offset_minutes`
- `ended_utc_offset_minutes`
- `duration_minutes`
- `day_minutes`
- `night_minutes`
- `solar_calculation_version`
- `solar_latitude`
- `solar_longitude`
- `tzdata_version`
- `road_type`
- `weather`
- `notes`
- `source`
- `source_reference`

Rules:

- export in UTF-8
- use ISO-like local datetime strings with timezone offset
- use RFC 3339 UTC strings for canonical start/end instants; local strings are
  human-readable redundant values
- export only non-deleted completed drives
- preserve each drive's stable UUID and original provenance
- validate `format_version`, headers, every row, and all duplicate IDs before
  making any database change
- recompute duration and day/night minutes from the canonical UTC interval and
  recorded calculation context; reject internal inconsistencies rather than
  trusting redundant CSV totals
- calculate a SHA-256 hash for the exact uploaded file and store it as the
  unique `import_batches.content_sha256`
- perform the entire import and its audit rows in one transaction
- if the same completed file is imported again, return the original import
  summary without creating rows
- if a drive UUID already exists with identical canonical content, skip it
- if a drive UUID exists with different content, abort the whole import and
  report the conflict; never silently overwrite
- if power or process failure interrupts import, the transaction rolls back and
  retry is safe

CSV import is append/reconcile only. Full replacement belongs to the full-state
restore workflow below.

## Full-State Archives

A full-state archive is the disaster-recovery backup. It contains a consistent
SQLite snapshot with completed and soft-deleted drives, source warnings, every
live-drive lifecycle state, configuration, import raw snapshots and audit rows,
schema history, and application audit events. Credentials remain external and
are documented separately.

Storage defaults:

- state directory: `/home/ahern/.local/state/driving-log`
- live database:
  `/home/ahern/.local/state/driving-log/driving-log.sqlite3`
- archive directory: `/home/ahern/.local/state/driving-log/archives`
- staged restore directory:
  `/home/ahern/.local/state/driving-log/restore-requests`
- directory mode `0700`; database and archives mode `0600`

The user-level systemd units express `/home/ahern` with the `%h` specifier, but
the application resolves and reports the absolute path shown above.

Archive creation:

1. Use Python's SQLite online backup API to write a temporary snapshot without
   copying the live database or WAL files directly.
2. Run `PRAGMA quick_check` against the snapshot.
3. Record archive format, application version, schema version, creation time,
   database size, and the snapshot's SHA-256 in a manifest.
4. Package the snapshot and manifest into one versioned archive bundle.
5. `fsync` the bundle, atomically rename it into place, then `fsync` the archive
   directory.
6. Mark the archive verified only after reopening the bundle, checking the
   snapshot against the manifest hash, and running `quick_check`.

`driving-log-archive.timer` creates and verifies one archive daily. Retain the
most recent 14 daily archives and 8 weekly archives. A verified pre-migration
archive is mandatory and is not removed by ordinary daily retention.

### External replication

Local archives protect against database corruption but not loss of Rocky's
disk. Configure `DRIVING_LOG_EXTERNAL_ARCHIVE_DIR` to a mounted external disk,
NAS, or other destination outside the live database filesystem.

After local verification, the archive service copies the bundle to a temporary
name at that destination, `fsync`s it, atomically renames it, and independently
verifies its manifest hash and SQLite `quick_check`. Retain at least 8 verified
weekly external archives. Failed or unavailable replication never deletes a
previous external archive and produces a dashboard and `doctor` warning.

`archive create --out PATH` supports an explicit one-off external destination,
and the web download provides a manual off-host copy. Production readiness must
show either a verified configured external archive or an explicitly
acknowledged warning that disaster recovery is limited to the Rocky disk.

### Restore orchestration

CLI restoration runs directly while the web service is stopped. Web restoration
uses an external user-systemd helper so the request is not responsible for
stopping and restarting its own process:

1. The web process uploads an archive into a UUID-named restore
   request directory and verifies it without changing live state.
2. After explicit confirmation, it atomically writes an HMAC-signed request
   manifest containing the operation ID, expected archive hash, and bounded
   execution window. The signing key is in the mode-`0600` service environment
   file, not the database or archive.
3. It starts `driving-log-restore@OPERATION_ID.service` and returns HTTP 202 with
   the operation ID. The signed request manifest contains a short `not_before`
   time so the helper cannot stop the web service until the response has had
   time to flush.
4. The helper re-verifies the staged archive and request manifest, performs the
   restore procedure below, restarts the web service, and atomically writes a
   result file outside the restored database.
5. After reconnect, the browser reads the result by operation ID. The same
   result is available through `./driving-log archive restore-status ID` if the
   web service could not restart.

Restore procedure:

1. Stop the web service and acquire the application lock.
2. Verify the archive hash, archive format, SQLite integrity, and supported
   schema before touching the live database.
3. Move the current database and any WAL/SHM files to a timestamped quarantine
   directory; never overwrite the only copy.
4. Restore to a temporary file in the state directory, set permissions,
   `fsync`, and atomically rename it to the live database path.
5. Start the service, run readiness and `quick_check`, and retain the quarantine
   copy until the restored service is manually accepted.

The automated integration suite must prove that a restored archive reproduces
all authoritative tables, including an active live drive, deleted rows,
configuration, imports, and audits.

## CLI Interface

Provide simple commands runnable from this repo.

Recommended command surface:

- `./driving-log bootstrap`
- `./driving-log serve`
- `./driving-log start`
- `./driving-log stop`
- `./driving-log restart`
- `./driving-log status`
- `./driving-log doctor [--json]`
- `./driving-log db check`
- `./driving-log live status`
- `./driving-log imports status`
- `./driving-log seed`
- `./driving-log csv export --out driving-log.csv`
- `./driving-log csv import --in driving-log.csv`
- `./driving-log archive create [--out PATH]`
- `./driving-log archive list`
- `./driving-log archive replicate`
- `./driving-log archive verify [ARCHIVE]`
- `./driving-log archive restore ARCHIVE --confirm`
- `./driving-log archive restore-status OPERATION_ID`

Use a small shell entrypoint plus the standard-library `argparse` module. The
commands are thin wrappers around the application's real service, import, and
archive logic so behavior stays consistent between CLI and web.

`doctor` is read-only and reports:

- application, Python, SQLite, and schema versions
- configured and resolved database/archive paths and file permissions
- effective SQLite journal, synchronous, foreign-key, and busy-timeout settings
- `PRAGMA quick_check` result
- active or ending live-drive ID, state, age, and provisional end presence,
  without private supervisor details
- incomplete or failed import batches
- newest full archive age and latest verification result
- newest external archive age, destination, and verification result
- filesystem free space
- user-service state and recent restart count
- local HTTP liveness/readiness independently from Tailscale Serve status
- Tailscale forwarding configuration and reachability

This separation makes it clear whether an outage is in the process, database,
local HTTP service, or Tailscale forwarding layer.

## Hosting And Persistence

Constraints:

- do not conflict with the existing service on port 80
- primary access will be over Tailscale

Recommended runtime:

- bind the app locally on `127.0.0.1:8766`
- expose it through Tailscale Serve or an equivalent forwarding rule
- keep the internal app port configurable
- keep all mutable state outside the Git worktree at
  `/home/ahern/.local/state/driving-log`

Persistence across reboot:

- install a user-level `systemd` unit
- enable lingering for the user if needed so the service survives logout
- provide CLI helpers that wrap `systemctl --user`

Recommended service units:

- `driving-log-web.service`
- `driving-log-archive.service`
- `driving-log-archive.timer`
- `driving-log-restore@.service`
- optional `driving-log-import.timer` later for scheduled form ingestion

### SQLite durability

Every database connection must enable and verify:

- `PRAGMA journal_mode = WAL`
- `PRAGMA synchronous = FULL`
- `PRAGMA foreign_keys = ON`
- a finite `busy_timeout`

All mutations use explicit transactions. Multi-row operations, imports,
live-drive transitions, warning acknowledgements, and audit events commit as
one unit. Write transactions use `BEGIN IMMEDIATE` when serialization is needed.
The service must never copy a live SQLite file as a backup.

On every startup:

1. Resolve and log the exact database path.
2. Open SQLite and apply connection pragmas.
3. Run `PRAGMA quick_check`.
4. Verify the schema version and migration checksums.
5. Apply required migrations only after creating a verified pre-migration
   archive.
6. Run `quick_check` again before the readiness endpoint succeeds.

If integrity checking, a migration, or post-migration checking fails, the
service refuses writes and readiness fails. It logs the recovery command and
archive candidates but never silently restores or discards the database.

### Schema migrations

Maintain an ordered `schema_migrations` table with version, name, checksum, and
applied timestamp. Each migration runs transactionally; failure rolls back and
prevents service startup. The application refuses to open a schema newer than
it understands. Downgrades are not automatic and require restoring a compatible
pre-migration archive.

### Health and logging

Expose localhost health endpoints:

- `/health/live`: process event loop is responsive
- `/health/ready`: database opens, schema is supported, integrity check passed,
  and migrations are complete

Emit one-line structured JSON logs to stdout/stderr for journald. Include event
name, timestamp, severity, request ID, relevant record IDs, duration, and
outcome. Exclude supervisor license numbers, credentials, raw import contents,
and other private form data. `./driving-log status` shows concise service state;
`doctor` performs the deeper read-only checks. Operators can inspect full logs
with `journalctl --user-unit driving-log-web.service`.

## Microsoft Forms / Spreadsheet Ingestion

This is an extension point, not a day-1 dependency.

Design now so it plugs in later without changing the core model.

### Desired flow

1. Wife submits a Microsoft Form.
2. Microsoft stores responses in a spreadsheet.
3. A scheduled importer reads new rows.
4. Imported rows are validated and inserted as `source = microsoft_form`.
5. Suspect rows are visible in the warnings view.

### Interface contract

Define an importer abstraction now:

- input: raw row payload
- input identity: source type, stable source instance ID, and stable row key
- output: normalized drive candidate plus warnings/errors

That same interface can be used for:

- seed PDF rows
- `log.txt`
- CSV backup import
- future Microsoft rows

### Suggested spreadsheet fields

The future form should collect:

- start date
- start time
- end date or an `Ends next day` answer
- end time
- supervisor name
- road type
- weather
- notes

Night/day should still be computed by the app.

### Idempotency and updates

Use the Microsoft Form response ID as `source_row_key`; never use spreadsheet
row number because sorting and row insertion can change it. Store a stable form
or workbook ID as `source_instance_id`.

Each scheduled acquisition may read the whole table. In one transaction:

- a previously unseen source key creates one drive and import row
- an existing key with identical normalized content is a no-op
- an existing key with changed content is reported as a conflict requiring
  explicit review; it never creates another drive or silently overwrites edits
- the batch records inserted, unchanged, conflicted, and invalid row counts

The whole-snapshot SHA-256 makes retry of an identical acquisition a no-op; the
per-row unique source identity makes a later snapshot containing old plus new
responses safe. Interrupted batches roll back and can be retried.

### Scraping versus API

Prefer structured spreadsheet export or API access over brittle HTML scraping if
Microsoft tooling allows it. The design should keep the importer behind a single
adapter so the acquisition method can change without touching validation or
storage logic.

## Security / Access

Primary audience is the household. The deployment deliberately uses tailnet
membership and tailnet policy as its authorization boundary, matching the
existing Wordle service. This is an explicit low-threat household trade-off,
not a general-purpose internet deployment posture.

Required controls:

- bind only to localhost and expose only through Tailscale Serve
- accept tailnet-only HTTP because Tailscale encrypts node-to-node transport
- do not enable Funnel or bind the application to a LAN or tailnet interface
- do not add an application login, authentication cookie, Origin/Host allowlist,
  or CSRF layer
- use POST for mutations and never mutate state from GET requests
- use stable request IDs and state/version checks to make duplicate taps,
  retries, and stale pages safe; these are correctness controls, not
  authentication
- keep active-drive ownership in SQLite rather than a browser session,
  address, or identity header
- accept Tailscale identity headers only as optional audit metadata
- require an HMAC-signed, expiring, one-use helper request for archive restore
- return archive and CSV downloads with `Cache-Control: no-store` and attachment
  disposition
- never place operation secrets or supervisor license numbers in URLs or logs

Opening a new browser session displays the dashboard immediately and recovers
any active or ending drive from SQLite.

For the Microsoft-form ingestion path, credentials should live outside the repo
in environment variables or a local config file excluded from version control.

## Reporting / DMV Export

In addition to CSV drive-log backup and full-state archives, the app should
produce a filled, flattened DL-4A PDF:

- chronological table matching the `DL-4A` columns
- total day hours
- total night hours
- grand total
- one row per non-deleted completed drive, with exact `H:MM` durations
- supervisor license mapping from `supervisor_profiles`, falling back to
  legacy per-drive license fields
- blank supervisor cells when a drive has no supervising driver
- blank customer and certification sections for later handwritten completion
- continuation pages derived from the official packaged template, with totals
  only on the final page

The review page reports unmapped supervising drivers, blank-supervisor rows,
page count, and calendar-week overages. These warnings do not block export or
change the authoritative raw totals.

## Test And Release Strategy

### Unit tests

Use deterministic clocks and fixed Apex coordinates. Cover:

- duration validation at zero, negative, 5 hours, and just over 5 hours
- sunrise minus 15 minutes and sunset plus 15 minutes boundary behavior
- drives entirely in day or night and drives split across either boundary
- spring DST gaps, both fall-DST fold occurrences, and UTC round trips in
  `America/New_York`
- standard-time and daylight-time dates
- midnight crossing, explicit next-day input, end-before-start behavior, and
  splitting at local date and calendar-week boundaries
- overlap creation and removal after soft deletion
- calendar-week boundaries, exactly 600 minutes, first minute of overage,
  multiple over-cap drives, historical import, and deletion-driven recomputation
- CSV format versions, duplicate file hashes, identical UUID retries,
  conflicting UUIDs, and all-or-nothing row validation
- seed parsing ambiguities, source/computed day-night mismatches, and duplicate
  candidates
- repeated Microsoft snapshots containing old plus new rows, identical-row
  no-ops, and changed-row conflicts

Astronomy tests use known expected Apex sunrise/sunset fixtures with a documented
tolerance, while business-boundary tests inject exact sunrise/sunset values so
library updates cannot obscure a 15-minute-rule regression.

### SQLite integration and recovery tests

Run against real temporary SQLite files with production pragmas. Cover:

- schema creation and every migration path from each supported schema version
- failed migration rollback and refusal to start on newer or damaged schemas
- WAL recovery after killing a subprocess during manual entry, deletion, live
  start, active-to-ending transition, ending-state finalization, cancel, CSV
  import, archive creation, and migration
- live finalization before-commit and after-commit failure points, proving retry
  creates exactly one completed drive
- ending-state recovery after browser, process, and host restart
- duplicate manual and live requests with same and conflicting idempotency data
- edit retries after response loss and conflicting edits from two browser
  sessions
- interrupted CSV import rollback and safe retry
- `quick_check` startup failure and read-only/unready behavior
- archive hash/integrity rejection
- local and external archive retention, interrupted replication, and external
  hash/integrity rejection
- full restore equality for every authoritative table, including soft-deleted
  rows, configuration, raw imports, audits, and an active live drive
- quarantine preservation and rollback when post-restore readiness fails
- web restore staging, manifest HMAC and `not_before` enforcement, helper
  handoff, result-file recovery, and failed web-service restart

Tests must use subprocess termination and reopen the database rather than merely
raising an exception inside one connection.

### Browser tests

Playwright WebKit runs at representative iPhone viewport sizes and verifies:

- light theme during the solar day and dark theme during solar night
- day-to-night and night-to-day theme flips at exact supplied boundaries
- native `color-scheme` and browser `theme-color` update with the page palette
- dashboard gauge and night total without horizontal scrolling
- minimum touch-target sizing and readable entry controls
- manual entry validation and warning display
- drive listing, details, editing, concurrent-edit conflict, deletion, CSV
  download/upload, and archive controls
- tailnet HTTP operation without application login and one-use signed restore
  requests
- start, elapsed display, durable ending state, resume, finalization, cancel,
  double-tap, and refresh behavior
- fixed local start-time display and drift-free `H:MM:SS` duration updates
- no recurring HTTP requests while observing a live timer for at least 65
  seconds
- continued timer and theme-boundary projection while network requests fail
- background/suspend return immediately catches up elapsed time and theme before
  successful server resynchronization
- end-request retry after a response is lost
- active-drive recovery in a fresh browser context with no cookies or local
  storage
- active-drive recovery after restarting the service between page loads
- ending-state completion recovery in a fresh browser context and after service
  restart
- offline/Tailscale-unavailable presentation without losing browser form state
- end-time correction after reconnection delay

Before a release is installed as the persistent service, perform an actual
iPhone Chrome smoke test through Tailscale: start a drive, close/reap the
browser, disconnect Tailscale, reconnect from a changed network, open a new
session, verify the active timer, then end and confirm exactly one saved drive.

### Release gates

Required before deployment:

- unit, SQLite integration/recovery, and Playwright suites pass
- `git diff --check` passes
- a fresh bootstrap works from the committed hash-locked dependencies
- seed import totals and warnings are manually reconciled
- `doctor` reports healthy local service, database, local archive, external
  archive, and Tailscale layers, or the external-backup limitation is explicitly
  acknowledged
- a full archive is created, verified, restored into a disposable state
  directory, and compared with the source database
- start/stop/restart and reboot persistence are smoke-tested

## Open Issues Identified From Current Seed Data

These should be handled explicitly during implementation:

1. The final `records/log.txt` row has no separately written duration, but its
   start/end timestamps define 21 minutes. Import that interval normally and
   preserve the original row as provenance.
2. The Road Ready PDF includes duplicated-looking timestamps in at least one
   place (`08/10/2025 6:27 PM` appears twice). Treat as potential duplicates,
   not automatic deletion.
3. The Road Ready PDF and `log.txt` may overlap in chronology in future
   revisions. Provenance plus overlap warnings are required.
4. The DMV's 10-hours-per-week counting rule should produce a prominent
   advisory warning without reducing the authoritative logged-hours total.

## Recommended Delivery Phases

### Phase 1

- database schema
- durability pragmas, migrations, integrity checks, and idempotency
- seed import
- dashboard
- solar-driven light/dark system UI
- manual entry
- drive list, edit, and delete
- CSV export/import
- full-state archive create/verify/restore, external replication, and automated
  retention
- day/night computation
- warnings engine
- loopback-only serving, retry-safe mutations, and signed restore requests
- unit and SQLite recovery tests

### Phase 2

- live-drive start/end/cancel
- durable ending-state recovery and resume
- client-projected live start time, duration, and solar-boundary theme changes
- user-level `systemd` service helpers
- Tailscale-forwarded deployment docs
- operational health, doctor, and structured logging
- Playwright mobile-WebKit and actual-iPhone reconnect tests

### Phase 3

- print/DMV export view
- Microsoft Form spreadsheet ingestion
- scheduled importer

## Configuration Decisions

- Sunrise/sunset location: Apex, North Carolina.
- Timezone: `America/New_York`.
- Canonical time storage: UTC instants with local timezone, offset, and pinned
  tzdata context.
- Calendar week: Sunday through Saturday in local time; configurable if DMV
  provides a different interpretation.
- Supervisor DL number and state: not yet available; keep nullable until
  supplied and warn before DMV-facing export.
- Dashboard: show all recorded time as progress toward 60 hours. Flag any week
  over 10 hours and show its overage without reducing the displayed total.
- Theme: light during the Apex solar day and dark during solar night, using the
  same 15-minute sunrise/sunset offsets as drive classification.
- Live display: show local start time and `H:MM:SS` duration, projected
  client-side between server synchronization events with no per-second requests.
- Web access: tailnet-only HTTP through Tailscale Serve, with no application
  login; tailnet policy is the authorization boundary.
- External archive destination: not yet selected; deployment must configure one
  or explicitly acknowledge same-disk-only disaster recovery.

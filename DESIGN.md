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

The product should track the 10-hours-per-week rule and flag it in the UI,
even if the first version does not block entry on that basis.

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

## Data Model

### 1. `drives`

Canonical stored record for a completed drive.

Fields:

- `id` UUID or integer primary key
- `driver_name`
- `supervisor_name`
- `supervisor_dl_number`
- `supervisor_dl_state`
- `started_at_local` local wall time in `America/New_York`
- `ended_at_local` local wall time in `America/New_York`
- `duration_minutes`
- `day_minutes`
- `night_minutes`
- `road_type` enum-like text: `local`, `highway`, `mixed`, `unknown`
- `weather` free text
- `notes` free text
- `source` enum-like text:
  `manual`, `live_drive`, `seed_pdf`, `seed_log_txt`, `microsoft_form`, `csv_import`
- `source_reference` original source locator, such as file name plus row/index
- `created_at`
- `updated_at`
- `deleted_at` nullable soft-delete timestamp

Rules:

- `duration_minutes = ended_at_local - started_at_local`
- `duration_minutes = day_minutes + night_minutes`
- only non-deleted rows count toward totals

### 2. `drive_warnings`

Stores non-fatal validation findings attached to a drive.

Fields:

- `id`
- `drive_id`
- `warning_code`
- `warning_message`
- `created_at`

Examples:

- `long_drive`
- `crosses_midnight`
- `overlaps_existing_drive`
- `exceeds_weekly_countable_cap`
- `seed_ambiguous_duration`
- `seed_possible_duplicate`

### 3. `live_drives`

Represents a drive that has started but not yet been finalized.

Fields:

- `id`
- `driver_name`
- `supervisor_name`
- `started_at_local`
- `started_from` such as `web`
- `created_at`

Constraints:

- only one active live drive at a time for this household
- ending a live drive creates a row in `drives`
- canceling a live drive deletes or archives this row without creating a drive

### 4. `import_batches`

Tracks seed, CSV, and future form imports.

Fields:

- `id`
- `source_type`
- `source_name`
- `imported_at`
- `raw_snapshot_path` or stored blob reference
- `status`
- `summary_json`

### 5. `import_rows`

Optional but useful for auditability.

Fields:

- `id`
- `import_batch_id`
- `source_row_key`
- `raw_text`
- `parsed_payload_json`
- `result_drive_id` nullable
- `status`
- `error_message` nullable

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
- Parse daytime or nighttime duration from the respective column.
- Compute `ended_at_local = started_at_local + duration`.
- Keep environment text as `road_type` when possible.
- Store `Sean Ahern` as supervisor where present.

For `log.txt`:

- Accept both `start only + duration` and `start-end + duration` formats.
- If a row includes start and end but no duration, compute from timestamps.
- If a row includes duration but no end, compute end from start plus duration.
- If a row lacks enough information to compute duration, import it as a failed
  row in `import_rows` and require manual completion in the UI.

The last line in `records/log.txt` currently appears incomplete:

- `2026-07-24 11:10-11:31: local and highways with wet roads, cloudy conditions`

That line has start/end times but no explicit minutes. The importer should
compute a 21 minute duration from the times and attach a warning noting that
duration was inferred from timestamps rather than written explicitly.

### Authoritative-record rule

After initial seed, the database is authoritative.

- The original source files are historical inputs, not ongoing truth.
- All later edits happen through the app or explicit import.
- Every imported row keeps provenance so disputes can be traced back.

## Day/Night Classification

Night driving must be computed from entered timestamps, not manually toggled.

### Time basis

- All times are local to `America/New_York`.
- Store and compute against timezone-aware datetimes.
- Daylight saving time versus standard time is handled by the timezone database,
  not by custom logic.

### Sunrise/sunset rule

For each drive date:

- obtain sunrise and sunset for the local area
- define daytime start as `sunrise - 15 minutes`
- define daytime end as `sunset + 15 minutes`
- any portion outside that daytime window counts as night minutes

Implications:

- A single drive can contribute to both day and night tallies.
- The system should split each drive minute-by-minute or at least by boundary
  intersections to compute exact day/night minutes.

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

- end time is before start time
- duration is zero or negative
- required fields are missing for a completed drive

### Soft warnings

Allow save, but show prominent warnings when:

- duration exceeds 5 hours
- drive crosses midnight
- drive overlaps another saved drive
- weekly countable total would exceed 10 hours for that week
- imported row required inferred duration or other assumptions

### Weekly cap handling

The DMV form states that no more than 10 hours per week may count toward the 60.

The two totals have different meanings:

- `actual_total_minutes` is every valid minute recorded in the log.
- `countable_total_minutes` is the amount that can count toward the DMV's
  60-hour requirement after limiting each week to 10 hours.

For example, if 12 hours were logged in one week, the actual total would
increase by 12 hours but the DMV-countable total would increase by at most
10 hours.

Recommended behavior:

- keep `actual_total_minutes` as the sum of all drives
- also compute `countable_total_minutes` using the 10-hour-per-week cap
- show the DMV-countable total as the primary 60-hour progress value
- show the actual total and a clear weekly-cap warning only if the values diverge
- use `countable_total_minutes` for readiness status

The supplied form does not define the week boundary. Use a documented calendar
week convention in version 1 and keep that convention configurable. Before
final DMV submission, any week over 10 hours should be reviewed manually.

## Web Interface

The main usage mode is phone-first while seated in the passenger seat. The UI
should favor large targets, high contrast, and minimal typing.

### 1. Dashboard

Primary landing page.

Shows:

- circular progress gauge for `countable_total / 60 hours`
- separate night progress gauge or clear secondary stat for `night / 10 hours`
- numeric totals:
  `countable total`, `actual total`, `night total`, `remaining`
- active live-drive banner if a drive is in progress
- quick actions:
  `Start a drive`, `Add drive manually`, `View drives`, `Import`, `Export`

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
- delete an incorrect drive from the details view

Displayed row summary:

- date
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

- date
- start time
- end time
- supervisor name
- supervisor DL number
- supervisor DL state
- road type
- weather
- notes

Supervisor DL number and state are configuration-backed defaults, but must
remain optional until the family's license details are available. The UI should
identify missing supervisor-license details before generating the DMV-facing
report rather than blocking ordinary drive entry.

On submit:

- show computed duration
- show computed day/night split
- show warnings before final confirm if needed

### 4. Live drive

Entry point:

- tap `Start a drive`

At start:

- record current local timestamp
- optionally capture default supervisor immediately

During active drive:

- dashboard shows elapsed time and `End drive` / `Cancel drive`

End flow:

- tap `End drive`
- record current local timestamp
- prompt only for remaining metadata:
  `road_type`, `weather`, `notes`, optional supervisor confirmation
- show warnings and computed totals before final save

Cancel flow:

- tap `Cancel drive`
- require confirmation
- remove the active live drive without creating a completed drive

### 5. Import/export pages

Web actions:

- export all non-deleted drives to CSV
- import CSV backup
- show import summary with created rows, skipped rows, warnings, and failures

## CSV Format

The CSV should be a stable archival format, not a UI dump.

Recommended columns:

- `id`
- `driver_name`
- `supervisor_name`
- `supervisor_dl_number`
- `supervisor_dl_state`
- `started_at_local`
- `ended_at_local`
- `duration_minutes`
- `day_minutes`
- `night_minutes`
- `road_type`
- `weather`
- `notes`
- `source`
- `source_reference`
- `deleted_at`

Rules:

- export in UTF-8
- use ISO-like local datetime strings with timezone offset
- import should validate schema version and report row-level errors
- import should support either create-only mode or replace-from-backup mode

For safety, first version should implement:

- `export`
- `import append`

Add full replace/restore only after a backup/restore workflow is tested.

## CLI Interface

Provide simple commands runnable from this repo.

Recommended command surface:

- `./driving-log serve`
- `./driving-log start`
- `./driving-log stop`
- `./driving-log restart`
- `./driving-log status`
- `./driving-log seed`
- `./driving-log export --out backup.csv`
- `./driving-log import --in backup.csv`

Implementation options:

- a small shell wrapper that calls the Python module
- or a Python CLI via `typer`

The commands should be thin wrappers around the app's real service/import logic
so behavior stays consistent between CLI and web.

## Hosting And Persistence

Constraints:

- do not conflict with the existing service on port 80
- primary access will be over Tailscale

Recommended runtime:

- bind the app locally on a non-80 port such as `127.0.0.1:8765`
- expose it through Tailscale Serve or an equivalent forwarding rule
- keep the internal app port configurable

Persistence across reboot:

- install a user-level `systemd` unit
- enable lingering for the user if needed so the service survives logout
- provide CLI helpers that wrap `systemctl --user`

Recommended service units:

- `driving-log-web.service`
- optional `driving-log-import.timer` later for scheduled form ingestion

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
- output: normalized drive candidate plus warnings/errors

That same interface can be used for:

- seed PDF rows
- `log.txt`
- CSV backup import
- future Microsoft rows

### Suggested spreadsheet fields

The future form should collect:

- date
- start time
- end time
- supervisor name
- road type
- weather
- notes

Night/day should still be computed by the app.

### Scraping versus API

Prefer structured spreadsheet export or API access over brittle HTML scraping if
Microsoft tooling allows it. The design should keep the importer behind a single
adapter so the acquisition method can change without touching validation or
storage logic.

## Security / Access

Primary audience is the household, so keep the first version simple.

Recommended first version:

- bind to localhost only
- rely on Tailscale exposure for remote access
- optional shared secret or basic auth if the forwarded endpoint is broader

For the Microsoft-form ingestion path, credentials should live outside the repo
in environment variables or a local config file excluded from version control.

## Reporting / DMV Export

In addition to CSV backup, the app should produce a DMV-friendly view:

- chronological table matching the `DL-4A` columns
- total day hours
- total night hours
- grand total

Future enhancement:

- generate a filled PDF or print-friendly page aligned to `DL-4A`

That is not required for the authoritative-record design, but the data model
should support it directly.

## Open Issues Identified From Current Seed Data

These should be handled explicitly during implementation:

1. The final `records/log.txt` row has no written duration, only start/end
   times and notes. Importer should infer 21 minutes and mark the inference.
2. The Road Ready PDF includes duplicated-looking timestamps in at least one
   place (`08/10/2025 6:27 PM` appears twice). Treat as potential duplicates,
   not automatic deletion.
3. The Road Ready PDF and `log.txt` may overlap in chronology in future
   revisions. Provenance plus overlap warnings are required.
4. The DMV's 10-hours-per-week counting rule should be represented in the
   readiness calculation, not buried in notes.

## Recommended Delivery Phases

### Phase 1

- database schema
- seed import
- dashboard
- manual entry
- drive list and delete
- CSV export/import
- day/night computation
- warnings engine

### Phase 2

- live-drive start/end/cancel
- user-level `systemd` service helpers
- Tailscale-forwarded deployment docs

### Phase 3

- print/DMV export view
- Microsoft Form spreadsheet ingestion
- scheduled importer

## Configuration Decisions

- Sunrise/sunset location: Apex, North Carolina.
- Timezone: `America/New_York`.
- Supervisor DL number and state: not yet available; keep nullable until
  supplied and warn before DMV-facing export.
- Dashboard: show DMV-countable progress toward 60 hours. Show the actual total
  separately only when a weekly cap causes the values to differ.

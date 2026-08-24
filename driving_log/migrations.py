from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


SCHEMA_V1 = """
CREATE TABLE configuration (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE import_batches (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_name TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    format_version TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    raw_snapshot BLOB NOT NULL,
    status TEXT NOT NULL,
    summary_json TEXT NOT NULL
);

CREATE TABLE live_drives (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    request_payload_hash TEXT NOT NULL,
    driver_name TEXT NOT NULL,
    supervisor_name TEXT,
    started_at_utc TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    started_utc_offset_minutes INTEGER NOT NULL,
    started_from TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active','ending','completed','cancelled')),
    provisional_ended_at_utc TEXT,
    provisional_ended_utc_offset_minutes INTEGER,
    end_request_id TEXT UNIQUE,
    completed_drive_id TEXT UNIQUE,
    finalization_payload_hash TEXT,
    finalized_at TEXT,
    created_at TEXT NOT NULL,
    CHECK (
      (status = 'active' AND provisional_ended_at_utc IS NULL)
      OR status IN ('ending','completed','cancelled')
    )
);
CREATE UNIQUE INDEX one_open_live_drive
ON live_drives((1)) WHERE status IN ('active','ending');

CREATE TABLE drives (
    id TEXT PRIMARY KEY,
    request_id TEXT UNIQUE,
    request_payload_hash TEXT,
    live_drive_id TEXT UNIQUE REFERENCES live_drives(id),
    driver_name TEXT NOT NULL,
    supervisor_name TEXT,
    supervisor_dl_number TEXT,
    supervisor_dl_state TEXT,
    started_at_utc TEXT NOT NULL,
    ended_at_utc TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    started_utc_offset_minutes INTEGER NOT NULL,
    ended_utc_offset_minutes INTEGER NOT NULL,
    duration_minutes INTEGER NOT NULL CHECK (duration_minutes > 0),
    day_minutes INTEGER NOT NULL CHECK (day_minutes >= 0),
    night_minutes INTEGER NOT NULL CHECK (night_minutes >= 0),
    solar_calculation_version TEXT NOT NULL,
    solar_latitude REAL NOT NULL,
    solar_longitude REAL NOT NULL,
    tzdata_version TEXT NOT NULL,
    road_type TEXT NOT NULL CHECK (road_type IN ('local','highway','mixed','unknown')),
    weather TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL CHECK (
      source IN ('manual','live_drive','seed_pdf','seed_log_txt','microsoft_form')
    ),
    source_reference TEXT,
    import_batch_id TEXT REFERENCES import_batches(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    deleted_at TEXT,
    CHECK (duration_minutes = day_minutes + night_minutes)
);

CREATE TABLE drive_warnings (
    id TEXT PRIMARY KEY,
    drive_id TEXT NOT NULL REFERENCES drives(id),
    warning_code TEXT NOT NULL,
    warning_message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(drive_id, warning_code, warning_message)
);

CREATE TABLE import_rows (
    id TEXT PRIMARY KEY,
    import_batch_id TEXT NOT NULL REFERENCES import_batches(id),
    source_type TEXT NOT NULL,
    source_instance_id TEXT NOT NULL,
    source_row_key TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    parsed_payload_json TEXT NOT NULL,
    result_drive_id TEXT REFERENCES drives(id),
    status TEXT NOT NULL,
    error_message TEXT,
    UNIQUE(source_type, source_instance_id, source_row_key)
);

CREATE TABLE audit_events (
    id TEXT PRIMARY KEY,
    request_id TEXT UNIQUE,
    action TEXT NOT NULL,
    payload_hash TEXT,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    outcome TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    actor_identity TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX drives_started_idx ON drives(started_at_utc) WHERE deleted_at IS NULL;
CREATE INDEX drives_import_idx ON drives(import_batch_id);
CREATE INDEX warnings_drive_idx ON drive_warnings(drive_id);
CREATE INDEX import_rows_batch_idx ON import_rows(import_batch_id);
"""

SCHEMA_V2 = """
ALTER TABLE drives ADD COLUMN end_location TEXT NOT NULL DEFAULT '';
"""

SCHEMA_V3 = """
CREATE TABLE supervisor_profiles (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL UNIQUE,
    dl_number TEXT NOT NULL,
    dl_state TEXT NOT NULL CHECK (
      length(dl_state) = 2 AND dl_state = upper(dl_state)
    ),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

SCHEMA_V4 = """
CREATE TABLE saved_locations (
    id TEXT PRIMARY KEY,
    owner_identity TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    latitude REAL NOT NULL CHECK (latitude >= -90 AND latitude <= 90),
    longitude REAL NOT NULL CHECK (longitude >= -180 AND longitude <= 180),
    radius_meters INTEGER NOT NULL CHECK (radius_meters BETWEEN 10 AND 5000),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_identity, normalized_name)
);
CREATE INDEX saved_locations_owner_idx ON saved_locations(owner_identity, name COLLATE NOCASE);
"""

MIGRATIONS = (
    Migration(1, "initial_schema", SCHEMA_V1),
    Migration(2, "drive_end_location", SCHEMA_V2),
    Migration(3, "supervisor_profiles", SCHEMA_V3),
    Migration(4, "saved_locations", SCHEMA_V4),
)
LATEST_SCHEMA_VERSION = MIGRATIONS[-1].version

from __future__ import annotations

import math
import sqlite3
import uuid
from typing import cast

from driving_log.db import Database, utc_now_text
from driving_log.records import ConflictError, NotFoundError, payload_hash


class SavedLocationService:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _validated(
        name: str, latitude: float, longitude: float, radius_meters: int
    ) -> tuple[str, str, float, float, int]:
        display_name = " ".join(name.split())
        if not display_name or len(display_name) > 100:
            raise ValueError("location name is required and must be at most 100 characters")
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ValueError("location coordinates are invalid")
        if not 10 <= radius_meters <= 5000:
            raise ValueError("location radius must be between 10 and 5,000 meters")
        return display_name, display_name.casefold(), latitude, longitude, radius_meters

    def list_for_owner(self, owner_identity: str | None) -> list[sqlite3.Row]:
        connection = self.database.connect_readonly()
        try:
            return connection.execute(
                "SELECT * FROM saved_locations WHERE owner_identity=? "
                "ORDER BY name COLLATE NOCASE, id",
                (owner_identity or "",),
            ).fetchall()
        finally:
            connection.close()

    def create(
        self,
        *,
        name: str,
        latitude: float,
        longitude: float,
        radius_meters: int,
        owner_identity: str | None,
        request_id: str,
        actor_identity: str | None,
    ) -> sqlite3.Row:
        display_name, normalized, lat, lon, radius = self._validated(
            name, latitude, longitude, radius_meters
        )
        owner = owner_identity or ""
        location_id = str(uuid.uuid4())
        digest = payload_hash(
            {
                "action": "saved_location.create",
                "owner": owner,
                "name": display_name,
                "latitude": lat,
                "longitude": lon,
                "radius_meters": radius,
            }
        )
        with self.database.transaction() as connection:
            prior = connection.execute(
                "SELECT action, payload_hash, entity_id FROM audit_events WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if prior:
                if prior["action"] != "saved_location.create" or prior["payload_hash"] != digest:
                    raise ConflictError("request ID was already used with different location data")
                return self.get(
                    str(prior["entity_id"]), owner_identity=owner, connection=connection
                )
            now = utc_now_text()
            try:
                connection.execute(
                    """INSERT INTO saved_locations
                    (id, owner_identity, name, normalized_name, latitude, longitude,
                     radius_meters, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (location_id, owner, display_name, normalized, lat, lon, radius, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("a saved location already has that name") from exc
            self.database.audit(
                connection,
                event_id=str(uuid.uuid4()),
                request_id=request_id,
                action="saved_location.create",
                payload_hash=digest,
                entity_type="saved_location",
                entity_id=location_id,
                outcome="created",
                metadata={"name": display_name, "radius_meters": radius},
                actor_identity=actor_identity,
            )
            return self.get(location_id, owner_identity=owner, connection=connection)

    def get(
        self,
        location_id: str,
        *,
        owner_identity: str | None,
        connection: sqlite3.Connection | None = None,
    ) -> sqlite3.Row:
        selected = connection or self.database.connect_readonly()
        try:
            row = selected.execute(
                "SELECT * FROM saved_locations WHERE id=? AND owner_identity=?",
                (location_id, owner_identity or ""),
            ).fetchone()
            if not row:
                raise NotFoundError("saved location not found")
            return cast(sqlite3.Row, row)
        finally:
            if connection is None:
                selected.close()

    def delete(
        self,
        location_id: str,
        *,
        owner_identity: str | None,
        request_id: str,
        actor_identity: str | None,
    ) -> None:
        owner = owner_identity or ""
        digest = payload_hash({"action": "saved_location.delete", "location_id": location_id})
        with self.database.transaction() as connection:
            prior = connection.execute(
                "SELECT action, payload_hash FROM audit_events WHERE request_id=?", (request_id,)
            ).fetchone()
            if prior:
                if prior["action"] != "saved_location.delete" or prior["payload_hash"] != digest:
                    raise ConflictError("request ID was already used for another mutation")
                return
            current = self.get(location_id, owner_identity=owner, connection=connection)
            connection.execute(
                "DELETE FROM saved_locations WHERE id=? AND owner_identity=?", (location_id, owner)
            )
            self.database.audit(
                connection,
                event_id=str(uuid.uuid4()),
                request_id=request_id,
                action="saved_location.delete",
                payload_hash=digest,
                entity_type="saved_location",
                entity_id=location_id,
                outcome="deleted",
                metadata={"name": current["name"]},
                actor_identity=actor_identity,
            )

    def match(self, *, latitude: float, longitude: float, owner_identity: str | None) -> str | None:
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ValueError("location coordinates are invalid")
        owner = owner_identity or ""
        connection = self.database.connect_readonly()
        try:
            rows = connection.execute(
                "SELECT * FROM saved_locations WHERE owner_identity IN (?, '') "
                "ORDER BY owner_identity DESC, radius_meters ASC, name COLLATE NOCASE",
                (owner,),
            ).fetchall()
        finally:
            connection.close()
        matches = [
            (
                self._distance_meters(
                    latitude, longitude, float(row["latitude"]), float(row["longitude"])
                ),
                row,
            )
            for row in rows
        ]
        within = [
            (distance, row) for distance, row in matches if distance <= int(row["radius_meters"])
        ]
        return str(min(within, key=lambda item: item[0])[1]["name"]) if within else None

    @staticmethod
    def _distance_meters(
        first_lat: float, first_lon: float, second_lat: float, second_lon: float
    ) -> float:
        lat_delta = math.radians(second_lat - first_lat)
        lon_delta = math.radians(second_lon - first_lon)
        first = math.radians(first_lat)
        second = math.radians(second_lat)
        a = (
            math.sin(lat_delta / 2) ** 2
            + math.cos(first) * math.cos(second) * math.sin(lon_delta / 2) ** 2
        )
        return 6_371_000 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

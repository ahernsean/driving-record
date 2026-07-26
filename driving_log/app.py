from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException

from driving_log import __version__
from driving_log.archive import application_lock
from driving_log.config import Settings
from driving_log.db import Database
from driving_log.logging_config import configure_logging
from driving_log.web import register_web


def create_app(settings: Settings | None = None) -> FastAPI:
    selected = settings or Settings.from_env()
    database = Database(selected.database_path, selected.archive_dir)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        selected.ensure_directories()
        with application_lock(selected.state_dir, exclusive=False):
            # Liveness remains available while readiness explains the safe refusal.
            with suppress(Exception):
                database.initialize()
            yield

    app = FastAPI(title="Daniel Driving Log", version=__version__, lifespan=lifespan)
    app.state.settings = selected
    app.state.database = database

    @app.get("/health/live")
    async def live() -> dict[str, object]:
        return {"status": "live", "version": __version__}

    @app.get("/health/ready")
    async def ready() -> dict[str, object]:
        status = database.health()
        if not status["ready"]:
            raise HTTPException(status_code=503, detail=status)
        return {"status": "ready", **status}

    register_web(app, selected, database)
    return app


app = create_app()

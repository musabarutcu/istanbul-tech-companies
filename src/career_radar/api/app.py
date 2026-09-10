"""FastAPI uygulaması.

Boru hattı ile arayüz arasındaki tek sözleşme burası. `pipeline/` bu modülü import
etmez; bağımlılık tek yönlü, böylece boru hattı arayüz olmadan da (CLI'dan) koşar.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .. import db
from .routes_companies import router as companies_router

PROJECT_ROOT = Path(__file__).resolve().parents[3]
STATIC_DIR = PROJECT_ROOT / "static"
DB_PATH = PROJECT_ROOT / "data" / "radar.db"


def create_app(db_path: Path | str | None = None) -> FastAPI:
    load_dotenv(PROJECT_ROOT / ".env")
    db.configure(db_path or DB_PATH)
    db.init()

    app = FastAPI(title="Career Radar", version="0.1.0", docs_url="/api/docs")
    app.include_router(companies_router)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()

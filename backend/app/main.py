from pathlib import Path
import sys

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.api import dashboard, data_analysis, health, import_preview, imports, user
from app.core.config import get_settings
from app.core.database import initialize_database
from app.services.smartlab_service import initialize_engine, start_embed_server

settings = get_settings()

app = FastAPI(title=settings.app_name, version="0.1.0")
initialize_database()
initialize_engine()
start_embed_server()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/api")
app.include_router(dashboard.router, prefix="/api")
app.include_router(user.router, prefix="/api")
app.include_router(import_preview.router, prefix="/api")
app.include_router(imports.router, prefix="/api")
app.include_router(data_analysis.router, prefix="/api")

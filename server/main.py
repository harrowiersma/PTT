import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from server.api.auth import router as auth_router
from server.api.channels import router as channels_router
from server.api.status import router as status_router
from server.api.users import router as users_router
from server.config import settings
from server.database import init_db
from server.murmur.client import MurmurClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

murmur_client: MurmurClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global murmur_client

    logger.info("Starting PTT Admin Service")

    # Initialize database
    await init_db()
    logger.info("Database initialized")

    # Connect to Murmur
    murmur_client = MurmurClient(
        host=settings.murmur_ice_host,
        port=settings.murmur_ice_port,
        secret=settings.murmur_ice_secret,
    )
    connected = murmur_client.connect()
    if connected:
        logger.info("Connected to Murmur ICE interface")
    else:
        logger.warning(
            "Could not connect to Murmur ICE. "
            "Admin service will run without Murmur integration. "
            "Users and channels will be managed in the database only."
        )

    yield

    # Cleanup
    if murmur_client:
        murmur_client.disconnect()
    logger.info("PTT Admin Service stopped")


app = FastAPI(
    title=settings.app_name,
    description="Admin service for self-hosted PTT server (Murmur backend)",
    version="0.1.0",
    lifespan=lifespan,
)

# API routes
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(channels_router)
app.include_router(status_router)

# Serve dashboard static files
dashboard_dir = Path(__file__).parent / "dashboard"
if dashboard_dir.exists():
    app.mount("/", StaticFiles(directory=str(dashboard_dir), html=True), name="dashboard")

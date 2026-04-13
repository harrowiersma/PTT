import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from server.api.admin import router as admin_router
from server.api.auth import router as auth_router
from server.api.channels import router as channels_router
from server.api.dispatch import router as dispatch_router
from server.api.gps import router as gps_router
from server.api.sos import router as sos_router
from server.api.status import router as status_router
from server.api.users import router as users_router
from server.api.weather import router as weather_router
from server.config import settings, validate_settings
from server.database import init_db
from server.murmur.client import MurmurClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting PTT Admin Service")
    validate_settings()

    # Initialize database
    await init_db()
    logger.info("Database initialized")

    # Connect to Murmur
    client = MurmurClient(
        host=settings.murmur_ice_host,
        port=settings.murmur_ice_port,
        secret=settings.murmur_ice_secret,
        mumble_host=settings.murmur_host,
        mumble_port=settings.murmur_port,
    )
    connected = client.connect()
    app.state.murmur_client = client

    if connected and client.has_mumble:
        # Set up SOS acknowledgement via text message in Emergency channel
        def on_sos_acknowledge(username: str):
            """Called when someone types OK/ACKNOWLEDGE in Emergency channel."""
            import asyncio
            from server.api.sos import _restore_channels, _get_murmur
            from sqlalchemy import select, update
            from server.database import async_session
            from server.models import User, SOSEvent

            async def _do_acknowledge():
                # Check if this user is an admin
                async with async_session() as db:
                    result = await db.execute(
                        select(User).where(User.username == username)
                    )
                    user = result.scalar_one_or_none()
                    if not user or not user.is_admin:
                        logger.info("SOS ack from '%s' ignored (not an admin)", username)
                        if client.has_mumble and client._mumble:
                            for cid, ch in client._mumble.channels.items():
                                if ch["name"] == "Emergency":
                                    client._mumble.channels[cid].send_text_message(
                                        f"<i>{username}: only admins can acknowledge SOS</i>"
                                    )
                                    break
                        return

                    # Acknowledge all active SOS events
                    from datetime import datetime, timezone
                    await db.execute(
                        update(SOSEvent)
                        .where(SOSEvent.acknowledged == False)
                        .values(
                            acknowledged=True,
                            acknowledged_by=username,
                            acknowledged_at=datetime.now(timezone.utc),
                        )
                    )
                    await db.commit()

                # Restore channels
                murmur = _get_murmur()
                _restore_channels(murmur)
                logger.info("SOS acknowledged by admin '%s' via Emergency channel text", username)

            # Run async function from sync callback
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.ensure_future(_do_acknowledge())
                else:
                    loop.run_until_complete(_do_acknowledge())
            except Exception as e:
                logger.error("Error in SOS acknowledge callback: %s", e)

        client.set_sos_acknowledge_callback(on_sos_acknowledge)
        logger.info("SOS text acknowledgement enabled (admin types OK in Emergency channel)")

    # Start Weather ATIS bot
    if connected and client.has_mumble:
        try:
            from server.weather_bot import WeatherBot
            from server.traccar_client import TraccarClient
            weather_bot = WeatherBot(client, TraccarClient)
            weather_bot.start()
            logger.info("Weather ATIS bot started")
        except Exception as e:
            logger.warning("Weather bot failed to start: %s", e)

    if connected:
        logger.info("Connected to Murmur via pymumble")
    else:
        logger.warning(
            "Could not connect to Murmur ICE. "
            "Admin service will run without Murmur integration. "
            "Users and channels will be managed in the database only."
        )

    yield

    # Cleanup
    if app.state.murmur_client:
        app.state.murmur_client.disconnect()
    logger.info("PTT Admin Service stopped")


app = FastAPI(
    title=settings.app_name,
    description="Admin service for self-hosted PTT server (Murmur backend)",
    version="0.1.0",
    lifespan=lifespan,
)

# API routes
app.include_router(admin_router)
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(channels_router)
app.include_router(status_router)
app.include_router(gps_router)
app.include_router(sos_router)
app.include_router(dispatch_router)
app.include_router(weather_router)

# Serve dashboard static files
dashboard_dir = Path(__file__).parent / "dashboard"
if dashboard_dir.exists():
    app.mount("/", StaticFiles(directory=str(dashboard_dir), html=True), name="dashboard")

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from server.api.admin import router as admin_router
from server.api.loneworker import router as loneworker_router
from server.api.auth import router as auth_router
from server.api.bulk import router as bulk_router
from server.api.call_groups import router as call_groups_router
from server.api.call_logs import router as call_logs_router
from server.api.metrics import router as metrics_router
from server.api.channels import router as channels_router
from server.api.dispatch import router as dispatch_router
from server.api.dispatch_locations import router as dispatch_locations_router
from server.api.dispatch_settings import router as dispatch_settings_router
from server.api.dispatch_messages import router as dispatch_messages_router
from server.api.features import router as features_router
from server.api.gps import router as gps_router
from server.api.provisioning import router as provisioning_router
from server.api.sip import router as sip_router
from server.api.sip import internal_router as sip_internal_router
from server.api.sos import router as sos_router
from server.api.status import router as status_router
from server.api.user_status import router as user_status_router
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
    logger.info("Starting openPTT TRX-Server")
    validate_settings()

    # Initialize database
    await init_db()
    logger.info("Database initialized")

    # Refresh feature-flag cache once at startup so conditional task
    # launches below see the operator's configured state.
    from server.database import async_session as _async_session
    from server.features import is_enabled as _feature_enabled
    from server.features import refresh_cache as _refresh_features
    async with _async_session() as _db:
        await _refresh_features(_db)

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
    if connected and client.has_mumble and _feature_enabled("weather"):
        try:
            from server.weather_bot import WeatherBot
            from server.traccar_client import TraccarClient
            weather_bot = WeatherBot(client, TraccarClient)
            await weather_bot.start()
            app.state.weather_bot = weather_bot
            logger.info("Weather ATIS bot started")
        except Exception as e:
            logger.warning("Weather bot failed to start: %s", e)
    elif not _feature_enabled("weather"):
        logger.info("Weather ATIS disabled by feature flag — skipping bot")

    # Start lone worker overdue checker (voice reminders for overdue workers)
    if connected and client.has_mumble and _feature_enabled("lone_worker"):
        try:
            from server.api.loneworker import start_overdue_checker
            start_overdue_checker(client)
        except Exception as e:
            logger.warning("Lone worker checker failed to start: %s", e)
    elif not _feature_enabled("lone_worker"):
        logger.info("Lone worker disabled by feature flag — checker not started")

    # Start Phone-channel ACL eligible-set poller. Refreshes the
    # MurmurClient's in-memory username allowlist every 30 s from the
    # users table so the PYMUMBLE_CLBK_USERUPDATED callback can reject
    # unauthorized entries without a cross-thread DB query.
    phone_acl_task = None
    if connected and client.has_mumble and _feature_enabled("sip"):
        import asyncio
        from sqlalchemy import select
        from server.database import async_session
        from server.models import User

        async def _refresh_phone_eligibles():
            while True:
                try:
                    async with async_session() as db:
                        result = await db.execute(
                            select(User.username).where(
                                User.can_answer_calls.is_(True),
                                User.is_active.is_(True),
                            )
                        )
                        eligibles = {row[0] for row in result.all()}
                    client.update_phone_eligible(eligibles)
                except Exception as e:
                    logger.warning("phone-acl: eligible refresh failed: %s", e)
                await asyncio.sleep(30)

        try:
            phone_acl_task = asyncio.create_task(_refresh_phone_eligibles())
            app.state.phone_acl_task = phone_acl_task
            logger.info("Phone ACL eligible-set poller started (30 s)")
        except Exception as e:
            logger.warning("Phone ACL poller failed to start: %s", e)
    elif not _feature_enabled("sip"):
        logger.info("SIP disabled by feature flag — Phone ACL poller not started")

    # Start call-group ACL poller. Mirrors phone-acl but covers every
    # channel with a call_group_id. Always on (not feature-gated) — the
    # default NULL group_id preserves visible-to-all behaviour so running
    # the poller on a fresh DB is a no-op.
    call_group_task = None
    if connected and client.has_mumble:
        import asyncio
        from sqlalchemy import select
        from server.database import async_session
        from server.models import Channel, User, UserCallGroup

        async def _refresh_call_groups():
            while True:
                try:
                    async with async_session() as db:
                        rows = (await db.execute(
                            select(User.username, User.is_admin, UserCallGroup.call_group_id)
                            .outerjoin(UserCallGroup, UserCallGroup.user_id == User.id)
                        )).all()
                        user_groups: dict[str, set[int]] = {}
                        user_admin: dict[str, bool] = {}
                        for username, is_admin, gid in rows:
                            lc = username.lower()
                            user_admin[lc] = bool(is_admin)
                            if gid is not None:
                                user_groups.setdefault(lc, set()).add(gid)
                            else:
                                user_groups.setdefault(lc, set())

                        # Channels keyed by their Mumble id — that's what
                        # the bounce check sees from USERUPDATED.
                        crows = (await db.execute(
                            select(Channel.mumble_id, Channel.call_group_id)
                            .where(Channel.mumble_id.is_not(None))
                        )).all()
                        channel_groups = {mid: gid for (mid, gid) in crows}

                    client.update_call_group_state(
                        user_groups, channel_groups, user_admin,
                    )
                except Exception as e:
                    logger.warning("call-groups: refresh failed: %s", e)
                await asyncio.sleep(30)

        try:
            call_group_task = asyncio.create_task(_refresh_call_groups())
            app.state.call_group_task = call_group_task
            logger.info("Call-group state poller started (30 s)")
        except Exception as e:
            logger.warning("Call-group poller failed to start: %s", e)

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
    weather_bot = getattr(app.state, "weather_bot", None)
    if weather_bot is not None:
        weather_bot.stop()
    phone_acl_task = getattr(app.state, "phone_acl_task", None)
    if phone_acl_task is not None:
        phone_acl_task.cancel()
    call_group_task = getattr(app.state, "call_group_task", None)
    if call_group_task is not None:
        call_group_task.cancel()
    if app.state.murmur_client:
        app.state.murmur_client.disconnect()
    logger.info("openPTT TRX-Server stopped")


app = FastAPI(
    title=settings.app_name,
    description="openPTT TRX-Server admin dashboard",
    version="0.1.0",
    lifespan=lifespan,
)

# API routes
app.include_router(admin_router)
app.include_router(auth_router)
# user_status_router must be registered BEFORE users_router so its
# concrete /api/users/status path wins over /api/users/{user_id}.
app.include_router(user_status_router)
app.include_router(users_router)
app.include_router(channels_router)
app.include_router(call_groups_router)
app.include_router(status_router)
app.include_router(gps_router)
app.include_router(sip_router)
app.include_router(sip_internal_router)
app.include_router(provisioning_router)
app.include_router(sos_router)
app.include_router(dispatch_router)
app.include_router(dispatch_locations_router)
app.include_router(dispatch_settings_router)
app.include_router(dispatch_messages_router)
app.include_router(weather_router)
app.include_router(bulk_router)
app.include_router(loneworker_router)
app.include_router(metrics_router)
app.include_router(features_router)
app.include_router(call_logs_router)

# Serve dashboard static files
dashboard_dir = Path(__file__).parent / "dashboard"
if dashboard_dir.exists():
    app.mount("/", StaticFiles(directory=str(dashboard_dir), html=True), name="dashboard")

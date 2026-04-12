from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Admin service
    app_name: str = "PTT Admin"
    secret_key: str = "change-me-in-production"
    access_token_expire_minutes: int = 60
    admin_username: str = "admin"
    admin_password: str = "admin"

    # Database
    database_url: str = "postgresql+asyncpg://ptt:ptt@localhost:5432/ptt"
    database_url_sync: str = "postgresql+psycopg2://ptt:ptt@localhost:5432/ptt"

    # Murmur ICE
    murmur_ice_host: str = "127.0.0.1"
    murmur_ice_port: int = 6502
    murmur_ice_secret: str = ""

    # Murmur connection (Docker service name)
    murmur_host: str = "murmur"
    murmur_port: int = 64738

    # Server public address (for QR codes)
    public_host: str = "localhost"
    public_port: int = 443

    # Traccar GPS tracking
    traccar_api_url: str = "http://traccar:8082"
    traccar_admin_email: str = "admin@ptt.local"
    traccar_admin_password: str = "admin"

    # SOS notifications
    sos_webhook_url: str = ""
    sos_smtp_host: str = ""
    sos_smtp_port: int = 587
    sos_smtp_user: str = ""
    sos_smtp_password: str = ""
    sos_email_to: str = ""

    model_config = {"env_prefix": "PTT_", "env_file": ".env"}


settings = Settings()


def validate_settings() -> None:
    """Refuse to start with insecure defaults. Called during app lifespan."""
    import sys
    import logging

    logger = logging.getLogger(__name__)
    fatal = False

    if settings.secret_key == "change-me-in-production":
        logger.critical("PTT_SECRET_KEY is the default value. Set a real secret in .env")
        fatal = True
    if settings.admin_password == "admin":
        logger.critical("PTT_ADMIN_PASSWORD is 'admin'. Set a real password in .env")
        fatal = True

    if fatal:
        logger.critical("Refusing to start with insecure defaults. Create .env from .env.example or run scripts/install.sh")
        sys.exit(1)

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

    # Murmur connection
    murmur_host: str = "127.0.0.1"
    murmur_port: int = 64738

    # Server public address (for QR codes)
    public_host: str = "localhost"
    public_port: int = 443

    model_config = {"env_prefix": "PTT_", "env_file": ".env"}


settings = Settings()

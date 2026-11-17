"""
Application configuration using Pydantic Settings.
Loads configuration from environment variables with validation.
"""

from datetime import datetime, timezone
from typing import List, Optional
from pydantic import Field, field_validator, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings with environment variable support."""

    # Application
    APP_NAME: str = "TrueLive Portal API"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    ENVIRONMENT: str = "production"

    # API Configuration
    API_V1_PREFIX: str = "/api/v1"
    SECRET_KEY: str = Field(..., min_length=32)
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Database
    DATABASE_URL: PostgresDsn
    DATABASE_POOL_SIZE: int = 20
    DATABASE_MAX_OVERFLOW: int = 40
    DATABASE_ECHO: bool = False

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_PASSWORD: Optional[str] = None

    # WebSocket
    WEBSOCKET_URL: str = "http://localhost:8080"
    WEBSOCKET_HOST: str = "0.0.0.0"
    WEBSOCKET_PORT: int = 8080

    # JWT for PC Authentication
    JWT_SECRET: str = Field(..., min_length=16)
    JWT_PC_TOKEN_EXPIRE_HOURS: int = 8760  # 1 year
    # Dual-secret rotation window (plan 06 D30, OD-23). During a rotation the
    # relay also verifies PC tokens with JWT_SECRET_PREVIOUS, but only while
    # the current time is before JWT_ROTATION_GRACE_UNTIL (ISO 8601, a naive
    # value is UTC; or Unix seconds). Without a window end the previous secret
    # is never accepted. Both come from .env only, like JWT_SECRET.
    JWT_SECRET_PREVIOUS: Optional[str] = None
    JWT_ROTATION_GRACE_UNTIL: Optional[datetime] = None
    # The relay re-issues a PC token that expires within this many days
    # (0 turns expiry renewal off; previous-secret renewal is unaffected).
    PC_TOKEN_RENEW_BEFORE_DAYS: int = Field(30, ge=0, le=365)

    @field_validator("JWT_SECRET_PREVIOUS", mode="before")
    @classmethod
    def _blank_previous_secret_is_none(cls, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if isinstance(value, str) and len(value) < 16:
            raise ValueError("JWT_SECRET_PREVIOUS must be at least 16 characters (like JWT_SECRET)")
        return value

    @field_validator("JWT_ROTATION_GRACE_UNTIL", mode="before")
    @classmethod
    def _blank_window_is_none(cls, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())  # Unix seconds
        return value

    @field_validator("JWT_ROTATION_GRACE_UNTIL", mode="after")
    @classmethod
    def _window_is_utc(cls, value):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    # Shared relay / deploy environment (plan 06 D28, X-18). docker-compose.yml
    # gives the API, the relay and both Celery services one env block, so all
    # four see the same values; app/core/startup_check.py logs them at start.
    # Fernet key for encrypted system_settings. Must be identical in every
    # process (NEW-47); without it, encrypted core.* settings refuse writes.
    SETTINGS_ENCRYPTION_KEY: Optional[str] = None
    # Shared secret of portal -> relay sender sessions (D12). Empty means the
    # relay runs legacy-open, with a warning.
    RELAY_SENDER_SECRET: str = ""
    # Kill switches and timers of the confirmed-deploy path (D14, D15, D17).
    DEPLOY_INJECT_CONFIG_ID: bool = True
    RELAY_STATION_REPORTS: bool = True  # "off" makes the report handlers return disabled
    DEPLOY_CATCHUP_ENABLED: bool = False
    DEPLOY_ACK_TIMEOUT_SEC: int = Field(300, ge=30, le=86400)
    # Engine.IO / Socket.IO packet logging in the relay (D12). Off by default:
    # packets carry tokens and layouts (NEW-56).
    RELAY_DEBUG_LOGGING: bool = False
    # OD-23 (b): refuse a PC token that differs from pcs.auth_token. Off by
    # default (OD-23 (a)): the relay registers the PC and flags token_outdated.
    # Turn it on only once the flag is clean fleet-wide (re-token through D30).
    RELAY_REQUIRE_CURRENT_TOKEN: bool = False

    # CORS (stored as string, parsed to list via property)
    CORS_ORIGINS_STR: str = "http://localhost:3000,http://localhost:8000,http://localhost:3001"

    @property
    def CORS_ORIGINS(self) -> List[str]:
        """Parse CORS origins from comma-separated string to list."""
        if isinstance(self.CORS_ORIGINS_STR, str):
            return [origin.strip() for origin in self.CORS_ORIGINS_STR.split(",")]
        return self.CORS_ORIGINS_STR

    # Celery
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/0"

    # Snapshot Configuration
    SNAPSHOT_MAX_AGE_HOURS: int = 24
    SNAPSHOT_CAPTURE_TIMEOUT: int = 10
    SNAPSHOT_MAX_WORKERS: int = 5
    SNAPSHOT_BATCH_TIME_LIMIT: int = 300

    # Background Tasks
    BACKGROUND_TASK_INTERVAL: int = 600  # 10 minutes

    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "json"

    # File Upload
    MAX_UPLOAD_SIZE_MB: int = 10
    UPLOAD_DIR: str = "/app/uploads"

    # Security
    BCRYPT_ROUNDS: int = 12
    PASSWORD_MIN_LENGTH: int = 8

    # Rate Limiting
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_PER_MINUTE: int = 60

    # Email Configuration (SMTP)
    SMTP_HOST: str = "mail.usvg.ai"
    SMTP_PORT: int = 587
    SMTP_USER: str = "info@usvg.ai"
    SMTP_PASSWORD: str = ""  # Set in .env file
    SMTP_FROM_EMAIL: str = "info@usvg.ai"
    SMTP_FROM_NAME: str = "TrueLive Portal"
    SMTP_USE_TLS: bool = True
    FRONTEND_URL: str = "http://localhost:3000"
    INVITATION_TOKEN_EXPIRY_HOURS: int = 72

    # Alerting feature — SMTP ingest + MinIO + downstream webhook
    ALERT_DOMAIN: str = "alerts.usvg.ai"
    ALERT_LMTP_SOCKET: str = "/var/run/truelive/ingest.sock"
    ALERT_RATE_LIMIT_PER_MINUTE: int = 60
    ALERT_MAX_MESSAGE_SIZE: int = 26214400  # 25 MB (matches Postfix message_size_limit)

    # MinIO / S3
    MINIO_ENDPOINT: str = "s3.usvg.ai"
    MINIO_ACCESS_KEY: Optional[str] = None
    MINIO_SECRET_KEY: Optional[str] = None
    MINIO_SECURE: bool = True
    MINIO_REGION: str = "us-east-1"
    MINIO_RAW_MAIL_BUCKET: str = "truelive-raw-mail"
    MINIO_ALERT_MEDIA_BUCKET: str = "truelive-alert-media"
    MINIO_PRESIGN_EXPIRY_DAYS: int = 7

    # Webhook delivery
    WEBHOOK_TIMEOUT_SECONDS: int = 5
    WEBHOOK_HMAC_TIMESTAMP_SKEW_SECONDS: int = 300  # 5 min replay-protection window
    WEBHOOK_RETRY_SCHEDULE_SECONDS: List[int] = Field(
        default_factory=lambda: [60, 300, 1800, 7200, 43200]  # 1m, 5m, 30m, 2h, 12h
    )

    # Retention (days)
    RETENTION_RAW_MAIL_DAYS: int = 90
    RETENTION_ALERTS_DAYS: int = 90
    RETENTION_ALERT_MEDIA_DAYS: int = 30
    RETENTION_WEBHOOK_DELIVERIES_DAYS: int = 30

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore"
    )

    @property
    def database_url_sync(self) -> str:
        """Get synchronous database URL as string."""
        return str(self.DATABASE_URL)

    @property
    def max_upload_size_bytes(self) -> int:
        """Get max upload size in bytes."""
        return self.MAX_UPLOAD_SIZE_MB * 1024 * 1024


# Create global settings instance
settings = Settings()

"""
Application configuration using Pydantic Settings.
Loads configuration from environment variables with validation.
"""

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

    # SureView Integration
    SUREVIEW_USERNAME: Optional[str] = None
    SUREVIEW_PASSWORD: Optional[str] = None
    SUREVIEW_API_URL: Optional[str] = None
    SUREVIEW_LOGIN_URL: Optional[str] = None

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

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
    APP_NAME: str = "Shomer Portal API"
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

    # CORS
    CORS_ORIGINS: List[str] = [
        "http://localhost:3000",
        "http://localhost:8000",
    ]

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, v):
        """Parse CORS origins from string or list."""
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",")]
        return v

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

"""
Seed default system settings into the database.

This module contains the default settings configuration and seeding logic.
"""

import logging
from sqlalchemy.orm import Session
from app.models.system_setting import SystemSetting

logger = logging.getLogger(__name__)

# Default settings configuration
DEFAULT_SETTINGS = [
    # SMTP Settings
    {
        "key": "smtp.host",
        "value": "",
        "category": "smtp",
        "description": "SMTP server hostname",
        "is_encrypted": False,
        "data_type": "string"
    },
    {
        "key": "smtp.port",
        "value": "587",
        "category": "smtp",
        "description": "SMTP server port",
        "is_encrypted": False,
        "data_type": "integer"
    },
    {
        "key": "smtp.username",
        "value": "",
        "category": "smtp",
        "description": "SMTP authentication username",
        "is_encrypted": False,
        "data_type": "string"
    },
    {
        "key": "smtp.password",
        "value": "",
        "category": "smtp",
        "description": "SMTP authentication password",
        "is_encrypted": True,
        "data_type": "string"
    },
    {
        "key": "smtp.from_email",
        "value": "",
        "category": "smtp",
        "description": "Default sender email address",
        "is_encrypted": False,
        "data_type": "string"
    },
    {
        "key": "smtp.from_name",
        "value": "Shomer Portal",
        "category": "smtp",
        "description": "Default sender display name",
        "is_encrypted": False,
        "data_type": "string"
    },
    {
        "key": "smtp.use_tls",
        "value": "true",
        "category": "smtp",
        "description": "Enable TLS encryption",
        "is_encrypted": False,
        "data_type": "boolean"
    },

    # SureView Settings
    {
        "key": "sureview.api_url",
        "value": "",
        "category": "sureview",
        "description": "SureView API base URL",
        "is_encrypted": False,
        "data_type": "string"
    },
    {
        "key": "sureview.username",
        "value": "",
        "category": "sureview",
        "description": "SureView API username",
        "is_encrypted": False,
        "data_type": "string"
    },
    {
        "key": "sureview.password",
        "value": "",
        "category": "sureview",
        "description": "SureView API password",
        "is_encrypted": True,
        "data_type": "string"
    },
    {
        "key": "sureview.sync_enabled",
        "value": "true",
        "category": "sureview",
        "description": "Enable automatic sync",
        "is_encrypted": False,
        "data_type": "boolean"
    },
    {
        "key": "sureview.sync_interval",
        "value": "300",
        "category": "sureview",
        "description": "Sync interval in seconds",
        "is_encrypted": False,
        "data_type": "integer"
    },

    # Tasks Settings
    {
        "key": "tasks.cleanup_enabled",
        "value": "true",
        "category": "tasks",
        "description": "Enable automatic cleanup tasks",
        "is_encrypted": False,
        "data_type": "boolean"
    },
    {
        "key": "tasks.cleanup_interval",
        "value": "86400",
        "category": "tasks",
        "description": "Cleanup interval in seconds (24h)",
        "is_encrypted": False,
        "data_type": "integer"
    },
    {
        "key": "tasks.audit_log_retention",
        "value": "90",
        "category": "tasks",
        "description": "Audit log retention days",
        "is_encrypted": False,
        "data_type": "integer"
    },

    # Snapshots Settings
    {
        "key": "snapshots.cache_enabled",
        "value": "true",
        "category": "snapshots",
        "description": "Enable snapshot caching",
        "is_encrypted": False,
        "data_type": "boolean"
    },
    {
        "key": "snapshots.cache_ttl",
        "value": "300",
        "category": "snapshots",
        "description": "Cache TTL in seconds",
        "is_encrypted": False,
        "data_type": "integer"
    },
    {
        "key": "snapshots.max_concurrent",
        "value": "10",
        "category": "snapshots",
        "description": "Max concurrent snapshot requests",
        "is_encrypted": False,
        "data_type": "integer"
    },
    {
        "key": "snapshots.timeout",
        "value": "30",
        "category": "snapshots",
        "description": "Snapshot request timeout in seconds",
        "is_encrypted": False,
        "data_type": "integer"
    },

    # WebSocket Settings
    {
        "key": "websocket.host",
        "value": "0.0.0.0",
        "category": "websocket",
        "description": "WebSocket server bind host",
        "is_encrypted": False,
        "data_type": "string"
    },
    {
        "key": "websocket.port",
        "value": "8080",
        "category": "websocket",
        "description": "WebSocket server port",
        "is_encrypted": False,
        "data_type": "integer"
    },
    {
        "key": "websocket.ping_interval",
        "value": "25",
        "category": "websocket",
        "description": "Ping interval in seconds",
        "is_encrypted": False,
        "data_type": "integer"
    },
    {
        "key": "websocket.ping_timeout",
        "value": "60",
        "category": "websocket",
        "description": "Ping timeout in seconds",
        "is_encrypted": False,
        "data_type": "integer"
    },

    # Security Settings
    {
        "key": "security.max_login_attempts",
        "value": "5",
        "category": "security",
        "description": "Max failed login attempts before lockout",
        "is_encrypted": False,
        "data_type": "integer"
    },
    {
        "key": "security.lockout_duration",
        "value": "900",
        "category": "security",
        "description": "Account lockout duration in seconds",
        "is_encrypted": False,
        "data_type": "integer"
    },
    {
        "key": "security.password_min_length",
        "value": "8",
        "category": "security",
        "description": "Minimum password length",
        "is_encrypted": False,
        "data_type": "integer"
    },
    {
        "key": "security.session_timeout",
        "value": "3600",
        "category": "security",
        "description": "Session timeout in seconds",
        "is_encrypted": False,
        "data_type": "integer"
    },

    # Token Settings
    {
        "key": "tokens.access_token_expire",
        "value": "30",
        "category": "tokens",
        "description": "Access token expiry in minutes",
        "is_encrypted": False,
        "data_type": "integer"
    },
    {
        "key": "tokens.refresh_token_expire",
        "value": "10080",
        "category": "tokens",
        "description": "Refresh token expiry in minutes (7 days)",
        "is_encrypted": False,
        "data_type": "integer"
    },
    {
        "key": "tokens.pc_token_expire",
        "value": "525600",
        "category": "tokens",
        "description": "PC auth token expiry in minutes (1 year)",
        "is_encrypted": False,
        "data_type": "integer"
    },
]


def seed_settings(db: Session, force: bool = False) -> dict:
    """
    Seed default settings into the database.

    Args:
        db: Database session
        force: If True, update existing settings with default values

    Returns:
        Dictionary with counts of created and skipped settings
    """
    created = 0
    skipped = 0
    updated = 0

    for setting_data in DEFAULT_SETTINGS:
        # Check if setting already exists
        existing = db.query(SystemSetting).filter(
            SystemSetting.key == setting_data["key"]
        ).first()

        if existing:
            if force:
                # Update existing setting
                for key, value in setting_data.items():
                    setattr(existing, key, value)
                updated += 1
            else:
                skipped += 1
            continue

        # Create new setting
        setting = SystemSetting(**setting_data)
        db.add(setting)
        created += 1

    db.commit()

    return {
        "created": created,
        "skipped": skipped,
        "updated": updated,
        "total": len(DEFAULT_SETTINGS)
    }

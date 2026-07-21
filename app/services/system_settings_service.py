"""
System settings service for managing runtime configuration.

Provides methods to read, update, and encrypt system settings with caching support.
"""

import logging
from typing import Optional, Dict, Any, List
from sqlalchemy.orm import Session
from cryptography.fernet import Fernet
import base64
import os

from app.models.system_setting import SystemSetting
from app.core.config import settings as app_settings

logger = logging.getLogger(__name__)


class SystemSettingsService:
    """Service for managing system settings with encryption and caching."""

    def __init__(self):
        """Initialize settings service with encryption key."""
        # Get encryption key from environment or generate one
        # In production, this should be stored securely in ENV
        encryption_key = os.getenv('SETTINGS_ENCRYPTION_KEY')
        if not encryption_key:
            # Generate a key for development (NOT for production!)
            encryption_key = Fernet.generate_key().decode()
            logger.warning(
                "No SETTINGS_ENCRYPTION_KEY found in environment. "
                "Using generated key (NOT suitable for production!)"
            )

        self.fernet = Fernet(encryption_key.encode() if isinstance(encryption_key, str) else encryption_key)
        self._cache: Dict[str, Any] = {}

    def _encrypt_value(self, value: str) -> str:
        """
        Encrypt a value using Fernet symmetric encryption.

        Args:
            value: Plain text value to encrypt

        Returns:
            Base64-encoded encrypted value
        """
        if not value:
            return value

        encrypted_bytes = self.fernet.encrypt(value.encode())
        return base64.b64encode(encrypted_bytes).decode()

    def _decrypt_value(self, encrypted_value: str) -> str:
        """
        Decrypt a value using Fernet symmetric encryption.

        Args:
            encrypted_value: Base64-encoded encrypted value

        Returns:
            Decrypted plain text value
        """
        if not encrypted_value:
            return encrypted_value

        try:
            encrypted_bytes = base64.b64decode(encrypted_value.encode())
            decrypted_bytes = self.fernet.decrypt(encrypted_bytes)
            return decrypted_bytes.decode()
        except Exception as e:
            logger.error(f"Failed to decrypt value: {e}")
            return encrypted_value  # Return as-is if decryption fails

    def _convert_value(self, value: str, data_type: str) -> Any:
        """
        Convert string value to appropriate Python type.

        Args:
            value: String value from database
            data_type: Target data type (string, integer, boolean)

        Returns:
            Converted value
        """
        if value is None or value == "":
            return None

        if data_type == "integer":
            return int(value)
        elif data_type == "boolean":
            return value.lower() in ('true', '1', 'yes', 'on')
        else:
            return value

    def get_setting(
        self,
        db: Session,
        key: str,
        default: Any = None,
        use_cache: bool = True
    ) -> Any:
        """
        Get a setting value by key.

        Args:
            db: Database session
            key: Setting key (e.g., 'smtp.host')
            default: Default value if setting not found
            use_cache: Whether to use cached value

        Returns:
            Setting value converted to appropriate type
        """
        # Check cache first
        if use_cache and key in self._cache:
            return self._cache[key]

        # Query database
        setting = db.query(SystemSetting).filter(SystemSetting.key == key).first()

        if not setting or setting.value is None:
            # Try to get from ENV as fallback
            env_key = key.upper().replace('.', '_')
            env_value = os.getenv(env_key, default)
            return env_value

        # Decrypt if needed
        value = setting.value
        if setting.is_encrypted:
            value = self._decrypt_value(value)

        # Convert to appropriate type
        converted_value = self._convert_value(value, setting.data_type)

        # Cache the value
        if use_cache:
            self._cache[key] = converted_value

        return converted_value

    def get_settings_by_category(
        self,
        db: Session,
        category: str,
        mask_sensitive: bool = True
    ) -> List[SystemSetting]:
        """
        Get all settings in a specific category.

        Args:
            db: Database session
            category: Category name (e.g., 'smtp', 'tasks')
            mask_sensitive: Whether to mask encrypted values

        Returns:
            List of SystemSetting objects
        """
        settings = db.query(SystemSetting).filter(
            SystemSetting.category == category
        ).all()

        if mask_sensitive:
            for setting in settings:
                if setting.is_encrypted and setting.value:
                    setting.value = "********"

        return settings

    def get_all_settings_grouped(
        self,
        db: Session,
        mask_sensitive: bool = True
    ) -> Dict[str, List[SystemSetting]]:
        """
        Get all settings grouped by category.

        Args:
            db: Database session
            mask_sensitive: Whether to mask encrypted values

        Returns:
            Dictionary with category names as keys and setting lists as values
        """
        all_settings = db.query(SystemSetting).all()

        grouped = {}
        for setting in all_settings:
            if setting.category not in grouped:
                grouped[setting.category] = []

            # Mask sensitive values if requested
            if mask_sensitive and setting.is_encrypted and setting.value:
                setting.value = "********"

            grouped[setting.category].append(setting)

        return grouped

    def update_setting(
        self,
        db: Session,
        key: str,
        value: str,
        user_id: Optional[str] = None
    ) -> SystemSetting:
        """
        Update a setting value.

        Args:
            db: Database session
            key: Setting key
            value: New value (will be encrypted if setting.is_encrypted is True)
            user_id: ID of user making the change (for audit trail)

        Returns:
            Updated SystemSetting object

        Raises:
            ValueError: If setting key not found
        """
        setting = db.query(SystemSetting).filter(SystemSetting.key == key).first()

        if not setting:
            raise ValueError(f"Setting with key '{key}' not found")

        # Encrypt value if needed
        if setting.is_encrypted:
            value = self._encrypt_value(value)

        # Update setting
        setting.value = value
        setting.updated_by = user_id
        db.commit()
        db.refresh(setting)

        # Clear cache for this key
        if key in self._cache:
            del self._cache[key]

        logger.info(f"Setting '{key}' updated by user {user_id}")
        return setting

    def update_settings_bulk(
        self,
        db: Session,
        settings_dict: Dict[str, str],
        user_id: Optional[str] = None
    ) -> List[SystemSetting]:
        """
        Update multiple settings at once.

        Args:
            db: Database session
            settings_dict: Dictionary of key-value pairs to update
            user_id: ID of user making the changes

        Returns:
            List of updated SystemSetting objects
        """
        updated_settings = []

        for key, value in settings_dict.items():
            try:
                setting = self.update_setting(db, key, value, user_id)
                updated_settings.append(setting)
            except ValueError as e:
                logger.warning(f"Failed to update setting '{key}': {e}")

        return updated_settings

    def clear_cache(self):
        """Clear the settings cache."""
        self._cache.clear()
        logger.info("Settings cache cleared")

    def validate_smtp_settings(self, db: Session) -> tuple[bool, str]:
        """
        Validate that required SMTP settings are configured.

        Args:
            db: Database session

        Returns:
            Tuple of (is_valid, error_message)
        """
        required_keys = ['smtp.host', 'smtp.port', 'smtp.from_email']

        for key in required_keys:
            value = self.get_setting(db, key)
            if not value:
                return False, f"Missing required setting: {key}"

        return True, "SMTP settings are valid"


# Create global service instance
system_settings_service = SystemSettingsService()

"""
System Settings API endpoints for runtime configuration management.

All endpoints require Super Admin access.

Masking: encrypted values are shown as ``********``. The fleet Core keys
(category ``core``, C10) show ``''`` (unmanaged) and ``none`` (forced empty)
as they are, so the portal can tell unmanaged from set; a set value is always
masked, and no response carries ciphertext. ORM rows are never changed to
mask them.

Validation (C10): writes to the fleet core.* keys follow
app/services/core_station_config.py (ws/wss URL, placeholders, heartbeat 0 or
5-3600, true/false, the pbkdf2 admin hash); a refused value answers 422 with
the key names and reasons (never the value), and a bulk update writes nothing.
"""

from fastapi import APIRouter, HTTPException, status
from typing import List, Dict, Optional
from datetime import datetime, timezone
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from app.api.deps import DBSession, SuperAdminUser
from app.schemas.system_setting import (
    SystemSettingResponse,
    SystemSettingUpdate,
    SystemSettingsBulkUpdate,
    SystemSettingsByCategoryResponse,
    SMTPTestRequest,
    SMTPTestResponse,
)
from app.services import core_station_config
from app.services.system_settings_service import (
    EncryptionKeyUnavailable,
    InvalidSettingValue,
    SettingDecryptError,
    system_settings_service,
)

logger = logging.getLogger(__name__)
router = APIRouter()


MASK = "********"


def _shown_value(setting) -> Optional[str]:
    """The value a response may carry: plain settings as stored; encrypted ones
    masked, except a fleet core key whose plaintext is '' or 'none'."""
    value = setting.value
    if not setting.is_encrypted or not value:
        return value
    if setting.key in core_station_config.SPECS:
        try:
            plaintext = system_settings_service.decrypt_strict(value)
        except SettingDecryptError:
            return MASK
        if plaintext in core_station_config.UNMASKED_VALUES:
            return plaintext
    return MASK


def _response(setting) -> SystemSettingResponse:
    """A response model with the shown value; the ORM row is not modified."""
    return SystemSettingResponse.model_validate(setting).model_copy(update={"value": _shown_value(setting)})


def _invalid(exc: InvalidSettingValue) -> HTTPException:
    """422 for fleet core.* values that break their rules (C10). Key names and
    reasons only, never the value."""
    logger.warning(f"Refused settings write: invalid {', '.join(exc.problems)}")
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="Invalid value: " + "; ".join(f"{key} {reason}" for key, reason in exc.problems.items())
        + ". Nothing was changed.",
    )


def _key_unavailable(exc: EncryptionKeyUnavailable) -> HTTPException:
    """503 for a write that needs SETTINGS_ENCRYPTION_KEY (C18, NEW-47)."""
    logger.error(f"Refused write of encrypted shared settings {exc.keys}: SETTINGS_ENCRYPTION_KEY is missing")
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=(
            "SETTINGS_ENCRYPTION_KEY is not configured on the server, so encrypted settings "
            f"{', '.join(exc.keys)} cannot be stored. Nothing was changed."
        ),
    )


@router.get("/", response_model=Dict[str, List[SystemSettingResponse]])
async def get_all_settings(
    db: DBSession,
    current_user: SuperAdminUser
):
    """
    Get all system settings grouped by category.

    Only Super Admins can access settings.
    Sensitive values (passwords) are masked with asterisks (see the module
    docstring for the fleet core keys).

    Args:
        db: Database session
        current_user: Current authenticated super admin user

    Returns:
        Dictionary with category names as keys and setting lists as values
    """
    settings_grouped = system_settings_service.get_all_settings_grouped(
        db, mask_sensitive=False
    )

    # Convert to response format (masked here, without touching the ORM rows)
    response = {}
    for category, settings in settings_grouped.items():
        response[category] = [_response(setting) for setting in settings]

    return response


@router.get("/category/{category}", response_model=SystemSettingsByCategoryResponse)
async def get_settings_by_category(
    category: str,
    db: DBSession,
    current_user: SuperAdminUser
):
    """
    Get all settings in a specific category.

    Args:
        category: Category name (smtp, tasks, snapshots, etc.)
        db: Database session
        current_user: Current authenticated super admin user

    Returns:
        Settings in the specified category

    Raises:
        404: If category not found or empty
    """
    settings = system_settings_service.get_settings_by_category(
        db, category, mask_sensitive=False
    )

    if not settings:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No settings found for category '{category}'"
        )

    return SystemSettingsByCategoryResponse(
        category=category,
        settings=[_response(setting) for setting in settings]
    )


@router.put("/bulk", response_model=List[SystemSettingResponse])
async def update_settings_bulk(
    data: SystemSettingsBulkUpdate,
    db: DBSession,
    current_user: SuperAdminUser
):
    """
    Update multiple settings at once.

    Args:
        data: Dictionary of key-value pairs to update
        db: Database session
        current_user: Current authenticated super admin user

    Returns:
        List of updated settings

    Raises:
        422: If a fleet core.* value breaks its rules (nothing is written)
        503: If the batch holds an encrypted core.* key and SETTINGS_ENCRYPTION_KEY is missing
             (nothing is written)
    """
    try:
        updated_settings = system_settings_service.update_settings_bulk(
            db, data.settings, str(current_user.user_id)
        )

        logger.info(
            f"{len(updated_settings)} settings updated by super admin {current_user.username}"
        )

        return [_response(setting) for setting in updated_settings]

    except InvalidSettingValue as e:
        raise _invalid(e)
    except EncryptionKeyUnavailable as e:
        raise _key_unavailable(e)
    except Exception as e:
        logger.error(f"Error updating settings in bulk: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update settings: {str(e)}"
        )


@router.get("/{key}", response_model=SystemSettingResponse)
async def get_setting(
    key: str,
    db: DBSession,
    current_user: SuperAdminUser
):
    """
    Get a single setting by key.

    Args:
        key: Setting key (e.g., 'smtp.host')
        db: Database session
        current_user: Current authenticated super admin user

    Returns:
        Setting details

    Raises:
        404: If setting not found
    """
    from app.models.system_setting import SystemSetting

    setting = db.query(SystemSetting).filter(SystemSetting.key == key).first()

    if not setting:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Setting with key '{key}' not found"
        )

    return _response(setting)


@router.put("/{key}", response_model=SystemSettingResponse)
async def update_setting(
    key: str,
    data: SystemSettingUpdate,
    db: DBSession,
    current_user: SuperAdminUser
):
    """
    Update a single setting value.

    Args:
        key: Setting key to update
        data: New value
        db: Database session
        current_user: Current authenticated super admin user

    Returns:
        Updated setting

    Raises:
        404: If setting not found
        422: If a fleet core.* value breaks its rules
        503: If the setting is an encrypted core.* key and SETTINGS_ENCRYPTION_KEY is missing
    """
    try:
        setting = system_settings_service.update_setting(
            db, key, data.value, str(current_user.user_id)
        )

        logger.info(f"Setting '{key}' updated by super admin {current_user.username}")

        return _response(setting)

    except InvalidSettingValue as e:
        raise _invalid(e)
    except EncryptionKeyUnavailable as e:
        raise _key_unavailable(e)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Error updating setting '{key}': {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update setting: {str(e)}"
        )


@router.post("/test/smtp", response_model=SMTPTestResponse)
async def test_smtp_connection(
    data: SMTPTestRequest,
    db: DBSession,
    current_user: SuperAdminUser
):
    """
    Test SMTP connection with current settings.

    Sends a test email to verify configuration.

    Args:
        data: Test email address
        db: Database session
        current_user: Current authenticated super admin user

    Returns:
        Test result with success status and details
    """
    try:
        # Validate settings exist
        is_valid, error_msg = system_settings_service.validate_smtp_settings(db)
        if not is_valid:
            return SMTPTestResponse(
                success=False,
                message=error_msg,
                details=None
            )

        # Get SMTP settings
        smtp_host = system_settings_service.get_setting(db, 'smtp.host')
        smtp_port = system_settings_service.get_setting(db, 'smtp.port')
        smtp_user = system_settings_service.get_setting(db, 'smtp.username')
        smtp_password = system_settings_service.get_setting(db, 'smtp.password')
        smtp_from_email = system_settings_service.get_setting(db, 'smtp.from_email')
        smtp_from_name = system_settings_service.get_setting(db, 'smtp.from_name', 'TrueLive Portal')
        smtp_use_tls = system_settings_service.get_setting(db, 'smtp.use_tls', True)

        # Create test message
        msg = MIMEMultipart()
        msg['From'] = f"{smtp_from_name} <{smtp_from_email}>"
        msg['To'] = data.test_email
        msg['Subject'] = "TrueLive Portal - SMTP Test"

        body = f"""
        This is a test email from TrueLive Portal.

        If you received this email, your SMTP configuration is working correctly.

        Sent by: {current_user.full_name}
        Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}

        ---
        TrueLive Portal System Settings
        """
        msg.attach(MIMEText(body, 'plain'))

        # Connect and send
        with smtplib.SMTP(smtp_host, int(smtp_port)) as server:
            if smtp_use_tls:
                server.starttls()

            if smtp_user and smtp_password:
                server.login(smtp_user, smtp_password)

            server.send_message(msg)

        return SMTPTestResponse(
            success=True,
            message=f"Test email sent successfully to {data.test_email}",
            details={
                "smtp_host": smtp_host,
                "smtp_port": smtp_port,
                "from_email": smtp_from_email,
                "use_tls": smtp_use_tls
            }
        )

    except smtplib.SMTPAuthenticationError as e:
        return SMTPTestResponse(
            success=False,
            message="SMTP authentication failed",
            details={"error": str(e)}
        )
    except smtplib.SMTPConnectError as e:
        return SMTPTestResponse(
            success=False,
            message="Failed to connect to SMTP server",
            details={"error": str(e)}
        )
    except Exception as e:
        logger.error(f"SMTP test failed: {e}")
        return SMTPTestResponse(
            success=False,
            message="SMTP test failed",
            details={"error": str(e)}
        )

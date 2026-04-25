"""
System Settings API endpoints for runtime configuration management.

All endpoints require Super Admin access.
"""

from fastapi import APIRouter, HTTPException, status
from typing import List, Dict
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
    SureViewTestResponse
)
from app.services.system_settings_service import system_settings_service

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/", response_model=Dict[str, List[SystemSettingResponse]])
async def get_all_settings(
    db: DBSession,
    current_user: SuperAdminUser
):
    """
    Get all system settings grouped by category.

    Only Super Admins can access settings.
    Sensitive values (passwords) are masked with asterisks.

    Args:
        db: Database session
        current_user: Current authenticated super admin user

    Returns:
        Dictionary with category names as keys and setting lists as values
    """
    settings_grouped = system_settings_service.get_all_settings_grouped(
        db, mask_sensitive=True
    )

    # Convert to response format
    response = {}
    for category, settings in settings_grouped.items():
        response[category] = [
            SystemSettingResponse.model_validate(setting)
            for setting in settings
        ]

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
        category: Category name (sureview, smtp, tasks, snapshots, etc.)
        db: Database session
        current_user: Current authenticated super admin user

    Returns:
        Settings in the specified category

    Raises:
        404: If category not found or empty
    """
    settings = system_settings_service.get_settings_by_category(
        db, category, mask_sensitive=True
    )

    if not settings:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No settings found for category '{category}'"
        )

    return SystemSettingsByCategoryResponse(
        category=category,
        settings=[
            SystemSettingResponse.model_validate(setting)
            for setting in settings
        ]
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
        400: If validation fails
    """
    try:
        updated_settings = system_settings_service.update_settings_bulk(
            db, data.settings, str(current_user.user_id)
        )

        # Mask sensitive values
        for setting in updated_settings:
            if setting.is_encrypted and setting.value:
                setting.value = "********"

        logger.info(
            f"{len(updated_settings)} settings updated by super admin {current_user.username}"
        )

        return [
            SystemSettingResponse.model_validate(setting)
            for setting in updated_settings
        ]

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

    # Mask sensitive value
    if setting.is_encrypted and setting.value:
        setting.value = "********"

    return SystemSettingResponse.model_validate(setting)


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
        400: If validation fails
    """
    try:
        setting = system_settings_service.update_setting(
            db, key, data.value, str(current_user.user_id)
        )

        # Mask sensitive value in response
        if setting.is_encrypted and setting.value:
            setting.value = "********"

        logger.info(f"Setting '{key}' updated by super admin {current_user.username}")

        return SystemSettingResponse.model_validate(setting)

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


@router.post("/test/sureview", response_model=SureViewTestResponse)
async def test_sureview_connection(
    db: DBSession,
    current_user: SuperAdminUser
):
    """
    Test SureView API connection with current settings.

    Attempts to authenticate and fetch server list.

    Args:
        db: Database session
        current_user: Current authenticated super admin user

    Returns:
        Test result with success status and details
    """
    try:
        # Validate settings exist
        is_valid, error_msg = system_settings_service.validate_sureview_settings(db)
        if not is_valid:
            return SureViewTestResponse(
                success=False,
                message=error_msg,
                details=None
            )

        # Get SureView settings
        username = system_settings_service.get_setting(db, 'sureview.username')
        password = system_settings_service.get_setting(db, 'sureview.password')
        api_url = system_settings_service.get_setting(db, 'sureview.api_url')
        login_url = system_settings_service.get_setting(db, 'sureview.login_url')

        # Test connection using SureView service
        from app.services.sureview_service import automate_login, get_server_list

        # Override settings temporarily for test
        import os
        old_username = os.getenv('SUREVIEW_USERNAME')
        old_password = os.getenv('SUREVIEW_PASSWORD')
        old_api_url = os.getenv('SUREVIEW_API_URL')

        os.environ['SUREVIEW_USERNAME'] = username
        os.environ['SUREVIEW_PASSWORD'] = password
        os.environ['SUREVIEW_API_URL'] = api_url
        if login_url:
            os.environ['SUREVIEW_LOGIN_URL'] = login_url

        try:
            # Attempt login
            cookies = automate_login()
            if not cookies:
                return SureViewTestResponse(
                    success=False,
                    message="Failed to authenticate to SureView",
                    details={"api_url": api_url}
                )

            # Try to fetch servers
            servers = get_server_list(cookies)
            server_count = len(servers) if servers else 0

            return SureViewTestResponse(
                success=True,
                message="Successfully authenticated to SureView API",
                details={
                    "api_url": api_url,
                    "servers_found": server_count
                }
            )

        finally:
            # Restore original settings
            if old_username:
                os.environ['SUREVIEW_USERNAME'] = old_username
            if old_password:
                os.environ['SUREVIEW_PASSWORD'] = old_password
            if old_api_url:
                os.environ['SUREVIEW_API_URL'] = old_api_url

    except Exception as e:
        logger.error(f"SureView test failed: {e}")
        return SureViewTestResponse(
            success=False,
            message="SureView connection test failed",
            details={"error": str(e)}
        )

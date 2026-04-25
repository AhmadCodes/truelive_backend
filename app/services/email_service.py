"""
Email service for sending emails via SMTP.
Handles invitation emails and other notification emails.
"""

import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)


class EmailService:
    """Service for sending emails via SMTP."""

    def __init__(self):
        """Initialize email service with SMTP settings."""
        self.smtp_host = settings.SMTP_HOST
        self.smtp_port = settings.SMTP_PORT
        self.smtp_user = settings.SMTP_USER
        self.smtp_password = settings.SMTP_PASSWORD
        self.from_email = settings.SMTP_FROM_EMAIL
        self.from_name = settings.SMTP_FROM_NAME
        self.use_tls = settings.SMTP_USE_TLS

    def send_email(
        self,
        to_email: str,
        subject: str,
        html_body: str,
        text_body: Optional[str] = None
    ) -> bool:
        """
        Send an email via SMTP.

        Args:
            to_email: Recipient email address
            subject: Email subject
            html_body: HTML content of the email
            text_body: Plain text fallback (optional)

        Returns:
            True if email sent successfully, False otherwise
        """
        try:
            # Create message
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = f"{self.from_name} <{self.from_email}>"
            msg['To'] = to_email

            # Add text and HTML parts
            if text_body:
                text_part = MIMEText(text_body, 'plain')
                msg.attach(text_part)

            html_part = MIMEText(html_body, 'html')
            msg.attach(html_part)

            # Send email
            logger.info(f"Connecting to SMTP server {self.smtp_host}:{self.smtp_port}")

            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=10) as server:
                server.set_debuglevel(1)  # Enable debug output

                if self.use_tls:
                    logger.info("Starting TLS...")
                    server.starttls()

                logger.info(f"Logging in as {self.smtp_user}...")
                server.login(self.smtp_user, self.smtp_password)

                logger.info(f"Sending email to {to_email}...")
                server.send_message(msg)

            logger.info(f"Email sent successfully to {to_email}")
            return True

        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"SMTP authentication failed: {e}")
            return False
        except smtplib.SMTPException as e:
            logger.error(f"SMTP error occurred: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to send email to {to_email}: {e}")
            return False

    def send_invitation_email(
        self,
        to_email: str,
        invitation_token: str,
        invited_by: str
    ) -> bool:
        """
        Send an invitation email with registration link.

        Args:
            to_email: Recipient email address
            invitation_token: Unique invitation token
            invited_by: Name of the user who sent the invitation

        Returns:
            True if email sent successfully, False otherwise
        """
        # Build registration URL
        registration_url = f"{settings.FRONTEND_URL}/register?token={invitation_token}"

        # Load HTML template
        template_path = Path(__file__).parent.parent / "templates" / "email" / "invitation_email.html"

        try:
            with open(template_path, 'r', encoding='utf-8') as f:
                html_template = f.read()
        except FileNotFoundError:
            logger.warning(f"Email template not found at {template_path}, using fallback")
            html_template = self._get_fallback_invitation_template()

        # Replace placeholders
        html_body = html_template.replace("{{email}}", to_email)
        html_body = html_body.replace("{{registration_url}}", registration_url)
        html_body = html_body.replace("{{invited_by}}", invited_by)
        html_body = html_body.replace("{{expiry_hours}}", str(settings.INVITATION_TOKEN_EXPIRY_HOURS))

        # Plain text fallback
        text_body = f"""
You have been invited to join TrueLive Portal by {invited_by}.

Click the link below to create your account:
{registration_url}

This invitation will expire in {settings.INVITATION_TOKEN_EXPIRY_HOURS} hours.

If you did not expect this invitation, please ignore this email.

---
TrueLive Portal Team
        """.strip()

        return self.send_email(
            to_email=to_email,
            subject="You're invited to join TrueLive Portal",
            html_body=html_body,
            text_body=text_body
        )

    def _get_fallback_invitation_template(self) -> str:
        """Get a simple fallback HTML template if the template file is not found."""
        return """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px;">
    <div style="background-color: #f8f9fa; padding: 20px; border-radius: 5px;">
        <h1 style="color: #2c3e50; margin-top: 0;">You're Invited!</h1>
        <p>Hello,</p>
        <p>You have been invited by <strong>{{invited_by}}</strong> to join TrueLive Portal.</p>
        <p>Click the button below to create your account:</p>
        <div style="text-align: center; margin: 30px 0;">
            <a href="{{registration_url}}" style="background-color: #007bff; color: white; padding: 12px 30px; text-decoration: none; border-radius: 5px; display: inline-block;">Create Account</a>
        </div>
        <p style="color: #666; font-size: 14px;">
            This invitation will expire in <strong>{{expiry_hours}} hours</strong>.
        </p>
        <p style="color: #666; font-size: 14px;">
            If you did not expect this invitation, please ignore this email.
        </p>
        <hr style="border: none; border-top: 1px solid #ddd; margin: 20px 0;">
        <p style="color: #999; font-size: 12px; text-align: center;">
            TrueLive Portal - Surveillance Camera Management System<br>
            This is an automated message, please do not reply.
        </p>
    </div>
</body>
</html>
        """.strip()


# Create global email service instance
email_service = EmailService()

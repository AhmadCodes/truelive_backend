"""
Invitation management API endpoints.
Handles sending email invitations for user registration.
"""

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func
from typing import List
from datetime import datetime, timedelta, timezone
import uuid
import logging

from app.api.deps import AdminUser, DBSession, CurrentUser
from app.models.user import InvitationToken, User
from app.schemas.invitation import (
    InvitationSendRequest,
    InvitationSendResponse,
    InvitationListResponse
)
from app.services.email_service import email_service
from app.core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/send", response_model=InvitationSendResponse, status_code=status.HTTP_201_CREATED)
async def send_invitation(
    invitation_data: InvitationSendRequest,
    current_user: AdminUser,
    db: DBSession
):
    """
    Send an email invitation to a new user.

    Only admins and super admins can send invitations.

    Args:
        invitation_data: Invitation request data (email, role)
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Invitation send response with token details

    Raises:
        HTTPException: If email already registered or email send fails
    """
    # Check if email is already registered
    existing_user = db.query(User).filter(User.email == invitation_data.email).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"User with email '{invitation_data.email}' already exists"
        )

    # If there's already a pending invitation for this email, delete it
    # This allows re-sending invitations (e.g., if email wasn't received)
    existing_invitations = db.query(InvitationToken).filter(
        InvitationToken.email == invitation_data.email,
        InvitationToken.is_used == False
    ).all()

    if existing_invitations:
        for existing_invitation in existing_invitations:
            db.delete(existing_invitation)
        db.flush()  # Ensure deletions are processed before creating new invitation
        logger.info(
            f"Invalidated {len(existing_invitations)} existing invitation(s) for {invitation_data.email}"
        )

    # Validate role
    if invitation_data.role not in ['user', 'admin', 'super_admin']:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid role '{invitation_data.role}'. Must be one of: user, admin, super_admin"
        )

    # Only super_admin can invite admin or super_admin
    if invitation_data.role in ['admin', 'super_admin'] and current_user.role != 'super_admin':
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only super admins can invite admin or super_admin users"
        )

    # Generate unique token
    token = str(uuid.uuid4())
    expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.INVITATION_TOKEN_EXPIRY_HOURS)

    # Create invitation record
    new_invitation = InvitationToken(
        token=token,
        email=invitation_data.email,
        role=invitation_data.role,
        invited_by_id=current_user.user_id,
        expires_at=expires_at
    )

    db.add(new_invitation)
    db.commit()
    db.refresh(new_invitation)

    # Send invitation email
    email_sent = email_service.send_invitation_email(
        to_email=invitation_data.email,
        invitation_token=token,
        invited_by=current_user.full_name
    )

    if not email_sent:
        # Rollback the invitation if email fails
        db.delete(new_invitation)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to send invitation email. Please check email configuration."
        )

    logger.info(
        f"Invitation sent to {invitation_data.email} by {current_user.username} "
        f"(role: {invitation_data.role}, expires: {expires_at})"
    )

    return InvitationSendResponse(
        success=True,
        message=f"Invitation sent successfully to {invitation_data.email}",
        invitation_id=str(new_invitation.id),
        email=invitation_data.email,
        expires_at=expires_at
    )


@router.get("", response_model=List[InvitationListResponse])
async def list_invitations(
    current_user: AdminUser,
    db: DBSession,
    include_used: bool = False,
    include_expired: bool = False
):
    """
    List all invitations.

    Only admins and super admins can list invitations.

    Args:
        current_user: Current authenticated admin or super admin
        db: Database session
        include_used: Include used invitations (default: False)
        include_expired: Include expired invitations (default: False)

    Returns:
        List of invitations
    """
    query = db.query(InvitationToken)

    # Filter by used status
    if not include_used:
        query = query.filter(InvitationToken.is_used == False)

    # Filter by expiration
    if not include_expired:
        query = query.filter(InvitationToken.expires_at > datetime.now(timezone.utc))

    invitations = query.order_by(InvitationToken.created_at.desc()).all()

    # Build response with additional computed fields
    result = []
    for invitation in invitations:
        inv_response = InvitationListResponse.model_validate(invitation)

        # Add invited_by username
        if invitation.invited_by:
            inv_response.invited_by_username = invitation.invited_by.username

        # Check if expired
        inv_response.is_expired = invitation.expires_at < datetime.now(timezone.utc)

        result.append(inv_response)

    return result


@router.delete("/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invitation(
    invitation_id: str,
    current_user: AdminUser,
    db: DBSession
):
    """
    Revoke (delete) an invitation.

    Only admins and super admins can revoke invitations.

    Args:
        invitation_id: Invitation ID
        current_user: Current authenticated admin or super admin
        db: Database session

    Raises:
        HTTPException: If invitation not found or already used
    """
    invitation = db.query(InvitationToken).filter(InvitationToken.id == invitation_id).first()

    if not invitation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Invitation with ID '{invitation_id}' not found"
        )

    if invitation.is_used:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot revoke an invitation that has already been used"
        )

    db.delete(invitation)
    db.commit()

    logger.info(
        f"Invitation to {invitation.email} revoked by {current_user.username}"
    )


@router.get("/{invitation_id}", response_model=InvitationListResponse)
async def get_invitation(
    invitation_id: str,
    current_user: AdminUser,
    db: DBSession
):
    """
    Get details of a specific invitation.

    Only admins and super admins can view invitation details.

    Args:
        invitation_id: Invitation ID
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Invitation details

    Raises:
        HTTPException: If invitation not found
    """
    invitation = db.query(InvitationToken).filter(InvitationToken.id == invitation_id).first()

    if not invitation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Invitation with ID '{invitation_id}' not found"
        )

    inv_response = InvitationListResponse.model_validate(invitation)

    # Add invited_by username
    if invitation.invited_by:
        inv_response.invited_by_username = invitation.invited_by.username

    # Check if expired
    inv_response.is_expired = invitation.expires_at < datetime.now(timezone.utc)

    return inv_response


@router.get("/stats/summary")
async def get_invitation_stats(
    current_user: AdminUser,
    db: DBSession
):
    """
    Get invitation statistics.

    Only admins and super admins can view stats.

    Args:
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Invitation statistics
    """
    total = db.query(func.count(InvitationToken.id)).scalar() or 0
    pending = db.query(func.count(InvitationToken.id)).filter(
        InvitationToken.is_used == False,
        InvitationToken.expires_at > datetime.now(timezone.utc)
    ).scalar() or 0
    used = db.query(func.count(InvitationToken.id)).filter(
        InvitationToken.is_used == True
    ).scalar() or 0
    expired = db.query(func.count(InvitationToken.id)).filter(
        InvitationToken.is_used == False,
        InvitationToken.expires_at <= datetime.now(timezone.utc)
    ).scalar() or 0

    return {
        "total": total,
        "pending": pending,
        "used": used,
        "expired": expired
    }

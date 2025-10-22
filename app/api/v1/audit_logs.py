"""
Audit log API endpoints.
"""

from fastapi import APIRouter, HTTPException, status, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, desc
from typing import Optional
from datetime import datetime
from uuid import UUID
import json

from app.api.deps import AdminUser, DBSession
from app.models.user import AuditLog, User
from app.schemas.audit_log import (
    AuditLogResponse,
    AuditLogListResponse,
    AuditLogStatsResponse
)


router = APIRouter()


@router.get("", response_model=AuditLogListResponse)
async def list_audit_logs(
    current_user: AdminUser,
    db: DBSession,
    user_id: Optional[UUID] = Query(None, description="Filter by user ID"),
    action: Optional[str] = Query(None, description="Filter by action (partial match)"),
    resource_type: Optional[str] = Query(None, description="Filter by resource type"),
    resource_id: Optional[str] = Query(None, description="Filter by resource ID"),
    date_from: Optional[datetime] = Query(None, description="Filter logs from this date"),
    date_to: Optional[datetime] = Query(None, description="Filter logs until this date"),
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(50, ge=1, le=1000, description="Items per page")
):
    """
    List audit logs with filtering and pagination.

    Only admins and super admins can view audit logs.

    Args:
        current_user: Current authenticated admin or super admin
        db: Database session
        user_id: Filter by user ID
        action: Filter by action (supports partial match)
        resource_type: Filter by resource type
        resource_id: Filter by resource ID
        date_from: Filter logs from this date
        date_to: Filter logs until this date
        page: Page number (default: 1)
        per_page: Items per page (default: 50, max: 1000)

    Returns:
        Paginated list of audit logs with metadata
    """
    # Build query
    query = db.query(AuditLog)

    # Apply filters
    if user_id:
        query = query.filter(AuditLog.user_id == user_id)

    if action:
        query = query.filter(AuditLog.action.ilike(f"%{action}%"))

    if resource_type:
        query = query.filter(AuditLog.resource_type == resource_type)

    if resource_id:
        query = query.filter(AuditLog.resource_id == resource_id)

    if date_from:
        query = query.filter(AuditLog.created_at >= date_from)

    if date_to:
        query = query.filter(AuditLog.created_at <= date_to)

    # Get total count
    total = query.count()

    # Calculate pagination
    total_pages = (total + per_page - 1) // per_page
    offset = (page - 1) * per_page

    # Order by created_at descending (newest first)
    query = query.order_by(desc(AuditLog.created_at))

    # Apply pagination
    logs = query.offset(offset).limit(per_page).all()

    # Enrich with username
    enriched_logs = []
    for log in logs:
        log_dict = {
            "id": log.id,
            "user_id": log.user_id,
            "action": log.action,
            "resource_type": log.resource_type,
            "resource_id": log.resource_id,
            "changes": json.loads(log.changes) if log.changes else None,
            "ip_address": log.ip_address,
            "user_agent": log.user_agent,
            "created_at": log.created_at,
            "updated_at": log.updated_at,
            "username": None
        }

        # Get username if user_id exists
        if log.user_id:
            user = db.query(User).filter(User.user_id == log.user_id).first()
            if user:
                log_dict["username"] = user.username

        enriched_logs.append(AuditLogResponse(**log_dict))

    return AuditLogListResponse(
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        logs=enriched_logs
    )


@router.get("/stats", response_model=AuditLogStatsResponse)
async def get_audit_log_stats(
    current_user: AdminUser,
    db: DBSession
):
    """
    Get audit log statistics.

    Only admins and super admins can view stats.

    Args:
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Audit log statistics including counts by action, resource type, and user
    """
    # Total logs
    total_logs = db.query(func.count(AuditLog.id)).scalar()

    # Logs by action
    logs_by_action = {}
    action_counts = db.query(
        AuditLog.action,
        func.count(AuditLog.id)
    ).group_by(AuditLog.action).all()

    for action, count in action_counts:
        logs_by_action[action] = count

    # Logs by resource type
    logs_by_resource_type = {}
    resource_type_counts = db.query(
        AuditLog.resource_type,
        func.count(AuditLog.id)
    ).group_by(AuditLog.resource_type).all()

    for resource_type, count in resource_type_counts:
        logs_by_resource_type[resource_type] = count

    # Top 10 users by log count
    logs_by_user = {}
    user_counts = db.query(
        User.username,
        func.count(AuditLog.id)
    ).join(AuditLog, User.user_id == AuditLog.user_id)\
     .group_by(User.username)\
     .order_by(desc(func.count(AuditLog.id)))\
     .limit(10)\
     .all()

    for username, count in user_counts:
        logs_by_user[username] = count

    # Recent activity (last 10 logs)
    recent_logs = db.query(AuditLog)\
        .order_by(desc(AuditLog.created_at))\
        .limit(10)\
        .all()

    # Enrich recent logs with username
    enriched_recent_logs = []
    for log in recent_logs:
        log_dict = {
            "id": log.id,
            "user_id": log.user_id,
            "action": log.action,
            "resource_type": log.resource_type,
            "resource_id": log.resource_id,
            "changes": json.loads(log.changes) if log.changes else None,
            "ip_address": log.ip_address,
            "user_agent": log.user_agent,
            "created_at": log.created_at,
            "updated_at": log.updated_at,
            "username": None
        }

        if log.user_id:
            user = db.query(User).filter(User.user_id == log.user_id).first()
            if user:
                log_dict["username"] = user.username

        enriched_recent_logs.append(AuditLogResponse(**log_dict))

    return AuditLogStatsResponse(
        total_logs=total_logs,
        logs_by_action=logs_by_action,
        logs_by_resource_type=logs_by_resource_type,
        logs_by_user=logs_by_user,
        recent_activity=enriched_recent_logs
    )


@router.get("/{log_id}", response_model=AuditLogResponse)
async def get_audit_log(
    log_id: UUID,
    current_user: AdminUser,
    db: DBSession
):
    """
    Get single audit log by ID.

    Only admins and super admins can view audit logs.

    Args:
        log_id: Audit log UUID
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Audit log details

    Raises:
        HTTPException: If audit log not found
    """
    log = db.query(AuditLog).filter(AuditLog.id == log_id).first()

    if not log:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Audit log not found"
        )

    # Enrich with username
    log_dict = {
        "id": log.id,
        "user_id": log.user_id,
        "action": log.action,
        "resource_type": log.resource_type,
        "resource_id": log.resource_id,
        "changes": json.loads(log.changes) if log.changes else None,
        "ip_address": log.ip_address,
        "user_agent": log.user_agent,
        "created_at": log.created_at,
        "updated_at": log.updated_at,
        "username": None
    }

    if log.user_id:
        user = db.query(User).filter(User.user_id == log.user_id).first()
        if user:
            log_dict["username"] = user.username

    return AuditLogResponse(**log_dict)

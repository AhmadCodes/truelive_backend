"""
SQLAlchemy ORM models for the Shomer Portal backend.
"""

from app.models.base import BaseModel, TimestampMixin
from app.models.user import User, InvitationToken, AuditLog
from app.models.category import SiteCategory, SiteCategoryMapping
from app.models.site import Site
from app.models.camera import Camera
from app.models.screenshot import Screenshot
from app.models.site_camera_layout import SiteCamerasLayoutConfig, SiteCamerasLayout
from app.models.pc import PC
from app.models.screen import Screen
from app.models.view import View
from app.models.screen_mapping import ScreenMapping

__all__ = [
    # Base classes
    "BaseModel",
    "TimestampMixin",

    # User models
    "User",
    "InvitationToken",
    "AuditLog",

    # Category models
    "SiteCategory",
    "SiteCategoryMapping",

    # Site models
    "Site",
    "Camera",
    "Screenshot",
    "SiteCamerasLayoutConfig",
    "SiteCamerasLayout",

    # PC and screen models
    "PC",
    "Screen",
    "View",
    "ScreenMapping",
]

"""
SQLAlchemy ORM models for the TrueLive Portal backend.
"""

from app.models.base import BaseModel, TimestampMixin
from app.models.user import User, InvitationToken, AuditLog
from app.models.category import SiteCategory, SiteCategoryMapping
from app.models.site import Site
from app.models.device import Device
from app.models.camera import Camera
from app.models.snapshot import Snapshot
from app.models.site_camera_layout import SiteCamerasLayoutConfig, SiteCamerasLayout
from app.models.pc import PC
from app.models.screen_layout import ScreenLayout
from app.models.screen import Screen
from app.models.view import View
from app.models.screen_mapping import ScreenMapping
from app.models.pc_screen_mapping_state import PcScreenMappingState
from app.models.system_setting import SystemSetting
from app.models.alerting import (
    AlertAddress,
    RawMessage,
    Alert,
    AlertMedia,
    RAW_MESSAGE_STATUSES,
    PARSER_CONFIDENCES,
    ALERT_EVENT_TYPES,
    ALERT_MEDIA_KINDS,
)
from app.models.webhook import WebhookConsumer, WebhookDelivery, DELIVERY_STATUSES
from app.models.service_account import ServiceAccount, ServiceAccountToken

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
    # Site / Device models
    "Site",
    "Device",
    "Camera",
    "Snapshot",
    "SiteCamerasLayoutConfig",
    "SiteCamerasLayout",
    # PC and screen models
    "PC",
    "ScreenLayout",
    "Screen",
    "View",
    "ScreenMapping",
    "PcScreenMappingState",
    # System models
    "SystemSetting",
    # Alerting models
    "AlertAddress",
    "RawMessage",
    "Alert",
    "AlertMedia",
    "WebhookConsumer",
    "WebhookDelivery",
    "ServiceAccount",
    "ServiceAccountToken",
    # Alerting status / type constants
    "RAW_MESSAGE_STATUSES",
    "PARSER_CONFIDENCES",
    "ALERT_EVENT_TYPES",
    "ALERT_MEDIA_KINDS",
    "DELIVERY_STATUSES",
]

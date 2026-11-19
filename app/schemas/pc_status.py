"""
Response models of the PC status and deploy-history API (plan 06 D16, OD-22;
NEW-51, E20, X-16).

Every timestamp is a timezone-aware UTC datetime (NEW-51: the routes used to
return naive local times next to naive UTC ones). Nothing here carries a
layout body, a token or a URL: deploy rows hold ids, states, counts and a
content hash, and free-text details have URLs replaced by ``<url>``.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ActorStamp(BaseModel):
    """Who started a deploy (``pc_deployments.created_by_*``)."""

    type: str = Field(..., description="user, service_account or system")
    id: Optional[str] = None
    label: Optional[str] = None


class DeploymentSummary(BaseModel):
    """One deploy of a layout to a PC (``pc_deployments``, D13/D14/D15)."""

    config_id: str = Field(..., description="The deploy's config_id (the row id)")
    pc_id: str
    status: str = Field(..., description="pending, delivered, received, accepted, applied, failed, unconfirmed, "
                                         "superseded, offline or relay_failed")
    status_detail: Optional[str] = Field(None, description="Reason or detail of the state (URLs replaced)")
    trigger: str = Field(..., description="pc_deploy, layout_deploy or catch_up")
    confirmable: bool = Field(..., description="The station can confirm applied (Client >= 1.4.0); "
                                               "a 1.1.x row ends at received")
    layout_id: Optional[str] = None
    layout_name: Optional[str] = None
    config_hash: Optional[str] = Field(None, description="sha256 of the deployed content (no body)")
    screens: Optional[int] = None
    mode: Optional[str] = None
    used_fallback: Optional[bool] = None
    persisted: Optional[bool] = None
    source: Optional[str] = None
    client_version: Optional[str] = None
    core_version: Optional[str] = None
    deployed_by: Optional[ActorStamp] = None
    created_at: Optional[datetime] = None
    status_changed_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    received_at: Optional[datetime] = None
    accepted_at: Optional[datetime] = None
    applied_at: Optional[datetime] = None
    failed_at: Optional[datetime] = None
    unconfirmed_at: Optional[datetime] = None
    superseded_at: Optional[datetime] = None


class PCHealthSummary(BaseModel):
    """The PC's latest station status, as ``/pcs/status`` shows it (D19's
    Core health cell). Register-only rows (no station_status yet) have
    ``health`` null and ``health_stale`` true."""

    health: Optional[str] = Field(None, description="ok, degraded, unhealthy, stopped or unknown")
    health_stale: bool = Field(..., description="No station_status for more than 180 s")
    reasons: List[str] = Field(default_factory=list)
    received_at: Optional[datetime] = Field(None, description="When the relay got the last station_status")
    reported_at: Optional[datetime] = Field(None, description="The station's clock (ts) of that status")
    registered_at: Optional[datetime] = Field(None, description="When the relay last stored register versions")
    # Versions and build class (E20)
    client_version: Optional[str] = None
    client_build: Optional[str] = None
    core_version: Optional[str] = Field(None, description="Core version reported at register")
    core_installed_version: Optional[str] = None
    core_running_version: Optional[str] = None
    core_version_source: Optional[str] = None
    core_build_class: Optional[str] = Field(None, description="legacy, reports_v1, reports_v2 or unknown")
    delivery_policy: Optional[str] = None
    # Core and telemetry
    core_state: Optional[str] = None
    core_pid: Optional[int] = None
    core_uptime_sec: Optional[int] = None
    core_restarts_24h: Optional[int] = None
    core_last_exit: Optional[str] = None
    telemetry_mode: Optional[str] = Field(None, description="stdout_only, listener, fanout, none")
    tap_state: Optional[str] = None
    # The wall
    running_config_id: Optional[str] = None
    deploying: Optional[bool] = None
    screens: Optional[int] = None
    streams_total: Optional[int] = None
    streams_playing: Optional[int] = None
    streams_connecting: Optional[int] = None
    streams_failed_count: Optional[int] = None
    relay_flags: Dict[str, bool] = Field(default_factory=dict,
                                         description="Relay-observed flags, e.g. token_outdated, token_drop_needed")


class PCHealthDetail(PCHealthSummary):
    """Everything ``pc_health`` holds for one PC (``/pcs/{id}/health``)."""

    pc_id: str
    last_message_id: Optional[str] = None
    core_exe_sha256: Optional[str] = None
    core_exe_size: Optional[int] = None
    capabilities: List[str] = Field(default_factory=list)
    telemetry_channels: Dict[str, Any] = Field(default_factory=dict)
    streams_failed: List[str] = Field(default_factory=list, description="Failed source ids (at most 50)")
    flags: Dict[str, bool] = Field(default_factory=dict, description="The Client's flags")
    readiness: List[Dict[str, Any]] = Field(default_factory=list, description="[{code, detail}]")
    # C23 config status (key names, never values)
    core_config_mode: Optional[str] = None
    mode_set_by: Optional[str] = None
    core_revision: Optional[str] = None
    config_core_version: Optional[str] = None
    core_caps: List[str] = Field(default_factory=list)
    migration_status: Optional[str] = None
    drift_key_names: List[str] = Field(default_factory=list)
    legacy_available: Optional[bool] = None
    last_config_error: Optional[str] = None
    data_dir_acl_warning: Optional[bool] = None
    fleet_errors_count: Optional[int] = None
    updated_at: Optional[datetime] = None
    # Stream providers: null when the PC's latest deploy has no provider camera.
    provider_streams: Optional[Dict[str, Any]] = Field(
        None, description="{cameras, unresolved: [{camera_id, camera_name, code, message}], last_delivery_at, "
                          "acknowledged, next_renewal_at, blocked}; times are Unix seconds")


class DeploymentHistory(BaseModel):
    """A page of a PC's deploys, newest first."""

    pc_id: str
    total: int = Field(..., description="Deploys of this PC (all pages)")
    limit: int
    offset: int
    items: List[DeploymentSummary]

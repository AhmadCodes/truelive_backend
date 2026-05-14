"""
Pydantic schemas for service-account auth.

A **service account** is a non-human principal — an external system (e.g. a
downstream monitoring platform) that needs to call TrueLive APIs without a
human admin's JWT. Each service account holds one or more scoped bearer tokens
in the format `tlsa_<base64url>`. Auth header: `Authorization: Bearer tlsa_<...>`.
"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ---------- scopes ---------- #
#
# Scopes are the closed set of permissions a service account can hold. New
# scopes require a code change (intentional — scopes are a security boundary).
# Each scope's meaning is documented inline below; the endpoint create
# description in app/api/v1/service_accounts.py mirrors this table.

Scope = Literal[
    "alerts:read",
    "alerts:raw:read",
    "webhook:manage",
    "addresses:read",
    "sites:read",
    "sites:manage",
    "cameras:read",
    "cameras:manage",
]
"""Valid permission scopes for service-account tokens.

Alerting:

- `alerts:read`      — GET on /alerts, /alerts/{id}, /alerts/{id}/deliveries,
                       and /alerts/{id}/media/{media_id}.
- `alerts:raw:read`  — GET on /alerts/{id}/raw (raw RFC822 source). Separate
                       from `alerts:read` so callers can grant the parsed view
                       without granting access to the unredacted original mail.
- `webhook:manage`   — Full CRUD on /alerting/webhook-consumers, scoped to
                       rows the caller owns.
- `addresses:read`   — GET on /cameras/{id}/alert-addresses.

Inventory (read + manage are independent; `:manage` also satisfies any
`:read` requirement, so granting just `:manage` covers both):

- `sites:read`       — GET on /sites, /sites/{id}, and /sites/{id}/camera-layout.
- `sites:manage`     — POST/PUT/PATCH/DELETE on /sites and its sub-resources
                       (category, auto-populate-cameras, camera-layout).
- `cameras:read`     — GET on /cameras, /cameras/count, /cameras/{id}, and
                       /cameras/site/{id}.
- `cameras:manage`   — POST/PUT/PATCH/DELETE on /cameras (including the
                       mark-as-seen and toggle-new sub-resources).
"""


class ServiceAccountCreate(BaseModel):
    """Request body for creating a new service account.

    A service account is just the principal — it holds no credentials by
    itself. After creation, issue one or more tokens via
    `POST /service-accounts/{id}/tokens`.
    """
    name: str = Field(
        ..., min_length=1, max_length=255,
        description=(
            "Unique human-readable identifier, e.g. `acme-monitoring`. Use one "
            "service account per integrating system."
        ),
        examples=["acme-monitoring"],
    )
    description: Optional[str] = Field(
        None,
        description="Free-form notes — who owns it, what it's used for, when to revoke.",
        examples=["Production monitoring platform — owns webhook consumer + reads alerts."],
    )
    scopes: list[Scope] = Field(
        default_factory=list,
        description=(
            "Permissions to grant. Each item must be one of these four values:\n\n"
            "- `alerts:read` — read normalized alerts, deliveries, and media URLs "
            "(`GET /alerts`, `GET /alerts/{id}`, `GET /alerts/{id}/deliveries`, "
            "`GET /alerts/{id}/media/{media_id}`)\n"
            "- `alerts:raw:read` — read the raw RFC822 source (`GET /alerts/{id}/raw`). "
            "Separate from `alerts:read` so a caller can hold the parsed view "
            "without the unredacted original mail.\n"
            "- `webhook:manage` — full CRUD on `/alerting/webhook-consumers`, "
            "restricted to consumers the caller owns.\n"
            "- `addresses:read` — read per-camera alert addresses "
            "(`GET /cameras/{id}/alert-addresses`).\n\n"
            "Empty list is valid: the account exists but its tokens hold zero "
            "permissions (useful for pre-provisioning or temporary disable)."
        ),
        examples=[["alerts:read", "webhook:manage"]],
    )


class ServiceAccountUpdate(BaseModel):
    """Partial update — only the fields you want to change need to be present."""
    description: Optional[str] = Field(None, description="New description.")
    scopes: Optional[list[Scope]] = Field(
        None,
        description=(
            "Replace the scope list. Each item must be one of these four values:\n\n"
            "- `alerts:read` — read normalized alerts, deliveries, and media URLs\n"
            "- `alerts:raw:read` — read the raw RFC822 source\n"
            "- `webhook:manage` — full CRUD on webhook consumers (own rows only)\n"
            "- `addresses:read` — read per-camera alert addresses\n\n"
            "**This is a full replacement, not a merge** — the list you send "
            "becomes the new list. Pass `[]` to strip all permissions while "
            "keeping the account row. Omit the field entirely to leave scopes "
            "unchanged."
        ),
        examples=[["alerts:read", "alerts:raw:read", "webhook:manage", "addresses:read"]],
    )
    is_active: Optional[bool] = Field(
        None,
        description=(
            "Set false to disable the account globally. All of its tokens stop "
            "working immediately, even ones that aren't individually revoked."
        ),
    )


class ServiceAccountResponse(BaseModel):
    """A service account record (no token material)."""
    id: str = Field(..., description="UUID of the service account.")
    name: str
    description: Optional[str] = None
    scopes: list[Scope] = Field(
        default_factory=list,
        description=(
            "Permissions this account currently holds. Each value is one of:\n\n"
            "- `alerts:read` — read normalized alerts, deliveries, and media URLs\n"
            "- `alerts:raw:read` — read the raw RFC822 source\n"
            "- `webhook:manage` — full CRUD on webhook consumers (own rows only)\n"
            "- `addresses:read` — read per-camera alert addresses"
        ),
    )
    is_active: bool = Field(
        ..., description="If false, all tokens for this account are rejected at auth time.",
    )
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ServiceAccountTokenCreate(BaseModel):
    """Request body for issuing a new bearer token for an existing account."""
    name: str = Field(
        ..., min_length=1, max_length=255,
        description=(
            "Human-friendly token label — use this to track rotations. Recommended "
            "format: `<env>-<date>` or `<env>-<kid>`."
        ),
        examples=["prod-2026-05", "staging-key-1"],
    )
    expires_at: Optional[datetime] = Field(
        None,
        description=(
            "Optional UTC expiry. Once past, the token is rejected at auth time. "
            "Null = no expiry (rotate manually). Tokens are also rejected if "
            "`revoked_at` is set."
        ),
        examples=["2027-05-14T00:00:00Z"],
    )


class ServiceAccountTokenResponse(BaseModel):
    """Token metadata. Returned by list/get endpoints — never includes the raw secret.

    Use `ServiceAccountTokenWithSecret` only on the initial POST response.
    """
    id: str = Field(..., description="UUID of the token row.")
    name: str
    expires_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = Field(
        None, description="UTC timestamp of the most recent successful auth.",
    )
    revoked_at: Optional[datetime] = Field(
        None, description="If set, the token is permanently invalidated.",
    )
    created_at: datetime

    class Config:
        from_attributes = True


class ServiceAccountTokenWithSecret(ServiceAccountTokenResponse):
    """Returned **once** on token creation. The raw secret is never re-displayed.

    Treat this response body like a password reveal: store the `secret` in a
    secret manager immediately, then discard the response. If you lose it,
    issue a new token and revoke this one.
    """
    secret: str = Field(
        ...,
        description=(
            "The raw bearer token, e.g. `tlsa_<base64url-token>`. Use as "
            "`Authorization: Bearer <secret>` on subsequent requests. The "
            "`tlsa_` prefix makes tokens easy to spot in logs."
        ),
        examples=["tlsa_Xb3p9Hf2NkLqW8aZ-3kK0pQ1rS2tU3vW4xY5z"],
    )

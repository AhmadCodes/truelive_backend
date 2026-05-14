"""
Main FastAPI application entry point.
"""

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import SQLAlchemyError
import time
import logging

from app.core.config import settings
from app.database import check_db_health, SessionLocal
from app.api.v1 import auth, sites, cameras, pcs, screens, views, users, categories, sureview, snapshots, configs, stream, invitations, audit_logs, settings as settings_router
from app.api.v1 import alert_addresses, alerts as alerts_router, webhook_consumers, service_accounts


# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# OpenAPI tag metadata — drives the section headers + descriptions in Swagger UI.
# Only the alerting / service-account groups get explicit entries here; tags not
# listed get default section headers from FastAPI.
openapi_tags = [
    {
        "name": "Alerting — Addresses",
        "description": (
            "Per-camera inbound email addresses (`cam-<token>@alerts.usvg.ai`). "
            "An operator pastes these into the upstream alert source (e.g. "
            "Calipsa); mail arriving at the address is routed through Postfix → "
            "LMTP → truelive-smtp-ingest → MinIO + raw_messages, then parsed "
            "into an alert.\n\n"
            "Use these endpoints to **provision**, **rotate**, **quarantine**, "
            "or **revoke** addresses. The auto-provision hook on camera create "
            "already gives every new camera one active address, so you'll "
            "typically only use these endpoints for rotation or runaway-camera "
            "mitigation.\n\n"
            "**Address lifecycle:**\n"
            "- `is_active=true, is_quarantined=false` — accepting mail\n"
            "- `is_active=true, is_quarantined=true` — hard-blocked (reversible)\n"
            "- `is_active=false` — revoked (permanent for that token)\n"
        ),
    },
    {
        "name": "Alerting — Alerts",
        "description": (
            "Retrieve normalized alerts, their raw RFC822 source, attached "
            "media (snapshots / video clips), and webhook delivery history. "
            "Same endpoints serve human admins and downstream platforms (via "
            "a service-account token with `alerts:read` / `alerts:raw:read`).\n\n"
            "**Retention:** alerts and raw mail live 90 days; media lives 30 "
            "days. After 30 days, an alert's `media` array will be empty even "
            "if it originally had attachments — the parsed text remains "
            "available."
        ),
    },
    {
        "name": "Alerting — Webhooks",
        "description": (
            "Configure downstream consumers that receive HMAC-signed JSON "
            "POSTs for each normalized alert. v1 supports one active consumer.\n\n"
            "**Delivery contract (summary):**\n"
            "- `POST application/json` to the consumer's URL\n"
            "- Headers: `X-TrueLive-Signature: sha256=<hex>`, `-Timestamp`, "
            "`-Alert-Id`, `-Delivery-Id`\n"
            "- Consumer must ack 2xx within 5 seconds\n"
            "- Retry chain: 1m, 5m, 30m, 2h, 12h (6 attempts, ~15h window)\n"
            "- Consumer **must** dedupe on `X-TrueLive-Alert-Id` (idempotency)\n\n"
            "Full schema + signing reference: see "
            "`experiments/alerting_feature/webhook_contract.md`."
        ),
    },
    {
        "name": "Service Accounts",
        "description": (
            "Non-human principals with scoped bearer tokens. Use these when an "
            "external system needs to call TrueLive APIs without a human "
            "admin's JWT.\n\n"
            "**Auth header:** `Authorization: Bearer tlsa_<token>`\n\n"
            "**Available scopes:**\n"
            "- `alerts:read` — read normalized alerts + media URLs\n"
            "- `alerts:raw:read` — read raw RFC822 source (separate from "
            "`alerts:read` so callers can hold parsed view without unredacted "
            "original)\n"
            "- `webhook:manage` — CRUD on webhook-consumers (own rows only)\n"
            "- `addresses:read` — read per-camera alert addresses\n\n"
            "**Token lifecycle:**\n"
            "1. Admin creates the account here with the right scopes.\n"
            "2. Admin issues a token — raw `tlsa_<...>` value returned ONCE.\n"
            "3. Hand off to the downstream platform out of band.\n"
            "4. To rotate: issue a new token, swap on downstream side, then "
            "soft-revoke the old one (sets `revoked_at`).\n"
            "5. To globally disable: set `is_active=false` on the account — "
            "all tokens stop working immediately."
        ),
    },
]


# Create FastAPI application
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="TrueLive Portal Backend API for camera surveillance management",
    docs_url=f"{settings.API_V1_PREFIX}/docs",
    redoc_url=f"{settings.API_V1_PREFIX}/redoc",
    openapi_url=f"{settings.API_V1_PREFIX}/openapi.json",
    openapi_tags=openapi_tags,
)


# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


# Add Trusted Host middleware (security)
if not settings.DEBUG:
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["*"]  # Configure properly in production
    )


# Request timing middleware
@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    """Add processing time to response headers."""
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-Process-Time"] = str(process_time)
    return response


# Exception handlers
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle validation errors with detailed error messages."""
    # Process errors to ensure they are JSON serializable
    errors = []
    for error in exc.errors():
        # Convert any non-serializable objects to strings
        processed_error = {}
        for key, value in error.items():
            if key == "ctx" and isinstance(value, dict):
                # Recursively process context dict
                processed_error[key] = {k: str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v
                                       for k, v in value.items()}
            elif not isinstance(value, (str, int, float, bool, list, dict, type(None))):
                processed_error[key] = str(value)
            else:
                processed_error[key] = value
        errors.append(processed_error)

    return JSONResponse(
        status_code=422,
        content={
            "detail": "Validation error",
            "errors": errors,
        }
    )


@app.exception_handler(SQLAlchemyError)
async def sqlalchemy_exception_handler(request: Request, exc: SQLAlchemyError):
    """Handle database errors."""
    logger.error(f"Database error: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": "Database error occurred"}
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Handle general exceptions."""
    logger.error(f"Unexpected error: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"}
    )


# Health check endpoints
@app.get("/health", tags=["Health"])
async def health_check():
    """
    Health check endpoint for monitoring.
    Returns application and database health status.
    """
    db_healthy = check_db_health()

    return {
        "status": "healthy" if db_healthy else "unhealthy",
        "version": settings.APP_VERSION,
        "environment": settings.ENVIRONMENT,
        "database": "connected" if db_healthy else "disconnected",
    }


@app.get("/", tags=["Root"])
async def root():
    """Root endpoint with API information."""
    return {
        "message": "TrueLive Portal API",
        "version": settings.APP_VERSION,
        "docs": f"{settings.API_V1_PREFIX}/docs",
    }


# Include API routers
app.include_router(
    auth.router,
    prefix=f"{settings.API_V1_PREFIX}/auth",
    tags=["Authentication"]
)

app.include_router(
    invitations.router,
    prefix=f"{settings.API_V1_PREFIX}/invitations",
    tags=["Invitations"]
)

app.include_router(
    sites.router,
    prefix=f"{settings.API_V1_PREFIX}/sites",
    tags=["Sites"]
)

app.include_router(
    cameras.router,
    prefix=f"{settings.API_V1_PREFIX}/cameras",
    tags=["Cameras"]
)

app.include_router(
    pcs.router,
    prefix=f"{settings.API_V1_PREFIX}/pcs",
    tags=["PCs"]
)

app.include_router(
    screens.router,
    prefix=f"{settings.API_V1_PREFIX}/screens",
    tags=["Screens"]
)

app.include_router(
    views.router,
    prefix=f"{settings.API_V1_PREFIX}/views",
    tags=["Views"]
)

app.include_router(
    users.router,
    prefix=f"{settings.API_V1_PREFIX}/users",
    tags=["Users"]
)

app.include_router(
    audit_logs.router,
    prefix=f"{settings.API_V1_PREFIX}/audit-logs",
    tags=["Audit Logs"]
)

app.include_router(
    categories.router,
    prefix=f"{settings.API_V1_PREFIX}/categories",
    tags=["Categories"]
)

app.include_router(
    sureview.router,
    prefix=f"{settings.API_V1_PREFIX}/sureview",
    tags=["SureView"]
)

app.include_router(
    settings_router.router,
    prefix=f"{settings.API_V1_PREFIX}/settings",
    tags=["System Settings"]
)

app.include_router(
    snapshots.router,
    prefix=f"{settings.API_V1_PREFIX}/snapshots",
    tags=["Snapshots"]
)

app.include_router(
    configs.router,
    prefix=f"{settings.API_V1_PREFIX}/configs",
    tags=["Configurations"]
)

app.include_router(
    stream.router,
    prefix=f"{settings.API_V1_PREFIX}/stream",
    tags=["Streaming"]
)

# ---------------- Alerting feature ---------------- #
# Alert addresses are mounted at the root API prefix so the existing
# /cameras/{id}/alert-addresses path nests naturally.
app.include_router(
    alert_addresses.router,
    prefix=settings.API_V1_PREFIX,
    tags=["Alerting — Addresses"],
)

app.include_router(
    alerts_router.router,
    prefix=f"{settings.API_V1_PREFIX}/alerts",
    tags=["Alerting — Alerts"],
)

app.include_router(
    webhook_consumers.router,
    prefix=f"{settings.API_V1_PREFIX}/alerting",
    tags=["Alerting — Webhooks"],
)

app.include_router(
    service_accounts.router,
    prefix=f"{settings.API_V1_PREFIX}/service-accounts",
    tags=["Service Accounts"],
)


# Startup event
@app.on_event("startup")
async def startup_event():
    """Execute on application startup."""
    logger.info(f"Starting {settings.APP_NAME} v{settings.APP_VERSION}")
    logger.info(f"Environment: {settings.ENVIRONMENT}")
    logger.info(f"Debug mode: {settings.DEBUG}")

    # Check database connection
    if check_db_health():
        logger.info("Database connection established")

        # Seed default system settings if not present
        try:
            from app.services.settings_seeder import seed_settings
            db = SessionLocal()
            result = seed_settings(db)
            if result["created"] > 0:
                logger.info(f"Seeded {result['created']} default system settings")
            else:
                logger.debug("System settings already seeded")
            db.close()
        except Exception as e:
            logger.warning(f"Failed to seed system settings: {e}")
    else:
        logger.error("Failed to connect to database")


# Shutdown event
@app.on_event("shutdown")
async def shutdown_event():
    """Execute on application shutdown."""
    logger.info("Shutting down application")

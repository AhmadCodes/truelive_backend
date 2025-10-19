# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Shomer Portal is a FastAPI-based surveillance camera management system that replaced a Streamlit application. The backend manages sites, cameras, PCs, screens/views, and users with JWT authentication. It communicates with PC client applications via WebSocket to deploy camera grid configurations.

### Core Architecture

**Multi-layered FastAPI application:**
- **API Layer** (`app/api/v1/`): REST endpoints with OpenAPI docs
- **Models Layer** (`app/models/`): SQLAlchemy ORM models for PostgreSQL
- **Schemas Layer** (`app/schemas/`): Pydantic models for request/response validation
- **Services Layer** (`app/services/`): Business logic (config generation, screenshot capture, SureView integration, WebSocket)
- **Tasks Layer** (`app/tasks/`): Celery background tasks
- **Core Layer** (`app/core/`): Configuration and security (JWT, password hashing)

**Key architectural patterns:**
- Dependency injection via `app/api/deps.py` for authentication and authorization
- Role-based access control (user, admin, super_admin) with hierarchical permissions
- Type aliases for clean dependencies: `CurrentUser`, `AdminUser`, `SuperAdminUser`, `DBSession`
- Token-based auth for both users (JWT) and PC clients (JWT with longer expiration)

### Database Architecture

**13 SQLAlchemy models with complex relationships:**
- `User`, `InvitationToken`, `AuditLog` - User management
- `SiteCategory`, `SiteCategoryMapping` - Site categorization with OSD colors
- `Site`, `Camera`, `Screenshot` - Surveillance infrastructure
- `PC`, `Screen`, `View`, `ScreenMapping` - Display management
- `SiteCamerasLayoutConfig`, `SiteCamerasLayout` - Camera grid layouts

**Critical relationships:**
- Sites have multiple cameras
- PCs have self-referencing relationship (manager → controller)
- Screens belong to PCs and contain multiple Views
- Views define camera grid layouts (rows × columns)
- ScreenMapping links views to specific camera positions
- SiteCamerasLayout defines which cameras appear in site-wide grids

### Configuration Transformation Pipeline

The most complex aspect is transforming database structure to device JSON:

1. **Database structure** → Query screen mappings, sites, cameras
2. **Intermediate format** (`services/config_generator.py`) → Nested dict with pcs/screens/views/mappings
3. **Device JSON** → Array of screens with source_groups for camera switching

**Device JSON Structure Overview:**

The configuration JSON sent to PC clients defines display layouts with rotating camera views. See `json_format.md` for detailed documentation.

**High-level structure:**
```json
{
  "width": 640,
  "height": 480,
  "screens": [...]
}
```

**Screen object fields:**
- `id` - Screen identifier from database (format: `pc{id}_screen_{screen_id}`)
- `display_idx` - Display index (0-based) for multi-monitor setups
- `switchInterval` - Seconds between view rotations (e.g., 10)
- `title` - Human-readable screen name (e.g., "Monitor 1")
- `source_groups` - Array of tile arrays defining camera grid layout

**Source Groups Structure (Critical):**

`source_groups` is an array where each element represents a **tile** in the display grid:
- For a 4x4 layout: 16 elements (though not all may be populated)
- For a 3x4 layout: 12 elements
- Each tile array contains **camera objects** that rotate based on `switchInterval`

**Camera Object Fields:**
- `id` - Camera ID from database (format: `{site_id}_{camera_id}`)
- `osd_text` - On-screen display text: `"{camera_name} ({site_name})"`
- `url` - RTSP URL (URL-encoded via `app/utils/url_processor.py`)
- `osd_color` - Hex color from site category (format: `"0xFFRRGGBB"`)
- `LocationUris` - Array of RTSP URLs from `SiteCamerasLayout` for site-wide camera switching
- `use_tcp` - Boolean flag for TCP transport (default: false)

**Empty Tile Representation:**

To leave a tile blank during view rotation:
```json
{
  "id": "",
  "osd_text": "",
  "url": "",
  "osd_color": "0xFFFFFFFF",
  "LocationUris": [],
  "use_tcp": false
}
```

**Example - Tile with 2 Views:**

A single tile that rotates between 2 cameras every 10 seconds:
```json
{
  "source_groups": [
    [
      {
        "id": "10538_193628",
        "osd_text": " 13-21 Lexington 132 ( 13-21 Lexington)",
        "url": "rtsp://admin:password@66.108.96.217:8554/Streaming/channels/202",
        "osd_color": "0xFFDC42FF",
        "LocationUris": ["rtsp://...", "rtsp://..."],
        "use_tcp": false
      },
      {
        "id": "10207_195041",
        "osd_text": "Embedded Net DVR  13 (Embedded Net DVR Chabad)",
        "url": "rtsp://admin:12345%40ny@110emerson.ddns.net:8554/Streaming/Channels/1302",
        "osd_color": "0xFFFF0006",
        "LocationUris": ["rtsp://...", "rtsp://..."],
        "use_tcp": false
      }
    ]
  ]
}
```

**Key Concepts:**
- **View Rotation**: Camera objects within a tile array rotate sequentially based on `switchInterval`
- **Grid Position**: Index in `source_groups` determines tile position (row-major order)
- **Multi-Site Support**: Different cameras from different sites can appear in same grid with color-coded OSD
- **URL Encoding**: Passwords with special characters (@, :, /) are URL-encoded before transmission

### Real-time Communication

**WebSocket Server** (`app/services/websocket_server.py`):
- Socket.IO server on port 8080
- PC client registration with JWT validation
- Configuration deployment to specific PCs
- Online/offline status tracking
- Message routing between portal and PC clients

**Events handled:**
- `connect` - Client connection
- `register` - PC authentication with JWT
- `message` - Route config/commands to target PC
- `get_clients` - Request connected PC list
- `disconnect` - Cleanup on disconnect

## Development Commands

### Local Development Setup

```bash
# Create conda environment
conda create -n py312 python=3.12 -y
conda activate py312

# Install dependencies
pip install -r requirements.txt
pip install -r test-requirements.txt

# Copy and configure environment
cp .env.example .env
# Edit .env - set SECRET_KEY (32+ chars) and JWT_SECRET (16+ chars)
```

### Database Operations

```bash
# Start PostgreSQL and Redis via Docker
docker-compose up -d postgres redis

# Create migration from model changes
alembic revision --autogenerate -m "description"

# Apply migrations
alembic upgrade head

# Downgrade one migration
alembic downgrade -1

# Initialize database with admin user and default category
python scripts/init_db.py
# Default credentials: admin / admin123 (CHANGE IMMEDIATELY)
```

### Running the Application

```bash
# Run FastAPI (local development)
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Run Celery worker
celery -A app.tasks.celery_app worker --loglevel=info

# Run Celery beat scheduler
celery -A app.tasks.celery_app beat --loglevel=info

# Run WebSocket server
python -m app.services.websocket_server

# Run all services via Docker
docker-compose up -d

# View logs
docker-compose logs -f backend
```

### Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=app --cov-report=html

# Run specific test file
pytest tests/unit/test_config_generator.py -v

# Run specific test
pytest tests/unit/test_config_generator.py::test_function_name -v
```

### Code Quality

```bash
# Format code
black app/

# Lint code
flake8 app/

# Type checking
mypy app/
```

## Key Implementation Details

### Authentication Flow

1. User logs in via `/api/v1/auth/login` → returns access + refresh tokens
2. Client includes token in `Authorization: Bearer <token>` header
3. `get_current_user` dependency validates JWT and loads user from DB
4. Role checks via `get_admin_user` or `get_super_admin_user` dependencies
5. PC clients use separate JWT tokens with longer expiration (8760 hours)

### Configuration Deployment Flow

1. Admin creates/updates screens, views, and camera mappings via API
2. Admin calls deploy endpoint → triggers WebSocket message
3. WebSocket server routes config to target PC by ID
4. PC client receives config, applies layout, updates `last_applied` timestamp

### Background Tasks

**Celery tasks run every 10 minutes** (`BACKGROUND_TASK_INTERVAL=600`):
- Screenshot capture: OpenCV-based RTSP frame capture for camera thumbnails
- SureView sync: Selenium-based scraping of SureView NVR for device discovery

### RTSP URL Processing

The `app/utils/url_processor.py` handles URL encoding:
- Encodes passwords in RTSP URLs (handles special characters like @, :, /)
- Critical for cameras with complex passwords
- Used by config generator before sending URLs to PC clients

## Common Patterns

### Creating New API Endpoints

Follow the pattern in `app/api/v1/sites.py`:

```python
from fastapi import APIRouter, HTTPException, status
from app.api.deps import DBSession, CurrentUser, AdminUser
from app.schemas.resource import ResourceCreate, ResourceResponse

router = APIRouter()

@router.post("/", response_model=ResourceResponse, status_code=status.HTTP_201_CREATED)
async def create_resource(
    data: ResourceCreate,
    db: DBSession,
    current_user: AdminUser  # Use AdminUser or SuperAdminUser as needed
):
    # Implementation
    pass
```

### Creating Pydantic Schemas

Pattern in `app/schemas/site.py`:

```python
from pydantic import BaseModel, Field
from datetime import datetime

class ResourceBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)

class ResourceCreate(ResourceBase):
    pass

class ResourceUpdate(BaseModel):
    name: str | None = None

class ResourceResponse(ResourceBase):
    id: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
```

### Working with Database Models

All models inherit from `BaseModel` which includes UUID `id` and timestamps. Use the `to_dict()` method for serialization:

```python
from app.models.site import Site

site = db.query(Site).filter(Site.id == site_id).first()
site_dict = site.to_dict()  # Includes id, created_at, updated_at
```

## Environment Variables

**Required (app will not start without these):**
- `SECRET_KEY` - Min 32 chars for general encryption
- `JWT_SECRET` - Min 16 chars for JWT signing
- `DATABASE_URL` - PostgreSQL connection string

**Optional but recommended:**
- `REDIS_URL` - For Celery and caching
- `CELERY_BROKER_URL` - Message broker for background tasks
- `WEBSOCKET_URL` - WebSocket server endpoint
- `SUREVIEW_USERNAME`, `SUREVIEW_PASSWORD`, `SUREVIEW_API_URL` - SureView integration

Generate secure keys:
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"  # SECRET_KEY
python -c "import secrets; print(secrets.token_urlsafe(24))"  # JWT_SECRET
```

## Docker Architecture

**Multi-stage Dockerfile:**
- Base stage: Python 3.11 + system deps (gcc, libpq, chromium for Selenium)
- Development stage: Adds dev tools (pytest, black, mypy)
- Production stage: Non-root user, health checks

**Docker Compose services:**
- `postgres` - PostgreSQL 15 database
- `redis` - Cache and message broker
- `backend` - FastAPI app (uvicorn with 4 workers)
- `celery_worker` - Background task processor
- `celery_beat` - Task scheduler
- `websocket` - Socket.IO server
- `migration` - One-time Alembic migration runner

**Important:** After changing `requirements.txt`, rebuild:
```bash
docker-compose down
docker-compose build --no-cache
docker-compose up -d
```

## Testing Structure

**Fixtures in `tests/conftest.py`:**
- `engine`, `db_session` - In-memory SQLite for tests
- `sample_site`, `sample_camera`, `sample_pc`, etc. - Pre-populated test data
- `mock_cv2`, `mock_selenium`, `mock_socketio_client` - External dependency mocks
- `mock_settings` - Application settings for tests

**Test organization:**
- `tests/unit/` - Service and utility function tests
- `tests/integration/` - API endpoint tests
- `tests/test_api/` - Additional API tests
- `tests/test_models/` - Model validation tests

## File Naming Conventions

- Models: `app/models/resource.py` (singular)
- Schemas: `app/schemas/resource.py` (matches model name)
- API routes: `app/api/v1/resources.py` (plural for collection endpoints)
- Services: `app/services/resource_service.py` (descriptive)
- Tasks: `app/tasks/resource_tasks.py` (Celery tasks)

## Common Troubleshooting

**Database connection issues:**
```bash
docker-compose ps postgres  # Check status
docker-compose logs postgres  # View logs
docker-compose exec postgres psql -U shomer -d shomer_portal  # Direct access
```

**WebSocket connection issues:**
```bash
docker-compose logs websocket
curl http://localhost:8080/socket.io/?transport=polling  # Test endpoint
```

**Migration conflicts:**
- Always review auto-generated migrations before applying
- Use `alembic downgrade -1` to undo if needed
- Check `alembic/versions/` for migration history

**Celery tasks not running:**
```bash
docker-compose logs celery_worker
docker-compose logs celery_beat
# Ensure Redis is healthy
docker-compose ps redis
```

## API Documentation

Once running, access:
- Swagger UI: http://localhost:8000/api/v1/docs
- ReDoc: http://localhost:8000/api/v1/redoc
- Health check: http://localhost:8000/health

The OpenAPI schema is auto-generated from Pydantic models and route definitions.

## SureView Integration Details

### SureView API Endpoints (Already Implemented)

The following endpoints are available at `/api/v1/sureview/`:

1. **POST /get_sites** - Get sites filtered by customer_id and optionally by site_ids
   - Requires authentication (CurrentUser)
   - Returns detailed site information including address, contacts, notes, camera counts

2. **POST /get_all_sites** - Get all sites grouped by customer_id
   - Requires authentication (CurrentUser)
   - Returns summary of all sites organized by customer

3. **POST /get_cameras** - Get all cameras for a specific site
   - Requires authentication (CurrentUser)
   - Returns camera details including camera_id, camera_name, rtsp_url

4. **POST /sync** - Manually trigger SureView synchronization
   - Requires admin privileges (AdminUser)
   - Fetches servers from SureView API, retrieves group details for each server
   - Updates database with sites and cameras, including customer_id from referenceId

### SureView Data Flow

The sync process (`app/services/sureview_service.py`):
1. Authenticates to SureView via Selenium (automated login)
2. Calls `GET /api/servers/GetServerList` to get all servers
3. For each server, calls `GET /api/groups/{groupID}` to get site details:
   - `referenceId` → stored as `customer_id` in Site model
   - `address`, `telephone`, `telephone2`, `telephonePolice`, `telephoneFire`, `notes`, `latLong`
4. Calls `GET /api/devices/GetByServerId` to get cameras for each server
5. Creates/updates Site and Camera records in database
6. Removes stale entries not present in SureView

### Important Site Model Fields

The Site model includes SureView-specific fields:
- `customer_id` - From SureView's `referenceId` (indexed for filtering)
- `address` - Physical location
- `telephone`, `telephone2` - Contact numbers
- `telephone_police`, `telephone_fire` - Emergency contacts
- `notes` - Site instructions and details
- `lat_long` - GPS coordinates
- `sureview_site` - Boolean flag for SureView-managed sites


## Development Environment Notes

**Conda Environment:**
- Use conda environment: `streaming_app`
- If it doesn't exist, create it with: `conda create -n streaming_app python=3.12 -y`

**Test Files and Experiments:**
- **IMPORTANT**: ALL test scripts, experimental code, validation tools, and results MUST be placed in the `experiments/` folder
- **NEVER** create test files or results directly in the project root
- The `experiments/` folder is gitignored and organized as follows:
  - `experiments/e2e_tests/` - End-to-end integration tests
  - `experiments/config_tests/` - Configuration validation tests
  - `experiments/reports/` - Documentation and test reports
  - `experiments/results/` - JSON outputs and test results
- When creating new test files, always place them in the appropriate experiments subdirectory
- See `experiments/README.md` for detailed organization and usage instructions
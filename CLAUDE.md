# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

TrueLive Portal is a FastAPI-based surveillance camera management system. The backend manages sites, cameras, PCs, screens/views, and users with JWT authentication. It communicates with PC client applications via WebSocket to deploy camera grid configurations.

## Project Structure

- **Backend:** `/root/streaming_app` (this repository) - FastAPI + PostgreSQL + Redis + Celery
- **Frontend:** `/root/streaming_app_frontend` - Next.js React application

## Development Commands

```bash
# Environment setup (use existing conda env if available)
conda activate streaming_app
# Or create new: conda create -n streaming_app python=3.12 -y

# Install dependencies
pip install -r requirements.txt
pip install -r test-requirements.txt

# Database operations
docker-compose up -d postgres redis          # Start services
alembic revision --autogenerate -m "desc"    # Create migration
alembic upgrade head                         # Apply migrations
alembic downgrade -1                         # Undo last migration
python scripts/init_db.py                    # Initialize with admin user (admin/admin123)

# Run application locally
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
celery -A app.tasks.celery_app worker --loglevel=info
celery -A app.tasks.celery_app beat --loglevel=info
python -m app.services.websocket_server

# Docker (full stack)
docker-compose up -d
docker-compose logs -f backend
docker-compose build --no-cache  # After requirements.txt changes

# Testing
pytest                                                     # All tests
pytest --cov=app --cov-report=html                        # With coverage
pytest tests/unit/test_config_generator.py -v             # Specific file
pytest tests/unit/test_config_generator.py::test_name -v  # Specific test

# Code quality
black app/
flake8 app/
mypy app/
```

## Architecture

### Application Layers

```
app/
├── api/v1/         # REST endpoints (auth, sites, cameras, pcs, screens, users, categories, etc.)
├── api/deps.py     # Dependency injection (CurrentUser, AdminUser, SuperAdminUser, DBSession)
├── models/         # SQLAlchemy ORM models (13 tables)
├── schemas/        # Pydantic request/response validation
├── services/       # Business logic (config_generator, websocket_server, sureview_service, etc.)
├── tasks/          # Celery background tasks (screenshot capture, SureView sync)
├── core/           # Configuration (config.py) and security (JWT, password hashing)
└── utils/          # Utilities (url_processor for RTSP URL encoding)
```

### Key Patterns

- **Dependency injection** via `app/api/deps.py` for auth and DB sessions
- **Role-based access control**: user, admin, super_admin with hierarchical permissions
- **Type aliases**: `CurrentUser`, `AdminUser`, `SuperAdminUser`, `DBSession`
- **JWT tokens**: Short-lived for users, long-lived (8760h) for PC clients

### Database Models

- **User management:** User, InvitationToken, AuditLog
- **Site categorization:** SiteCategory, SiteCategoryMapping (with OSD colors)
- **Surveillance:** Site, Camera, Screenshot
- **Display management:** PC (self-referencing manager→controller), Screen, View, ScreenMapping
- **Camera layouts:** SiteCamerasLayoutConfig, SiteCamerasLayout

### Configuration Transformation Pipeline

The core complexity is transforming database structure to device JSON:

1. **Database** → Query screen mappings, sites, cameras
2. **Config Generator** (`services/config_generator.py`) → Nested dict with pcs/screens/views/mappings
3. **Device JSON** → Array of screens with source_groups for camera switching

See `json_format.md` for complete device JSON structure documentation.

### Real-time Communication

WebSocket Server (`app/services/websocket_server.py`):
- Socket.IO on port 8080
- PC client registration with JWT validation
- Configuration deployment to specific PCs
- Events: `connect`, `register`, `message`, `get_clients`, `disconnect`

### Background Tasks

Celery tasks run every 10 minutes (`BACKGROUND_TASK_INTERVAL=600`):
- **Screenshot capture**: OpenCV-based RTSP frame capture
- **SureView sync**: Selenium-based scraping for device discovery

## Environment Variables

**Required:**
- `SECRET_KEY` - Min 32 chars for encryption
- `JWT_SECRET` - Min 16 chars for JWT signing
- `DATABASE_URL` - PostgreSQL connection string

**Optional:**
- `REDIS_URL`, `CELERY_BROKER_URL` - For background tasks
- `WEBSOCKET_URL` - WebSocket server endpoint
- `SUREVIEW_USERNAME`, `SUREVIEW_PASSWORD`, `SUREVIEW_API_URL` - SureView integration

Generate keys: `python -c "import secrets; print(secrets.token_urlsafe(32))"`

## Common Patterns

### Creating API Endpoints

```python
from fastapi import APIRouter, HTTPException, status
from app.api.deps import DBSession, CurrentUser, AdminUser

router = APIRouter()

@router.post("/", response_model=ResourceResponse, status_code=status.HTTP_201_CREATED)
async def create_resource(
    data: ResourceCreate,
    db: DBSession,
    current_user: AdminUser  # Use AdminUser or SuperAdminUser as needed
):
    pass
```

### Creating Pydantic Schemas

```python
from pydantic import BaseModel, Field

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

### Working with Models

All models inherit from `BaseModel` with UUID `id` and timestamps. Use `to_dict()` for serialization.

## RTSP URL Processing

`app/utils/url_processor.py` handles URL encoding for passwords with special characters (@, :, /). Critical for cameras with complex passwords.

## Docker Services

- `postgres` - PostgreSQL 15
- `redis` - Cache and message broker
- `backend` - FastAPI (uvicorn, 4 workers)
- `celery_worker` - Background task processor
- `celery_beat` - Task scheduler
- `websocket` - Socket.IO server
- `migration` - One-time Alembic runner

## Testing

Fixtures in `tests/conftest.py`:
- `engine`, `db_session` - In-memory SQLite
- `sample_site`, `sample_camera`, `sample_pc`, etc. - Pre-populated test data
- `mock_cv2`, `mock_selenium`, `mock_socketio_client` - External dependency mocks

Organization: `tests/unit/`, `tests/integration/`, `tests/test_api/`, `tests/test_models/`

## SureView Integration

Endpoints at `/api/v1/sureview/`:
- `POST /get_sites` - Get sites by customer_id
- `POST /get_all_sites` - Get all sites grouped by customer
- `POST /get_cameras` - Get cameras for a site
- `POST /sync` - Trigger synchronization (AdminUser)

Sync process (`app/services/sureview_service.py`):
1. Selenium login to SureView
2. `GET /api/servers/GetServerList` → all servers
3. `GET /api/groups/{groupID}` → site details (referenceId → customer_id)
4. `GET /api/devices/GetByServerId` → cameras
5. Create/update Site and Camera records, remove stale entries

## API Documentation

- Swagger UI: http://localhost:8000/api/v1/docs
- ReDoc: http://localhost:8000/api/v1/redoc
- Health: http://localhost:8000/health

## Test Files and Experiments

**IMPORTANT:** ALL test scripts, experimental code, and results MUST be placed in `experiments/` folder (gitignored):
- `experiments/e2e_tests/` - End-to-end tests
- `experiments/config_tests/` - Configuration validation
- `experiments/reports/` - Documentation and reports
- `experiments/results/` - JSON outputs and results

**NEVER** create test files directly in the project root.

## Troubleshooting

```bash
# Database
docker-compose ps postgres
docker-compose logs postgres
docker-compose exec postgres psql -U truelive -d truelive_portal

# WebSocket
docker-compose logs websocket
curl http://localhost:8080/socket.io/?transport=polling

# Celery
docker-compose logs celery_worker
docker-compose logs celery_beat
docker-compose ps redis
```


# Agentic SDLC and Spec-Driven Development

Kiro-style Spec-Driven Development on an agentic SDLC

## Project Context

### Paths
- Steering: `.kiro/steering/`
- Specs: `.kiro/specs/`

### Steering vs Specification

**Steering** (`.kiro/steering/`) - Guide AI with project-wide rules and context
**Specs** (`.kiro/specs/`) - Formalize development process for individual features

### Active Specifications
- Check `.kiro/specs/` for active specifications
- Use `/kiro-spec-status [feature-name]` to check progress

## Development Guidelines
- Think in English, generate responses in English. All Markdown content written to project files (e.g., requirements.md, design.md, tasks.md, research.md, validation reports) MUST be written in the target language configured for this specification (see spec.json.language).

## Minimal Workflow
- Phase 0 (optional): `/kiro-steering`, `/kiro-steering-custom`
- Discovery: `/kiro-discovery "idea"` — determines action path, writes brief.md + roadmap.md for multi-spec projects
- Phase 1 (Specification):
  - Single spec: `/kiro-spec-quick {feature} [--auto]` or step by step:
    - `/kiro-spec-init "description"`
    - `/kiro-spec-requirements {feature}`
    - `/kiro-validate-gap {feature}` (optional: for existing codebase)
    - `/kiro-spec-design {feature} [-y]`
    - `/kiro-validate-design {feature}` (optional: design review)
    - `/kiro-spec-tasks {feature} [-y]`
  - Multi-spec: `/kiro-spec-batch` — creates all specs from roadmap.md in parallel by dependency wave
- Phase 2 (Implementation): `/kiro-impl {feature} [tasks]`
  - Without task numbers: autonomous mode (subagent per task + independent review + final validation)
  - With task numbers: manual mode (selected tasks in main context, still reviewer-gated before completion)
  - `/kiro-validate-impl {feature}` (standalone re-validation)
- Progress check: `/kiro-spec-status {feature}` (use anytime)

## Skills Structure
Skills are located in `.claude/skills/kiro-*/SKILL.md`
- Each skill is a directory with a `SKILL.md` file
- Skills run inline with access to conversation context
- Skills may delegate parallel research to subagents for efficiency
- Additional files (templates, examples) can be added to skill directories
- `kiro-review` — task-local adversarial review protocol used by reviewer subagents
- `kiro-debug` — root-cause-first debug protocol used by debugger subagents
- `kiro-verify-completion` — fresh-evidence gate before success or completion claims
- **If there is even a 1% chance a skill applies to the current task, invoke it.** Do not skip skills because the task seems simple.

## Development Rules
- 3-phase approval workflow: Requirements → Design → Tasks → Implementation
- Human review required each phase; use `-y` only for intentional fast-track
- Keep steering current and verify alignment with `/kiro-spec-status`
- Follow the user's instructions precisely, and within that scope act autonomously: gather the necessary context and complete the requested work end-to-end in this run, asking questions only when essential information is missing or the instructions are critically ambiguous.

## Steering Configuration
- Load entire `.kiro/steering/` as project memory
- Default files: `product.md`, `tech.md`, `structure.md`
- Custom files are supported (managed via `/kiro-steering-custom`)

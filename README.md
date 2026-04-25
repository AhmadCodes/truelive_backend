# TrueLive Portal - FastAPI Backend

Complete backend API for TrueLive Portal camera surveillance management system.

## Overview

This is a production-ready FastAPI backend that replaces the Streamlit application with a RESTful API and WebSocket server. The system manages:

- **Sites & Cameras**: Surveillance site and camera configuration
- **PCs & Screens**: Multi-screen display management
- **Views & Layouts**: Custom camera grid layouts
- **Users & Auth**: JWT-based authentication with RBAC
- **Real-time Communication**: WebSocket for live configuration deployment
- **Background Tasks**: Screenshot capture and SureView device synchronization

## Technology Stack

- **FastAPI** - Modern async web framework
- **PostgreSQL** - Production database
- **Redis** - Caching and message broker
- **Celery** - Background task processing
- **Socket.IO** - WebSocket communication
- **SQLAlchemy** - ORM
- **Alembic** - Database migrations
- **JWT** - Authentication tokens
- **Docker** - Containerization

## Project Structure

```
backend/
├── app/
│   ├── api/
│   │   ├── deps.py              # Dependency injection (✅ Complete)
│   │   └── v1/
│   │       ├── auth.py          # Authentication endpoints (✅ Complete)
│   │       ├── sites.py         # Site management (✅ Complete)
│   │       ├── cameras.py       # Camera management (⚠️  Stub)
│   │       ├── pcs.py           # PC management (⚠️  Stub)
│   │       ├── screens.py       # Screen/View management (⚠️  Stub)
│   │       ├── users.py         # User management (⚠️  Stub)
│   │       └── categories.py    # Category management (⚠️  Stub)
│   │
│   ├── core/
│   │   ├── config.py            # Settings (✅ Complete)
│   │   └── security.py          # JWT & password hashing (✅ Complete)
│   │
│   ├── models/                  # SQLAlchemy models (✅ All 13 tables complete)
│   │   ├── base.py
│   │   ├── user.py
│   │   ├── site.py
│   │   ├── camera.py
│   │   ├── screenshot.py
│   │   ├── pc.py
│   │   ├── category.py
│   │   ├── site_camera_layout.py
│   │   └── ...
│   │
│   ├── schemas/                 # Pydantic schemas (⚠️  Partial)
│   │   ├── auth.py              # (✅ Complete)
│   │   └── site.py              # (✅ Complete)
│   │
│   ├── services/                # Business logic (❌ To implement)
│   │   ├── config_generator.py
│   │   ├── screenshot_service.py
│   │   ├── sureview_service.py
│   │   └── websocket_service.py
│   │
│   ├── tasks/                   # Background tasks (❌ To implement)
│   │   ├── celery_app.py
│   │   ├── screenshot_tasks.py
│   │   └── sureview_tasks.py
│   │
│   ├── utils/                   # Utilities (❌ To implement)
│   │   └── url_processor.py
│   │
│   ├── database.py              # DB connection (✅ Complete)
│   └── main.py                  # FastAPI app (✅ Complete)
│
├── alembic/                     # Migrations (⚠️  Setup complete, needs migrations)
│   ├── env.py                   # (✅ Complete)
│   └── versions/
│
├── docker/
│   ├── Dockerfile               # (✅ Complete)
│   └── docker-compose.yml       # (✅ Complete)
│
├── requirements.txt             # (✅ Complete)
├── .env.example                 # (✅ Complete)
└── README.md                    # This file
```

## Quick Start

### Prerequisites

- Python 3.11+
- Docker & Docker Compose
- PostgreSQL 15+ (if running locally)
- Redis 7+

### 1. Environment Setup

```bash
# Clone/navigate to backend directory
cd backend/

# Create conda environment
conda create -n py312 python=3.12 -y
conda activate py312

# Install dependencies
pip install -r requirements.txt

# Copy environment file
cp .env.example .env

# Edit .env and set your secrets
nano .env  # Update SECRET_KEY, JWT_SECRET, DATABASE_URL, etc.
```

### 2. Database Setup (Local Development)

```bash
# Start PostgreSQL and Redis with Docker
docker-compose up -d postgres redis

# Wait for services to be healthy
docker-compose ps

# Run database migrations
alembic upgrade head

# Create initial super admin user (manual via psql or Python script)
```

### 3. Run the Application

#### Option A: Docker Compose (Recommended)

```bash
# Start all services
docker-compose up -d

# View logs
docker-compose logs -f backend

# Access API
# http://localhost:8000/api/v1/docs
```

#### Option B: Local Development

```bash
# Activate conda environment
conda activate py312

# Run FastAPI
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# In separate terminals:
# Run Celery worker
celery -A app.tasks.celery_app worker --loglevel=info

# Run Celery beat
celery -A app.tasks.celery_app beat --loglevel=info

# Run WebSocket server
python -m app.services.websocket_server
```

### 4. API Documentation

Once running, access:
- **Swagger UI**: http://localhost:8000/api/v1/docs
- **ReDoc**: http://localhost:8000/api/v1/redoc
- **OpenAPI JSON**: http://localhost:8000/api/v1/openapi.json

## Implementation Status

### ✅ Completed

1. **Project Structure** - Complete directory layout with all folders
2. **Configuration** - Settings, environment variables, logging
3. **Database Models** - All 13 tables with relationships, indexes, constraints
4. **Authentication** - JWT tokens, password hashing, login/logout
5. **Authorization** - Role-based access control (user/admin/super_admin)
6. **Dependencies** - FastAPI dependency injection for auth
7. **Docker Setup** - Multi-stage Dockerfile, docker-compose with all services
8. **Alembic** - Migration framework configured
9. **Core API** - Sites management endpoints (complete example)

### ⚠️  In Progress / To Complete

1. **Pydantic Schemas** - Create for cameras, PCs, screens, views, users, categories
2. **API Endpoints** - Complete implementation for:
   - Cameras (CRUD + screenshot serving)
   - PCs (CRUD + token generation)
   - Screens & Views (CRUD + layout management)
   - Users (CRUD + invitation system)
   - Categories (CRUD)
   - Configuration deployment

3. **Services**:
   - Configuration generator (DB → device JSON)
   - Screenshot capture service (OpenCV)
   - SureView integration (Selenium)
   - WebSocket service

4. **Background Tasks**:
   - Celery app configuration
   - Screenshot update task (every 10 min)
   - SureView sync task (every 10 min)

5. **Database Migrations**:
   - Create initial migration with all tables
   - Add seed data (default category, admin user)

6. **Testing**:
   - Unit tests
   - Integration tests
   - API endpoint tests

## Implementation Guide

### Creating Remaining API Endpoints

Follow the pattern from `sites.py`. Each endpoint file should:

1. Import dependencies from `app.api.deps`
2. Use Pydantic schemas for request/response validation
3. Apply proper authorization (CurrentUser, AdminUser, SuperAdminUser)
4. Handle errors with HTTPException
5. Return appropriate status codes

**Example for cameras.py:**

```python
from fastapi import APIRouter, HTTPException, status
from app.api.deps import DBSession, CurrentUser, AdminUser
from app.models.camera import Camera
from app.schemas.camera import CameraCreate, CameraResponse

router = APIRouter()

@router.post("/{site_id}/cameras", response_model=CameraResponse)
async def create_camera(
    site_id: str,
    camera_data: CameraCreate,
    db: DBSession,
    current_user: AdminUser
):
    # Implementation here
    pass
```

### Creating Pydantic Schemas

Create schemas in `app/schemas/` following the pattern from `auth.py` and `site.py`:

```python
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime

class CameraBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    rtsp_url: str

class CameraCreate(CameraBase):
    pass

class CameraResponse(CameraBase):
    id: str
    site_id: str
    created_at: datetime

    class Config:
        from_attributes = True
```

### Database Migrations

```bash
# Create migration automatically from model changes
alembic revision --autogenerate -m "Initial migration with all tables"

# Review the generated migration in alembic/versions/
# Edit if needed, then apply:
alembic upgrade head

# Downgrade if needed
alembic downgrade -1
```

### Background Tasks with Celery

Create `app/tasks/celery_app.py`:

```python
from celery import Celery
from app.core.config import settings

celery_app = Celery(
    "truelive_tasks",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)

# Import tasks
from app.tasks import screenshot_tasks, sureview_tasks
```

### WebSocket Server

Create `app/services/websocket_server.py` using Socket.IO:

```python
import socketio
from app.core.config import settings

sio = socketio.Server(async_mode='eventlet', cors_allowed_origins='*')
app = socketio.WSGIApp(sio)

# PC client tracking
connected_pcs = {}

@sio.event
def connect(sid, environ):
    print(f"Client connected: {sid}")

@sio.event
def register(sid, data):
    # Validate JWT token
    # Register PC client
    # Broadcast client list update
    pass

if __name__ == '__main__':
    import eventlet
    eventlet.wsgi.server(eventlet.listen(('', 8080)), app)
```

## API Endpoints Reference

Based on the specification in `truelive_portal_features.md`, implement these ~75 endpoints:

### Authentication
- POST `/api/v1/auth/login` ✅
- POST `/api/v1/auth/refresh` ✅
- POST `/api/v1/auth/logout` ✅
- GET `/api/v1/auth/me` ✅

### Sites
- GET `/api/v1/sites` ✅
- POST `/api/v1/sites` ✅
- GET `/api/v1/sites/{site_id}` ✅
- PUT `/api/v1/sites/{site_id}` ✅
- DELETE `/api/v1/sites/{site_id}` ✅
- PUT `/api/v1/sites/{site_id}/category` ✅

### Cameras
- GET `/api/v1/cameras`
- POST `/api/v1/sites/{site_id}/cameras`
- PUT `/api/v1/cameras/{camera_id}`
- DELETE `/api/v1/cameras/{camera_id}`
- GET `/api/v1/cameras/{camera_id}/screenshot`

### PCs
- GET `/api/v1/pcs`
- POST `/api/v1/pcs`
- PUT `/api/v1/pcs/{pc_id}`
- DELETE `/api/v1/pcs/{pc_id}`
- POST `/api/v1/pcs/{pc_id}/tokens/generate`
- POST `/api/v1/pcs/{pc_id}/deploy`
- GET `/api/v1/pcs/{pc_id}/config`
- GET `/api/v1/pcs/{pc_id}/status`

### Screens & Views
- GET `/api/v1/pcs/{pc_id}/screens`
- POST `/api/v1/pcs/{pc_id}/screens`
- GET `/api/v1/screens/{screen_id}/views`
- POST `/api/v1/screens/{screen_id}/views`
- PUT `/api/v1/views/{view_id}`
- DELETE `/api/v1/views/{view_id}`
- PUT `/api/v1/views/{view_id}/mappings`
- DELETE `/api/v1/views/{view_id}/mappings/{row}/{col}`

### Users
- GET `/api/v1/users`
- POST `/api/v1/users`
- PUT `/api/v1/users/{user_id}`
- DELETE `/api/v1/users/{user_id}`
- POST `/api/v1/users/{user_id}/invite`
- PATCH `/api/v1/users/{user_id}/password`

### Categories
- GET `/api/v1/categories`
- POST `/api/v1/categories`
- PUT `/api/v1/categories/{category_id}`
- DELETE `/api/v1/categories/{category_id}`

(See `PLAN.md` and `truelive_portal_features.md` for complete list)

## Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=app --cov-report=html

# Run specific test file
pytest tests/test_api/test_auth.py -v
```

## Deployment

### Production Checklist

- [ ] Set strong SECRET_KEY and JWT_SECRET
- [ ] Configure PostgreSQL with proper credentials
- [ ] Set up Redis with password
- [ ] Configure CORS origins properly
- [ ] Enable HTTPS
- [ ] Set up monitoring (Sentry, logging)
- [ ] Configure backup strategy
- [ ] Set up CI/CD pipeline
- [ ] Review security settings
- [ ] Load test the application

### Docker Production Deployment

```bash
# Build and start all services
docker-compose -f docker-compose.yml up -d --build

# View logs
docker-compose logs -f

# Scale workers
docker-compose up -d --scale celery_worker=4
```

## Troubleshooting

### Database Connection Issues

```bash
# Check PostgreSQL is running
docker-compose ps postgres

# View PostgreSQL logs
docker-compose logs postgres

# Connect to PostgreSQL
docker-compose exec postgres psql -U truelive -d truelive_portal
```

### Celery Tasks Not Running

```bash
# Check Redis is running
docker-compose ps redis

# View Celery worker logs
docker-compose logs celery_worker

# Check Celery beat schedule
docker-compose logs celery_beat
```

### WebSocket Connection Issues

```bash
# Check WebSocket server is running
docker-compose ps websocket

# Test WebSocket connection
curl http://localhost:8080/socket.io/?transport=polling
```

## Documentation

- **API Specification**: `truelive_portal_features.md` - Complete API docs
- **Screen Layout**: `SCREEN_LAYOUT_ANALYSIS.md` - Detailed implementation guide
- **Implementation Plan**: `PLAN.md` - Development roadmap
- **Original Analysis**: `CLAUDE_PLAN.md` - Project analysis

## Contributing

1. Follow existing code patterns
2. Write tests for new features
3. Update documentation
4. Use type hints
5. Run black formatter: `black app/`
6. Check with flake8: `flake8 app/`

## License

Proprietary - TrueLive Portal

## Support

For issues and questions, refer to:
- API Documentation: http://localhost:8000/api/v1/docs
- Project Documentation: See docs/ folder
- Source Documentation: `truelive_portal_features.md`

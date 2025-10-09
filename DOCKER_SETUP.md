# Docker Setup Instructions

## Quick Fix for Current Errors

The errors you're seeing are due to missing environment variables. Follow these steps:

### 1. Create `.env` file from example

```bash
cp .env.example .env
```

### 2. Update the `.env` file with proper values

Edit `.env` and set at least these required fields:

```env
SECRET_KEY=your-super-secret-key-at-least-32-characters-long-change-this
JWT_SECRET=your-jwt-secret-key-minimum-16-chars
```

**Important**: Generate strong random keys for production:

```bash
# Generate SECRET_KEY (32+ characters)
python -c "import secrets; print(secrets.token_urlsafe(32))"

# Generate JWT_SECRET (16+ characters)
python -c "import secrets; print(secrets.token_urlsafe(24))"
```

### 3. Rebuild and restart containers

```bash
# Stop all containers
docker-compose down

# Rebuild images (important after requirements.txt change)
docker-compose build --no-cache

# Start containers
docker-compose up -d
```

### 4. Check container status

```bash
docker-compose ps
docker-compose logs -f
```

## What Was Fixed

1. **Added PyJWT package** to `requirements.txt` - Required by the websocket server
2. **Added missing environment variables** to all services in `docker-compose.yml`:
   - `SECRET_KEY` - Added to celery_worker, celery_beat, websocket, and migration
   - `JWT_SECRET` - Added to celery_worker, celery_beat, websocket, and migration

## Environment Variables by Service

### Backend
- DATABASE_URL ✓
- REDIS_URL ✓
- CELERY_BROKER_URL ✓
- CELERY_RESULT_BACKEND ✓
- WEBSOCKET_URL ✓
- SECRET_KEY ✓
- JWT_SECRET ✓

### Celery Worker & Beat
- DATABASE_URL ✓
- REDIS_URL ✓
- CELERY_BROKER_URL ✓
- CELERY_RESULT_BACKEND ✓
- SECRET_KEY ✓ (FIXED)
- JWT_SECRET ✓ (FIXED)

### WebSocket
- DATABASE_URL ✓
- REDIS_URL ✓
- SECRET_KEY ✓ (FIXED)
- JWT_SECRET ✓
- WEBSOCKET_PORT ✓

### Migration
- DATABASE_URL ✓
- SECRET_KEY ✓ (FIXED)
- JWT_SECRET ✓ (FIXED)

## Troubleshooting

### If containers still fail:

1. **Check logs for specific service:**
   ```bash
   docker-compose logs celery_worker
   docker-compose logs celery_beat
   docker-compose logs websocket
   ```

2. **Verify .env file exists and has values:**
   ```bash
   cat .env | grep SECRET_KEY
   cat .env | grep JWT_SECRET
   ```

3. **Ensure keys meet minimum length requirements:**
   - SECRET_KEY: minimum 32 characters
   - JWT_SECRET: minimum 16 characters

4. **Clean rebuild:**
   ```bash
   docker-compose down -v
   docker-compose build --no-cache
   docker-compose up -d
   ```

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

### Shared backend environment (backend, websocket, celery_worker, celery_beat)

Since plan 06 D28 these four services use one list, `x-backend-env` at the top of
`docker-compose.yml`, and all four read `.env` (`env_file`). The API, the relay and
Celery therefore see the same values.

| Variable | Source | Notes |
|---|---|---|
| DATABASE_URL, REDIS_URL, CELERY_BROKER_URL, CELERY_RESULT_BACKEND | `x-backend-env` | The relay now also gets the broker (D17 queues work from it) |
| WEBSOCKET_URL, WEBSOCKET_PORT, DEBUG, ENVIRONMENT | `x-backend-env` | |
| SECRET_KEY, JWT_SECRET | `.env` | Celery and the relay used `${VAR:-<default>}` before D28: the `.env` value, or a committed fallback when `.env` lacked it. The fallback is gone, so a missing value now stops the service at start instead of running with a different secret than the API |
| SETTINGS_ENCRYPTION_KEY | `.env` | **Required.** Fernet key shared by every process (NEW-47). Missing: ERROR at start, and writes to encrypted `core.*` settings get 503 |
| RELAY_SENDER_SECRET | `.env` | Portal -> relay sender auth (D12). Empty: legacy-open relay, WARNING at start |
| DEPLOY_INJECT_CONFIG_ID (true), RELAY_STATION_REPORTS (on), DEPLOY_CATCHUP_ENABLED (false), DEPLOY_ACK_TIMEOUT_SEC (300), RELAY_DEBUG_LOGGING (false), RELAY_REQUIRE_CURRENT_TOKEN (false) | `x-backend-env` default, overridable from `.env` or the shell | Kill switches and timers of the confirmed-deploy path (D14, D15, D17, D12) |

Backend and Celery also read `./secrets/alerting.env`; the relay reads only `.env`.

**Relay trust (D12).** The API opens its relay session as a *sender*: the Socket.IO
CONNECT auth payload carries `{"role": "sender", "secret": RELAY_SENDER_SECRET}`
(never the URL). With the secret set, the relay refuses a sender with a wrong or
missing secret, accepts `message` (type `config` only) from senders only, and sends
the presence list (`clients_update`, `clients_list`) to senders only; a rejected
message gets an `error` event and nobody is disconnected. With the secret unset the
relay is *legacy-open*: any session that has not registered as a PC may push, as
before D12 (a WARNING at start and on the first such message). Set the same secret
for the API and the relay; a mismatch makes every deploy fail with "Could not reach
the display client".

`register` accepts only a valid `pc_auth` token for a PC that exists in `pcs`; the
token's pc_id wins. A token that differs from the stored `pcs.auth_token` registers
with the flag `token_outdated` (OD-23 (a), logged as a WARNING without the token).
`RELAY_REQUIRE_CURRENT_TOKEN=true` refuses such tokens (OD-23 (b)); switch it on only
when no station logs `token_outdated` any more. A second registration of the same
pc_id takes over and is flagged `duplicate_registration`. If the database cannot be
read during `register`, the PC is registered fail-open with `db_unverified`.
`registered` carries `features` (only for handlers the relay has) and
`protocol_hash`. Packet logging is off unless `RELAY_DEBUG_LOGGING=true` (packets
carry tokens and layouts). The relay start line `Relay trust: sender_auth=...
require_current_token=... packet_logging=... features=...` shows the active mode.
The HTTP endpoint `/api/connected-pcs` is unchanged and still unauthenticated
(open question 21); the compose file publishes the relay port on all interfaces.

**Deploy tracking (D14).** Every deploy is a `pc_deployments` row whose id is the
`config_id` put at the payload root (`DEPLOY_INJECT_CONFIG_ID=false` sends the
layout unchanged; nothing is then confirmable). The row is committed before the API
calls the relay's `deploy_config` (answer: delivered, offline or error); the PC's
Socket.IO ACK makes the relay mark it `received`, and for Client 1.1.x, whose rows
end there, stamp `pcs.last_applied`. Deploy responses never carry the layout.

**Station reports (D15).** Clients that register with the capability
`deploy_result.v1` / `station_status.v1` get those features in `registered` and
report through them; reports are validated (D1, 64 KB, no credentials), deduplicated
by message_id and answered with an ACK. `RELAY_STATION_REPORTS=off` (kill switch)
answers every report `disabled` and advertises `station_reports.disabled`; deploys
keep flowing and end at `received`. Health goes to `pc_health` in one batched upsert
every 15 s; a relay restart loses at most that much. A timeout sweep every 30 s marks
pending rows older than 60 s `relay_failed` and confirmable rows not confirmed within
`DEPLOY_ACK_TIMEOUT_SEC` (accepted rows: twice that) `unconfirmed`. The start line
`Relay reports: station_reports=... health_flush=15s deploy_sweep=30s ack_timeout=300s`
shows the mode (`RELAY_HEALTH_FLUSH_SECONDS` and `RELAY_DEPLOY_SWEEP_SECONDS` exist
for tests; leave them unset in production). Presence stamps (`last_connected`,
`last_seen`, `last_applied`) no longer move `pcs.updated_at`.

**Startup self-check.** Each of the four services logs one line at start:

```
startup_check service=websocket version=... deploy_inject_config_id=true relay_station_reports=on
  deploy_catchup_enabled=false deploy_ack_timeout_sec=300 relay_debug_logging=false
  relay_sender_secret=fp:<12 hex> settings_encryption_key=ok fp:<12 hex>
  jwt_secret_is_client_constant=false broker=reachable (redis:6379)
```

A secret appears only as `fp:` plus the first 12 hex characters of its sha256
(`printf %s "$VALUE" | sha256sum | cut -c1-12`). The fingerprints must be equal in
all four services. `jwt_secret_is_client_constant=true` is gate G-1 failing (NEW-72):
do not rotate JWT_SECRET at once, follow OD-23 and D30. The relay logs a WARNING and
keeps serving when the broker is unreachable. `python -m app.core.startup_check
--service <name> --check-broker` prints the same report as JSON inside a container.
Celery workers also answer `app.tasks.pc_maintenance.env_self_check` with it.

**Before deploying D28 to an existing install** (the relay and Celery lose the
committed `${VAR:-<default>}` fallbacks for SECRET_KEY and JWT_SECRET):
1. G-2: `grep -c '^SETTINGS_ENCRYPTION_KEY=' .env` must print 1 (no value is shown).
2. Compare the running relay's JWT_SECRET with the API's without printing either:
   `[ "$(docker exec truelive_websocket sh -c 'printf %s "$JWT_SECRET" | sha256sum')" = "$(docker exec truelive_backend sh -c 'printf %s "$JWT_SECRET" | sha256sum')" ] && echo equal || echo DIFFERENT`.
   It should print equal (both come from `.env`). If it prints DIFFERENT, the relay runs
   on the committed fallback: stop, because PC tokens are verified with the relay's value
   and the switch would drop stations off the relay. Follow OD-23 / D30 instead.
3. After `docker compose up -d`, check that the four `startup_check` lines show the
   same fingerprints, and re-run G-1 against the relay.

### Migration
- DATABASE_URL, SECRET_KEY, JWT_SECRET (inline, unchanged by D28)

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

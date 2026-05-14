# Plans.md — Alerting Feature

Source spec: `experiments/alerting_feature/feature_description.md` (v1, locked design).

Implementation strategy: build in waves. Each wave is one or more commits. Tests + sanity verification at end of each wave. Postfix host install / MinIO docker-compose setup / production cutover are out of scope this run (handed off to ops; see `experiments/alerting_feature/ops_action_items.md`).

## Format

`| # | Task | Content | DoD | Depends | Status |`

## Wave 1 — Foundation

| # | Task | Content | DoD | Depends | Status |
|---|---|---|---|---|---|
| 1.1 | Models | New SQLAlchemy models for `alert_addresses`, `raw_messages`, `alerts`, `alert_media`, `webhook_consumers`, `webhook_deliveries`, `service_accounts`, `service_account_tokens`. Register in `app/models/__init__.py`. | All 8 classes importable; correct FKs/indexes per §6 of spec | — | cc:TODO |
| 1.2 | Schemas | Pydantic schemas for all new endpoints (`app/schemas/alerting.py`, `app/schemas/service_account.py`) | Schemas validate sample payloads | 1.1 | cc:TODO |
| 1.3 | Migration 007 | Alembic migration for all new tables. Native range partitioning by month on `raw_messages`, `alerts`, `alert_media`, `webhook_deliveries`. Auto-provision nightly job to add next-month partitions (deferred to Wave 6 retention work). | `alembic upgrade head` clean in fresh DB; `alembic downgrade base` clean | 1.1 | cc:TODO |
| 1.4 | MinIO client wrapper | `app/services/minio_client.py` — put/get/presign methods, address via `s3.usvg.ai`. Bucket names: `truelive-raw-mail`, `truelive-alert-media` | Module imports; client created lazily; raises clear error if creds missing | — | cc:TODO |
| 1.5 | HMAC + signing utils | `app/utils/hmac_sign.py` — `sign(body, secret) -> 'sha256=<hex>'`, replay-protection timestamp helper | unit-tested | — | cc:TODO |
| 1.6 | Rate limiter | `app/utils/rate_limiter.py` — Redis token bucket per `alert_address_id` | unit-tested with fakeredis | — | cc:TODO |
| 1.7 | Service-account auth | `app/api/deps.py` extension — `ServiceAccount` dependency that accepts `Bearer tlsa_*` tokens (bcrypt verify), scope check helper | ServiceAccount + scope decorator working in a probe endpoint | 1.1 | cc:TODO |

## Wave 2 — SMTP ingest

| # | Task | Content | DoD | Depends | Status |
|---|---|---|---|---|---|
| 2.1 | LMTP handler | `app/services/smtp_ingest.py` — `AlertLMTPHandler` (aiosmtpd LMTP mode). Recipient validation, rate-limit, persist-before-ACK (MinIO write → raw_messages row → 250 ACK → enqueue Celery). | Handler class importable; module-level entrypoint runs aiosmtpd LMTP on unix socket | 1.1, 1.4, 1.6 | cc:TODO |
| 2.2 | Reconciliation | Startup scan for `raw_messages.status='received'` stuck > 60s; re-enqueue. | function exists, invoked at process boot | 2.1 | cc:TODO |
| 2.3 | Entrypoint | `python -m app.services.smtp_ingest` runnable as a separate process | runs and binds the LMTP unix socket | 2.1, 2.2 | cc:TODO |

## Wave 3 — Parser worker

| # | Task | Content | DoD | Depends | Status |
|---|---|---|---|---|---|
| 3.1 | Parser registry | `app/services/alert_parsers/__init__.py` — registry interface. Each parser returns normalized fields per spec §10. Includes `unknown` fallback. | registry returns a parser for any input; unparsed never raises | — | cc:TODO |
| 3.2 | Calipsa templates | At least one real template parser using the .eml samples in `experiments/alerting_feature/email_samples/` | sample .eml roundtrips → expected `event_type`, `event_subtype`, attachments | 3.1 | cc:TODO |
| 3.3 | Celery task: process_inbound_alert | `app/tasks/process_inbound_alert.py` — fetch raw, parse MIME, run parser, persist alerts + alert_media, update raw_messages.status, enqueue deliver_webhook. | Task callable; happy-path manual invocation produces correct rows | 1.1, 1.4, 3.1, 3.2 | cc:TODO |

## Wave 4 — Webhook delivery

| # | Task | Content | DoD | Depends | Status |
|---|---|---|---|---|---|
| 4.1 | Celery task: deliver_webhook | `app/tasks/deliver_webhook.py` — HMAC sign, POST to consumer, 5s timeout, status update, retry chain (1m,5m,30m,2h,12h), giving_up state. | task callable; success/timeout/non-2xx all handled | 1.1, 1.5 | cc:TODO |
| 4.2 | Celery queues | Register `alert_parse` and `alert_deliver` queues, `--prefetch-multiplier=1` on deliver pool | celery config updated | 3.3, 4.1 | cc:TODO |
| 4.3 | Schedule integration | Tie process_inbound_alert.delay() into ingest, and deliver_webhook.delay() into parser worker | end-to-end pipeline reachable in code | 2.1, 3.3, 4.1 | cc:TODO |

## Wave 5 — API surface

| # | Task | Content | DoD | Depends | Status |
|---|---|---|---|---|---|
| 5.1 | Alert addresses | `app/api/v1/alert_addresses.py` — list/create/delete/rotate/quarantine endpoints | OpenAPI shows endpoints; basic CRUD round-trips | 1.1, 1.2 | cc:TODO |
| 5.2 | Auto-provision hook | Camera create → post-commit insert of one active `alert_address` if absent. Camera detail response includes active address. | creating a camera produces an alert_address row | 1.1, 5.1 | cc:TODO |
| 5.3 | Alerts retrieval | `app/api/v1/alerts.py` — list/get/raw/media endpoints | endpoints respond; presigned URLs fresh | 1.1, 1.2, 1.4 | cc:TODO |
| 5.4 | Webhook consumers | `app/api/v1/webhook_consumers.py` — register/list/update/delete/test endpoints. GuardDesk registers via service-account auth. | endpoints respond; HMAC secret stored encrypted-at-rest | 1.1, 1.2, 1.7 | cc:TODO |
| 5.5 | Delivery observability | `/alerts/{id}/deliveries`, `/alerts/{id}/redeliver` | endpoints respond | 1.1, 4.1 | cc:TODO |
| 5.6 | Service-account admin | `app/api/v1/service_accounts.py` — create accounts/tokens (admin); token shown once on creation | endpoints respond | 1.1, 1.7 | cc:TODO |
| 5.7 | Router wiring | Mount new routers in `app/main.py` under `/api/v1/` | new endpoints visible in `/api/v1/docs` | 5.1-5.6 | cc:TODO |

## Wave 6 — Retention + observability

| # | Task | Content | DoD | Depends | Status |
|---|---|---|---|---|---|
| 6.1 | Retention task | Celery-beat task that drops partitions past retention (90d raw_messages/alerts, 30d alert_media/webhook_deliveries) | task callable; documented schedule (02:00 daily) | 1.3 | cc:TODO |
| 6.2 | Partition rollover | Celery-beat task that creates next month's partitions a week in advance | task callable | 1.3 | cc:TODO |
| 6.3 | Metrics | Prometheus counters/histograms/gauges per spec §14.1 | `/metrics` endpoint exposes them (or wire into existing exporter) | 2.1, 3.3, 4.1 | cc:TODO |

## Wave 7 — Tests

| # | Task | Content | DoD | Depends | Status |
|---|---|---|---|---|---|
| 7.1 | Model tests | Cascade behavior, partition routing | pytest passes | 1.1, 1.3 | cc:TODO |
| 7.2 | Parser tests | Each parser handles sample .eml → expected output | pytest passes | 3.1, 3.2 | cc:TODO |
| 7.3 | Webhook tests | HMAC sign/verify, retry chain math, idempotency | pytest passes | 4.1 | cc:TODO |
| 7.4 | API tests | CRUD round-trips for alert-addresses, alerts, webhook-consumers, service-accounts | pytest passes | 5.* | cc:TODO |
| 7.5 | Service-account auth tests | Token validation, scope checks, rejection of missing/expired tokens | pytest passes | 1.7 | cc:TODO |

## Wave 8 — Verification + handoff

| # | Task | Content | DoD | Depends | Status |
|---|---|---|---|---|---|
| 8.1 | Run migration 007 | `alembic upgrade head` clean against actual DB. Round-trip downgrade. | DB at head; downgrade idempotent | 1.3, 7.* | cc:TODO |
| 8.2 | Backend container rebuild plan | Document docker-compose change needed (bind-mount `alembic/`) so future migrations work without image rebuild | docs updated | 1.3 | cc:TODO |
| 8.3 | Webhook contract doc for GuardDesk | Generated from §15 of spec | `experiments/alerting_feature/webhook_contract.md` exists | 4.1, 10 | cc:TODO |
| 8.4 | Status update in feature_description.md §18 | Mark code-side build items done; flag remaining ops items | spec updated | all above | cc:TODO |

## Out of scope this session

- Postfix install + config on host (§7.1) — ops task, requires apt + service restart
- MinIO container setup in docker-compose — needs separate ops decision (existing stack has no MinIO)
- Production cutover, staging burn-in
- LLM-driven parser generation (deferred per spec §3)
- Image rebuild + container restart for ops items

These are tracked in the spec itself and `ops_action_items.md`.

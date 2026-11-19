# Stream providers

A stream provider is an account at a camera vendor that hands out HLS stream addresses on request. TrueLive fetches the address of each of the provider's cameras, puts it into the layout sent to a station, and renews it before it expires. Nobody pastes stream addresses. Lumana is the first provider type; others are added as backend modules (see the developer part).

Backend 1.2.0, portal 0.3.0. Stations need TrueLive Live (Core) 1.3.0 or later; 1.3.1 renews without a visible interruption.

## For operators

### Add a provider

1. Portal, **Stream providers** (admins only), **Add provider**.
2. Give it a name, pick the type (Lumana) and enter the type's fields. For Lumana: the API key, and the API address (leave the default unless Lumana gave you another).
3. **Test** asks the vendor whether the key works. It opens no stream.

The key is stored encrypted on the server and is never shown again: the page says only that a key is set and when it was changed. To replace it, use **Edit** and type the new key; leaving the box empty keeps the stored one. Two accounts at the same vendor are two providers.

The server must have `SETTINGS_ENCRYPTION_KEY` set (the same value in the API, the relay and both Celery services), or saving a key is refused.

**Remove** is refused while cameras still use the provider; the message names them. Move or delete those cameras first.

### Add a provider camera

On the camera form choose **From a provider** as the source, pick the provider and enter the camera's ID as the vendor shows it. Everything else (name, NVR, screen layouts, deploy) works as for any camera. A camera with a typed-in RTSP or HLS address behaves exactly as before.

### Stations

- **Core version.** A station gets provider addresses only when it runs TrueLive Live 1.3.0 or later. TrueLive Client 1.2.0 does not report the Core version, so tick **Runs TrueLive Live 1.3.0 or later** on the PC's Edit form. A version a later Client reports wins over the box. Without it the provider cells of that station stay dark and the PC's health panel says why. Older Core cannot play HLS.
- **Delivery mode.** Set the Client's delivery mode to **Both** on stations with provider cameras. In the default mode **File** every renewal restarts TrueLive Live and the whole wall blanks for some seconds.
- Each renewal shows the Client's "Configuration Applied" balloon for 3 seconds.

### What the statuses mean

On the camera tables, under the camera's provider and ID:

| Status | Meaning |
|---|---|
| Address valid until *time* | A station plays it; it is renewed well before that time. |
| Not in use | No connected station shows this camera, so no address is held. |
| Key refused | The vendor refused the provider's key. Enter a valid key on the Stream providers page. |
| Camera unknown | The vendor does not know this camera ID. |
| Session limit | The vendor refused a new session (see Limits). |
| Provider not answering | The vendor did not answer. Stations keep the address they have until it expires; the server keeps trying (after 30 s, then doubling up to 15 minutes). |
| Address expiring | The vendor has not given a fresh address and the current one ends within 30 minutes. |

On a PC's health panel, **Provider cameras**: when stream addresses were last sent, whether the station acknowledged them, when the next renewal is due, and every camera that is not playable with its reason. "The layout changed since the last deploy" means the server lost its copy of what the station runs and the layout was edited since: deploy to that PC. Renewals are not deploys and do not appear in the deploy history.

A provider cell that cannot be resolved fails alone; the rest of the layout deploys and plays.

### Limits

- **Lumana allows 10 live sessions per edge device.** One camera uses one session, shared by all stations and by the portal's test stream. The Stream providers page shows the count and warns from 8. More than 10 cameras of one edge cannot be live at once.
- A session nobody uses is given back after 5 minutes.
- Camera snapshots are taken from a provider camera only while a station is using it.
- A station that cannot reach the TrueLive server for longer than an address lives (Lumana: 5 hours) loses its provider cells until it reconnects.
- `STREAM_PROVIDERS_ENABLED=false` in `.env` turns the feature off: provider cells are not resolved and nothing is renewed.

### Settings

Portal, **Settings**, **Stream providers** (super admins). Changes apply within a minute, no restart.

| Key | Default | Meaning |
|---|---|---|
| `stream_providers.renew_fraction` | 0.5 | renew at this fraction of the address lifetime (0.05 to 0.95) |
| `stream_providers.renew_min_margin_sec` | 1800 | never later than this before the expiry |
| `stream_providers.watch_interval_sec` | 60 | how often the server checks each address in use; 0 turns the check off |
| `stream_providers.release_after_sec` | 300 | give back an address no connected station uses |
| `stream_providers.deploy_resolve_budget_sec` | 6 | longest a deploy waits for providers |
| `stream_providers.request_timeout_sec` | 8 | timeout of every request to a provider |
| `stream_providers.pc_refresh_min_interval_sec` | 60 | at most one automatic delivery per station in this time |

### Upgrade

Run `alembic upgrade head` (migration `016_stream_providers`: one new table, one new nullable column on `pcs`; nothing existing changes). Restart the API, the relay, the Celery worker and Celery beat: beat runs the new task `stream-providers-tick` every 30 seconds. Rollback: `alembic downgrade 015_pc_deploy_tracking` with the 1.2.0 code, which drops the providers and the PC flags; cameras that reference a provider must be changed or removed first.

## For developers

### How it works

- A provider camera is stored as `cameras.rtsp_url = provider://<provider id>/<camera id, percent-encoded>` (`app/utils/url_processor.py`). The reference holds no secret and never reaches a station.
- `app/services/stream_providers/engine.py` knows no vendor. At deploy it resolves the references of all targets in one bounded, parallel pass and replaces them in a copy of the layout (`url` and every `LocationUris` entry); the deploy's content hash stays the one of the unresolved layout. What cannot be resolved becomes `http://127.0.0.1:9/stream-unavailable/<code>`, which Core fails for that cell only.
- Redis (`truelive:sp:*`) holds the minted address per camera and, per PC, the unresolved layout as deployed plus a fingerprint of what was sent. Everything there is encrypted with the settings Fernet and can be lost: it is rebuilt from the database, and a PC's layout only when it still hashes to the PC's latest deploy.
- Celery beat runs `stream_provider_tasks.tick` every 30 s: renew all in-use addresses of a provider when the first is due, probe addresses in use and ask again for ones that stopped answering, deliver to stations whose addresses or acknowledgement are behind, release unused addresses.
- A renewal is delivered through the relay's sender event `refresh_config`: the same layout with the same root `config_id`, forwarded as the `config` event, with no `pc_deployments` row and no audit entry. The station's ACK is written to `truelive:sp:ack:<pc id>`.
- The relay queues `refresh_pc` when a station registers and `station_failed` when a `station_status` lists failed cells (once per minute per PC).
- Nothing logs a URL, a token or a key. httpx's logger is raised to WARNING because it logs request URLs.

### Add a provider type

One module and its tests; the engine, the API and the portal do not change.

1. `app/services/stream_providers/<vendor>.py`: subclass `StreamProvider` (`base.py`).
   - `type_id`, `label`, and `fields`: `ProviderField(name, label, kind, required, default, help)` with `kind` one of `text`, `url`, `secret`, `int`. The portal builds its form from these; `secret` fields are stored encrypted and never returned.
   - `mint(settings, camera_id, previous, http, last_info)` returns `Address(url, expires_at, handle, info)`. `settings` is the stored fields with the secrets decrypted. `previous` is the address minted before: renew it if the vendor can (the engine keeps `handle` for that). `expires_at` is Unix time or `None`. `info` is non-secret and shown to operators; `sessions` and `limit` in it give the session count on the Stream providers page.
   - `check(settings, http)` proves the settings work without opening a stream and returns a short text.
   - `release(...)` gives an address back, if the vendor has such a call. `probe(address, http)` defaults to "the playlist answers 200 and starts with `#EXTM3U`".
   - Raise `ProviderError(code, message)` with code `auth`, `not_found`, `limit`, `unavailable` or `invalid`. The message is shown to operators: plain words, no URL, no secret. Use the given `http` client (it carries the timeout) and do not retry: the engine backs off.
2. Register it in `app/services/stream_providers/__init__.py`: `registry.register(<Vendor>Provider())`.
3. Tests against a local fake of the vendor on 127.0.0.1 (see `tests/fake_lumana.py`, `tests/unit/test_stream_providers_unit.py`). `tests/stream_provider_testkit.py` has `FixedProvider`, a second type that exists only in the tests and runs through the same engine.

If the vendor's renewed address keeps its path and changes only the query string, Core 1.3.1 adopts it without restarting the picture. A vendor whose renewed address has a new path restarts the cell at each renewal.

### Tests

```
python -m pytest tests/unit/test_stream_providers_unit.py tests/unit/test_stream_providers_engine.py \
    tests/unit/test_stream_providers_api.py tests/unit/test_relay_stream_providers.py -o addopts='' -p no:cacheprovider
```

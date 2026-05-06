# Mail Aggregator — Tests

## Layout

- `tests/unit/`        — pure unit tests (no I/O, no docker).
- `tests/integration/` — requires Postgres + Redis + MinIO running.
- `tests/worker/`      — sync_cycle / cleanup / imap_fetcher with mocked `imap-tools`.
- `tests/contract/`    — validate API response shapes against `docs/04-api-contracts.md`.
- `tests/frontend/`    — render Jinja templates and inspect HTML.

Test infra source of truth: `docs/06-security.md`, `docs/05-modules.md`, `docs/04-api-contracts.md`.

## Bringing up dependencies (Windows / PowerShell)

QA created an override file `docker-compose.test.yml` that exposes
`postgres → 127.0.0.1:55432`, `redis → 127.0.0.1:56379`, `minio →
127.0.0.1:59000`. Boot the three services and run alembic:

```powershell
cd D:\BA\mail-agregator
docker compose -f docker-compose.yml -f docker-compose.test.yml up -d postgres redis minio
uv run alembic upgrade head
```

NOTE: prod compose drops ALL caps from Redis. Alpine Redis needs SETUID/SETGID
to drop privileges, and on Docker Desktop for Windows that crashloops.
The test override re-grants the four caps Alpine needs (no security loss for
tests bound to localhost only).

NOTE: prod compose runs `minio-bootstrap` to create a least-privilege service
account. That image tag (`minio/mc:RELEASE.2024-08-26T15-33-30Z`) is no
longer on Docker Hub. For tests we point `S3_ACCESS_KEY/SECRET_KEY` directly
at `MINIO_ROOT_USER/PASSWORD` in `.env` — fine because the bucket is local
and short-lived. Production must continue to use the bootstrap script.

## Running

```powershell
# Unit tests (no docker required):
uv run pytest tests/unit -v

# Integration (needs the docker stack):
uv run pytest tests/integration -v

# Worker:
uv run pytest tests/worker -v

# Contract:
uv run pytest tests/contract -v

# Frontend (template smoke):
uv run pytest tests/frontend -v

# Full coverage:
uv run pytest --cov=backend --cov=worker --cov=shared --cov-report=term-missing
```

## Tearing down

```powershell
docker compose -f docker-compose.yml -f docker-compose.test.yml down -v
```

`-v` removes the named volumes so the next run starts from a clean DB.

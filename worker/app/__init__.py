"""Worker app package: APScheduler entrypoint, sync_cycle, retention_cleanup.

Note on architecture:
    The worker imports from ``backend.app.repositories.*``,
    ``backend.app.crm_push``, ``backend.app.oauth``, ``backend.app.exceptions``
    and ``backend.app.security``. This is an intentional, accepted coupling per
    the rework round 2 reviewer note: relocating repositories under ``shared/``
    is a wider refactor that should land via an ADR. Both containers (api +
    worker) ship ``backend/`` + ``worker/`` + ``shared/`` via
    ``deploy/Dockerfile``, so the import is at runtime cost-free.
"""

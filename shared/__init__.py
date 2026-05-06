"""Shared package: models, config, crypto, logging, storage, redis client.

Imported by both ``backend.app`` (FastAPI) and ``worker.app`` (APScheduler).
Anything in here MUST be safe to import from either context — no FastAPI
dependencies, no APScheduler dependencies.
"""

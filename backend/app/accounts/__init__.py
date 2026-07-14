"""Mail accounts module: CRUD + IMAP/SMTP test login + force-sync marker.

ADR-0044 §4 (phase A3): the HTML router went away with the UI; the service is
reused by the external write API (``backend/app/external/write_service.py``).
"""

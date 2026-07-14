"""Mail send module: SMTP send core + best-effort IMAP append.

ADR-0044 §4 (phase A3): the HTML/form router went away with the UI; the module
now only exposes the send core reused by the external reply (ADR-0035).
"""

"""Thin async data-access objects on top of SQLAlchemy 2.x.

Each repository wraps an :class:`AsyncSession`. Service-layer code calls
methods here; routes never touch ORM directly.
"""

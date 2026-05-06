"""Authentication module: login, logout, set-password, super-admin seed."""

from backend.app.auth.router import router
from backend.app.auth.service import AuthService, seed_super_admin

__all__ = ["AuthService", "router", "seed_super_admin"]

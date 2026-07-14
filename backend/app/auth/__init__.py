"""Auth module (ADR-0044 §4, phase A3).

The session ``AuthService`` and the HTML router went away with the cookie UI;
what is left is the ``crm-service`` technical-user seed used by the API lifespan
and by the external write path.
"""

from backend.app.auth.service import CRM_SERVICE_USERNAME, seed_crm_service_user

__all__ = ["CRM_SERVICE_USERNAME", "seed_crm_service_user"]

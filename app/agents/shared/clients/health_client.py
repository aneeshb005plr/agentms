# app/agents/shared/clients/health_client.py
# Health check client — Dataverse integration placeholder.
#
# Phase 1: always returns None — no Dataverse connection yet.
# Phase 2: fill in _fetch_from_dataverse() when PwC team confirms:
#   - Dataverse API endpoint or direct connection string
#   - Authentication method (service principal, managed identity etc.)
#   - Table schema for health check data
#   - How application names map to Dataverse records
#
# The interface is clean and stable — agent code never changes.
# Only this file changes when Phase 2 integration is added.
#
# Health check data structure (what Phase 2 will return):
#   {
#       "app_name":      str        — application name
#       "status":        str        — "healthy" | "degraded" | "down"
#       "last_checked":  datetime   — last health check timestamp
#       "message":       str | None — status message if degraded/down
#       "checked_at":    datetime   — when this record was stored
#   }

import logging
from datetime import datetime

from app.config import settings

logger = logging.getLogger(__name__)


class HealthClient:
    """
    Dataverse health check client — placeholder for Phase 2.
    Clean interface so agent code never needs to change.
    """

    def __init__(self):
        self._initialised: bool = False

    def initialise(self) -> None:
        """
        Called once at FastAPI lifespan startup.
        Phase 1: just logs that health check is in placeholder mode.
        Phase 2: initialise Dataverse connection here.
        """
        # Phase 2: initialise Dataverse client here
        # e.g. msal / azure-identity for service principal auth
        self._initialised = True
        logger.info(
            "HealthClient initialised — Phase 1 placeholder mode "
            "(Dataverse integration pending PwC team confirmation)"
        )

    async def get_app_health(self, app_name: str) -> dict | None:
        """
        Returns health status for a given application.

        Phase 1: always returns None — no Dataverse connection.
        Phase 2: queries Dataverse table for last 30-min health check data.

        Args:
            app_name: application name derived from vector search results
                      e.g. "SAP", "Workday", "ServiceNow"

        Returns:
            Health status dict or None if:
            - Phase 1 (always)
            - App not found in Dataverse
            - Health check data older than 30 minutes
        """
        if not self._initialised:
            raise RuntimeError("HealthClient not initialised. Call initialise() first.")

        # Phase 1 — return None always
        # Agent handles None gracefully — skips health context in response
        logger.debug(
            "HealthClient.get_app_health('%s') — Phase 1 placeholder, returning None",
            app_name,
        )
        return None

        # ── Phase 2 implementation goes here ─────────────────────────────────
        # Uncomment and implement when Dataverse connection is confirmed:
        #
        # try:
        #     return await self._fetch_from_dataverse(app_name)
        # except Exception as e:
        #     logger.error("Dataverse health check failed for %s: %s", app_name, str(e))
        #     return None  # fail gracefully — don't break the agent

    async def _fetch_from_dataverse(self, app_name: str) -> dict | None:
        """
        Phase 2 — Fetch health status from Dataverse.
        Queries health check table for records within last 30 minutes.
        Returns None if no recent record found.

        TODO Phase 2:
        - Confirm Dataverse endpoint / connection method with PwC team
        - Confirm table name and schema
        - Confirm app_name to Dataverse record mapping
        - Add authentication (service principal or managed identity)
        """
        raise NotImplementedError(
            "Dataverse integration not implemented yet. "
            "Confirm connection details with PwC team."
        )


# Singleton
health_client = HealthClient()
# app/agents/tools/health_tool.py
# Application health check tool — wraps HealthClient (Dataverse placeholder).
#
# Phase 1: always returns no health data — HealthClient returns None.
# Phase 2: returns real health status from Dataverse when integration is ready.
#
# Agent uses this tool when:
#   - app_identified is set (derived from vector search results)
#   - User's question might be related to a known outage or degradation
#   - Agent wants to check if the app is healthy before giving troubleshooting steps
#
# If health data shows app is down/degraded:
#   - Agent informs user the team is aware of the issue
#   - Agent skips detailed troubleshooting (pointless if app is down)
#   - Agent suggests checking back later or raising a ticket

import logging

from langchain_core.tools import tool

from app.agents.clients.health_client import health_client

logger = logging.getLogger(__name__)


@tool
async def check_app_health(app_name: str) -> str:
    """
    Check the current health status of a XYZ application.

    Use this tool when:
    - The user is reporting an issue with a specific application
    - You have identified which application the user is asking about
    - You want to check if there is a known outage or degradation before
      providing troubleshooting steps

    This tool queries the last 30-minute health check data for the application.
    If the application is down or degraded, inform the user that the team is
    already aware and investigating — skip detailed troubleshooting in that case.

    Args:
        app_name: Name of the XYZ application to check.
                  Use the exact application name as identified from the
                  knowledge base search results.
                  Examples: "SAP", "Workday", "ServiceNow", "Astro"

    Returns:
        Current health status of the application, or a message indicating
        health data is not available (Phase 1).
    """
    logger.info("check_app_health called for app: %s", app_name)

    try:
        health_data = await health_client.get_app_health(app_name)
    except Exception as e:
        logger.error("Health check failed for %s: %s", app_name, str(e))
        return (
            f"HEALTH_CHECK_ERROR: Could not retrieve health status for {app_name}. "
            "Continue with troubleshooting steps as normal."
        )

    if health_data is None:
        # Phase 1 — always None
        # Phase 2 — None means app not found or data too old
        return (
            f"HEALTH_DATA_UNAVAILABLE: No health check data available for {app_name}. "
            "Continue with troubleshooting steps based on knowledge base results."
        )

    # Phase 2 — real health data returned
    status  = health_data.get("status", "unknown")
    message = health_data.get("message")
    checked = health_data.get("last_checked", "unknown")

    if status == "healthy":
        return (
            f"APP_HEALTHY: {app_name} is currently healthy "
            f"(last checked: {checked}). "
            "Proceed with troubleshooting steps."
        )

    elif status in ("degraded", "down"):
        base = (
            f"APP_{status.upper()}: {app_name} is currently {status} "
            f"(last checked: {checked}). "
            "The IT team is aware and investigating. "
        )
        if message:
            base += f"Status message: {message}. "
        base += (
            "Inform the user that this is a known issue being investigated. "
            "Suggest they check back later or raise a ticket for tracking."
        )
        return base

    return (
        f"HEALTH_STATUS_UNKNOWN: {app_name} health status is unknown. "
        "Continue with troubleshooting steps as normal."
    )
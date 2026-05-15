# tests/conftest.py
# Shared pytest fixtures for all tests.
# Uses create_app() factory — each test gets a fresh app instance.

import pytest
from httpx import AsyncClient, ASGITransport
from app.main import create_app


@pytest.fixture
def app():
    """Fresh FastAPI app instance per test."""
    return create_app()


@pytest.fixture
async def client(app):
    """Async HTTP client for endpoint testing."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test"
    ) as client:
        yield client
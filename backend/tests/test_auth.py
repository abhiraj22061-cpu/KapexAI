import backend.utils.jwt_utils as jwt_utils
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from httpx import AsyncClient, ASGITransport

import backend.routers.auth as auth_module
from backend.main import app
from backend.middleware.auth import get_current_user


@pytest.fixture
def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def mock_lifespan():
    with (
        patch("backend.main.connect_db", new_callable=AsyncMock),
        patch("backend.main.disconnect_db", new_callable=AsyncMock),
        patch("backend.main.connect_redis", new_callable=AsyncMock),
        patch("backend.main.disconnect_redis", new_callable=AsyncMock),
    ):
        yield


@pytest.fixture(autouse=True)
async def lifespan_events(mock_lifespan):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.get("/health")
    yield
    app.dependency_overrides.clear()


class TestGoogleOAuthUrl:
    @pytest.mark.asyncio
    async def test_returns_google_oauth_url(self, client):
        with (
            patch("backend.routers.auth.GOOGLE_CLIENT_ID", "test-client-id"),
            patch("backend.routers.auth.GOOGLE_REDIRECT_URI", "http://localhost:8000/auth/google/callback"),
        ):
            async with client as c:
                response = await c.get("/auth/google")

        assert response.status_code == 200
        data = response.json()
        assert "url" in data
        assert "accounts.google.com/o/oauth2/v2/auth" in data["url"]
        assert "test-client-id" in data["url"]
        assert "openid" in data["url"]
        assert "email" in data["url"]
        assert "profile" in data["url"]

    @pytest.mark.asyncio
    async def test_returns_500_when_google_not_configured(self, client):
        with (
            patch("backend.routers.auth.GOOGLE_CLIENT_ID", None),
            patch("backend.routers.auth.GOOGLE_REDIRECT_URI", None),
        ):
            async with client as c:
                response = await c.get("/auth/google")

        assert response.status_code == 500


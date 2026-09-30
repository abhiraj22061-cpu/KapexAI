import json
from datetime import datetime, timezone
import backend.utils.jwt_utils as jwt_utils
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from httpx import AsyncClient, ASGITransport

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


# ── Health ────────────────────────────────────────────────────

class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_health_endpoint_returns_ok(self, client):
        async with client as c:
            response = await c.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


# ── Waitlist ─────────────────────────────────────────────────

class TestWaitlistEndpoint:
    @pytest.mark.asyncio
    async def test_join_waitlist_with_email_only(self, client):
        async with client as c:
            response = await c.post("/waitlist", json={"email": "test@example.com"})
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "Successfully joined the waitlist!"
        assert data["email"] == "test@example.com"

    @pytest.mark.asyncio
    async def test_join_waitlist_with_email_and_name(self, client):
        async with client as c:
            response = await c.post(
                "/waitlist", json={"email": "test@example.com", "name": "John Doe"}
            )
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "Successfully joined the waitlist!"
        assert data["email"] == "test@example.com"

    @pytest.mark.asyncio
    async def test_join_waitlist_invalid_email(self, client):
        async with client as c:
            response = await c.post("/waitlist", json={"email": "not-an-email"})
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_join_waitlist_missing_email(self, client):
        async with client as c:
            response = await c.post("/waitlist", json={"name": "John Doe"})
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_join_waitlist_empty_email(self, client):
        async with client as c:
            response = await c.post("/waitlist", json={"email": ""})
        assert response.status_code == 422


# ── CORS ──────────────────────────────────────────────────────

class TestCORSMiddleware:
    @pytest.mark.asyncio
    async def test_cors_allows_localhost_3000(self, client):
        async with client as c:
            response = await c.options(
                "/waitlist",
                headers={
                    "Origin": "http://localhost:3000",
                    "Access-Control-Request-Method": "POST",
                },
            )
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"

    @pytest.mark.asyncio
    async def test_cors_allows_credentials(self, client):
        async with client as c:
            response = await c.options(
                "/waitlist",
                headers={
                    "Origin": "http://localhost:3000",
                    "Access-Control-Request-Method": "POST",
                },
            )
        assert response.headers.get("access-control-allow-credentials") == "true"

    @pytest.mark.asyncio
    async def test_cors_allows_all_methods(self, client):
        async with client as c:
            response = await c.options(
                "/waitlist",
                headers={
                    "Origin": "http://localhost:3000",
                    "Access-Control-Request-Method": "POST",
                },
            )
        allowed_methods = response.headers.get("access-control-allow-methods", "")
        assert "POST" in allowed_methods
        assert "GET" in allowed_methods

    @pytest.mark.asyncio
    async def test_cors_allows_all_headers(self, client):
        async with client as c:
            response = await c.options(
                "/waitlist",
                headers={
                    "Origin": "http://localhost:3000",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type,authorization",
                },
            )
        allowed_headers = response.headers.get("access-control-allow-headers", "")
        assert "content-type" in allowed_headers.lower()
        assert "authorization" in allowed_headers.lower()

    @pytest.mark.asyncio
    async def test_cors_blocks_other_origins(self, client):
        async with client as c:
            response = await c.options(
                "/waitlist",
                headers={
                    "Origin": "http://evil.com",
                    "Access-Control-Request-Method": "POST",
                },
            )
        assert response.headers.get("access-control-allow-origin") != "http://evil.com"


# ── Create Chat Session ──────────────────────────────────────

class TestCreateChatSession:
    @pytest.mark.asyncio
    async def test_creates_session_and_pushes_job(self, client):
        session_id = "session-456"
        mock_user = MagicMock(id="user-123", email="test@test.com")
        mock_db = MagicMock()
        mock_db.session.create = AsyncMock(return_value=Mock(id=session_id))
        mock_db.session.update = AsyncMock()

        async def mock_get_current_user(authorization: str = None):
            return mock_user

        app.dependency_overrides[get_current_user] = mock_get_current_user

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.db", mock_db),
                patch("backend.main.uuid4", return_value="job-456"),
                patch("backend.main.redis.lpush", new_callable=AsyncMock) as mock_lpush,
                patch("backend.main.redis.set", new_callable=AsyncMock),
                patch("backend.main.redis.delete", new_callable=AsyncMock),
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token({"user_id": "user-123", "email": "test@test.com"})

                async with client as c:
                    response = await c.post(
                        "/create_chat_session",
                        json={"content": "AI SaaS"},
                        headers={"Authorization": f"Bearer {token}"},
                    )

            assert response.status_code == 201
            data = response.json()
            assert data["session_id"] == session_id
            assert data["job_id"] == "job-456"
            mock_lpush.assert_called_once_with(
                "jobs:queue",
                json.dumps(
                    {"job_id": "job-456", "session_id": session_id, "user_input": "AI SaaS"}
                ),
            )
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_returns_401_when_token_missing(self, client):
        async with client as c:
            response = await c.post(
                "/create_chat_session",
                json={"content": "AI SaaS"},
            )

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_returns_401_when_token_invalid(self, client):
        async with client as c:
            response = await c.post(
                "/create_chat_session",
                json={"content": "AI SaaS"},
                headers={"Authorization": "Bearer invalid-token"},
            )

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_returns_422_for_missing_content(self, client):
        mock_user = MagicMock(id="user-123", email="test@test.com")
        mock_db = MagicMock()
        mock_db.session.create = AsyncMock(return_value=Mock(id="session-456"))

        async def mock_get_current_user(authorization: str = None):
            return mock_user

        app.dependency_overrides[get_current_user] = mock_get_current_user

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.db", mock_db),
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token({"user_id": "user-123", "email": "test@test.com"})

                async with client as c:
                    response = await c.post(
                        "/create_chat_session",
                        json={},
                        headers={"Authorization": f"Bearer {token}"},
                    )
            assert response.status_code == 422
        finally:
            app.dependency_overrides.clear()


# ── Push Chat Message ────────────────────────────────────────

class TestPushChatMessage:
    @pytest.mark.asyncio
    async def test_pushes_message_to_queue(self, client):
        session_id = "session-789"
        mock_user = MagicMock(id="user-123", email="test@test.com")
        mock_db = MagicMock()
        mock_db.session.create = AsyncMock()
        mock_db.session.update = AsyncMock()

        async def mock_get_current_user(authorization: str = None):
            return mock_user

        app.dependency_overrides[get_current_user] = mock_get_current_user

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.get_session", new_callable=AsyncMock, return_value=Mock(id=session_id, userId="user-123")),
                patch("backend.main.uuid4", return_value="job-789"),
                patch("backend.main.redis.lpush", new_callable=AsyncMock) as mock_lpush,
                patch("backend.main.redis.set", new_callable=AsyncMock),
                patch("backend.main.redis.delete", new_callable=AsyncMock),
                patch("backend.main.db", mock_db),
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token({"user_id": "user-123", "email": "test@test.com"})

                async with client as c:
                    response = await c.post(
                        "/push_chat_message",
                        json={"session_id": session_id, "content": "My answers here"},
                        headers={"Authorization": f"Bearer {token}"},
                    )

            assert response.status_code == 201
            assert response.json()["session_id"] == session_id
            assert response.json()["job_id"] == "job-789"
            mock_lpush.assert_called_once_with(
                "jobs:queue",
                json.dumps(
                    {"job_id": "job-789", "session_id": session_id, "user_input": "My answers here"}
                ),
            )
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_returns_401_when_token_missing(self, client):
        async with client as c:
            response = await c.post(
                "/push_chat_message",
                json={"session_id": "s1", "content": "hi"},
            )

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_returns_401_when_token_invalid(self, client):
        async with client as c:
            response = await c.post(
                "/push_chat_message",
                json={"session_id": "s1", "content": "hi"},
                headers={"Authorization": "Bearer invalid-token"},
            )

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_returns_404_when_session_not_found(self, client):
        mock_user = MagicMock(id="user-123", email="test@test.com")
        mock_db = MagicMock()

        async def mock_get_current_user(authorization: str = None):
            return mock_user

        app.dependency_overrides[get_current_user] = mock_get_current_user

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.get_session", new_callable=AsyncMock, return_value=None),
                patch("backend.main.db", mock_db),
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token({"user_id": "user-123", "email": "test@test.com"})

                async with client as c:
                    response = await c.post(
                        "/push_chat_message",
                        json={"session_id": "missing", "content": "hi"},
                        headers={"Authorization": f"Bearer {token}"},
                    )

            assert response.status_code == 404
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_returns_404_when_session_belongs_to_other_user(self, client):
        mock_user = MagicMock(id="user-123", email="test@test.com")
        mock_db = MagicMock()

        async def mock_get_current_user(authorization: str = None):
            return mock_user

        app.dependency_overrides[get_current_user] = mock_get_current_user

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.get_session", new_callable=AsyncMock, return_value=Mock(id="s1", userId="other-user")),
                patch("backend.main.db", mock_db),
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token({"user_id": "user-123", "email": "test@test.com"})

                async with client as c:
                    response = await c.post(
                        "/push_chat_message",
                        json={"session_id": "s1", "content": "hi"},
                        headers={"Authorization": f"Bearer {token}"},
                    )

            assert response.status_code == 404
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_returns_422_for_missing_content(self, client):
        mock_user = MagicMock(id="user-123", email="test@test.com")
        mock_db = MagicMock()

        async def mock_get_current_user(authorization: str = None):
            return mock_user

        app.dependency_overrides[get_current_user] = mock_get_current_user

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.db", mock_db),
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token({"user_id": "user-123", "email": "test@test.com"})

                async with client as c:
                    response = await c.post(
                        "/push_chat_message",
                        json={"session_id": "s1"},
                        headers={"Authorization": f"Bearer {token}"},
                    )
            assert response.status_code == 422
        finally:
            app.dependency_overrides.clear()


# ── Submit Questionnaire Answers ─────────────────────────────

class TestSubmitQuestionnaireAnswers:
    @pytest.mark.asyncio
    async def test_submits_structured_answers(self, client):
        session_id = "session-900"
        mock_user = MagicMock(id="user-123", email="test@test.com")
        mock_db = MagicMock()
        mock_db.session.update = AsyncMock()

        async def mock_get_current_user(authorization: str = None):
            return mock_user

        app.dependency_overrides[get_current_user] = mock_get_current_user

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.get_session", new_callable=AsyncMock, return_value=Mock(id=session_id, userId="user-123")),
                patch("backend.main.uuid4", return_value="job-900"),
                patch("backend.main.redis.lpush", new_callable=AsyncMock) as mock_lpush,
                patch("backend.main.redis.set", new_callable=AsyncMock),
                patch("backend.main.redis.delete", new_callable=AsyncMock),
                patch("backend.main.db", mock_db),
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token({"user_id": "user-123", "email": "test@test.com"})

                async with client as c:
                    response = await c.post(
                        "/submit_questionnaire_answers",
                        json={
                            "session_id": session_id,
                            "answers": [
                                {"key": "q1", "answer": "dried mango"},
                                {"key": "q2", "answer": "local farms"},
                            ],
                        },
                        headers={"Authorization": f"Bearer {token}"},
                    )

            assert response.status_code == 201
            data = response.json()
            assert data["session_id"] == session_id
            assert data["job_id"] == "job-900"
            payload = json.dumps(
                {
                    "kind": "questionnaire_answers",
                    "answers": [
                        {"key": "q1", "answer": "dried mango"},
                        {"key": "q2", "answer": "local farms"},
                    ],
                }
            )
            mock_lpush.assert_called_once_with(
                "jobs:queue",
                json.dumps(
                    {"job_id": "job-900", "session_id": session_id, "user_input": payload}
                ),
            )
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_returns_401_when_token_missing(self, client):
        async with client as c:
            response = await c.post(
                "/submit_questionnaire_answers",
                json={"session_id": "s1", "answers": [{"key": "q1", "answer": "x"}]},
            )

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_returns_404_when_session_not_found(self, client):
        mock_user = MagicMock(id="user-123", email="test@test.com")
        mock_db = MagicMock()

        async def mock_get_current_user(authorization: str = None):
            return mock_user

        app.dependency_overrides[get_current_user] = mock_get_current_user

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.get_session", new_callable=AsyncMock, return_value=None),
                patch("backend.main.db", mock_db),
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token({"user_id": "user-123", "email": "test@test.com"})

                async with client as c:
                    response = await c.post(
                        "/submit_questionnaire_answers",
                        json={"session_id": "missing", "answers": [{"key": "q1", "answer": "x"}]},
                        headers={"Authorization": f"Bearer {token}"},
                    )

            assert response.status_code == 404
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_returns_404_when_session_belongs_to_other_user(self, client):
        mock_user = MagicMock(id="user-123", email="test@test.com")
        mock_db = MagicMock()

        async def mock_get_current_user(authorization: str = None):
            return mock_user

        app.dependency_overrides[get_current_user] = mock_get_current_user

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.get_session", new_callable=AsyncMock, return_value=Mock(id="s1", userId="other-user")),
                patch("backend.main.db", mock_db),
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token({"user_id": "user-123", "email": "test@test.com"})

                async with client as c:
                    response = await c.post(
                        "/submit_questionnaire_answers",
                        json={"session_id": "s1", "answers": [{"key": "q1", "answer": "x"}]},
                        headers={"Authorization": f"Bearer {token}"},
                    )

            assert response.status_code == 404
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_returns_422_when_answers_missing(self, client):
        mock_user = MagicMock(id="user-123", email="test@test.com")
        mock_db = MagicMock()

        async def mock_get_current_user(authorization: str = None):
            return mock_user

        app.dependency_overrides[get_current_user] = mock_get_current_user

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.db", mock_db),
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token({"user_id": "user-123", "email": "test@test.com"})

                async with client as c:
                    response = await c.post(
                        "/submit_questionnaire_answers",
                        json={"session_id": "s1"},
                        headers={"Authorization": f"Bearer {token}"},
                    )

            assert response.status_code == 422
        finally:
            app.dependency_overrides.clear()


# ── Get Sessions ─────────────────────────────────────────────

class TestGetSessions:
    @pytest.mark.asyncio
    async def test_returns_serialized_sessions(self, client):
        session_mock = Mock(
            id="s1",
            business_idea="AI SaaS",
            status="ACTIVE",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        mock_user = MagicMock(id="user-123", email="test@test.com")
        mock_db = MagicMock()
        mock_db.dashboard.find_many = AsyncMock(return_value=[])

        async def mock_get_current_user(authorization: str = None):
            return mock_user

        app.dependency_overrides[get_current_user] = mock_get_current_user

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.get_all_sessions", new_callable=AsyncMock, return_value=[session_mock]),
                patch("backend.main.db", mock_db),
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token({"user_id": "user-123", "email": "test@test.com"})

                async with client as c:
                    response = await c.get(
                        "/get_sessions",
                        headers={"Authorization": f"Bearer {token}"},
                    )

            assert response.status_code == 200
            assert response.json() == {
                "data": [
                    {
                        "id": "s1",
                        "business_idea": "AI SaaS",
                        "status": "ACTIVE",
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "dashboards": [],
                    }
                ]
            }
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_groups_dashboards_under_their_session(self, client):
        session_mock = Mock(
            id="s1",
            business_idea="AI SaaS",
            status="ACTIVE",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        dashboard_mocks = [
            Mock(id="d1", sessionId="s1",
                 created_at=datetime(2026, 1, 2, tzinfo=timezone.utc)),
            Mock(id="d2", sessionId="s1",
                 created_at=datetime(2026, 1, 3, tzinfo=timezone.utc)),
        ]
        dashboard_mocks[0].name = "SWOT Analysis"
        dashboard_mocks[1].name = "Market Analysis"
        mock_user = MagicMock(id="user-123", email="test@test.com")
        mock_db = MagicMock()
        mock_db.dashboard.find_many = AsyncMock(return_value=dashboard_mocks)

        async def mock_get_current_user(authorization: str = None):
            return mock_user

        app.dependency_overrides[get_current_user] = mock_get_current_user

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.get_all_sessions", new_callable=AsyncMock, return_value=[session_mock]),
                patch("backend.main.db", mock_db),
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token({"user_id": "user-123", "email": "test@test.com"})

                async with client as c:
                    response = await c.get(
                        "/get_sessions",
                        headers={"Authorization": f"Bearer {token}"},
                    )

            assert response.status_code == 200
            body = response.json()
            assert body["data"][0]["dashboards"] == [
                {"id": "d1", "name": "SWOT Analysis", "created_at": "2026-01-02T00:00:00+00:00"},
                {"id": "d2", "name": "Market Analysis", "created_at": "2026-01-03T00:00:00+00:00"},
            ]
            mock_db.dashboard.find_many.assert_awaited_once()
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_returns_401_when_token_missing(self, client):
        async with client as c:
            response = await c.get("/get_sessions")

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_returns_401_when_token_invalid(self, client):
        async with client as c:
            response = await c.get(
                "/get_sessions",
                headers={"Authorization": "Bearer invalid-token"},
            )

        assert response.status_code == 401


# ── Get Messages ─────────────────────────────────────────────

class TestGetMessages:
    @pytest.mark.asyncio
    async def test_returns_messages_for_session(self, client):
        mock_user = MagicMock(id="user-123", email="test@test.com")
        message_mocks = [
            Mock(
                id="m1",
                role="USER",
                agent="CHAT",
                content={"type": "chat", "content": "hi"},
                created_at=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
            ),
            Mock(
                id="m2",
                role="ASSISTANT",
                agent="TOOL",
                content={"type": "swot", "content": "done", "sections": {}},
                created_at=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
            ),
        ]
        mock_db = MagicMock()
        mock_db.message.find_many = AsyncMock(return_value=message_mocks)

        async def mock_get_current_user(authorization: str = None):
            return mock_user

        app.dependency_overrides[get_current_user] = mock_get_current_user

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.get_session", new_callable=AsyncMock, return_value=Mock(id="s1", userId="user-123")),
                patch("backend.main.redis.get", new_callable=AsyncMock, return_value=None),
                patch("backend.main.db", mock_db),
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token({"user_id": "user-123", "email": "test@test.com"})

                async with client as c:
                    response = await c.get(
                        "/get_messages?session_id=s1",
                        headers={"Authorization": f"Bearer {token}"},
                    )

            assert response.status_code == 200
            assert response.json()["pending"] is None
            assert response.json()["data"] == [
                {
                    "id": "m1",
                    "role": "USER",
                    "agent": "CHAT",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "type": "chat",
                    "content": "hi",
                },
                {
                    "id": "m2",
                    "role": "ASSISTANT",
                    "agent": "TOOL",
                    "created_at": "2026-01-01T00:01:00+00:00",
                    "type": "swot",
                    "content": "done",
                    "sections": {},
                },
            ]
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_returns_404_when_session_not_found(self, client):
        mock_user = MagicMock(id="user-123", email="test@test.com")
        mock_db = MagicMock()

        async def mock_get_current_user(authorization: str = None):
            return mock_user

        app.dependency_overrides[get_current_user] = mock_get_current_user

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.get_session", new_callable=AsyncMock, return_value=None),
                patch("backend.main.db", mock_db),
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token({"user_id": "user-123", "email": "test@test.com"})

                async with client as c:
                    response = await c.get(
                        "/get_messages?session_id=missing",
                        headers={"Authorization": f"Bearer {token}"},
                    )

            assert response.status_code == 404
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_returns_404_when_session_belongs_to_other_user(self, client):
        mock_user = MagicMock(id="user-123", email="test@test.com")
        mock_db = MagicMock()

        async def mock_get_current_user(authorization: str = None):
            return mock_user

        app.dependency_overrides[get_current_user] = mock_get_current_user

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.get_session", new_callable=AsyncMock, return_value=Mock(id="s1", userId="other-user")),
                patch("backend.main.db", mock_db),
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token({"user_id": "user-123", "email": "test@test.com"})

                async with client as c:
                    response = await c.get(
                        "/get_messages?session_id=s1",
                        headers={"Authorization": f"Bearer {token}"},
                    )

            assert response.status_code == 404
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_returns_401_when_token_missing(self, client):
        async with client as c:
            response = await c.get("/get_messages?session_id=s1")

        assert response.status_code == 401


# ── Dashboards ────────────────────────────────────────────────

def _auth_override():
    mock_user = MagicMock(id="user-123", email="test@test.com")

    async def mock_get_current_user(authorization: str = None):
        return mock_user

    app.dependency_overrides[get_current_user] = mock_get_current_user
    return mock_user


class TestGetDashboards:
    @pytest.mark.asyncio
    async def test_returns_dashboard_list_for_owned_session(self, client):
        _auth_override()
        rows = [
            Mock(id="d1", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
            Mock(id="d2", created_at=datetime(2026, 1, 2, tzinfo=timezone.utc)),
        ]
        rows[0].name = "SWOT Analysis"
        rows[1].name = "Market Analysis"
        mock_db = MagicMock()
        mock_db.dashboard.find_many = AsyncMock(return_value=rows)

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.get_session", new_callable=AsyncMock, return_value=Mock(id="s1", userId="user-123")),
                patch("backend.main.db", mock_db),
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token({"user_id": "user-123", "email": "test@test.com"})

                async with client as c:
                    response = await c.get(
                        "/get_dashboards?session_id=s1",
                        headers={"Authorization": f"Bearer {token}"},
                    )

            assert response.status_code == 200
            assert response.json()["data"] == [
                {"id": "d1", "name": "SWOT Analysis", "created_at": "2026-01-01T00:00:00+00:00"},
                {"id": "d2", "name": "Market Analysis", "created_at": "2026-01-02T00:00:00+00:00"},
            ]
            mock_db.dashboard.find_many.assert_awaited_once_with(
                where={"sessionId": "s1"}, order={"created_at": "asc"}
            )
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_returns_404_when_session_not_found(self, client):
        _auth_override()
        mock_db = MagicMock()

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.get_session", new_callable=AsyncMock, return_value=None),
                patch("backend.main.db", mock_db),
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token({"user_id": "user-123", "email": "test@test.com"})

                async with client as c:
                    response = await c.get(
                        "/get_dashboards?session_id=missing",
                        headers={"Authorization": f"Bearer {token}"},
                    )

            assert response.status_code == 404
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_returns_404_when_session_belongs_to_other_user(self, client):
        _auth_override()
        mock_db = MagicMock()

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.get_session", new_callable=AsyncMock, return_value=Mock(id="s1", userId="other-user")),
                patch("backend.main.db", mock_db),
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token({"user_id": "user-123", "email": "test@test.com"})

                async with client as c:
                    response = await c.get(
                        "/get_dashboards?session_id=s1",
                        headers={"Authorization": f"Bearer {token}"},
                    )

            assert response.status_code == 404
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_returns_401_when_token_missing(self, client):
        async with client as c:
            response = await c.get("/get_dashboards?session_id=s1")

        assert response.status_code == 401


class TestGetDashboard:
    @pytest.mark.asyncio
    async def test_returns_full_dashboard_payload(self, client):
        _auth_override()
        mock_db = MagicMock()
        full = Mock(
            id="d1",
            sessionId="s1",
            data={"kind": "swot", "title": "SWOT", "sections": []},
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        full.name = "SWOT Analysis"
        mock_db.dashboard.find_unique = AsyncMock(return_value=full)

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.get_session", new_callable=AsyncMock, return_value=Mock(id="s1", userId="user-123")),
                patch("backend.main.db", mock_db),
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token({"user_id": "user-123", "email": "test@test.com"})

                async with client as c:
                    response = await c.get(
                        "/get_dashboard?dashboard_id=d1",
                        headers={"Authorization": f"Bearer {token}"},
                    )

            assert response.status_code == 200
            body = response.json()["data"]
            assert body["id"] == "d1"
            assert body["name"] == "SWOT Analysis"
            assert body["session_id"] == "s1"
            assert body["created_at"] == "2026-01-01T00:00:00+00:00"
            assert body["data"]["kind"] == "swot"
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_returns_404_when_dashboard_missing(self, client):
        _auth_override()
        mock_db = MagicMock()
        mock_db.dashboard.find_unique = AsyncMock(return_value=None)

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.db", mock_db),
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token({"user_id": "user-123", "email": "test@test.com"})

                async with client as c:
                    response = await c.get(
                        "/get_dashboard?dashboard_id=missing",
                        headers={"Authorization": f"Bearer {token}"},
                    )

            assert response.status_code == 404
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_returns_404_when_session_belongs_to_other_user(self, client):
        _auth_override()
        mock_db = MagicMock()
        foreign = Mock(
            id="d1", sessionId="s1",
            data={}, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        foreign.name = "SWOT"
        mock_db.dashboard.find_unique = AsyncMock(return_value=foreign)

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.get_session", new_callable=AsyncMock, return_value=Mock(id="s1", userId="other-user")),
                patch("backend.main.db", mock_db),
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token({"user_id": "user-123", "email": "test@test.com"})

                async with client as c:
                    response = await c.get(
                        "/get_dashboard?dashboard_id=d1",
                        headers={"Authorization": f"Bearer {token}"},
                    )

            assert response.status_code == 404
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_returns_401_when_token_missing(self, client):
        async with client as c:
            response = await c.get("/get_dashboard?dashboard_id=d1")

        assert response.status_code == 401


# ── Rename Session ───────────────────────────────────────────

class TestRenameSession:
    async def test_renames_owned_session(self, client):
        mock_user = MagicMock(id="user-123", email="test@test.com")
        mock_db = MagicMock()
        mock_db.session.update = AsyncMock(return_value=Mock(id="s1", business_idea="New Name"))

        async def mock_get_current_user(authorization: str = None):
            return mock_user

        app.dependency_overrides[get_current_user] = mock_get_current_user

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.get_session", new_callable=AsyncMock, return_value=Mock(id="s1", userId="user-123")),
                patch("backend.main.db", mock_db),
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token({"user_id": "user-123", "email": "test@test.com"})

                async with client as c:
                    response = await c.post(
                        "/rename_session",
                        json={"session_id": "s1", "name": "  New Name  "},
                        headers={"Authorization": f"Bearer {token}"},
                    )

            assert response.status_code == 200
            assert response.json()["business_idea"] == "New Name"
            mock_db.session.update.assert_awaited_once_with(
                where={"id": "s1"},
                data={"business_idea": "New Name"},
            )
        finally:
            app.dependency_overrides.clear()

    async def test_returns_404_when_session_not_found(self, client):
        mock_user = MagicMock(id="user-123", email="test@test.com")
        mock_db = MagicMock()

        async def mock_get_current_user(authorization: str = None):
            return mock_user

        app.dependency_overrides[get_current_user] = mock_get_current_user

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.get_session", new_callable=AsyncMock, return_value=None),
                patch("backend.main.db", mock_db),
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token({"user_id": "user-123", "email": "test@test.com"})

                async with client as c:
                    response = await c.post(
                        "/rename_session",
                        json={"session_id": "missing", "name": "X"},
                        headers={"Authorization": f"Bearer {token}"},
                    )

            assert response.status_code == 404
        finally:
            app.dependency_overrides.clear()

    async def test_returns_404_when_session_belongs_to_other_user(self, client):
        mock_user = MagicMock(id="user-123", email="test@test.com")
        mock_db = MagicMock()

        async def mock_get_current_user(authorization: str = None):
            return mock_user

        app.dependency_overrides[get_current_user] = mock_get_current_user

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.get_session", new_callable=AsyncMock, return_value=Mock(id="s1", userId="other-user")),
                patch("backend.main.db", mock_db),
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token({"user_id": "user-123", "email": "test@test.com"})

                async with client as c:
                    response = await c.post(
                        "/rename_session",
                        json={"session_id": "s1", "name": "X"},
                        headers={"Authorization": f"Bearer {token}"},
                    )

            assert response.status_code == 404
        finally:
            app.dependency_overrides.clear()

    async def test_returns_401_when_token_missing(self, client):
        async with client as c:
            response = await c.post("/rename_session", json={"session_id": "s1", "name": "X"})

        assert response.status_code == 401

    async def test_returns_401_when_token_invalid(self, client):
        async with client as c:
            response = await c.post(
                "/rename_session",
                json={"session_id": "s1", "name": "X"},
                headers={"Authorization": "Bearer invalid-token"},
            )

        assert response.status_code == 401

    async def test_returns_422_for_empty_name(self, client):
        mock_user = MagicMock(id="user-123", email="test@test.com")
        mock_db = MagicMock()

        async def mock_get_current_user(authorization: str = None):
            return mock_user

        app.dependency_overrides[get_current_user] = mock_get_current_user

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.get_session", new_callable=AsyncMock, return_value=Mock(id="s1", userId="user-123")),
                patch("backend.main.db", mock_db),
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token({"user_id": "user-123", "email": "test@test.com"})

                async with client as c:
                    response = await c.post(
                        "/rename_session",
                        json={"session_id": "s1", "name": "   "},
                        headers={"Authorization": f"Bearer {token}"},
                    )

            assert response.status_code == 422
        finally:
            app.dependency_overrides.clear()


# ── Delete Session ───────────────────────────────────────────

class TestDeleteSession:
    async def test_deletes_session_messages_and_cached_state(self, client):
        mock_user = MagicMock(id="user-123", email="test@test.com")
        mock_db = MagicMock()
        mock_db.message.delete_many = AsyncMock()
        mock_db.dashboard.delete_many = AsyncMock()
        mock_db.session.delete = AsyncMock()

        async def mock_get_current_user(authorization: str = None):
            return mock_user

        app.dependency_overrides[get_current_user] = mock_get_current_user

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.get_session", new_callable=AsyncMock, return_value=Mock(id="s1", userId="user-123")),
                patch("backend.main.db", mock_db),
                patch("backend.main.redis.delete", new_callable=AsyncMock) as mock_redis_delete,
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token({"user_id": "user-123", "email": "test@test.com"})

                async with client as c:
                    response = await c.post(
                        "/delete_session",
                        json={"session_id": "s1"},
                        headers={"Authorization": f"Bearer {token}"},
                    )

            assert response.status_code == 200
            assert response.json()["session_id"] == "s1"
            mock_redis_delete.assert_any_await("langgraph_state:s1")
            mock_redis_delete.assert_any_await("pending:s1")
            mock_db.message.delete_many.assert_awaited_once_with(where={"sessionId": "s1"})
            mock_db.dashboard.delete_many.assert_awaited_once_with(where={"sessionId": "s1"})
            mock_db.session.delete.assert_awaited_once_with(where={"id": "s1"})
        finally:
            app.dependency_overrides.clear()

    async def test_returns_404_when_session_not_found(self, client):
        mock_user = MagicMock(id="user-123", email="test@test.com")
        mock_db = MagicMock()

        async def mock_get_current_user(authorization: str = None):
            return mock_user

        app.dependency_overrides[get_current_user] = mock_get_current_user

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.get_session", new_callable=AsyncMock, return_value=None),
                patch("backend.main.db", mock_db),
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token({"user_id": "user-123", "email": "test@test.com"})

                async with client as c:
                    response = await c.post(
                        "/delete_session",
                        json={"session_id": "missing"},
                        headers={"Authorization": f"Bearer {token}"},
                    )

            assert response.status_code == 404
        finally:
            app.dependency_overrides.clear()

    async def test_returns_404_when_session_belongs_to_other_user(self, client):
        mock_user = MagicMock(id="user-123", email="test@test.com")
        mock_db = MagicMock()

        async def mock_get_current_user(authorization: str = None):
            return mock_user

        app.dependency_overrides[get_current_user] = mock_get_current_user

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.get_session", new_callable=AsyncMock, return_value=Mock(id="s1", userId="other-user")),
                patch("backend.main.db", mock_db),
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token({"user_id": "user-123", "email": "test@test.com"})

                async with client as c:
                    response = await c.post(
                        "/delete_session",
                        json={"session_id": "s1"},
                        headers={"Authorization": f"Bearer {token}"},
                    )

            assert response.status_code == 404
        finally:
            app.dependency_overrides.clear()

    async def test_returns_401_when_token_missing(self, client):
        async with client as c:
            response = await c.post("/delete_session", json={"session_id": "s1"})

        assert response.status_code == 401

    async def test_returns_401_when_token_invalid(self, client):
        async with client as c:
            response = await c.post(
                "/delete_session",
                json={"session_id": "s1"},
                headers={"Authorization": "Bearer invalid-token"},
            )

        assert response.status_code == 401


# ── Business Profile ─────────────────────────────────────────

class TestGetBusinessProfile:
    async def test_returns_profile_content(self, client):
        mock_user = MagicMock(id="user-123", email="test@test.com")

        async def mock_get_current_user(authorization: str = None):
            return mock_user

        app.dependency_overrides[get_current_user] = mock_get_current_user

        profile_row = MagicMock(
            content={"your_name": "Cafe Pune", "location": "Pune"}
        )
        mock_utils_db = MagicMock()
        mock_utils_db.businessprofile.find_unique = AsyncMock(return_value=profile_row)

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.db", MagicMock()),
                patch("backend.utils.db_utils.db", mock_utils_db),
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token({"user_id": "user-123", "email": "test@test.com"})

                async with client as c:
                    response = await c.get(
                        "/get_business_profile",
                        headers={"Authorization": f"Bearer {token}"},
                    )

            assert response.status_code == 200
            assert response.json()["data"] == {
                "your_name": "Cafe Pune",
                "location": "Pune",
            }
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_returns_empty_content_when_no_profile(self, client):
        mock_user = MagicMock(id="user-123", email="test@test.com")

        async def mock_get_current_user(authorization: str = None):
            return mock_user

        app.dependency_overrides[get_current_user] = mock_get_current_user

        created = MagicMock(content={})
        mock_utils_db = MagicMock()
        mock_utils_db.businessprofile.find_unique = AsyncMock(return_value=None)
        mock_utils_db.businessprofile.create = AsyncMock(return_value=created)

        with (
            patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
            patch("backend.main.db", MagicMock()),
            patch("backend.utils.db_utils.db", mock_utils_db),
        ):
            from backend.utils.jwt_utils import create_token
            token = create_token({"user_id": "user-123", "email": "test@test.com"})

            async with client as c:
                response = await c.get(
                    "/get_business_profile",
                    headers={"Authorization": f"Bearer {token}"},
                )

        assert response.status_code == 200
        assert response.json()["data"] == {}
        mock_utils_db.businessprofile.create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_401_when_token_missing(self, client):
        async with client as c:
            response = await c.get("/get_business_profile")

        assert response.status_code == 401


class TestUpdateBusinessProfile:
    @pytest.mark.asyncio
    async def test_updates_and_returns_profile(self, client):
        mock_user = MagicMock(id="user-123", email="test@test.com")

        async def mock_get_current_user(authorization: str = None):
            return mock_user

        app.dependency_overrides[get_current_user] = mock_get_current_user

        profile_row = Mock(content={})
        mock_utils_db = MagicMock()
        mock_utils_db.businessprofile.find_unique = AsyncMock(return_value=profile_row)

        saved_content = {
            "your_name": "Cafe Pune",
            "industry": "F&B",
            "location": "Pune",
        }
        mock_db = MagicMock()
        mock_db.businessprofile.update = AsyncMock(
            return_value=Mock(content=saved_content)
        )

        try:
            with (
                patch.object(jwt_utils, "JWT_SECRET", "test-secret"),
                patch("backend.main.db", mock_db),
                patch("backend.utils.db_utils.db", mock_utils_db),
            ):
                from backend.utils.jwt_utils import create_token
                token = create_token(
                    {"user_id": "user-123", "email": "test@test.com"}
                )

                async with client as c:
                    response = await c.post(
                        "/update_business_profile",
                        json=saved_content,
                        headers={"Authorization": f"Bearer {token}"},
                    )

            assert response.status_code == 200
            assert response.json()["message"] == "success"
            assert response.json()["data"] == saved_content
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_returns_401_when_token_missing(self, client):
        async with client as c:
            response = await c.post(
                "/update_business_profile", json={"your_name": "Cafe"}
            )

        assert response.status_code == 401


# ── WebSocket Stream ─────────────────────────────────────────

class TestWebSocketStream:
    """The endpoint tails the Redis frame list (`stream:{id}:frames`) and only
    uses pub/sub as a wakeup, so replaying a turn that finished before the
    client connected must work even when `pending` is already cleared."""

    @staticmethod
    def _build_pubsub_mock():
        pubsub = MagicMock()
        pubsub.subscribe = AsyncMock()
        pubsub.unsubscribe = AsyncMock()
        pubsub.close = AsyncMock()
        pubsub.get_message = AsyncMock(return_value=None)
        return pubsub

    def test_replays_buffered_turn_when_pending_already_cleared(self):
        # The race: the worker finished (pending gone) before the client's
        # socket connected — the buffered frames must still be delivered.
        frames = [
            json.dumps({"type": "chat", "content": "Hello"}),
            json.dumps({"type": "end"}),
        ]
        pubsub = self._build_pubsub_mock()

        with (
            patch(
                "backend.main.redis.get", new_callable=AsyncMock, return_value=None
            ),
            patch(
                "backend.main.redis.lrange",
                new_callable=AsyncMock,
                return_value=frames,
            ),
            patch("backend.main.redis.pubsub", return_value=pubsub),
            TestClient(app) as client,
            client.websocket_connect("/ws/session/test-session") as ws,
        ):
                assert ws.receive_json() == {"type": "chat", "content": "Hello"}
                assert ws.receive_json() == {"type": "end"}

    def test_drains_frames_in_chunks_while_turn_runs(self):
        # Frames arriving between wakeups are read from the list by cursor.
        pubsub = self._build_pubsub_mock()
        lrange_results = [
            [json.dumps({"type": "chat", "content": "Step 1"})],
            [json.dumps({"type": "end"})],
        ]

        with (
            patch(
                "backend.main.redis.get", new_callable=AsyncMock, return_value=b"pending"
            ),
            patch(
                "backend.main.redis.lrange",
                new_callable=AsyncMock,
                side_effect=lrange_results,
            ),
            patch("backend.main.redis.pubsub", return_value=pubsub),
            TestClient(app) as client,
            client.websocket_connect("/ws/session/test-session") as ws,
        ):
                assert ws.receive_json() == {"type": "chat", "content": "Step 1"}
                assert ws.receive_json() == {"type": "end"}

    def test_waits_for_wakeup_when_buffer_empty_but_job_running(self):
        # Empty buffer + job in flight → poll pub/sub, then drain the end frame.
        pubsub = self._build_pubsub_mock()
        lrange_results = [
            [],  # nothing streamed yet
            [json.dumps({"type": "end"})],  # worker finished on the next poll
        ]

        with (
            patch(
                "backend.main.redis.get", new_callable=AsyncMock, return_value=b"pending"
            ),
            patch(
                "backend.main.redis.lrange",
                new_callable=AsyncMock,
                side_effect=lrange_results,
            ),
            patch("backend.main.redis.pubsub", return_value=pubsub),
            TestClient(app) as client,
            client.websocket_connect("/ws/session/test-session") as ws,
        ):
                assert ws.receive_json() == {"type": "end"}
        pubsub.get_message.assert_awaited()

    def test_closes_when_no_job_and_empty_buffer(self):
        pubsub = self._build_pubsub_mock()

        with (
            patch(
                "backend.main.redis.get", new_callable=AsyncMock, return_value=None
            ),
            patch(
                "backend.main.redis.lrange",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("backend.main.redis.pubsub", return_value=pubsub),
            TestClient(app) as client,
            client.websocket_connect("/ws/session/test-session") as ws,
        ):
                assert ws.receive_json() == {"type": "end"}

    def test_stop_at_end_frame_does_not_send_more(self):
        from starlette.websockets import WebSocketDisconnect

        # Two frames buffered with an end first — nothing after the end is sent.
        pubsub = self._build_pubsub_mock()
        frames = [
            json.dumps({"type": "end"}),
            json.dumps({"type": "chat", "content": "dropped"}),
        ]

        with (
            patch(
                "backend.main.redis.get", new_callable=AsyncMock, return_value=None
            ),
            patch(
                "backend.main.redis.lrange",
                new_callable=AsyncMock,
                return_value=frames,
            ),
            patch("backend.main.redis.pubsub", return_value=pubsub),
            TestClient(app) as client,
            client.websocket_connect("/ws/session/test-session") as ws,
        ):
                assert ws.receive_json() == {"type": "end"}
                with pytest.raises(WebSocketDisconnect):
                    ws.receive_json()


# ── WebSocket disconnect handling ────────────────────────────

class TestWebSocketDisconnect:
    @pytest.mark.asyncio
    async def test_safe_send_returns_false_on_disconnect(self):
        from starlette.websockets import WebSocketDisconnect
        from backend.main import _safe_send

        ws = MagicMock()
        ws.send_json = AsyncMock(
            side_effect=WebSocketDisconnect(code=1006)
        )

        assert await _safe_send(ws, {"type": "end"}) is False

    @pytest.mark.asyncio
    async def test_safe_send_returns_true_on_success(self):
        from backend.main import _safe_send

        ws = MagicMock()
        ws.send_json = AsyncMock()

        assert await _safe_send(ws, {"type": "end"}) is True
        ws.send_json.assert_awaited_once_with({"type": "end"})
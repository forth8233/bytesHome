import os
from pathlib import Path

TEST_DB = Path(__file__).resolve().parent.parent / "data" / "test_agent.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB.as_posix()}"
os.environ["AUTO_CREATE_SCHEMA"] = "true"
os.environ["INITIAL_ADMIN_PASSWORD"] = "test-password-123"
os.environ["APP_SECRET_KEY"] = "test-secret-key"
os.environ["APP_ENCRYPTION_KEY"] = ""
# 测试绝不连真实 Home Assistant：占位地址 + 空令牌使适配器直接跳过启动
os.environ["HA_BASE_URL"] = "http://127.0.0.1:1"
os.environ["HA_TOKEN"] = ""

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _clean_database():
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{TEST_DB}{suffix}")
        if path.exists():
            path.unlink()
    yield


@pytest.fixture(scope="session")
def client(_clean_database):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def admin_token(client) -> str:
    response = client.post(
        "/api/auth/login", json={"username": "admin", "password": "test-password-123"}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@pytest.fixture(scope="session")
def auth_headers(admin_token) -> dict[str, str]:
    return {"Authorization": f"Bearer {admin_token}"}

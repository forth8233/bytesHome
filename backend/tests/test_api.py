def test_healthz(client) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_login_rejects_bad_password(client) -> None:
    response = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert response.status_code == 401


def test_members_requires_auth(client) -> None:
    assert client.get("/api/members").status_code == 401


def test_admin_can_list_members(client, auth_headers) -> None:
    response = client.get("/api/members", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()[0]["username"] == "admin"


def test_me_returns_current_user(client, auth_headers) -> None:
    response = client.get("/api/auth/me", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["role"] == "admin"


def test_refresh_rotates_token(client) -> None:
    login = client.post(
        "/api/auth/login", json={"username": "admin", "password": "test-password-123"}
    )
    refresh_token = login.json()["refresh_token"]
    first = client.post("/api/auth/refresh", json={"refresh_token": refresh_token})
    assert first.status_code == 200
    second = client.post("/api/auth/refresh", json={"refresh_token": refresh_token})
    assert second.status_code == 401


def test_whitelist_roundtrip(client, auth_headers) -> None:
    payload = {
        "entities": {"allow": ["light.*"], "deny": ["lock.*"]},
        "services": {"allow": ["light.turn_on"], "deny": ["lock.unlock"]},
        "param_rules": {},
    }
    saved = client.post("/api/config/whitelist", json=payload, headers=auth_headers)
    assert saved.status_code == 200
    current = client.get("/api/config/whitelist", headers=auth_headers).json()
    assert current["entities"]["allow"] == ["light.*"]


def test_light_config_masks_key(client, auth_headers) -> None:
    saved = client.post(
        "/api/config/llm",
        json={
            "provider": "deepseek",
            "model_name": "deepseek-chat",
            "api_base": "https://api.deepseek.com/v1",
            "api_key": "sk-secret-value",
        },
        headers=auth_headers,
    )
    assert saved.status_code == 200
    assert saved.json()["primary"]["api_key"] == "***"
    shadow = client.get("/api/config/llm", headers=auth_headers).json()
    assert shadow["primary"]["api_key"] == "***"


def test_readyz_reports_not_ready_without_ha(client) -> None:
    assert client.get("/readyz").status_code == 503


def test_fallback_inherits_key_from_same_provider(client, auth_headers) -> None:
    saved = client.post(
        "/api/config/llm",
        json={
            "provider": "deepseek",
            "model_name": "deepseek-flash",
            "api_key": "sk-primary-key",
            "fallback_provider": "deepseek",
            "fallback_model": "deepseek-v4-pro",
        },
        headers=auth_headers,
    )
    assert saved.status_code == 200
    fallback = client.get("/api/config/llm", headers=auth_headers).json()["fallback"]
    assert fallback["api_key"] == "***"
    assert fallback["model_name"] == "deepseek-v4-pro"


def test_fallback_does_not_inherit_key_across_providers(client, auth_headers) -> None:
    saved = client.post(
        "/api/config/llm",
        json={
            "provider": "deepseek",
            "model_name": "deepseek-flash",
            "api_key": "sk-primary-key",
            "fallback_provider": "minimax",
            "fallback_model": "abab6.5s-chat",
        },
        headers=auth_headers,
    )
    assert saved.status_code == 200
    fallback = client.get("/api/config/llm", headers=auth_headers).json()["fallback"]
    assert fallback["api_key"] == ""
    assert fallback["api_base"] == "https://api.minimax.io/v1"

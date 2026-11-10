from __future__ import annotations

import pytest

WHITELIST = {
    "entities": {
        "allow": ["light.*", "scene.*", "script.*", "automation.*"],
        "deny": [],
    },
    "services": {
        "allow": [
            "light.turn_on",
            "scene.turn_on",
            "scene.create",
            "scene.delete",
            "scene.reload",
            "script.turn_on",
            "automation.trigger",
        ],
        "deny": [],
    },
    "param_rules": {},
}

AUTOMATION_YAML = """
alias: 22点关灯
trigger:
  - platform: time
    at: "22:00:00"
action:
  - service: light.turn_on
    target:
      entity_id: light.living
"""


class FakeRest:
    def __init__(self) -> None:
        self.states = [
            {
                "entity_id": "scene.hui_jia",
                "state": "2026-11-05T06:53:01+00:00",
                "attributes": {
                    "friendly_name": "回家",
                    "entity_id": ["light.living"],
                },
            },
            {
                "entity_id": "automation.guan_deng",
                "state": "on",
                "attributes": {
                    "friendly_name": "22点关灯",
                    "id": "ha_agent_x",
                    "mode": "single",
                },
            },
        ]
        self.configs = {
            "ha_agent_x": {
                "id": "ha_agent_x",
                "alias": "22点关灯",
                "trigger": [{"platform": "time", "at": "22:00:00"}],
                "action": [
                    {
                        "service": "light.turn_on",
                        "target": {"entity_id": ["light.living"]},
                    }
                ],
            }
        }
        self.calls: list[tuple] = []

    async def get_states(self):
        return self.states

    async def get_automation_config(self, config_id: str):
        return self.configs.get(config_id)

    async def write_automation(self, config_id: str, config: dict):
        self.configs[config_id] = config
        self.calls.append(("write_automation", config_id))
        return config

    async def delete_automation(self, config_id: str):
        self.configs.pop(config_id, None)
        self.calls.append(("delete_automation", config_id))

    async def call_service(self, domain: str, service: str, data: dict | None = None):
        self.calls.append((domain, service, data or {}))
        return []


@pytest.fixture()
def ha(client, auth_headers):
    from app.main import app

    adapter = app.state.ha_adapter
    original_rest = adapter.rest
    original_whitelist = client.get(
        "/api/config/whitelist", headers=auth_headers
    ).json()
    fake = FakeRest()
    adapter.rest = fake
    saved = client.post(
        "/api/config/whitelist", json=WHITELIST, headers=auth_headers
    )
    assert saved.status_code == 200, saved.text
    yield fake
    adapter.rest = original_rest
    client.post(
        "/api/config/whitelist", json=original_whitelist, headers=auth_headers
    )


def test_overview_lists_scenes_and_automations(client, auth_headers, ha) -> None:
    response = client.get("/api/scenes/overview", headers=auth_headers)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ha_scenes"][0]["entity_id"] == "scene.hui_jia"
    assert payload["ha_scenes"][0]["name"] == "回家"
    assert payload["ha_scenes"][0]["deletable"] is False
    assert payload["automations"][0]["config_id"] == "ha_agent_x"
    assert payload["scripts"] == []


def test_read_automation_returns_yaml(client, auth_headers, ha) -> None:
    response = client.get(
        "/api/scenes/automations/ha_agent_x", headers=auth_headers
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["config"]["alias"] == "22点关灯"
    assert "alias: 22点关灯" in payload["yaml"]


def test_read_missing_automation_is_404(client, auth_headers, ha) -> None:
    response = client.get("/api/scenes/automations/nope", headers=auth_headers)

    assert response.status_code == 404


def test_create_automation_assigns_id(client, auth_headers, ha) -> None:
    response = client.post(
        "/api/scenes/automations",
        json={"yaml": AUTOMATION_YAML},
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    config_id = response.json()["config_id"]
    assert config_id.startswith("ha_agent_")
    assert ha.configs[config_id]["alias"] == "22点关灯"
    assert ha.configs[config_id]["id"] == config_id


def test_update_automation_writes_config(client, auth_headers, ha) -> None:
    response = client.put(
        "/api/scenes/automations/ha_agent_x",
        json={"yaml": AUTOMATION_YAML},
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    assert ha.configs["ha_agent_x"]["id"] == "ha_agent_x"
    assert ("write_automation", "ha_agent_x") in ha.calls


def test_automation_yaml_rejects_dangerous_service(client, auth_headers, ha) -> None:
    response = client.post(
        "/api/scenes/automations",
        json={
            "yaml": (
                "alias: 危险\n"
                "trigger:\n"
                "  - platform: time\n"
                "    at: '22:00:00'\n"
                "action:\n"
                "  - service: shell_command.run\n"
            )
        },
        headers=auth_headers,
    )

    assert response.status_code == 422
    assert "shell_command" in response.json()["message"]


def test_delete_automation(client, auth_headers, ha) -> None:
    response = client.delete(
        "/api/scenes/automations/ha_agent_x", headers=auth_headers
    )

    assert response.status_code == 200, response.text
    assert "ha_agent_x" not in ha.configs


def test_activate_ha_scene(client, auth_headers, ha) -> None:
    response = client.post(
        "/api/scenes/ha/scene.hui_jia/activate", headers=auth_headers
    )

    assert response.status_code == 200, response.text
    assert ("scene", "turn_on", {"entity_id": "scene.hui_jia"}) in ha.calls


def test_create_ha_scene(client, auth_headers, ha) -> None:
    response = client.post(
        "/api/scenes/ha",
        json={"scene_id": "yewu", "snapshot_entities": ["light.living"]},
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    assert (
        "scene",
        "create",
        {"scene_id": "yewu", "snapshot_entities": ["light.living"]},
    ) in ha.calls
    assert ("scene", "reload", {}) in ha.calls


def test_delete_ha_scene(client, auth_headers, ha) -> None:
    response = client.delete("/api/scenes/ha/scene.hui_jia", headers=auth_headers)

    assert response.status_code == 200, response.text
    assert ("scene", "delete", {"entity_id": "scene.hui_jia"}) in ha.calls


def test_trigger_automation(client, auth_headers, ha) -> None:
    response = client.post(
        "/api/scenes/automations/automation.guan_deng/trigger",
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    assert (
        "automation",
        "trigger",
        {"entity_id": "automation.guan_deng"},
    ) in ha.calls


def test_entity_outside_whitelist_is_rejected(client, auth_headers, ha) -> None:
    client.post(
        "/api/config/whitelist",
        json={
            "entities": {"allow": ["light.*"], "deny": []},
            "services": {"allow": ["scene.turn_on"], "deny": []},
            "param_rules": {},
        },
        headers=auth_headers,
    )

    response = client.post(
        "/api/scenes/ha/scene.hui_jia/activate", headers=auth_headers
    )

    assert response.status_code == 400
    assert "whitelist" in response.json()["message"]


def test_non_admin_cannot_read_overview(client, auth_headers) -> None:
    created = client.post(
        "/api/members",
        json={
            "username": "scenes_reader",
            "display_name": "普通成员",
            "password": "reader-password-123",
            "role": "member",
        },
        headers=auth_headers,
    )
    assert created.status_code == 201, created.text
    login = client.post(
        "/api/auth/login",
        json={"username": "scenes_reader", "password": "reader-password-123"},
    )
    assert login.status_code == 200, login.text

    response = client.get(
        "/api/scenes/overview",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )

    assert response.status_code == 403

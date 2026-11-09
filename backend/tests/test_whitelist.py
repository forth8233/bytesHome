from app.safety.whitelist import WhitelistRules


def make_rules() -> WhitelistRules:
    return WhitelistRules.from_dict(
        {
            "entities": {"allow": ["light.*", "climate.living_room"], "deny": ["light.bedroom_strip"]},
            "services": {"allow": ["light.*", "climate.set_temperature"], "deny": ["light.turn_off"]},
            "param_rules": {
                "climate.set_temperature": {"temperature": [16, 30]},
                "light.turn_on": {"brightness_pct": [1, 100]},
            },
        }
    )


def test_entity_allow_and_deny() -> None:
    rules = make_rules()
    assert rules.entity_allowed("light.kitchen")
    assert rules.entity_allowed("climate.living_room")
    assert not rules.entity_allowed("switch.kettle")
    assert not rules.entity_allowed("light.bedroom_strip")


def test_deny_wins_over_allow() -> None:
    rules = make_rules()
    assert rules.service_allowed("light.turn_on")
    assert not rules.service_allowed("light.turn_off")


def test_empty_allow_list_is_closed_by_default() -> None:
    rules = WhitelistRules.from_dict({})
    assert not rules.entity_allowed("light.kitchen")
    assert not rules.service_allowed("light.turn_on")


def test_param_rules() -> None:
    rules = make_rules()
    assert rules.validate_params("climate.set_temperature", {"temperature": 22}) is None
    assert rules.validate_params("climate.set_temperature", {"temperature": 35}) is not None
    assert rules.validate_params("climate.set_temperature", {"temperature": "hot"}) is not None
    assert rules.validate_params("light.turn_on", {"brightness_pct": 1}) is None
    assert rules.validate_params("light.turn_on", {"brightness_pct": 101}) is not None

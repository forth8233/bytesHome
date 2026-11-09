from __future__ import annotations

from dataclasses import dataclass, field as dc_field, replace
from typing import Any

DEFAULT_STEPS = {
    "brightness": 10.0,
    "colorTemp": 500.0,
    "course": 10.0,
    "angle": 10.0,
    "targetTemp": 1.0,
    "volume": 10.0,
    "speed": 1.0,
    "targetHumidity": 5.0,
}

FUNCTION_LABELS = {
    "brightness": "亮度",
    "colorTemp": "色温",
    "color": "颜色",
    "mode": "模式",
    "speed": "风速",
    "targetTemp": "温度",
    "coolTemp": "冷光",
    "warmTemp": "暖光",
    "targetHumidity": "湿度",
    "course": "行程",
    "angle": "角度",
    "volume": "音量",
    "source": "音源",
    "status": "状态",
    "fault": "故障码",
    "value": "数值",
    "systemSwitch": "系统开关",
}

DEVICE_TYPE_LABELS = {
    "light": "灯光",
    "AC": "空调",
    "heating": "地暖",
    "freshAir": "新风",
    "curtain": "窗帘",
    "window": "窗户",
    "media": "多媒体",
    "tv": "电视",
    "humidifier": "加湿器",
    "dehumidifier": "除湿机",
    "airPurifier": "空气净化器",
    "smartLock": "智能锁",
    "switch": "开关",
    "cleaning": "清洁设备",
    "envSensor": "环境传感",
    "secuSensor": "安防传感",
    "fireSensor": "消防传感",
    "cooking": "厨电",
    "furniture": "家私",
    "bedding": "寝具",
}

DEVICE_TYPE_BY_DOMAIN = {
    "light": "light",
    "cover": "curtain",
    "media_player": "media",
    "fan": "freshAir",
    "humidifier": "humidifier",
    "lock": "smartLock",
    "switch": "switch",
    "vacuum": "cleaning",
    "scene": "scene",
    "script": "script",
    "automation": "automation",
}

CONTROLLABLE_DOMAINS = {
    "light",
    "switch",
    "climate",
    "cover",
    "media_player",
    "fan",
    "humidifier",
    "lock",
    "water_heater",
    "scene",
    "script",
    "automation",
}

SENSOR_TYPES = {
    "temperature": "envSensor",
    "humidity": "envSensor",
    "illuminance": "envSensor",
    "co2": "envSensor",
    "pm25": "envSensor",
    "motion": "secuSensor",
    "occupancy": "secuSensor",
    "door": "secuSensor",
    "window": "secuSensor",
    "smoke": "fireSensor",
    "gas": "fireSensor",
    "moisture": "fireSensor",
}

OFF_SERVICES = {"turn_off", "close_cover", "lock"}
ON_SERVICES = {"turn_on", "open_cover", "unlock"}


@dataclass(frozen=True)
class FunctionSpec:
    name: str
    kind: str
    attribute: str = ""
    unit: str = ""
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    values: tuple[str, ...] = ()
    service: str = ""
    field: str = ""
    read_factor: float = 1.0
    write_factor: float = 1.0
    readonly: bool = False
    value_labels: dict[str, str] = dc_field(default_factory=dict)

    def read(self, state: dict[str, Any]) -> Any:
        attributes = (state or {}).get("attributes") or {}
        if self.attribute == "state" or not self.attribute:
            raw = (state or {}).get("state")
        else:
            raw = attributes.get(self.attribute)
            if raw is None and self.attribute == "color_temp":
                mired = attributes.get("color_temp")
                raw = 1_000_000 / float(mired) if mired else None
        if raw is None:
            return None
        if self.kind == "number":
            try:
                return round(float(raw) * self.read_factor, 2)
            except (TypeError, ValueError):
                return None
        return str(raw)

    def clamp(self, value: float) -> float:
        if self.minimum is not None:
            value = max(self.minimum, value)
        if self.maximum is not None:
            value = min(self.maximum, value)
        return value

    def in_range(self, value: float) -> bool:
        if self.minimum is not None and value < self.minimum:
            return False
        if self.maximum is not None and value > self.maximum:
            return False
        return True


@dataclass(frozen=True)
class DeviceCapability:
    entity_id: str
    domain: str
    device_type: str
    name: str
    area_id: str | None
    functions: dict[str, FunctionSpec]

    def spec(self, function: str) -> FunctionSpec | None:
        return self.functions.get(function)

    def supports(self, function: str) -> bool:
        return function in self.functions

    @property
    def controllable(self) -> bool:
        return self.domain in CONTROLLABLE_DOMAINS

    @property
    def function_names(self) -> list[str]:
        return [
            name for name, spec in self.functions.items() if not spec.readonly
        ]


def _attrs(state: dict[str, Any] | None) -> dict[str, Any]:
    return ((state or {}).get("attributes") or {})


def _number(
    name: str,
    attribute: str,
    *,
    unit: str = "",
    minimum: float | None = None,
    maximum: float | None = None,
    step: float | None = None,
    service: str = "",
    field_name: str = "",
    read_factor: float = 1.0,
    write_factor: float = 1.0,
) -> FunctionSpec | None:
    if minimum is None or maximum is None or minimum >= maximum:
        return None
    return FunctionSpec(
        name=name,
        kind="number",
        attribute=attribute,
        unit=unit,
        minimum=minimum,
        maximum=maximum,
        step=step if step and step > 0 else DEFAULT_STEPS.get(name),
        service=service,
        field=field_name or name,
        read_factor=read_factor,
        write_factor=write_factor,
    )


def _enum(
    name: str,
    attribute: str,
    values: list[str] | tuple[str, ...],
    *,
    service: str,
    field_name: str,
    unit: str = "",
) -> FunctionSpec | None:
    cleaned = tuple(str(value) for value in values if value)
    if not cleaned:
        return None
    return FunctionSpec(
        name=name,
        kind="enum",
        attribute=attribute,
        unit=unit,
        values=cleaned,
        service=service,
        field=field_name or name,
    )


def _status_spec(domain: str) -> FunctionSpec:
    if domain == "cover":
        values = ("open", "closed", "opening", "closing")
        labels = {"open": "打开", "closed": "关闭", "opening": "正在打开", "closing": "正在关闭"}
    elif domain == "lock":
        values = ("locked", "unlocked")
        labels = {"locked": "已上锁", "unlocked": "已解锁"}
    elif domain == "scene":
        values = ("on", "off")
        labels = {"on": "已执行", "off": "未执行"}
    else:
        values = ("on", "off")
        labels = {"on": "打开", "off": "关闭"}
    return FunctionSpec(
        name="status",
        kind="state",
        attribute="state",
        values=values,
        value_labels=labels,
    )


def _light_functions(state: dict[str, Any]) -> dict[str, FunctionSpec]:
    attributes = _attrs(state)
    functions: dict[str, FunctionSpec] = {"status": _status_spec("light")}
    brightness = _number(
        "brightness",
        "brightness",
        unit="%",
        minimum=1,
        maximum=100,
        service="turn_on",
        field_name="brightness_pct",
        read_factor=100 / 255,
    )
    if brightness is not None:
        functions["brightness"] = brightness

    modes = set(attributes.get("supported_color_modes") or ())
    if attributes.get("color_temp_kelvin") is not None or attributes.get("color_temp") is not None or "color_temp" in modes:
        minimum = float(attributes.get("min_color_temp_kelvin") or 2000)
        maximum = float(attributes.get("max_color_temp_kelvin") or 6500)
        spec = _number(
            "colorTemp",
            "color_temp_kelvin",
            unit="K",
            minimum=minimum,
            maximum=maximum,
            service="turn_on",
            field_name="color_temp_kelvin",
        )
        if spec is not None:
            functions["colorTemp"] = spec
    if modes & {"rgb", "rgbw", "rgbww", "hs", "xy"}:
        functions["color"] = FunctionSpec(
            name="color",
            kind="color",
            attribute="rgb_color",
            service="turn_on",
            field="rgb_color",
        )
    return functions


def _climate_functions(state: dict[str, Any]) -> dict[str, FunctionSpec]:
    attributes = _attrs(state)
    domain = "climate"
    functions: dict[str, FunctionSpec] = {"status": _status_spec(domain)}
    minimum = float(attributes.get("min_temp") or 16)
    maximum = float(attributes.get("max_temp") or 30)
    step = float(attributes.get("target_temp_step") or STATE_DEFAULT_TEMP_STEP)
    unit = str(attributes.get("temperature_unit") or "°C")
    spec = _number(
        "targetTemp",
        "temperature",
        unit=unit,
        minimum=minimum,
        maximum=maximum,
        step=step,
        service="set_temperature",
        field_name="temperature",
    )
    if spec is not None and (attributes.get("temperature") is not None or attributes.get("hvac_modes")):
        functions["targetTemp"] = spec
    mode = _enum(
        "mode",
        "hvac_mode",
        attributes.get("hvac_modes") or (),
        service="set_hvac_mode",
        field_name="hvac_mode",
    )
    if mode is not None:
        functions["mode"] = mode
    speed = _enum(
        "speed",
        "fan_mode",
        attributes.get("fan_modes") or (),
        service="set_fan_mode",
        field_name="fan_mode",
        unit="档",
    )
    if speed is not None:
        functions["speed"] = speed
    return functions


STATE_DEFAULT_TEMP_STEP = 1.0


def _cover_functions(state: dict[str, Any]) -> dict[str, FunctionSpec]:
    attributes = _attrs(state)
    functions: dict[str, FunctionSpec] = {"status": _status_spec("cover")}
    if attributes.get("current_position") is not None:
        course = _number(
            "course",
            "current_position",
            unit="%",
            minimum=0,
            maximum=100,
            service="set_cover_position",
            field_name="position",
        )
        if course is not None:
            functions["course"] = course
    if attributes.get("current_tilt_position") is not None:
        angle = _number(
            "angle",
            "current_tilt_position",
            unit="°",
            minimum=0,
            maximum=100,
            service="set_cover_tilt_position",
            field_name="tilt_position",
        )
        if angle is not None:
            functions["angle"] = angle
    return functions


def _media_functions(state: dict[str, Any]) -> dict[str, FunctionSpec]:
    attributes = _attrs(state)
    functions: dict[str, FunctionSpec] = {"status": _status_spec("media_player")}
    if attributes.get("volume_level") is not None:
        volume = _number(
            "volume",
            "volume_level",
            unit="%",
            minimum=0,
            maximum=100,
            service="volume_set",
            field_name="volume_level",
            read_factor=100,
            write_factor=0.01,
        )
        if volume is not None:
            functions["volume"] = volume
    source = _enum(
        "source",
        "source",
        attributes.get("source_list") or (),
        service="select_source",
        field_name="source",
    )
    if source is not None:
        functions["source"] = source
    return functions


def _fan_functions(state: dict[str, Any]) -> dict[str, FunctionSpec]:
    attributes = _attrs(state)
    functions: dict[str, FunctionSpec] = {"status": _status_spec("fan")}
    if attributes.get("percentage") is not None:
        speed = _number(
            "speed",
            "percentage",
            unit="%",
            minimum=0,
            maximum=100,
            step=float(attributes.get("percentage_step") or 10),
            service="set_percentage",
            field_name="percentage",
        )
        if speed is not None:
            functions["speed"] = speed
    else:
        preset = _enum(
            "speed",
            "preset_mode",
            attributes.get("preset_modes") or attributes.get("fan_modes") or (),
            service="set_preset_mode",
            field_name="preset_mode",
            unit="档",
        )
        if preset is not None:
            functions["speed"] = preset
    return functions


def _humidifier_functions(state: dict[str, Any]) -> dict[str, FunctionSpec]:
    attributes = _attrs(state)
    functions: dict[str, FunctionSpec] = {"status": _status_spec("humidifier")}
    if attributes.get("humidity") is not None:
        humidity = _number(
            "targetHumidity",
            "humidity",
            unit="%",
            minimum=float(attributes.get("min_humidity") or 0),
            maximum=float(attributes.get("max_humidity") or 100),
            step=DEFAULT_STEPS["targetHumidity"],
            service="set_humidity",
            field_name="humidity",
        )
        if humidity is not None:
            functions["targetHumidity"] = humidity
    mode = _enum(
        "mode",
        "mode",
        attributes.get("available_modes") or (),
        service="set_mode",
        field_name="mode",
    )
    if mode is not None:
        functions["mode"] = mode
    return functions


def _switch_functions(state: dict[str, Any]) -> dict[str, FunctionSpec]:
    return {
        "status": _status_spec("switch"),
        "systemSwitch": _status_spec("switch"),
    }


def _sensor_functions(state: dict[str, Any], domain: str) -> dict[str, FunctionSpec]:
    attributes = _attrs(state)
    unit = str(attributes.get("unit_of_measurement") or "")
    functions: dict[str, FunctionSpec] = {
        "value": FunctionSpec(
            name="value",
            kind="number",
            attribute="state",
            unit=unit,
            readonly=True,
        )
    }
    if domain == "binary_sensor" or attributes.get("device_class") in (
        "problem",
        "smoke",
        "gas",
        "moisture",
        "door",
        "window",
    ):
        functions["fault"] = FunctionSpec(
            name="fault", kind="state", attribute="state", readonly=True
        )
    if attributes.get("device_class") == "temperature" and unit != "°C":
        functions["value"] = replace(functions["value"], unit=unit or "°C")
    return functions


def device_type_for(
    entity_id: str, state: dict[str, Any] | None, name: str = ""
) -> str:
    domain = entity_id.split(".", 1)[0]
    attributes = _attrs(state)
    device_class = str(attributes.get("device_class") or "")
    label = name or str(attributes.get("friendly_name") or "")

    if domain == "climate":
        if any(word in label for word in ("地暖", "暖气")):
            return "heating"
        if any(word in label for word in ("新风", "换气")):
            return "freshAir"
        return "AC"
    if domain == "cover":
        if "帘" in label or "幕" in label or "百叶" in label:
            return "curtain"
        return "window" if "窗" in label else "curtain"
    if domain == "media_player":
        return "tv" if "电视" in label or device_class == "tv" else "media"
    if domain == "humidifier":
        return "dehumidifier" if "除湿" in label else "humidifier"
    if domain in ("sensor", "binary_sensor"):
        return SENSOR_TYPES.get(device_class, "envSensor")
    mapping = DEVICE_TYPE_BY_DOMAIN.get(domain)
    if mapping:
        return mapping
    return ""


def build_capability(
    entity_id: str,
    state: dict[str, Any] | None,
    *,
    name: str = "",
    area_id: str | None = None,
    profile: dict[str, Any] | None = None,
) -> DeviceCapability:
    domain = entity_id.split(".", 1)[0]
    attributes = _attrs(state)
    display_name = name or str(attributes.get("friendly_name") or entity_id)

    if domain == "light":
        functions = _light_functions(state or {})
    elif domain == "climate":
        functions = _climate_functions(state or {})
    elif domain == "cover":
        functions = _cover_functions(state or {})
    elif domain == "media_player":
        functions = _media_functions(state or {})
    elif domain == "fan":
        functions = _fan_functions(state or {})
    elif domain == "humidifier":
        functions = _humidifier_functions(state or {})
    elif domain == "switch":
        functions = _switch_functions(state or {})
    elif domain == "lock":
        functions = {"status": _status_spec("lock")}
    elif domain == "water_heater":
        functions = _climate_functions(state or {})
    elif domain == "vacuum":
        functions = {"status": _status_spec("vacuum")}
    elif domain in ("scene", "script", "automation"):
        functions = {"status": _status_spec(domain)}
    elif domain in ("sensor", "binary_sensor"):
        functions = _sensor_functions(state or {}, domain)
    else:
        functions = {}

    if profile:
        functions = _apply_profile(functions, profile)

    return DeviceCapability(
        entity_id=entity_id,
        domain=domain,
        device_type=device_type_for(entity_id, state, display_name),
        name=display_name,
        area_id=area_id,
        functions=functions,
    )


def _apply_profile(
    functions: dict[str, FunctionSpec], profile: dict[str, Any]
) -> dict[str, FunctionSpec]:
    result = dict(functions)
    for name in profile.get("disabled") or ():
        result.pop(str(name), None)
    for name, step in (profile.get("steps") or {}).items():
        if name in result:
            try:
                result[name] = replace(result[name], step=float(step))
            except (TypeError, ValueError):
                continue
    for name, bounds in (profile.get("ranges") or {}).items():
        if name not in result or not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
            continue
        try:
            minimum, maximum = float(bounds[0]), float(bounds[1])
        except (TypeError, ValueError):
            continue
        if minimum < maximum:
            result[name] = replace(
                result[name], minimum=minimum, maximum=maximum
            )
    return result


def device_type_override(profile_type: str | None) -> str | None:
    return profile_type or None

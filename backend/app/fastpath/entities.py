from __future__ import annotations

import fnmatch
from dataclasses import dataclass

from app.fastpath.grammar import detect_domain, strip_domain_words
from app.fastpath.normalize import normalize

SHORTHAND_ALL_DOMAINS = {"light", "switch"}


@dataclass(frozen=True)
class AliasRecord:
    entity_id: str
    alias: str
    display_name: str
    area_id: str | None
    domain: str
    can_control: bool


@dataclass(frozen=True)
class EntityRef:
    entity_id: str
    name: str
    area_id: str | None
    domain: str
    can_control: bool


@dataclass
class Resolution:
    status: str
    entities: list[EntityRef]
    reason: str = ""
    room_text: str = ""


class EntityResolver:
    """别名字典 + HA 区域 + 状态缓存，只做确定性解析，不猜测。"""

    def __init__(self, aliases: list[AliasRecord], registry=None) -> None:
        self._aliases = aliases
        self._alias_by_name: dict[str, AliasRecord] = {}
        for record in aliases:
            self._alias_by_name.setdefault(normalize(record.alias), record)
            if record.display_name:
                self._alias_by_name.setdefault(normalize(record.display_name), record)
        self._registry = registry

    def resolve(
        self,
        target: str,
        domain: str | None,
        states: dict[str, dict],
        *,
        explicit_all: bool = False,
        member=None,
        view_patterns: list[str] | None = None,
        want_control: bool = True,
    ) -> Resolution:
        target = normalize(target)
        detected = detect_domain(target)
        domain = domain or detected
        refs = self._build_refs(states, domain)
        if view_patterns is not None and member is not None and member.role != "admin":
            refs = [
                ref
                for ref in refs
                if any(fnmatch.fnmatchcase(ref.entity_id, pattern) for pattern in view_patterns)
            ]

        if explicit_all:
            candidates = refs
            return self._finish(candidates, target, domain, explicit_all=True, want_control=want_control)

        alias = self._alias_by_name.get(target)
        if alias is not None:
            if domain and alias.domain and alias.domain != domain:
                return Resolution("none", [], "domain_mismatch")
            matched = [ref for ref in refs if ref.entity_id == alias.entity_id]
            if not matched:
                if view_patterns is not None and member is not None and member.role != "admin":
                    if not any(
                        fnmatch.fnmatchcase(alias.entity_id, pattern)
                        for pattern in view_patterns
                    ):
                        return Resolution("none", [], "not_visible")
                matched = [
                    EntityRef(
                        entity_id=alias.entity_id,
                        name=alias.display_name or alias.alias,
                        area_id=alias.area_id,
                        domain=alias.domain or alias.entity_id.split(".", 1)[0],
                        can_control=alias.can_control,
                    )
                ]
            return self._finish(matched, target, domain, want_control=want_control)

        named = [ref for ref in refs if normalize(ref.name) == target]
        if named:
            return self._finish(named, target, domain, want_control=want_control)

        room_text = strip_domain_words(target)
        if room_text:
            by_area = self._match_area(room_text, refs)
            if by_area:
                return self._finish(
                    by_area, target, domain, want_control=want_control, room_text=room_text
                )
            by_name = [ref for ref in refs if room_text in normalize(ref.name)]
            if by_name:
                return self._finish(
                    by_name, target, domain, want_control=want_control, room_text=room_text
                )
            return Resolution("none", [], "ambig_room", room_text)

        return self._finish(
            refs, target, domain, want_control=want_control, shorthand=True
        )

    def resolve_in_area(
        self,
        area_text: str,
        domain: str | None,
        states: dict[str, dict],
        *,
        view_patterns: list[str] | None = None,
        want_control: bool = True,
    ) -> Resolution:
        refs = self._build_refs(states, domain)
        if view_patterns is not None:
            refs = [
                ref
                for ref in refs
                if any(fnmatch.fnmatchcase(ref.entity_id, pattern) for pattern in view_patterns)
            ]
        if want_control:
            refs = [ref for ref in refs if ref.can_control]
        room_text = normalize(area_text)
        matched = self._match_area(room_text, refs)
        if not matched:
            return Resolution("none", [], "no_area_entity", room_text)
        return Resolution("ok", matched, room_text=room_text)

    def _build_refs(self, states: dict[str, dict], domain: str | None) -> list[EntityRef]:
        refs: dict[str, EntityRef] = {}
        for entity_id, state in states.items():
            entity_domain = entity_id.split(".", 1)[0]
            if domain and entity_domain != domain:
                continue
            attributes = (state or {}).get("attributes") or {}
            refs[entity_id] = EntityRef(
                entity_id=entity_id,
                name=str(attributes.get("friendly_name") or entity_id),
                area_id=self._area_of(entity_id),
                domain=entity_domain,
                can_control=True,
            )
        known = set(refs)
        for alias in self._aliases:
            if alias.entity_id in known:
                continue
            if domain and alias.domain != domain:
                continue
            refs[alias.entity_id] = EntityRef(
                entity_id=alias.entity_id,
                name=alias.display_name or alias.alias,
                area_id=alias.area_id or self._area_of(alias.entity_id),
                domain=alias.domain or alias.entity_id.split(".", 1)[0],
                can_control=alias.can_control,
            )
        return list(refs.values())

    def _finish(
        self,
        candidates: list[EntityRef],
        target: str,
        domain: str | None,
        *,
        explicit_all: bool = False,
        want_control: bool = True,
        room_text: str = "",
        shorthand: bool = False,
    ) -> Resolution:
        if want_control:
            candidates = [ref for ref in candidates if ref.can_control]
        candidates = sorted(candidates, key=lambda ref: (ref.area_id or "", ref.name, ref.entity_id))
        if not candidates:
            return Resolution("none", [], "no_entity", room_text)
        if len(candidates) == 1:
            return Resolution("ok", candidates, room_text=room_text)
        if explicit_all:
            return Resolution("ok", candidates, room_text=room_text)
        if shorthand and (domain in SHORTHAND_ALL_DOMAINS or (domain is None and not target)):
            return Resolution("ok", candidates, room_text=room_text)
        if domain is None:
            domains = {ref.domain for ref in candidates}
            if len(domains) == 1:
                return Resolution("ok", candidates, room_text=room_text)
        if target and strip_domain_words(target) == "" and domain in SHORTHAND_ALL_DOMAINS:
            return Resolution("ok", candidates, room_text=room_text)
        return Resolution("ambiguous", candidates[:3], "multi_candidate", room_text)

    def _match_area(self, room_text: str, refs: list[EntityRef]) -> list[EntityRef]:
        area_ids = {ref.area_id for ref in refs if ref.area_id}
        matched_areas = set()
        room = normalize(room_text)
        for area_id in area_ids:
            name = normalize(self._area_name(area_id) or "")
            if room == normalize(area_id) or (name and (room == name or room in name or name in room)):
                matched_areas.add(area_id)
        if not matched_areas:
            return []
        return [ref for ref in refs if ref.area_id in matched_areas]

    def _area_of(self, entity_id: str) -> str | None:
        if self._registry is None:
            return None
        area_of = getattr(self._registry, "area_of", None)
        return area_of(entity_id) if callable(area_of) else None

    def _area_name(self, area_id: str) -> str | None:
        if self._registry is None:
            return None
        area_name = getattr(self._registry, "area_name", None)
        return area_name(area_id) if callable(area_name) else None

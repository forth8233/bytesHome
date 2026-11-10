# -*- coding: utf-8 -*-
"""临时诊断：快路径为何无法把「打开筒灯」解析成设备动作。

复制 engine._resolve_schedule_actions 的两条路径（parse_intent+resolver / voice）
对「打开筒灯」做解析，打印每一步结果，定位定时落空的根因。
"""
import asyncio
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from sqlalchemy import select

from app.db.session import SessionFactory
from app.db.models import EntityAlias, VoiceLexiconEntry, VoiceDeviceProfile, VoiceGroupList, Member
from app.fastpath.entities import EntityResolver, AliasRecord
from app.fastpath.grammar import parse_intent
from app.fastpath.voice import VoiceParser
from app.services.config_service import ConfigService
from app.ha.adapter import HAAdapter
from app.services.bootstrap import bootstrap


async def main() -> None:
    config = ConfigService(SessionFactory)
    await config.load()
    await bootstrap(config)

    adapter = HAAdapter(config, call_timeout_seconds=10.0)
    await adapter.start()
    await asyncio.sleep(1.0)
    states = await adapter.cache.snapshot()
    registry = adapter.registry
    print("states:", len(states), "registry.entities:", len(getattr(registry, "entities", {}) or {}))

    async with SessionFactory() as s:
        alias_rows = (await s.scalars(select(EntityAlias).order_by(EntityAlias.id))).all()
        voice_rows = (await s.scalars(
            select(VoiceLexiconEntry).where(VoiceLexiconEntry.enabled.is_(True)).order_by(VoiceLexiconEntry.id)
        )).all()
        profile_rows = (await s.scalars(
            select(VoiceDeviceProfile).where(VoiceDeviceProfile.enabled.is_(True))
        )).all()
        group_rows = (await s.scalars(
            select(VoiceGroupList).where(VoiceGroupList.enabled.is_(True))
        )).all()
        member = (await s.scalars(select(Member).limit(1))).first()

    aliases = [
        AliasRecord(entity_id=r.entity_id, alias=r.alias, display_name=r.display_name,
                    area_id=r.area_id, domain=r.domain or r.entity_id.split(".", 1)[0],
                    can_control=r.can_control)
        for r in alias_rows
    ]
    print("aliases:", len(aliases), "voice:", len(voice_rows),
          "profiles:", len(profile_rows), "groups:", len(group_rows))

    controls = [parse_intent("打开筒灯"), parse_intent("30 秒后打开筒灯")]
    resolver = EntityResolver(aliases, registry)

    # 打印所有 light 实体及其 friendly_name（找「筒灯」）
    print("\n== light like entities ==")
    for eid, st in states.items():
        if eid.startswith("light."):
            fn = (st.get("attributes") or {}).get("friendly_name", "")
            print(f"  {eid:45} | {fn}")

    # 直接 resolve「筒灯」target=简体
    for tgt in ["筒灯", "灯", "打开筒灯"]:
        for domain in ["light", None]:
            res = resolver.resolve(tgt, domain, states, want_control=True)
            print(f"resolver.resolve({tgt!r}, domain={domain}) -> status={res.status} "
                  f"entities={[getattr(e,'entity_id','?') for e in res.entities]} reason={res.reason}")

    print("\nparser done")


asyncio.run(main())
from __future__ import annotations

import logging
import re

from app.chat.schema import MemoryExtraction
from app.db.models import Member
from app.llm.client import LLMClient
from app.memory.service import MemoryService

log = logging.getLogger(__name__)

KEY_PATTERN = re.compile(r"^[a-z_]+\.[^\s]{1,64}$")

_EXTRACTION_SYSTEM = (
    "你是家庭管家的记忆抽取器。根据用户说的一句话，判断其中是否表达了可长期沉淀的偏好。\n"
    "仅当用户给出了明确、稳定的偏好时才抽取，例如「以后主卧灯都调到70」「我习惯夏天空调26度」「不喜欢套自动开灯」。\n"
    "不要把一次性的设备指令（如「现在开灯」）当成偏好。\n"
    "输出 memory_items 数组；没有可沉淀的偏好时输出空数组。\n"
    "规则：\n"
    "- key 形如 域.名称，全部小写、点分（如 climate.夏季空调温度, light.主卧亮度）。\n"
    "- value：给用户原始表述的中文摘要；value_json：与 value_type 一致的结构化值。\n"
    "- value_type：number 用数字；number_range 用 [最小,最大]；boolean 用 true/false；text 用普通文本。\n"
    "- confidence 0~1，越明确越接近 1；scope 默认 member。\n"
    "- 用户表示要取消某条偏好时，intent 填 withdraw 并只填 key。"
)


def _build_extraction_messages(user_text: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _EXTRACTION_SYSTEM},
        {"role": "user", "content": f"请分析这句话：{user_text}"},
    ]


async def extract_and_store_memory(
    client: LLMClient,
    session_factory,
    member: Member,
    user_text: str,
) -> None:
    """对话慢路径后的后台记忆抽取：尽最大努力，失败不阻塞也不抛错。"""
    try:
        result = await client.structured_with_fallback(
            _build_extraction_messages(user_text),
            MemoryExtraction,
            schema_name="memory_extraction",
            temperature=0,
            max_tokens=400,
        )
        if not result.parsed_ok or not result.data:
            return
        extraction = MemoryExtraction.model_validate(result.data)
    except Exception:  # noqa: BLE001 - 抽取失败不影响对话
        log.exception("chat.memory_extract_llm_failed")
        return

    items = extraction.memory_items or []
    if not items:
        return

    async with session_factory() as session:
        service = MemoryService(session)
        for item in items:
            try:
                if item.intent == "withdraw":
                    await service.delete_preference_by_key(member.id, item.key)
                    continue
                if not KEY_PATTERN.match(item.key):
                    log.warning("chat.memory_extract_bad_key", key=item.key)
                    continue
                value = item.value.strip() or item.key
                value_json = item.value_json
                if item.value_type not in ("text",) and value_json is None:
                    # 没有可信结构化值时，回退为纯文本，避免写入校验不过的脏数据
                    value_json = None
                await service.upsert_preference(
                    member.id,
                    item.key,
                    value,
                    value_type=item.value_type,
                    value_json=value_json,
                    source="llm",
                    confidence=item.confidence,
                    scope=item.scope,
                )
            except Exception:  # noqa: BLE001 - 单条失败跳过
                log.exception("chat.memory_extract_store_failed", key=item.key)
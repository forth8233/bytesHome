from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

# 语音消息转写的全局开关与默认参数（system_config 的 channels.asr 块）。
DEFAULT_ASR: dict[str, Any] = {
    "enabled": False,
    # 兼容 OpenAI /audio/transcriptions 接口的云端 ASR
    # （SiliconFlow / Groq / 阿里云百炼 DashScope 兼容模式等均可）
    "api_base": "",
    "api_key": "",
    "model": "FunAudioLLM/SenseVoiceSmall",
    "language": "zh",
    "timeout_seconds": 30,
    "max_audio_mb": 10,
}

_TRANSCRIPTION_PATH = "/audio/transcriptions"
_PROMPT = "这是智能家居指令，请输出听到的中文句子。"
# 转写在 dispatcher worker 里同步等待，最多重试一次，别拖住队列
_MAX_ATTEMPTS = 2
_RETRY_STATUS = {"429", "500", "502", "503", "504"}

# 常见失败状态 → 给终端用户的提示（管理端仍看完整 asr_error）
_STATUS_HINTS: dict[str, str] = {
    "401": "语音服务的密钥无效或已过期",
    "402": "语音服务账户余额不足",
    "403": "语音服务拒绝了这次请求（权限或欠费）",
    "404": "语音服务地址或模型名不对",
    "413": "语音文件太大",
    "422": "语音服务不支持这份音频",
    "429": "语音服务限流，稍后再试",
}


def user_hint(error: str | None) -> str | None:
    """把 asr_error 翻译成一句终端用户能懂的提示；识别不出就返回 None。

    错误形如 "HTTP 402: {...}"（OpenAICompatibleTranscriber）或下载/参数类文本。
    """
    if not error:
        return None
    if error.startswith("HTTP "):
        hint = _STATUS_HINTS.get(error[5:8])
        if hint:
            return hint
    return None


class TranscribeError(Exception):
    """转写失败；细节进状态给管理员看，终端用户只收到统一提示。"""


class Transcriber(Protocol):
    """语音转文字的最小接口；以后接本地引擎（faster-whisper 等）实现同名方法即可。"""

    async def transcribe(self, data: bytes, filename: str) -> str: ...


class OpenAICompatibleTranscriber:
    """调 OpenAI 兼容 /audio/transcriptions 接口的转写器。

    飞书语音是 opus 编码（.ogg 容器），主流兼容接口直接收 ogg/opus，
    不转码、不落盘，内存里走完请求。
    httpx 客户端按实例复用（保活连接省掉每次的 TLS 握手），非线程安全，
    而调用方（dispatcher worker / 适配器线程循环）各自持有独立实例。
    """

    def __init__(
        self,
        *,
        api_base: str,
        api_key: str,
        model: str,
        language: str = "zh",
        timeout: float = 30.0,
    ) -> None:
        self._api_base = (api_base or "").rstrip("/")
        self._api_key = api_key or ""
        self._model = model or ""
        self._language = language or "zh"
        self._timeout = float(timeout)
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def transcribe(self, data: bytes, filename: str) -> str:
        last_error = ""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                client = self._get_client()
                response = await client.post(
                    f"{self._api_base}{_TRANSCRIPTION_PATH}",
                    headers={"Authorization": "Bearer " + self._api_key},
                    files={"file": (filename, data, "audio/ogg")},
                    data={
                        "model": self._model,
                        "language": self._language,
                        "prompt": _PROMPT,
                    },
                )
                if response.status_code == 200:
                    text = str(response.json().get("text") or "").strip()
                    if text:
                        return text
                    last_error = "转写结果为空"
                else:
                    last_error = f"HTTP {response.status_code}: {response.text[:200]}"
            except (httpx.HTTPError, ValueError) as exc:
                last_error = str(exc)[:200]
            # 4xx（除限流）是确定性失败，重试没有意义
            status = last_error[4:7] if last_error.startswith("HTTP ") else ""
            if status and status not in _RETRY_STATUS:
                break
            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(1.0)
        raise TranscribeError(last_error or "转写失败")

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None


def build_transcriber(asr_config: dict[str, Any]) -> OpenAICompatibleTranscriber | None:
    """按 channels.asr 配置装配转写器；配置不完整返回 None（语音降级为提示）。"""
    cfg = dict(DEFAULT_ASR)
    cfg.update(asr_config or {})
    if not cfg.get("enabled"):
        return None
    api_base = str(cfg.get("api_base") or "").strip()
    if not api_base.startswith(("http://", "https://")):
        return None
    return OpenAICompatibleTranscriber(
        api_base=api_base,
        api_key=str(cfg.get("api_key") or ""),
        model=str(cfg.get("model") or DEFAULT_ASR["model"]),
        language=str(cfg.get("language") or "zh"),
        timeout=float(cfg.get("timeout_seconds") or 30),
    )

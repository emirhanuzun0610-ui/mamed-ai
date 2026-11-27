"""Ücretsiz Türkçe TTS motoru (Microsoft Edge TTS / edge-tts)."""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any, List

from openjarvis.core.registry import TTSRegistry
from openjarvis.speech.tts import TTSBackend, TTSResult

_DEFAULT_VOICE_ID = os.environ.get("MAMED_TTS_VOICE", "tr-TR-AhmetNeural")
_SUPPORTED_VOICES = [
    "tr-TR-AhmetNeural",
    "tr-TR-EmelNeural",
]


def _format_rate(speed: float) -> str:
    """Convert numeric speed (e.g. 1.0, 1.2) to edge-tts rate string."""
    try:
        pct = int(round((float(speed) - 1.0) * 100))
    except (TypeError, ValueError):
        pct = 0
    pct = max(-50, min(100, pct))
    return f"+{pct}%" if pct >= 0 else f"{pct}%"


async def _synthesize_async(text: str, voice: str, rate: str) -> bytes:
    import edge_tts

    communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate)
    chunks: list[bytes] = []
    async for chunk in communicate.stream():
        if chunk.get("type") == "audio" and chunk.get("data"):
            chunks.append(chunk["data"])
    audio = b"".join(chunks)
    if not audio:
        raise RuntimeError("Edge-TTS ses verisi üretemedi")
    return audio


def _run_coro_sync(coro: Any) -> bytes:
    """Run an async coroutine safely even if called from a running event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result_box: dict[str, Any] = {}

    def _runner() -> None:
        try:
            result_box["audio"] = asyncio.run(coro)
        except Exception as exc:
            result_box["error"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join(timeout=60.0)
    if "error" in result_box:
        raise result_box["error"]
    if "audio" not in result_box:
        raise RuntimeError("Edge-TTS zaman aşımına uğradı")
    return result_box["audio"]


@TTSRegistry.register("edge_tts")
class EdgeTTSBackend(TTSBackend):
    """Ücretsiz ve doğal Türkçe metin okuma (Edge-TTS)."""

    backend_id = "edge_tts"

    def __init__(self, *, voice_id: str = "") -> None:
        self._voice_id = (
            voice_id
            or os.environ.get("MAMED_TTS_VOICE", "").strip()
            or _DEFAULT_VOICE_ID
        )

    def synthesize(
        self,
        text: str,
        *,
        voice_id: str = "",
        speed: float = 1.0,
        output_format: str = "mp3",
    ) -> TTSResult:
        resolved_voice = (voice_id or self._voice_id or _DEFAULT_VOICE_ID).strip()
        if not resolved_voice or resolved_voice.startswith(("bm_", "bf_", "am_", "af_")):
            resolved_voice = _DEFAULT_VOICE_ID
        rate_str = _format_rate(speed)
        audio = _run_coro_sync(_synthesize_async(text, resolved_voice, rate_str))
        return TTSResult(
            audio=audio,
            format="mp3",
            voice_id=resolved_voice,
            sample_rate=24000,
            metadata={"backend": "edge_tts", "voice": resolved_voice},
        )

    def available_voices(self) -> List[str]:
        return list(_SUPPORTED_VOICES)

    def health(self) -> bool:
        try:
            import edge_tts  # noqa: F401

            return True
        except ImportError:
            return False


__all__ = ["EdgeTTSBackend"]

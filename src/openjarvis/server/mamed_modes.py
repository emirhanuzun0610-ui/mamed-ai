"""Mamed görev karmaşıklık modları, kalıcı durum ve Türkçe sesli mod yönlendirmesi."""

from __future__ import annotations

import json
import os
import threading
import unicodedata
from pathlib import Path
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from openjarvis.core.paths import get_config_dir

ModeId = Literal["fast", "standard", "deep"]

_STATE_LOCK = threading.RLock()

_TR_MAP = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")


def _normalize_tr(text: str) -> str:
    ascii_text = text.translate(_TR_MAP).lower()
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", ascii_text) if not unicodedata.combining(ch)
    ).strip()


def _mode_definitions() -> Dict[ModeId, Dict[str, Any]]:
    fast_model = os.environ.get("MAMED_FAST_MODEL", "gemini-3.5-flash-lite").strip() or "gemini-3.5-flash-lite"
    standard_model = os.environ.get("MAMED_STANDARD_MODEL", "gemini-3.5-flash").strip() or "gemini-3.5-flash"
    deep_model = os.environ.get("MAMED_DEEP_MODEL", "gemini-3.5-flash").strip() or "gemini-3.5-flash"
    return {
        "fast": {
            "id": "fast",
            "label": "Hızlı Mod",
            "short_label": "Hızlı",
            "description": "Anlık sohbet, hızlı sorular ve basit komutlar için en düşük gecikme",
            "model": fast_model,
            "temperature": 0.5,
            "max_tokens": 1024,
            "use_tools": False,
            "color": "#10b981",
            "system_addendum": (
                "HIZLI MOD AKTİF: Kısa, doğrudan ve hızlı Türkçe yanıt ver. "
                "Gereksiz uzatma yapma."
            ),
        },
        "standard": {
            "id": "standard",
            "label": "Dengeli Mod",
            "short_label": "Dengeli",
            "description": "Günlük görevler, web araması, notlar ve dengeli hız/kalite",
            "model": standard_model,
            "temperature": 0.7,
            "max_tokens": 4096,
            "use_tools": True,
            "color": "#0ea5e9",
            "system_addendum": (
                "DENGELİ MOD AKTİF: Net, faydalı ve doğal Türkçe yanıt ver; "
                "gerektiğinde mevcut araçları kullan."
            ),
        },
        "deep": {
            "id": "deep",
            "label": "Derin Mod",
            "short_label": "Derin",
            "description": "Karmaşık mühendislik, araştırma, çok adımlı analiz ve sıkı doğrulama",
            "model": deep_model,
            "temperature": 0.3,
            "max_tokens": 8192,
            "use_tools": True,
            "color": "#a855f7",
            "system_addendum": (
                "DERİN / GELİŞMİŞ MOD AKTİF: Adım adım derinlemesine düşün, "
                "varsayımları ve kenar durumları titizlikle doğrula, kapsamlı ve "
                "yapılandırılmış Türkçe yanıt üret."
            ),
        },
    }


def _state_path() -> Path:
    return get_config_dir() / "mamed_state.json"


def get_mode_state() -> Dict[str, Any]:
    """Kalıcı Mamed mod durumunu döndürür (PC, mobil ve Telegram arasında ortak)."""
    modes = _mode_definitions()
    default_state: Dict[str, Any] = {"mode": "standard", "auto_route": False}
    with _STATE_LOCK:
        path = _state_path()
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    mode_val = loaded.get("mode", "standard")
                    if mode_val in modes:
                        default_state["mode"] = mode_val
                    default_state["auto_route"] = bool(loaded.get("auto_route", False))
            except Exception:
                pass
    mode_id: ModeId = default_state["mode"]
    return {
        "mode": mode_id,
        "auto_route": default_state["auto_route"],
        "effective_model": modes[mode_id]["model"],
        "modes": modes,
        "assistant_name": "Mamed",
        "wake_phrases": ["Mamed", "Mamed uyan"],
    }


def set_mode_state(
    *,
    mode: Optional[str] = None,
    auto_route: Optional[bool] = None,
) -> Dict[str, Any]:
    """Kalıcı Mamed mod durumunu günceller."""
    modes = _mode_definitions()
    with _STATE_LOCK:
        current = get_mode_state()
        new_mode = current["mode"]
        new_auto = current["auto_route"]
        if mode is not None:
            if mode not in modes:
                raise ValueError(f"Geçersiz mod: {mode}")
            new_mode = mode
        if auto_route is not None:
            new_auto = bool(auto_route)

        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"mode": new_mode, "auto_route": new_auto}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return get_mode_state()


def detect_mode_voice_command(text: str) -> Optional[Dict[str, Any]]:
    """Türkçe sesli/yazılı mod değiştirme komutlarını tanır."""
    norm = _normalize_tr(text)
    if not norm:
        return None

    # Otomatik yönlendirme komutları
    if any(k in norm for k in ("otomatik yonlendirmeyi ac", "otomatik modu ac", "oto modu ac")):
        return {"auto_route": True, "reply": "Otomatik karmaşıklık yönlendirmesi açıldı."}
    if any(k in norm for k in ("otomatik yonlendirmeyi kapat", "otomatik modu kapat", "oto modu kapat")):
        return {"auto_route": False, "reply": "Otomatik karmaşıklık yönlendirmesi kapatıldı."}

    is_switch_intent = any(
        w in norm for w in ("moda gec", "modu ac", "moduna gec", "mod aktif", "moda al")
    ) or norm in ("hizli mod", "basit mod", "dengeli mod", "standart mod", "derin mod", "gelismis mod")

    if not is_switch_intent:
        return None

    if any(k in norm for k in ("hizli", "basit")):
        return {"mode": "fast", "reply": "Hızlı Mod etkinleştirildi."}
    if any(k in norm for k in ("dengeli", "standart", "normal")):
        return {"mode": "standard", "reply": "Dengeli Mod etkinleştirildi."}
    if any(k in norm for k in ("derin", "gelismis", "karmasik")):
        return {"mode": "deep", "reply": "Derin Mod etkinleştirildi."}
    return None


_DEEP_KEYWORDS = (
    "mimari", "kod yaz", "hata ayikla", "debug", "refactor", "analiz et",
    "arastir", "karsilastir", "planla", "algoritma", "guvenlik", "optimize",
    "detayli", "kapsamli", "tasarla", "neden calismiyor", "dokuman",
)


def auto_route_mode(text: str) -> ModeId:
    """Mesaj karmaşıklığına göre otomatik mod seçer."""
    norm = _normalize_tr(text)
    if not norm:
        return "standard"
    if len(norm) > 220 or "```" in text or any(k in norm for k in _DEEP_KEYWORDS):
        return "deep"
    if len(norm) <= 45 and "?" not in norm or norm.startswith(("selam", "merhaba", "gunaydin", "saat kac", "nasilsin")):
        return "fast"
    return "standard"


def resolve_effective_mode(
    requested_mode: Optional[str],
    user_text: str,
) -> tuple[ModeId, Dict[str, Any], Optional[str]]:
    """İstek için geçerli modu, yapılandırmayı ve varsa doğrudan mod değiştirme yanıtını döndürür."""
    modes = _mode_definitions()
    cmd = detect_mode_voice_command(user_text)
    if cmd is not None:
        updated = set_mode_state(
            mode=cmd.get("mode"),
            auto_route=cmd.get("auto_route"),
        )
        m_id: ModeId = updated["mode"]
        return m_id, modes[m_id], cmd["reply"]

    state = get_mode_state()
    if requested_mode in modes:
        chosen: ModeId = requested_mode  # type: ignore[assignment]
    elif state["auto_route"] and user_text:
        chosen = auto_route_mode(user_text)
    else:
        chosen = state["mode"]
    return chosen, modes[chosen], None


# ---- FastAPI Router ----

mamed_router = APIRouter(prefix="/v1/mamed", tags=["mamed"])


class ModeUpdateRequest(BaseModel):
    mode: Optional[ModeId] = None
    auto_route: Optional[bool] = None


@mamed_router.get("/mode")
async def get_mamed_mode():
    """Aktif Mamed görev modunu ve mevcut mod tanımlarını döndürür."""
    return get_mode_state()


@mamed_router.put("/mode")
async def update_mamed_mode(body: ModeUpdateRequest):
    """Mamed görev modunu veya otomatik yönlendirmeyi günceller."""
    try:
        return set_mode_state(mode=body.mode, auto_route=body.auto_route)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


__all__ = [
    "ModeId",
    "auto_route_mode",
    "detect_mode_voice_command",
    "get_mode_state",
    "mamed_router",
    "resolve_effective_mode",
    "set_mode_state",
]

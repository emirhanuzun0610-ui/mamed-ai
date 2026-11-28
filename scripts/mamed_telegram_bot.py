"""Mamed Telegram Botu — Türkçe metin/ses komutları ve PC çevrimdışıyken Render bulut yedeği."""

from __future__ import annotations

import io
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Mamed-Telegram] %(levelname)s: %(message)s",
)
logger = logging.getLogger("mamed.telegram")

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_DIR = _REPO_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))
load_dotenv(_REPO_ROOT / "api_keys.env")

_VOICE_REPLIES_BY_CHAT: dict[int, bool] = {}


def _api_token() -> str:
    return (
        os.environ.get("MAMED_API_TOKEN", "").strip()
        or os.environ.get("OPENJARVIS_API_KEY", "").strip()
    )


def _auth_headers(target_kind: str = "yerel") -> dict[str, str]:
    if target_kind == "bulut":
        cloud_tok = os.environ.get("MAMED_CLOUD_API_KEY", "").strip()
        if cloud_tok:
            return {"Authorization": f"Bearer {cloud_tok}"}
    tok = _api_token()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _allowed_chat_ids() -> set[str]:
    raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    if not raw:
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


def _is_chat_allowed(chat_id: int) -> bool:
    allowed = _allowed_chat_ids()
    return not allowed or str(chat_id) in allowed


def select_backend_url(
    local_url: Optional[str] = None,
    cloud_url: Optional[str] = None,
) -> tuple[str, str]:
    """Yerel PC sunucusunu dener; çevrimdışıysa Render bulut sunucusuna geçer."""
    local = (local_url or os.environ.get("MAMED_LOCAL_URL", "http://127.0.0.1:8000")).rstrip("/")
    cloud = (cloud_url or os.environ.get("MAMED_CLOUD_FALLBACK_URL", "")).rstrip("/")

    try:
        resp = httpx.get(f"{local}/health", timeout=2.0)
        if resp.status_code == 200:
            return local, "yerel"
    except Exception:
        pass

    if cloud:
        return cloud, "bulut"
    return local, "yerel"


def _mode_label(mode_id: str) -> str:
    return {
        "fast": "Hızlı Mod",
        "standard": "Dengeli Mod",
        "deep": "Derin Mod",
    }.get(mode_id, mode_id)


def format_school_mail_status_text(info: dict) -> str:
    """Okul mail dinleyici durumunu Telegram mesaji olarak bicimlendirir."""
    running = bool(info.get("watcher_running"))
    status = str(info.get("status") or "-")
    conn = str(info.get("connection_type") or "-")
    conn_label = {
        "imap_idle": "IMAP IDLE (Anlık)",
        "imap_poll_15s": "IMAP 15sn Yoklama",
        "bagli_degil": "Bağlı Değil",
    }.get(conn, conn)
    last_check = info.get("last_check_at") or "Henüz kontrol edilmedi"
    prof_total = int(info.get("total_professor_mails") or 0)
    filtered_total = int(info.get("filtered_announcements_count") or 0)
    lines = [
        "Mamed Okul E-posta Durumu",
        f"Dinleyici: {'Çalışıyor' if running else 'Kapalı'} ({status})",
        f"Bağlantı: {conn_label} ({info.get('imap_host') or '-'}:{info.get('imap_port') or 993})",
        f"Hesap: {info.get('username') or 'Ayarlı değil'}",
        f"Son Kontrol: {last_check}",
        f"Yakalanan Hoca Maili: {prof_total} | Filtrelenen Duyuru: {filtered_total}",
    ]
    if info.get("last_error"):
        lines.append(f"Son Hata: {info['last_error']}")
    recent = info.get("recent_professor_mails") or []
    if recent:
        lines.append("\nSon Hoca Mailleri:")
        for idx, item in enumerate(recent[:3], start=1):
            notif = item.get("formatted_notification") or item.get("summary") or item.get("subject") or "-"
            lines.append(f"{idx}. {notif}")
    return "\n".join(lines)[:4000]


def format_school_mail_check_text(result: dict) -> str:
    """Anlik okul mail kontrol sonucunu Telegram mesaji olarak bicimlendirir."""
    if result.get("baseline_initialized"):
        return (
            "Mamed okul posta kutusu ilk kez taban olarak işaretlendi "
            f"(Son UID: {result.get('last_uid', 0)}). Bundan sonraki yeni hoca mailleri anında bildirilecek."
        )
    checked = int(result.get("checked_count") or 0)
    prof = int(result.get("professor_count") or 0)
    filtered = int(result.get("filtered_count") or 0)
    if checked == 0:
        return "Yeni okul e-postası bulunmuyor."
    return (
        f"Okul posta kutusu kontrol edildi: {checked} yeni e-posta incelendi "
        f"({prof} hoca maili, {filtered} duyuru/sistem filtrelendi)."
    )


async def _ask_mamed(text: str, *, mode: Optional[str] = None) -> tuple[str, str, str]:
    base_url, target_kind = select_backend_url()
    payload: dict = {
        "model": "",
        "messages": [{"role": "user", "content": text}],
        "stream": False,
    }
    if mode:
        payload["mode"] = mode

    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(
            f"{base_url}/v1/chat/completions",
            headers=_auth_headers(target_kind),
            json=payload,
        )
        if resp.status_code == 401:
            return "Sunucu erişim anahtarı geçersiz. Lütfen api_keys.env ayarını kontrol et.", target_kind, ""
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices") or []
        reply = (choices[0].get("message", {}).get("content") or "").strip() if choices else ""
        return reply or "Şu an yanıt üretemedim.", target_kind, data.get("model", "")


async def _synthesize_turkish(text: str) -> Optional[bytes]:
    base_url, target_kind = select_backend_url()
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                f"{base_url}/v1/speech/synthesize",
                headers=_auth_headers(target_kind),
                json={"text": text[:1500]},
            )
            if resp.status_code == 200 and resp.content:
                return resp.content
    except Exception as exc:
        logger.warning("Ses sentezi başarısız: %s", exc)
    return None


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        logger.error(
            "TELEGRAM_BOT_TOKEN ayarlı değil. Lütfen api_keys.env dosyasına ekleyin."
        )
        sys.exit(1)

    from telegram import Update
    from telegram.ext import (
        ApplicationBuilder,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if chat is None:
            return
        if not _is_chat_allowed(chat.id):
            await update.message.reply_text("Bu botu kullanma yetkiniz bulunmuyor.")
            return
        msg = (
            f"Merhaba, ben Mamed (Türkçe Kişisel Yapay Zeka Asistanın).\n\n"
            f"Sohbet Kimliğin (Chat ID): `{chat.id}`\n"
            f"(Güvenlik için bu kimliği api_keys.env içindeki TELEGRAM_ALLOWED_CHAT_IDS alanına ekleyebilirsin.)\n\n"
            "Komutlar:\n"
            "/mod hizli — Hızlı Mod\n"
            "/mod dengeli — Dengeli Mod\n"
            "/mod derin — Derin / Gelişmiş Mod\n"
            "/mod oto — Otomatik mod yönlendirmesini aç/kapat\n"
            "/durum — Aktif sunucu (PC veya Bulut) ve mod bilgisi\n"
            "/maildurum — Okul e-posta dinleyici durumu ve son hoca mailleri\n"
            "/mailkontrol — Okul e-postasını hemen şimdi kontrol et\n"
            "/ses — Sesli Türkçe yanıtları aç/kapat\n"
            "Doğrudan Türkçe yazabilir veya ses kaydı gönderebilirsin."
        )
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def cmd_durum(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if chat is None or not _is_chat_allowed(chat.id):
            return
        base_url, kind = select_backend_url()
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(f"{base_url}/v1/mamed/mode", headers=_auth_headers(kind))
                r.raise_for_status()
                info = r.json()
            durum_metni = (
                f"Sunucu: {'Yerel PC' if kind == 'yerel' else 'Uzak Bulut (Render)'} ({base_url})\n"
                f"Aktif Mod: {_mode_label(info.get('mode', 'standard'))}\n"
                f"Otomatik Yönlendirme: {'Açık' if info.get('auto_route') else 'Kapalı'}\n"
                f"Model: {info.get('effective_model', '-')}"
            )
        except Exception as exc:
            durum_metni = f"Sunucuya ulaşılamadı ({base_url}): {exc}"
        await update.message.reply_text(durum_metni)

    async def cmd_mod(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if chat is None or not _is_chat_allowed(chat.id):
            return
        arg = (context.args[0].lower() if context.args else "").strip()
        base_url, kind = select_backend_url()
        mapping = {"hizli": "fast", "hızlı": "fast", "basit": "fast", "dengeli": "standard", "standart": "standard", "derin": "deep", "gelismis": "deep", "gelişmiş": "deep"}
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                if arg == "oto":
                    cur = (await client.get(f"{base_url}/v1/mamed/mode", headers=_auth_headers(kind))).json()
                    new_auto = not bool(cur.get("auto_route"))
                    res = await client.put(
                        f"{base_url}/v1/mamed/mode",
                        headers=_auth_headers(kind),
                        json={"auto_route": new_auto},
                    )
                    res.raise_for_status()
                    await update.message.reply_text(
                        f"Otomatik karmaşıklık yönlendirmesi {'açıldı' if new_auto else 'kapatıldı'}."
                    )
                    return
                target = mapping.get(arg)
                if not target:
                    await update.message.reply_text("Kullanım: /mod hizli | dengeli | derin | oto")
                    return
                res = await client.put(
                    f"{base_url}/v1/mamed/mode",
                    headers=_auth_headers(kind),
                    json={"mode": target},
                )
                res.raise_for_status()
                data = res.json()
                await update.message.reply_text(
                    f"{_mode_label(target)} aktif edildi (Model: {data.get('effective_model')})."
                )
        except Exception as exc:
            await update.message.reply_text(f"Mod güncellenemedi: {exc}")

    async def cmd_ses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if chat is None or not _is_chat_allowed(chat.id):
            return
        cur = _VOICE_REPLIES_BY_CHAT.get(chat.id, False)
        _VOICE_REPLIES_BY_CHAT[chat.id] = not cur
        await update.message.reply_text(
            f"Sesli Türkçe yanıtlar {'açıldı' if not cur else 'kapatıldı'}."
        )

    async def cmd_maildurum(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if chat is None or not _is_chat_allowed(chat.id):
            return
        base_url, kind = select_backend_url()
        info = None
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(f"{base_url}/v1/mamed/school-mail/status", headers=_auth_headers(kind))
                if r.status_code == 200:
                    info = r.json()
        except Exception:
            info = None
        if info is None:
            from openjarvis.server.school_mail_watcher import get_school_mail_status

            info = get_school_mail_status()
        await update.message.reply_text(format_school_mail_status_text(info))

    async def cmd_mailkontrol(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if chat is None or not _is_chat_allowed(chat.id):
            return
        import asyncio

        base_url, kind = select_backend_url()
        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                r = await client.post(f"{base_url}/v1/mamed/school-mail/check-now", headers=_auth_headers(kind))
                if r.status_code == 200:
                    await update.message.reply_text(format_school_mail_check_text(r.json()))
                    return
                if r.status_code in (400, 502):
                    detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else r.text
                    await update.message.reply_text(f"Okul mail kontrolü yapılamadı: {detail}")
                    return
        except Exception:
            pass

        try:
            from openjarvis.server.school_mail_watcher import check_mailbox_once

            result = await asyncio.to_thread(check_mailbox_once)
            await update.message.reply_text(format_school_mail_check_text(result))
        except Exception as exc:
            await update.message.reply_text(f"Okul mail kontrolü başarısız: {exc}")

    async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if chat is None or not _is_chat_allowed(chat.id) or not update.message or not update.message.text:
            return
        try:
            reply, kind, _ = await _ask_mamed(update.message.text)
            suffix = "\n\n[Bulut Yedek]" if kind == "bulut" else ""
            await update.message.reply_text(f"{reply}{suffix}"[:4000])
            if _VOICE_REPLIES_BY_CHAT.get(chat.id, False):
                audio = await _synthesize_turkish(reply)
                if audio:
                    await update.message.reply_audio(audio=io.BytesIO(audio), filename="mamed.mp3")
        except Exception as exc:
            await update.message.reply_text(f"Hata oluştu: {exc}")

    async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if chat is None or not _is_chat_allowed(chat.id) or not update.message or not update.message.voice:
            return
        base_url, kind = select_backend_url()
        try:
            tg_file = await context.bot.get_file(update.message.voice.file_id)
            buf = io.BytesIO()
            await tg_file.download_to_memory(buf)
            buf.seek(0)
            async with httpx.AsyncClient(timeout=45.0) as client:
                tr = await client.post(
                    f"{base_url}/v1/speech/transcribe",
                    headers=_auth_headers(kind),
                    files={"file": ("voice.ogg", buf.getvalue(), "audio/ogg")},
                    data={"language": "tr", "hotwords": "Mamed, Mamed uyan"},
                )
                if tr.status_code != 200:
                    await update.message.reply_text("Sesli mesaj şu an çözümlenemedi.")
                    return
                text = (tr.json().get("text") or "").strip()
            if not text:
                await update.message.reply_text("Ses kaydı anlaşılamadı.")
                return
            reply, _, _ = await _ask_mamed(text)
            await update.message.reply_text(f"Söylenen: {text}\n\nMamed: {reply}"[:4000])
            audio = await _synthesize_turkish(reply)
            if audio:
                await update.message.reply_audio(audio=io.BytesIO(audio), filename="mamed.mp3")
        except Exception as exc:
            await update.message.reply_text(f"Sesli komut işlenemedi: {exc}")

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("yardim", cmd_start))
    app.add_handler(CommandHandler("durum", cmd_durum))
    app.add_handler(CommandHandler("mod", cmd_mod))
    app.add_handler(CommandHandler("ses", cmd_ses))
    app.add_handler(CommandHandler("maildurum", cmd_maildurum))
    app.add_handler(CommandHandler("mailkontrol", cmd_mailkontrol))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    logger.info("Mamed Telegram botu dinlemeye başladı.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

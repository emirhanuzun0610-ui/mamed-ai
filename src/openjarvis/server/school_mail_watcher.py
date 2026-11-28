"""Gazi Universitesi / IMAP IDLE (RFC 2177) okul e-posta anlik dinleyicisi.

Mamed sunucusu veya HUD kapali oldugunda dahi arka planda calisir; gelen
e-postalari ``BODY.PEEK[]`` ile okundu isaretlemeden ceker,
``school_mail_analyzer.analyze_school_mail`` ile siniflandirir ve yalnizca
hoca maillerini Telegram Bot API uzerinden aninda bildirir.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
import email
from email import policy
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
import html
import imaplib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import select
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import httpx
from dotenv import dotenv_values, load_dotenv

from openjarvis.core.paths import get_config_dir
from openjarvis.intelligence.school_mail_analyzer import (
    SchoolMailDecision,
    analyze_school_mail,
    record_school_mail_decision,
)

logger = logging.getLogger("mamed.school_mail_watcher")

MUTEX_NAME = "Local\\MamedSchoolMailWatcher_SingleInstance"
ERROR_ALREADY_EXISTS = 183
MAX_SEEN_IDS = 2000
MAX_RECENT_PROFESSOR_MAILS = 30
_SCRIPT_REPO_ROOT = Path(__file__).resolve().parents[3]

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_AUTH_ERROR_MARKERS = (
    "authenticationfailed",
    "invalid credentials",
    "login failed",
    "auth failed",
    "bad username or password",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def get_api_keys_env_path() -> Path:
    custom = os.environ.get("MAMED_API_KEYS_ENV", "").strip()
    if custom:
        return Path(custom).expanduser()
    return _SCRIPT_REPO_ROOT / "api_keys.env"


def get_school_mail_state_path() -> Path:
    return get_config_dir() / "school_mail_state.json"


def get_school_mail_log_path() -> Path:
    return get_config_dir() / "logs" / "school_mail_watcher.log"


def mask_secrets(text: str, extra_secrets: Sequence[str] = ()) -> str:
    """Log ve hata ciktilarindaki sifre, bot token ve API anahtarlarini maskeler."""
    masked = str(text or "")
    for secret in extra_secrets:
        if secret and len(secret) >= 3:
            masked = masked.replace(secret, "***")
    masked = re.sub(r"(/bot)[^/\s\"']+", r"\1***", masked)
    masked = re.sub(r"(#token=)[^\s\"'&]+", r"\1***", masked)
    masked = re.sub(
        r"((?:SCHOOL_MAIL_PASSWORD|TELEGRAM_BOT_TOKEN|GEMINI_API_KEY|GOOGLE_API_KEY|OPENJARVIS_API_KEY|MAMED_API_TOKEN)\s*=\s*)[^\s\"']+",
        r"\1***",
        masked,
        flags=re.IGNORECASE,
    )
    masked = re.sub(r"(Bearer\s+)[A-Za-z0-9._\-]+", r"\1***", masked, flags=re.IGNORECASE)
    return masked


def setup_watcher_logger(log_path: Optional[Path] = None) -> logging.Logger:
    target = log_path or get_school_mail_log_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    watcher_logger = logging.getLogger("mamed.school_mail_watcher")
    watcher_logger.setLevel(logging.INFO)
    watcher_logger.propagate = False
    watcher_logger.handlers.clear()
    handler = RotatingFileHandler(
        target,
        maxBytes=1_048_576,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    watcher_logger.addHandler(handler)
    return watcher_logger


def _parse_int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, val))


def _parse_chat_ids(raw: str) -> List[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


@dataclass
class SchoolMailConfig:
    imap_host: str = "imap.gazi.edu.tr"
    imap_port: int = 993
    username: str = ""
    password: str = ""
    folder: str = "INBOX"
    idle_refresh_seconds: int = 720
    poll_interval_seconds: int = 15
    telegram_bot_token: str = ""
    telegram_chat_ids: List[str] = field(default_factory=list)

    @property
    def imap_configured(self) -> bool:
        return bool(self.imap_host and self.username and self.password)

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_ids)

    @classmethod
    def from_env(cls, env_path: Optional[Path] = None) -> "SchoolMailConfig":
        target_env = env_path or get_api_keys_env_path()
        if target_env.is_file():
            load_dotenv(target_env, override=False)
        return cls(
            imap_host=os.environ.get("SCHOOL_MAIL_IMAP_HOST", "imap.gazi.edu.tr").strip() or "imap.gazi.edu.tr",
            imap_port=_parse_int_env("SCHOOL_MAIL_IMAP_PORT", 993, minimum=1, maximum=65535),
            username=os.environ.get("SCHOOL_MAIL_USERNAME", "").strip(),
            password=os.environ.get("SCHOOL_MAIL_PASSWORD", "").strip(),
            folder=os.environ.get("SCHOOL_MAIL_FOLDER", "INBOX").strip() or "INBOX",
            idle_refresh_seconds=_parse_int_env("SCHOOL_MAIL_IDLE_REFRESH_SECONDS", 720, minimum=600, maximum=840),
            poll_interval_seconds=_parse_int_env("SCHOOL_MAIL_POLL_INTERVAL_SECONDS", 15, minimum=5, maximum=300),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_ids=_parse_chat_ids(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")),
        )

    def refresh_dynamic_env(self, env_path: Optional[Path] = None) -> None:
        """api_keys.env sonradan doldurulursa yeniden baslatmadan guncel degerleri alir."""
        target_env = env_path or get_api_keys_env_path()
        if not target_env.is_file():
            return
        vals = dotenv_values(target_env)
        for key in (
            "SCHOOL_MAIL_IMAP_HOST",
            "SCHOOL_MAIL_IMAP_PORT",
            "SCHOOL_MAIL_USERNAME",
            "SCHOOL_MAIL_PASSWORD",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_ALLOWED_CHAT_IDS",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
        ):
            v = (vals.get(key) or "").strip()
            if v:
                os.environ[key] = v
        if (vals.get("SCHOOL_MAIL_IMAP_HOST") or "").strip():
            self.imap_host = (vals["SCHOOL_MAIL_IMAP_HOST"] or "").strip()
        if (vals.get("SCHOOL_MAIL_IMAP_PORT") or "").strip():
            self.imap_port = _parse_int_env("SCHOOL_MAIL_IMAP_PORT", self.imap_port, minimum=1, maximum=65535)
        if (vals.get("SCHOOL_MAIL_USERNAME") or "").strip():
            self.username = (vals["SCHOOL_MAIL_USERNAME"] or "").strip()
        if (vals.get("SCHOOL_MAIL_PASSWORD") or "").strip():
            self.password = (vals["SCHOOL_MAIL_PASSWORD"] or "").strip()
        if (vals.get("TELEGRAM_BOT_TOKEN") or "").strip():
            self.telegram_bot_token = (vals["TELEGRAM_BOT_TOKEN"] or "").strip()
        if (vals.get("TELEGRAM_ALLOWED_CHAT_IDS") or "").strip():
            self.telegram_chat_ids = _parse_chat_ids(vals["TELEGRAM_ALLOWED_CHAT_IDS"] or "")

    def masked_secrets(self) -> List[str]:
        return [s for s in (self.password, self.telegram_bot_token) if s]


def _default_state() -> Dict[str, Any]:
    return {
        "status": "bagli_degil",
        "connection_type": "bagli_degil",
        "pid": None,
        "imap_host": "",
        "imap_port": 993,
        "username": "",
        "folder": "INBOX",
        "initialized": False,
        "uidvalidity": "",
        "last_uid": 0,
        "seen_uids": [],
        "seen_message_ids": [],
        "last_connected_at": None,
        "last_check_at": None,
        "last_error": None,
        "updated_at": None,
        "total_professor_mails": 0,
        "filtered_announcements_count": 0,
        "filtered_system_count": 0,
        "recent_professor_mails": [],
    }


class SchoolMailStateStore:
    """``school_mail_state.json`` kalici durum ve tekrar-onleme deposu."""

    def __init__(self, state_path: Optional[Path] = None) -> None:
        self._path = Path(state_path).expanduser() if state_path is not None else get_school_mail_state_path()
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Dict[str, Any]:
        with self._lock:
            state = _default_state()
            if self._path.is_file():
                try:
                    loaded = json.loads(self._path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        state.update(loaded)
                except Exception:
                    pass
            if not isinstance(state.get("seen_uids"), list):
                state["seen_uids"] = []
            if not isinstance(state.get("seen_message_ids"), list):
                state["seen_message_ids"] = []
            if not isinstance(state.get("recent_professor_mails"), list):
                state["recent_professor_mails"] = []
            return state

    def save(self, state: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            payload = dict(state)
            payload["seen_uids"] = list(payload.get("seen_uids") or [])[-MAX_SEEN_IDS:]
            payload["seen_message_ids"] = list(payload.get("seen_message_ids") or [])[-MAX_SEEN_IDS:]
            payload["recent_professor_mails"] = list(payload.get("recent_professor_mails") or [])[:MAX_RECENT_PROFESSOR_MAILS]
            payload["updated_at"] = _now_iso()
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp_path.replace(self._path)
            return payload

    def update(self, **fields: Any) -> Dict[str, Any]:
        with self._lock:
            state = self.load()
            state.update(fields)
            return self.save(state)


def _decode_mime_header(value: Any) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(str(value)))).strip()
    except Exception:
        return str(value).strip()


def _html_to_text(html_body: str) -> str:
    cleaned = _SCRIPT_STYLE_RE.sub(" ", html_body or "")
    cleaned = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", cleaned)
    cleaned = _HTML_TAG_RE.sub(" ", cleaned)
    cleaned = html.unescape(cleaned)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in cleaned.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


def _extract_part_text(part: Any) -> str:
    try:
        content = part.get_content()
        if isinstance(content, bytes):
            charset = part.get_content_charset() or "utf-8"
            return content.decode(charset, errors="replace")
        return str(content or "")
    except Exception:
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            charset = part.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
        return str(part.get_payload() or "")


def parse_raw_email(raw_bytes: bytes) -> Dict[str, Any]:
    """Ham RFC 2822 e-postayi ``analyze_school_mail`` girdisine donusturur."""
    msg = email.message_from_bytes(raw_bytes, policy=policy.default)
    raw_from = _decode_mime_header(msg.get("From", ""))
    sender_name, sender_email = parseaddr(raw_from)
    sender_name = _decode_mime_header(sender_name) or sender_email
    sender_email = (sender_email or "").strip().lower()
    subject = _decode_mime_header(msg.get("Subject", ""))
    message_id = str(msg.get("Message-ID") or "").strip().strip("<>").lower()

    raw_date = str(msg.get("Date") or "").strip()
    received_at = _now_iso()
    if raw_date:
        try:
            dt = parsedate_to_datetime(raw_date)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            received_at = dt.astimezone().isoformat(timespec="seconds")
        except Exception:
            pass

    headers: Dict[str, str] = {}
    for hkey in (
        "Reply-To",
        "In-Reply-To",
        "References",
        "List-Unsubscribe",
        "List-Id",
        "Precedence",
        "Auto-Submitted",
        "Subject",
    ):
        val = msg.get(hkey)
        if val:
            headers[hkey.lower()] = _decode_mime_header(val)

    plain_parts: List[str] = []
    html_parts: List[str] = []
    attachments: List[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            disp = (part.get_content_disposition() or "").lower()
            filename = _decode_mime_header(part.get_filename() or "")
            if filename:
                attachments.append(filename)
                if disp == "attachment":
                    continue
            ctype = (part.get_content_type() or "").lower()
            if ctype == "text/plain" and disp != "attachment":
                plain_parts.append(_extract_part_text(part))
            elif ctype == "text/html" and disp != "attachment":
                html_parts.append(_extract_part_text(part))
    else:
        ctype = (msg.get_content_type() or "").lower()
        if ctype == "text/html":
            html_parts.append(_extract_part_text(msg))
        else:
            plain_parts.append(_extract_part_text(msg))

    body = "\n".join(p.strip() for p in plain_parts if p and p.strip()).strip()
    if not body and html_parts:
        body = _html_to_text("\n".join(html_parts))

    return {
        "sender_name": sender_name,
        "sender_email": sender_email,
        "subject": subject,
        "body": body,
        "headers": headers,
        "attachments": list(dict.fromkeys(a for a in attachments if a)),
        "message_id": message_id,
        "received_at": received_at,
    }


def send_telegram_notification(
    token: str,
    chat_ids: Sequence[str],
    text: str,
    *,
    timeout: float = 15.0,
) -> Tuple[bool, Optional[str]]:
    """Telegram Bot API uzerinden Mamed hoca mail bildirimini gonderir."""
    if not token or not chat_ids:
        return False, "TELEGRAM_BOT_TOKEN veya TELEGRAM_ALLOWED_CHAT_IDS eksik."
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    delivered = 0
    last_err: Optional[str] = None
    with httpx.Client(timeout=timeout) as client:
        for cid in chat_ids:
            try:
                resp = client.post(
                    url,
                    json={"chat_id": cid, "text": text[:4096]},
                )
                if resp.status_code == 200:
                    delivered += 1
                else:
                    last_err = f"Telegram HTTP {resp.status_code}: {resp.text[:200]}"
            except Exception as exc:
                last_err = mask_secrets(str(exc), [token])
    return delivered > 0, (None if delivered > 0 else last_err)


def _parse_uid_list(search_data: Sequence[Any]) -> List[int]:
    if not search_data:
        return []
    raw = search_data[0]
    if isinstance(raw, bytes):
        raw_str = raw.decode("ascii", errors="ignore")
    else:
        raw_str = str(raw or "")
    uids: List[int] = []
    for tok in raw_str.split():
        if tok.isdigit():
            uids.append(int(tok))
    return sorted(set(uids))


def _extract_fetch_raw_bytes(fetch_data: Sequence[Any]) -> Optional[bytes]:
    if not fetch_data:
        return None
    for item in fetch_data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])
    return None


def _select_mailbox(client: Any, folder: str) -> str:
    typ, _ = client.select(folder, readonly=True)
    if typ != "OK":
        typ, _ = client.select(folder, readonly=False)
        if typ != "OK":
            raise RuntimeError(f"IMAP klasoru secilemedi: {folder}")
    uv_resp = client.response("UIDVALIDITY")
    if uv_resp and len(uv_resp) >= 2 and uv_resp[1]:
        raw_uv = uv_resp[1][0]
        return raw_uv.decode("ascii", errors="ignore") if isinstance(raw_uv, bytes) else str(raw_uv)
    return ""


def _server_supports_idle(client: Any) -> bool:
    caps = getattr(client, "capabilities", ()) or ()
    cap_set = {c.decode("ascii", errors="ignore").upper() if isinstance(c, bytes) else str(c).upper() for c in caps}
    if "IDLE" in cap_set:
        return True
    try:
        typ, data = client.capability()
        if typ == "OK" and data:
            joined = " ".join(
                d.decode("ascii", errors="ignore") if isinstance(d, bytes) else str(d)
                for d in data
            ).upper()
            return "IDLE" in joined.split()
    except Exception:
        pass
    return False


def _is_auth_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _AUTH_ERROR_MARKERS)


class ImapIdleWatcher:
    """IMAP IDLE (RFC 2177) + 15 sn yedek yoklama tabanli okul mail dinleyicisi."""

    def __init__(
        self,
        config: Optional[SchoolMailConfig] = None,
        state_store: Optional[SchoolMailStateStore] = None,
        *,
        imap_factory: Optional[Callable[[str, int], Any]] = None,
        telegram_sender: Optional[Callable[[str, Sequence[str], str], Tuple[bool, Optional[str]]]] = None,
        analyzer: Optional[Callable[..., SchoolMailDecision]] = None,
        store_recorder: Optional[Callable[..., Dict[str, Any]]] = None,
        watcher_logger: Optional[logging.Logger] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        self.config = config or SchoolMailConfig.from_env()
        self.state_store = state_store or SchoolMailStateStore()
        self._imap_factory = imap_factory or (lambda host, port: imaplib.IMAP4_SSL(host, port, timeout=30))
        self._telegram_sender = telegram_sender or send_telegram_notification
        self._analyzer = analyzer or analyze_school_mail
        self._store_recorder = store_recorder or record_school_mail_decision
        self._logger = watcher_logger or setup_watcher_logger()
        self._stop_event = stop_event or threading.Event()

    def _safe_log(self, level: int, msg: str, *args: Any) -> None:
        formatted = msg % args if args else msg
        self._logger.log(level, mask_secrets(formatted, self.config.masked_secrets()))

    def _process_single_uid(
        self,
        client: Any,
        uid: int,
        uidvalidity: str,
        state: Dict[str, Any],
    ) -> Dict[str, Any]:
        uid_key = f"{uidvalidity}:{uid}" if uidvalidity else str(uid)
        seen_uids: List[str] = list(state.get("seen_uids") or [])
        seen_mids: List[str] = list(state.get("seen_message_ids") or [])

        if uid_key in seen_uids:
            state["last_uid"] = max(int(state.get("last_uid") or 0), uid)
            return {"status": "duplicate_uid"}

        typ, fetch_data = client.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if typ != "OK":
            raise RuntimeError(f"UID {uid} cekilemedi: {typ}")

        raw_bytes = _extract_fetch_raw_bytes(fetch_data)
        if not raw_bytes:
            state["last_uid"] = max(int(state.get("last_uid") or 0), uid)
            return {"status": "empty"}

        parsed = parse_raw_email(raw_bytes)
        msg_id = parsed["message_id"]

        seen_uids.append(uid_key)
        state["seen_uids"] = seen_uids
        state["last_uid"] = max(int(state.get("last_uid") or 0), uid)

        if msg_id and msg_id in seen_mids:
            self._safe_log(logging.INFO, "Tekrarlanan Message-ID atlandi (UID=%s).", uid)
            return {"status": "duplicate_message_id"}

        if msg_id:
            seen_mids.append(msg_id)
            state["seen_message_ids"] = seen_mids

        decision = self._analyzer(
            sender_name=parsed["sender_name"],
            sender_email=parsed["sender_email"],
            subject=parsed["subject"],
            body=parsed["body"],
            headers=parsed["headers"],
            attachments=parsed["attachments"],
        )

        self._store_recorder(
            decision,
            message_id=msg_id or uid_key,
            sender_name=parsed["sender_name"],
            sender_email=parsed["sender_email"],
            subject=parsed["subject"],
            received_at=parsed["received_at"],
            attachments=parsed["attachments"],
        )

        if decision.is_professor:
            self.config.refresh_dynamic_env()
            notif_text = decision.formatted_notification or f"{decision.professor_name} hoca mail attı: {decision.summary}"
            notified = False
            if self.config.telegram_configured:
                notified, tg_err = self._telegram_sender(
                    self.config.telegram_bot_token,
                    self.config.telegram_chat_ids,
                    notif_text,
                )
                if not notified and tg_err:
                    self._safe_log(logging.WARNING, "Telegram bildirimi gonderilemedi: %s", tg_err)
            else:
                self._safe_log(
                    logging.WARNING,
                    "Hoca maili yakalandi ancak TELEGRAM_BOT_TOKEN veya TELEGRAM_ALLOWED_CHAT_IDS eksik.",
                )

            state["total_professor_mails"] = int(state.get("total_professor_mails") or 0) + 1
            recent = list(state.get("recent_professor_mails") or [])
            recent.insert(
                0,
                {
                    "uid": uid,
                    "message_id": msg_id or uid_key,
                    "professor_name": decision.professor_name,
                    "sender_name": parsed["sender_name"],
                    "sender_email": parsed["sender_email"],
                    "subject": parsed["subject"],
                    "summary": decision.summary,
                    "formatted_notification": notif_text,
                    "received_at": parsed["received_at"],
                    "attachments": parsed["attachments"],
                    "notified": notified,
                    "notified_at": _now_iso() if notified else None,
                },
            )
            state["recent_professor_mails"] = recent[:MAX_RECENT_PROFESSOR_MAILS]
            self._safe_log(
                logging.INFO,
                "Hoca maili islendi (UID=%s, hoca=%s, telegram_bildirildi=%s).",
                uid,
                decision.professor_name,
                notified,
            )
            return {"status": "professor", "notified": notified, "professor_name": decision.professor_name}

        state["filtered_announcements_count"] = int(state.get("filtered_announcements_count") or 0) + 1
        if decision.category == "sistem_otomatik":
            state["filtered_system_count"] = int(state.get("filtered_system_count") or 0) + 1
        self._safe_log(
            logging.INFO,
            "Mail filtrelendi (UID=%s, kategori=%s).",
            uid,
            decision.category,
        )
        return {"status": "filtered", "category": decision.category}

    def sync_mailbox(self, client: Any, uidvalidity: str) -> Dict[str, Any]:
        """Kutuyu kontrol eder; ilk kurulumda taban alir, yeni mailleri isler."""
        state = self.state_store.load()
        prev_uv = str(state.get("uidvalidity") or "")
        if prev_uv and uidvalidity and prev_uv != uidvalidity:
            self._safe_log(logging.INFO, "IMAP UIDVALIDITY degisti (%s -> %s); taban yenileniyor.", prev_uv, uidvalidity)
            state["initialized"] = False
            state["last_uid"] = 0

        state["uidvalidity"] = uidvalidity
        state["imap_host"] = self.config.imap_host
        state["imap_port"] = self.config.imap_port
        state["username"] = self.config.username
        state["folder"] = self.config.folder

        if not state.get("initialized"):
            typ, search_data = client.uid("SEARCH", None, "ALL")
            if typ != "OK":
                raise RuntimeError(f"IMAP taban aramasi basarisiz: {typ}")
            all_uids = _parse_uid_list(search_data)
            max_uid = max(all_uids) if all_uids else 0
            state["initialized"] = True
            state["last_uid"] = max_uid
            if max_uid > 0:
                uv_prefix = f"{uidvalidity}:" if uidvalidity else ""
                seen_uids = list(state.get("seen_uids") or [])
                seen_uids.append(f"{uv_prefix}{max_uid}")
                state["seen_uids"] = seen_uids
            state["last_check_at"] = _now_iso()
            state["last_error"] = None
            self.state_store.save(state)
            self._safe_log(
                logging.INFO,
                "Ilk kurulum tabani kaydedildi (UIDVALIDITY=%s, last_uid=%s, toplam_mevcut=%s).",
                uidvalidity,
                max_uid,
                len(all_uids),
            )
            return {
                "baseline_initialized": True,
                "checked_count": 0,
                "professor_count": 0,
                "filtered_count": 0,
                "last_uid": max_uid,
            }

        last_uid = int(state.get("last_uid") or 0)
        criteria = f"UID {last_uid + 1}:*" if last_uid > 0 else "ALL"
        typ, search_data = client.uid("SEARCH", None, criteria)
        if typ != "OK":
            raise RuntimeError(f"IMAP yeni mail aramasi basarisiz: {typ}")
        new_uids = [u for u in _parse_uid_list(search_data) if u > last_uid]

        prof_count = 0
        filtered_count = 0
        for uid in new_uids:
            outcome = self._process_single_uid(client, uid, uidvalidity, state)
            if outcome.get("status") == "professor":
                prof_count += 1
            elif outcome.get("status") == "filtered":
                filtered_count += 1

        state["last_check_at"] = _now_iso()
        state["last_error"] = None
        self.state_store.save(state)
        return {
            "baseline_initialized": False,
            "checked_count": len(new_uids),
            "professor_count": prof_count,
            "filtered_count": filtered_count,
            "last_uid": int(state.get("last_uid") or 0),
        }

    def _wait_idle_cycle(self, client: Any) -> bool:
        """Tek bir RFC 2177 IDLE dongusu calistirir. Yeni mail sinyali gelirse True doner."""
        tag = client._new_tag()
        tag_bytes = tag if isinstance(tag, bytes) else str(tag).encode("ascii")
        client.send(tag_bytes + b" IDLE\r\n")

        cont = client.readline()
        if not cont or not cont.lstrip().startswith(b"+"):
            raise RuntimeError(f"IMAP IDLE baslatilamadi: {cont!r}")

        deadline = time.monotonic() + self.config.idle_refresh_seconds
        got_new_mail = False
        sock = getattr(client, "sock", None) or (client.socket() if callable(getattr(client, "socket", None)) else None)

        while not self._stop_event.is_set() and time.monotonic() < deadline:
            has_data = False
            if sock is not None and callable(getattr(sock, "has_pending_data", None)):
                has_data = bool(sock.has_pending_data(timeout=0.5))
            elif sock is not None:
                pending_fn = getattr(sock, "pending", None)
                if callable(pending_fn) and pending_fn() > 0:
                    has_data = True
                elif hasattr(sock, "fileno"):
                    readable, _, _ = select.select([sock], [], [], 2.0)
                    has_data = bool(readable)
                else:
                    time.sleep(0.5)
                    has_data = True
            else:
                time.sleep(1.0)
                continue

            if not has_data:
                continue

            line = client.readline()
            if not line:
                raise ConnectionError("IMAP IDLE baglantisi karsidan kapatildi.")
            upper = line.upper()
            if b"EXISTS" in upper or b"RECENT" in upper:
                got_new_mail = True
                break
            if b"BYE" in upper:
                raise ConnectionError("IMAP sunucusu IDLE sirasinda BYE gonderdi.")

        client.send(b"DONE\r\n")
        for _ in range(50):
            done_line = client.readline()
            if not done_line:
                raise ConnectionError("IMAP IDLE DONE yaniti alinamadi.")
            if done_line.startswith(tag_bytes + b" "):
                break
        return got_new_mail

    def run_once(self) -> Dict[str, Any]:
        """Tek seferlik baglanti acip yeni mailleri kontrol eder."""
        self.config.refresh_dynamic_env()
        if not self.config.imap_configured:
            self.state_store.update(
                status="yapilandirma_bekleniyor",
                connection_type="bagli_degil",
                imap_host=self.config.imap_host,
                imap_port=self.config.imap_port,
                username=self.config.username,
                folder=self.config.folder,
                last_error="Okul e-posta kullanici adi veya sifresi ayarli degil.",
            )
            raise ValueError("SCHOOL_MAIL_USERNAME veya SCHOOL_MAIL_PASSWORD ayarli değil.")

        client = self._imap_factory(self.config.imap_host, self.config.imap_port)
        try:
            typ, _ = client.login(self.config.username, self.config.password)
            if typ != "OK":
                raise RuntimeError(f"IMAP giris basarisiz: {typ}")
            uidvalidity = _select_mailbox(client, self.config.folder)
            supports_idle = _server_supports_idle(client)
            conn_type = "imap_idle" if supports_idle else "imap_poll_15s"
            self.state_store.update(
                connection_type=conn_type,
                last_connected_at=_now_iso(),
            )
            summary = self.sync_mailbox(client, uidvalidity)
            summary["connection_type"] = conn_type
            return summary
        finally:
            try:
                client.logout()
            except Exception:
                pass

    def run_forever(self) -> None:
        """Suresiz olarak IMAP IDLE (veya yedek 15 sn yoklama) dongusunu calistirir."""
        backoff = 2
        self.state_store.update(
            status="calisiyor",
            pid=os.getpid(),
            imap_host=self.config.imap_host,
            imap_port=self.config.imap_port,
            username=self.config.username,
            folder=self.config.folder,
        )
        self._safe_log(
            logging.INFO,
            "Okul mail dinleyicisi baslatildi (PID=%s, host=%s:%s, user=%s).",
            os.getpid(),
            self.config.imap_host,
            self.config.imap_port,
            self.config.username,
        )

        while not self._stop_event.is_set():
            self.config.refresh_dynamic_env()
            if not self.config.imap_configured:
                self.state_store.update(
                    status="yapilandirma_bekleniyor",
                    connection_type="bagli_degil",
                    pid=os.getpid(),
                    last_error="SCHOOL_MAIL_PASSWORD veya SCHOOL_MAIL_USERNAME bos.",
                )
                self._stop_event.wait(15.0)
                continue

            client = None
            try:
                client = self._imap_factory(self.config.imap_host, self.config.imap_port)
                typ, _ = client.login(self.config.username, self.config.password)
                if typ != "OK":
                    raise RuntimeError(f"IMAP giris basarisiz: {typ}")
                uidvalidity = _select_mailbox(client, self.config.folder)
                supports_idle = _server_supports_idle(client)
                conn_type = "imap_idle" if supports_idle else "imap_poll_15s"
                backoff = 2

                self.state_store.update(
                    status="dinleniyor",
                    connection_type=conn_type,
                    pid=os.getpid(),
                    last_connected_at=_now_iso(),
                    last_error=None,
                )
                self._safe_log(
                    logging.INFO,
                    "IMAP baglantisi kuruldu (%s, UIDVALIDITY=%s).",
                    conn_type,
                    uidvalidity,
                )

                self.sync_mailbox(client, uidvalidity)

                while not self._stop_event.is_set():
                    if supports_idle:
                        self._wait_idle_cycle(client)
                    else:
                        if self._stop_event.wait(self.config.poll_interval_seconds):
                            break
                        typ_noop, _ = client.noop()
                        if typ_noop != "OK":
                            raise ConnectionError(f"IMAP NOOP yaniti hatali: {typ_noop}")
                    self.sync_mailbox(client, uidvalidity)

            except Exception as exc:
                safe_err = mask_secrets(str(exc), self.config.masked_secrets())
                wait_sec = 300 if _is_auth_error(exc) else backoff
                self.state_store.update(
                    status="yeniden_baglaniyor",
                    connection_type="bagli_degil",
                    pid=os.getpid(),
                    last_error=safe_err,
                )
                self._safe_log(
                    logging.WARNING,
                    "IMAP baglanti/dinleme hatasi (%s sn sonra tekrar denenecek): %s",
                    wait_sec,
                    safe_err,
                )
                if not _is_auth_error(exc):
                    backoff = min(60, backoff * 2)
                self._stop_event.wait(wait_sec)
            finally:
                if client is not None:
                    try:
                        client.logout()
                    except Exception:
                        pass

        self.state_store.update(status="durduruldu", pid=None)
        self._safe_log(logging.INFO, "Okul mail dinleyicisi temiz sekilde durduruldu.")


def _is_pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                exit_code = wintypes.DWORD(0)
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return exit_code.value == 259  # STILL_ACTIVE
                return True
            finally:
                kernel32.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def get_school_mail_status(
    *,
    config: Optional[SchoolMailConfig] = None,
    state_store: Optional[SchoolMailStateStore] = None,
) -> Dict[str, Any]:
    """API ve Telegram /maildurum komutu icin guvenli (gizli bilgisiz) durum ozeti."""
    cfg = config or SchoolMailConfig.from_env()
    store = state_store or SchoolMailStateStore()
    state = store.load()
    pid = state.get("pid")
    watcher_running = _is_pid_alive(pid)
    return {
        "watcher_running": watcher_running,
        "status": state.get("status") if watcher_running else ("yapilandirma_bekleniyor" if not cfg.imap_configured else state.get("status", "durduruldu")),
        "connection_type": state.get("connection_type", "bagli_degil"),
        "pid": pid if watcher_running else None,
        "imap_host": cfg.imap_host,
        "imap_port": cfg.imap_port,
        "username": cfg.username,
        "folder": cfg.folder,
        "configured": cfg.imap_configured,
        "telegram_configured": cfg.telegram_configured,
        "initialized": bool(state.get("initialized")),
        "last_uid": int(state.get("last_uid") or 0),
        "last_connected_at": state.get("last_connected_at"),
        "last_check_at": state.get("last_check_at"),
        "last_error": state.get("last_error"),
        "updated_at": state.get("updated_at"),
        "total_professor_mails": int(state.get("total_professor_mails") or 0),
        "filtered_announcements_count": int(state.get("filtered_announcements_count") or 0),
        "filtered_system_count": int(state.get("filtered_system_count") or 0),
        "recent_professor_mails": list(state.get("recent_professor_mails") or [])[:10],
    }


def check_mailbox_once(
    *,
    config: Optional[SchoolMailConfig] = None,
    state_store: Optional[SchoolMailStateStore] = None,
) -> Dict[str, Any]:
    """Aninda manuel posta kontrolu yapar ve ozet dondurur."""
    watcher = ImapIdleWatcher(config=config, state_store=state_store)
    return watcher.run_once()


def acquire_single_instance_mutex(name: str = MUTEX_NAME) -> Optional[int]:
    """Windows üzerinde tek-ornek mutex alir; zaten calisiyorsa None doner."""
    if os.name != "nt":
        return 1
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, True, name)
    if not handle:
        return None
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return int(handle)


def release_single_instance_mutex(handle: Optional[int]) -> None:
    if os.name != "nt" or not handle:
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.ReleaseMutex(handle)
    kernel32.CloseHandle(handle)


__all__ = [
    "ImapIdleWatcher",
    "SchoolMailConfig",
    "SchoolMailStateStore",
    "acquire_single_instance_mutex",
    "check_mailbox_once",
    "get_api_keys_env_path",
    "get_school_mail_log_path",
    "get_school_mail_state_path",
    "get_school_mail_status",
    "mask_secrets",
    "parse_raw_email",
    "release_single_instance_mutex",
    "send_telegram_notification",
    "setup_watcher_logger",
]

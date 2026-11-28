"""Okul e-postalarını hoca / genel duyuru / sistem otomatik olarak ayıran ve özetleyen modül.

İki katmanlı mimari:
1. Katman: ``school_mail_rules.json`` kuralları (``professor_whitelist`` en yüksek
   öncelikli), ``announcement_blacklist``, e-posta başlıkları (``List-Unsubscribe``,
   ``Precedence``, ``Auto-Submitted``) ve genel duyuru gönderici önekleri.
2. Katman: Ön filtreden geçen mailleri Gemini ile sınıflandırır ve tarih, saat,
   teslim tarihi, derslik, kapsam, aksiyon, ek dosya ve link bilgilerini atlamadan
   kısa ve net Türkçe özet üretir. API anahtarı veya bağlantı yoksa kural/anahtar
   kelime tabanlı yedek Türkçe çözümleyici devreye girer.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Union,
)

from openjarvis.core.paths import get_config_dir

logger = logging.getLogger(__name__)

CategoryType = Literal["hoca", "genel_duyuru", "sistem_otomatik"]

DEFAULT_RULES_FILENAME = "school_mail_rules.json"
DEFAULT_STORE_FILENAME = "school_mail_summaries.json"
DEFAULT_MODEL = "gemini-3.5-flash"

_STORE_LOCK = threading.RLock()

_TR_LOWER_MAP = str.maketrans({"I": "ı", "İ": "i"})
_TR_UPPER_MAP = str.maketrans({"ı": "I", "i": "İ"})
_TR_ASCII_MAP = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")

_GENERIC_ANNOUNCEMENT_PREFIXES = frozenset(
    {
        "noreply",
        "no-reply",
        "no_reply",
        "donotreply",
        "do-not-reply",
        "do_not_reply",
        "duyuru",
        "duyurular",
        "oidb",
        "sks",
        "rektorluk",
        "ogrenciisleri",
        "ogrenci.isleri",
        "ogrenci_isleri",
        "bulten",
        "kutuphane",
        "obs",
        "gaziobs",
        "bidb",
        "bilgi",
        "basin",
        "guzem",
        "uzem",
        "etkinlik",
        "kariyer",
        "haber",
        "haberler",
        "basinyayin",
        "yemekhane",
        "kulup",
        "kulupler",
    }
)

_LMS_RELAY_MARKERS = (
    "guzem",
    "uzem",
    "moodle",
    "canvas",
    "lms",
    "classroom",
    "blackboard",
    "gaziobs",
    "obs",
)

_INSTITUTIONAL_SENDER_WORDS = frozenset(
    {
        "guzem",
        "uzem",
        "lms",
        "moodle",
        "canvas",
        "obs",
        "gaziobs",
        "rektorluk",
        "dekanlik",
        "oidb",
        "sks",
        "bidb",
        "kutuphane",
        "ogrenci",
        "isleri",
        "duyuru",
        "duyurular",
        "bulten",
        "basin",
        "kariyer",
        "bilgi",
        "yemekhane",
        "kulup",
        "kulupler",
        "topluluk",
        "universite",
        "universitesi",
        "merkezi",
        "mudurlugu",
        "baskanligi",
        "sistem",
        "destek",
        "noreply",
        "iletisim",
        "haber",
        "haberler",
        "etkinlik",
        "kampus",
        "gazi",
    }
)

_ACADEMIC_TITLE_RE = re.compile(
    r"\b(?:prof|doc|dr|yrd\s*\.?\s*doc|ogr\s*\.?\s*gor|ars\s*\.?\s*gor|"
    r"ogretim\s+uyesi|ogretim\s+gorevlisi|arastirma\s+gorevlisi|hoca(?:m)?)\b",
    re.IGNORECASE,
)

_LMS_PAREN_SUFFIX_RE = re.compile(
    r"\s*[\(\[]\s*(?:guzem|uzem|moodle|canvas|lms|classroom|blackboard|gaziobs|obs)\s*[\)\]]\s*$",
    re.IGNORECASE,
)
_LMS_TRAIL_SUFFIX_RE = re.compile(
    r"(?:\s+(?:via|uzerinden)\s+(?:guzem|uzem|moodle|canvas|lms|classroom|blackboard|gaziobs|obs)"
    r"|\s+(?:guzem|uzem|moodle|canvas|lms|classroom|blackboard|gaziobs|obs)\s+uzerinden)\s*$",
    re.IGNORECASE,
)

_SYSTEM_AUTO_PREFIXES = frozenset(
    {
        "mailer-daemon",
        "postmaster",
        "bounce",
        "bounces",
    }
)

_ACADEMIC_TITLE_CANONICAL = {
    "prof": "Prof.",
    "prof.": "Prof.",
    "dr": "Dr.",
    "dr.": "Dr.",
    "doc": "Doç.",
    "doc.": "Doç.",
    "doç": "Doç.",
    "doç.": "Doç.",
    "ogr": "Öğr.",
    "ogr.": "Öğr.",
    "öğr": "Öğr.",
    "öğr.": "Öğr.",
    "gor": "Gör.",
    "gor.": "Gör.",
    "gör": "Gör.",
    "gör.": "Gör.",
    "ars": "Arş.",
    "ars.": "Arş.",
    "arş": "Arş.",
    "arş.": "Arş.",
    "uyesi": "Üyesi",
    "üyesi": "Üyesi",
    "arastirma": "Araştırma",
    "araştırma": "Araştırma",
    "gorevlisi": "Görevlisi",
    "görevlisi": "Görevlisi",
    "hocam": "Hocam",
    "hoca": "Hoca",
}

_FALLBACK_PROFESSOR_KEYWORDS = (
    "sinav",
    "vize",
    "final",
    "butunleme",
    "quiz",
    "odev",
    "proje",
    "lab",
    "laboratuvar",
    "telafi",
    "ders iptal",
    "dersimiz",
    "ders saati",
    "derslik",
    "amfi",
    "notlar",
    "yoklama",
    "sunum",
    "teslim",
    "moodle",
    "canvas",
    "classroom",
    "degerli ogrenciler",
    "sevgili ogrenciler",
    "merhaba arkadaslar",
    "hocaniz",
)

_FALLBACK_ANNOUNCEMENT_KEYWORDS = (
    "rektorluk",
    "yemekhane",
    "menu",
    "kulup",
    "senlik",
    "konser",
    "anket",
    "bulten",
    "kariyer gunleri",
    "ogrenci isleri daire baskanligi",
    "saglik kultur ve spor",
    "kutuphane haftasi",
    "abonelikten cik",
    "unsubscribe",
)

_FALLBACK_SYSTEM_KEYWORDS = (
    "delivery status notification",
    "undelivered mail",
    "mail delivery failed",
    "automatic reply",
    "otomatik yanit",
    "dogrulama kodu",
    "tek kullanimlik sifre",
    "otp",
)

_GREETING_LINE_RE = re.compile(
    r"^(merhaba|selam|iyi\s+gunler|iyi\s+calismalar|iyi\s+aksamlar|sayin|degerli|sevgili)\b",
    re.IGNORECASE,
)
_SIGNOFF_LINE_RE = re.compile(
    r"^(saygilarimla|saygilar|iyi\s+calismalar|iyi\s+gunler|basarilar|kolay\s+gelsin|tesekkurler|prof\.|doc\.|dr\.|ogr\.)\b",
    re.IGNORECASE,
)
_HOCA_WORD_RE = re.compile(r"\bhoca(?:m|s[ıi]|n[ıi]z)?\b", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)
_EMAIL_EXTRACT_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")


def _tr_lower(text: str) -> str:
    return text.translate(_TR_LOWER_MAP).lower()


def _tr_capitalize_word(word: str) -> str:
    if not word:
        return ""
    first = word[0].translate(_TR_UPPER_MAP).upper()
    rest = _tr_lower(word[1:])
    return first + rest


def _tr_normalize(text: str) -> str:
    ascii_text = _tr_lower(text).translate(_TR_ASCII_MAP)
    cleaned = "".join(
        ch for ch in unicodedata.normalize("NFKD", ascii_text) if not unicodedata.combining(ch)
    )
    return re.sub(r"\s+", " ", cleaned).strip()


def _normalize_string_list(values: Any) -> List[str]:
    if not values:
        return []
    if isinstance(values, str):
        items = [part.strip() for part in values.split(",")]
    elif isinstance(values, Sequence):
        items = [str(item).strip() for item in values]
    else:
        return []
    result: List[str] = []
    seen: set[str] = set()
    for item in items:
        if not item:
            continue
        key = _tr_lower(item)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


@dataclass
class SchoolMailRules:
    """Okul e-posta sınıflandırma kuralları (``school_mail_rules.json`` şeması)."""

    professor_whitelist: List[str] = field(default_factory=list)
    announcement_blacklist: List[str] = field(default_factory=list)
    course_keywords: List[str] = field(default_factory=list)
    school_domains: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "SchoolMailRules":
        if not isinstance(data, Mapping):
            return cls()
        return cls(
            professor_whitelist=_normalize_string_list(data.get("professor_whitelist")),
            announcement_blacklist=_normalize_string_list(
                data.get("announcement_blacklist")
            ),
            course_keywords=_normalize_string_list(data.get("course_keywords")),
            school_domains=_normalize_string_list(data.get("school_domains")),
        )

    def to_dict(self) -> Dict[str, List[str]]:
        return {
            "professor_whitelist": list(self.professor_whitelist),
            "announcement_blacklist": list(self.announcement_blacklist),
            "course_keywords": list(self.course_keywords),
            "school_domains": list(self.school_domains),
        }


@dataclass
class SchoolMailDecision:
    """Kodlama Chati ve Mamed araçlarının tüketeceği sabit karar kontratı."""

    is_professor: bool
    professor_name: str
    category: CategoryType
    summary: str
    formatted_notification: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def get_school_mail_rules_path() -> Path:
    """Yerel ``school_mail_rules.json`` yolunu döndürür."""
    return get_config_dir() / DEFAULT_RULES_FILENAME


def get_school_mail_store_path() -> Path:
    """Yerel ``school_mail_summaries.json`` yolunu döndürür."""
    return get_config_dir() / DEFAULT_STORE_FILENAME


def load_school_mail_rules(path: Path | str | None = None) -> SchoolMailRules:
    """``school_mail_rules.json`` dosyasını yükler; yoksa veya bozuksa boş kural döner."""
    target = Path(path).expanduser() if path is not None else get_school_mail_rules_path()
    if not target.is_file():
        return SchoolMailRules()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("school_mail_rules.json okunamadı (%s): %s", target, exc)
        return SchoolMailRules()
    if not isinstance(raw, Mapping):
        return SchoolMailRules()
    return SchoolMailRules.from_dict(raw)


def save_school_mail_rules(
    rules: SchoolMailRules,
    path: Path | str | None = None,
) -> Path:
    """Verilen ``SchoolMailRules`` örneğini JSON olarak kaydeder."""
    target = Path(path).expanduser() if path is not None else get_school_mail_rules_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(rules.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def _resolve_rules(
    rules: Optional[Union[SchoolMailRules, Mapping[str, Any], Path, str]],
) -> SchoolMailRules:
    if rules is None:
        return load_school_mail_rules()
    if isinstance(rules, SchoolMailRules):
        return rules
    if isinstance(rules, Mapping):
        return SchoolMailRules.from_dict(rules)
    if isinstance(rules, (Path, str)):
        return load_school_mail_rules(rules)
    return SchoolMailRules()


def _extract_email_address(raw: str) -> str:
    if not raw:
        return ""
    match = _EMAIL_EXTRACT_RE.search(raw)
    return match.group(0).strip().lower() if match else raw.strip().lower()


def _normalize_headers(headers: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    if not headers:
        return {}
    normalized: Dict[str, str] = {}
    for key, value in headers.items():
        if key is None:
            continue
        k = str(key).strip().lower()
        if not k:
            continue
        if isinstance(value, (list, tuple)):
            v = " ".join(str(item).strip() for item in value if item is not None).strip()
        elif value is None:
            v = ""
        else:
            v = str(value).strip()
        normalized[k] = v
    return normalized


def _format_name_token(token: str) -> str:
    norm_key = _tr_normalize(token)
    if norm_key in _ACADEMIC_TITLE_CANONICAL:
        return _ACADEMIC_TITLE_CANONICAL[norm_key]
    if "-" in token:
        return "-".join(_tr_capitalize_word(part) for part in token.split("-") if part)
    return _tr_capitalize_word(token)


def _strip_lms_relay_suffix(name: str) -> str:
    """Gönderici adındaki ``(GUZEM)``, ``via Moodle`` gibi LMS platform eklerini temizler."""
    cleaned = (name or "").strip()
    while True:
        updated = _LMS_PAREN_SUFFIX_RE.sub("", cleaned).strip()
        updated = _LMS_TRAIL_SUFFIX_RE.sub("", updated).strip()
        if updated == cleaned:
            break
        cleaned = updated
    return cleaned


def _has_academic_title(text: str) -> bool:
    norm = _tr_normalize(text or "")
    if not norm:
        return False
    return bool(_ACADEMIC_TITLE_RE.search(norm))


def _looks_like_personal_name(sender_name: str) -> bool:
    """Gönderici adının kurumsal bir birim değil gerçek bir kişi/hoca adı olup olmadığını kontrol eder."""
    stripped = _strip_lms_relay_suffix(re.sub(r"<[^>]+>", "", sender_name or "").strip(" \"'\t\r\n"))
    for sep in (" | ", " / ", " - "):
        if sep in stripped:
            left = stripped.split(sep, 1)[0].strip()
            if left:
                stripped = left
                break
    if not stripped or "@" in stripped:
        return False
    norm = _tr_normalize(stripped)
    tokens = [tok.strip(".,;:()[]{}") for tok in norm.split() if tok.strip(".,;:()[]{}")]
    if not tokens:
        return False
    if any(tok in _INSTITUTIONAL_SENDER_WORDS for tok in tokens):
        return False
    if _has_academic_title(stripped):
        return True
    alpha_tokens = [tok for tok in tokens if any(c.isalpha() for c in tok)]
    return 2 <= len(alpha_tokens) <= 4 and all(len(tok) >= 2 for tok in alpha_tokens)


def _has_course_academic_content(subject: str, body: str, rules: SchoolMailRules) -> bool:
    combined_norm = _tr_normalize(f"{subject} {body}")
    if any(kw and _tr_normalize(kw) in combined_norm for kw in rules.course_keywords):
        return True
    return any(kw in combined_norm for kw in _FALLBACK_PROFESSOR_KEYWORDS)


def _is_lms_professor_relay(
    *,
    sender_name: str,
    sender_email: str,
    subject: str,
    body: str,
    headers: Dict[str, str],
    rules: SchoolMailRules,
) -> bool:
    """GUZEM/LMS üzerinden kendi adıyla ders duyurusu gönderen hocayı tespit eder."""
    clean_email = _extract_email_address(sender_email)
    reply_to = _extract_email_address(headers.get("reply-to", ""))
    combined_sender = _tr_normalize(f"{sender_name} {clean_email} {headers.get('list-id', '')}")
    has_lms_marker = any(marker in combined_sender for marker in _LMS_RELAY_MARKERS)
    if not has_lms_marker:
        return False

    personal_sender = _looks_like_personal_name(sender_name)
    personal_reply_to = bool(
        reply_to
        and reply_to != clean_email
        and reply_to.split("@", 1)[0] not in _GENERIC_ANNOUNCEMENT_PREFIXES
    )
    if not (personal_sender or personal_reply_to):
        return False
    return _has_course_academic_content(subject, body, rules)


def clean_professor_name(sender_name: str, sender_email: str = "") -> str:
    """Gönderici adını tırnak, e-posta adresi, LMS eki ve bölüm eklerinden arındırır."""
    raw = (sender_name or "").strip()
    raw = re.sub(r"<[^>]+>", "", raw).strip()
    raw = raw.strip(" \"'\t\r\n")
    raw = _strip_lms_relay_suffix(raw)

    # Bölüm / kurum soneklerini ayır: "Ahmet Yılmaz - Bilgisayar Müh."
    for sep in (" | ", " / "):
        if sep in raw:
            raw = raw.split(sep, 1)[0].strip()
    if " - " in raw:
        raw = raw.split(" - ", 1)[0].strip()
    raw = _strip_lms_relay_suffix(raw)

    # "SOYAD, Ad" biçimini "Ad Soyad" haline çevir
    if "," in raw and raw.count(",") == 1:
        left, right = [part.strip() for part in raw.split(",", 1)]
        if left and right and "@" not in left and "@" not in right:
            raw = f"{right} {left}"

    raw = re.sub(r"\s+", " ", raw).strip()

    if not raw or "@" in raw:
        email = _extract_email_address(sender_email)
        local = email.split("@", 1)[0] if "@" in email else email
        local = re.sub(r"[._-]+", " ", local)
        local = re.sub(r"\d+", "", local).strip()
        if not local:
            return ""
        return " ".join(_format_name_token(tok) for tok in local.split())

    tokens = raw.split()
    has_all_caps = any(len(tok) > 2 and tok.isupper() for tok in tokens)
    has_all_lower = all(tok.islower() for tok in tokens if any(c.isalpha() for c in tok))
    if has_all_caps or has_all_lower:
        return " ".join(_format_name_token(tok) for tok in tokens)

    # Normal büyük/küçük harf varsa yalnızca bilinen akademik kısaltmaları düzelt
    formatted: List[str] = []
    for tok in tokens:
        norm_key = _tr_normalize(tok)
        if norm_key in _ACADEMIC_TITLE_CANONICAL and tok.lower() in (
            "prof",
            "prof.",
            "dr",
            "dr.",
            "doc",
            "doc.",
            "doç",
            "doç.",
            "ogr",
            "ogr.",
            "öğr",
            "öğr.",
            "gor",
            "gor.",
            "gör",
            "gör.",
            "ars",
            "ars.",
            "arş",
            "arş.",
        ):
            formatted.append(_ACADEMIC_TITLE_CANONICAL[norm_key])
        else:
            formatted.append(tok)
    return " ".join(formatted)


def format_professor_notification(professor_name: str, summary: str) -> str:
    """Hoca bildirimi biçimi: ``"{professor_name} hoca mail attı: {summary}"``."""
    clean_summary = re.sub(r"\s+", " ", (summary or "")).strip()
    clean_name = re.sub(r"\s+", " ", (professor_name or "")).strip()
    if not clean_name:
        return f"Hoca mail attı: {clean_summary}"
    if _HOCA_WORD_RE.search(_tr_normalize(clean_name)):
        return f"{clean_name} mail attı: {clean_summary}"
    return f"{clean_name} hoca mail attı: {clean_summary}"


def _matches_rule_entry(
    entry: str,
    *,
    sender_email: str,
    sender_name_norm: str,
    reply_to_email: str,
    subject_norm: str = "",
) -> bool:
    raw_entry = entry.strip()
    if not raw_entry:
        return False
    entry_lower = _tr_lower(raw_entry)
    entry_norm = _tr_normalize(raw_entry)

    emails_to_check = [e for e in (sender_email, reply_to_email) if e]
    if "@" in entry_lower:
        if entry_lower.startswith("@"):
            domain = entry_lower.lstrip("@")
            return any(e.endswith(f"@{domain}") or e.endswith(f".{domain}") for e in emails_to_check)
        if entry_lower.endswith("@"):
            return any(e.startswith(entry_lower) for e in emails_to_check)
        return any(e == entry_lower or entry_lower in e for e in emails_to_check)

    if entry_norm and entry_norm in sender_name_norm:
        return True
    if entry_norm and any(entry_norm in _tr_normalize(e) for e in emails_to_check):
        return True
    if subject_norm and entry_norm and entry_norm in subject_norm:
        return True
    return False


def _layer1_prefilter(
    *,
    sender_name: str,
    sender_email: str,
    subject: str,
    body: str,
    headers: Dict[str, str],
    rules: SchoolMailRules,
) -> tuple[bool, bool, Optional[SchoolMailDecision]]:
    """1. Katman: whitelist önceliği, LMS hoca istisnası, blacklist, başlıklar ve genel duyuru adresleri.

    Returns:
        ``(whitelisted_professor, lms_professor_candidate, early_decision)`` üçlüsü.
        ``early_decision`` ``None`` değilse 2. katmana geçilmeden sonuç döndürülür.
    """
    clean_email = _extract_email_address(sender_email)
    reply_to = _extract_email_address(headers.get("reply-to", ""))
    sender_name_norm = _tr_normalize(sender_name)
    subject_norm = _tr_normalize(subject)

    # 1. Öncelik: professor_whitelist her zaman diğer tüm kurallardan üstündür.
    for entry in rules.professor_whitelist:
        if _matches_rule_entry(
            entry,
            sender_email=clean_email,
            sender_name_norm=sender_name_norm,
            reply_to_email=reply_to,
        ):
            return True, False, None

    # 1.5 Öncelik: GUZEM/LMS üzerinden kendi adıyla ders duyurusu atan hoca kaçırılmaz.
    if _is_lms_professor_relay(
        sender_name=sender_name,
        sender_email=sender_email,
        subject=subject,
        body=body,
        headers=headers,
        rules=rules,
    ):
        return False, True, None

    # 2. Öncelik: announcement_blacklist
    for entry in rules.announcement_blacklist:
        if _matches_rule_entry(
            entry,
            sender_email=clean_email,
            sender_name_norm=sender_name_norm,
            reply_to_email=reply_to,
            subject_norm=subject_norm,
        ):
            return False, False, SchoolMailDecision(
                is_professor=False,
                professor_name="",
                category="genel_duyuru",
                summary=_build_brief_announcement_summary(subject, body),
                formatted_notification="",
                reason=f"1. katman kara liste eşleşmesi: {entry}",
            )

    # 3. Öncelik: Saf sistem/teslimat önekleri
    local_part = clean_email.split("@", 1)[0] if "@" in clean_email else ""
    if local_part in _SYSTEM_AUTO_PREFIXES:
        return False, False, SchoolMailDecision(
            is_professor=False,
            professor_name="",
            category="sistem_otomatik",
            summary=_build_brief_announcement_summary(subject, body),
            formatted_notification="",
            reason=f"1. katman sistem gönderici öneki: {local_part}@",
        )

    # 4. Öncelik: List-Unsubscribe, Precedence, Auto-Submitted başlıkları
    if headers.get("list-unsubscribe"):
        return False, False, SchoolMailDecision(
            is_professor=False,
            professor_name="",
            category="genel_duyuru",
            summary=_build_brief_announcement_summary(subject, body),
            formatted_notification="",
            reason="1. katman List-Unsubscribe başlığı nedeniyle genel duyuru olarak elendi.",
        )

    precedence = headers.get("precedence", "").strip().lower()
    if precedence:
        tokens = set(re.split(r"[\s,;]+", precedence))
        if tokens & {"bulk", "list", "junk"}:
            return False, False, SchoolMailDecision(
                is_professor=False,
                professor_name="",
                category="genel_duyuru",
                summary=_build_brief_announcement_summary(subject, body),
                formatted_notification="",
                reason=f"1. katman Precedence={precedence} başlığı nedeniyle genel duyuru olarak elendi.",
            )
        if tokens & {"auto_reply", "auto-reply"}:
            return False, False, SchoolMailDecision(
                is_professor=False,
                professor_name="",
                category="sistem_otomatik",
                summary=_build_brief_announcement_summary(subject, body),
                formatted_notification="",
                reason=f"1. katman Precedence={precedence} başlığı nedeniyle otomatik sistem mesajı.",
            )

    auto_submitted = headers.get("auto-submitted", "").strip().lower()
    if auto_submitted and auto_submitted != "no":
        return False, False, SchoolMailDecision(
            is_professor=False,
            professor_name="",
            category="genel_duyuru",
            summary=_build_brief_announcement_summary(subject, body),
            formatted_notification="",
            reason=f"1. katman Auto-Submitted={auto_submitted} başlığı nedeniyle genel duyuru olarak elendi.",
        )

    # 5. Öncelik: Genel duyuru e-posta önekleri
    if local_part in _GENERIC_ANNOUNCEMENT_PREFIXES:
        return False, False, SchoolMailDecision(
            is_professor=False,
            professor_name="",
            category="genel_duyuru",
            summary=_build_brief_announcement_summary(subject, body),
            formatted_notification="",
            reason=f"1. katman genel duyuru adresi ({local_part}@) nedeniyle elendi.",
        )

    return False, False, None


def _strip_noise_lines(body: str) -> str:
    if not body:
        return ""
    lines = [line.strip() for line in body.replace("\r\n", "\n").split("\n")]
    kept: List[str] = []
    for idx, line in enumerate(lines):
        if not line:
            continue
        if line == "--":
            break
        norm_line = _tr_normalize(line)
        if idx < 2 and len(line) < 60 and _GREETING_LINE_RE.match(norm_line):
            continue
        if idx > 0 and len(line) < 70 and _SIGNOFF_LINE_RE.match(norm_line):
            if any(k in norm_line for k in ("saygilar", "iyi calismalar", "basarilar", "kolay gelsin", "tesekkurler")):
                break
        kept.append(line)
    text = " ".join(kept) if kept else re.sub(r"\s+", " ", body).strip()
    return re.sub(r"\s+", " ", text).strip()


def _build_brief_announcement_summary(subject: str, body: str) -> str:
    subj = re.sub(r"\s+", " ", (subject or "")).strip()
    cleaned_body = _strip_noise_lines(body)
    if subj and cleaned_body:
        snippet = cleaned_body[:220].rstrip(" ,;.")
        if len(cleaned_body) > 220:
            snippet += "..."
        return f"{subj} — {snippet}"
    if subj:
        return subj
    return cleaned_body[:240] or "Duyuru içeriği."


def _ensure_attachments_and_links_in_summary(
    summary: str,
    *,
    attachments: Sequence[str],
    body: str,
    is_professor: bool,
) -> str:
    result = re.sub(r"\s+", " ", (summary or "")).strip()
    if not is_professor:
        return result

    norm_summary = _tr_normalize(result)
    clean_attachments = [str(a).strip() for a in attachments if str(a).strip()]
    missing_attachments = [
        att for att in clean_attachments if _tr_normalize(att) not in norm_summary
    ]
    if missing_attachments:
        suffix = f"Ekler: {', '.join(missing_attachments)}."
        result = f"{result.rstrip('.')}." if result else ""
        result = f"{result} {suffix}".strip()

    urls = _URL_RE.findall(body or "")
    if urls:
        missing_urls = [u for u in dict.fromkeys(urls) if u not in result]
        if missing_urls:
            result = f"{result} Link: {', '.join(missing_urls[:2])}.".strip()

    return result


def _build_gemini_prompts(
    *,
    sender_name: str,
    sender_email: str,
    subject: str,
    body: str,
    headers: Dict[str, str],
    attachments: Sequence[str],
    rules: SchoolMailRules,
    whitelisted_professor: bool,
    lms_professor_candidate: bool = False,
) -> tuple[str, str]:
    system_prompt = (
        "Sen Mamed'in okul e-posta analiz ve özetleme motorusun. "
        "Görevin gelen üniversite/okul e-postasını analiz edip kesin olarak yalnızca geçerli JSON üretmektir.\n\n"
        "Sınıflandırma kuralları:\n"
        '1. "hoca": Ders hocasının veya asistanının sınav (vize/final/quiz/bütünleme), ödev, proje, '
        "ders iptali/saati, telafi dersi, laboratuvar, not açıklaması, ders materyali, birebir öğrenci "
        "yanıtı (Re:) veya ders grubu/sınıf duyuruları KESİNLİKLE 'hoca' kategorisindedir. "
        "Okul alan adından (school_domains, örn. adsoyad@okul.edu.tr) gelen ve kişisel isim/unvan "
        "(Prof., Doç., Dr., Öğr. Gör., Arş. Gör.) taşıyan ya da ders/sınav/ödev/proje/telafi içeren "
        "mailler ile GUZEM/UZEM/LMS üzerinden hocanın kendi adıyla gönderdiği ders duyuruları da "
        "KESİNLİKLE 'hoca' olarak sınıflandırılır.\n"
        '2. "genel_duyuru": Rektörlük, öğrenci işleri (genel bilgilendirme), etkinlik, kulüp, anket, '
        "yemekhane, kariyer günü, genel burs/yurt duyuruları 'genel_duyuru' kategorisindedir.\n"
        '3. "sistem_otomatik": Şifre sıfırlama, doğrulama kodu (OTP), otomatik teslimat/hata mesajı '
        "veya no-reply sistem bildirimleri 'sistem_otomatik' kategorisindedir.\n\n"
        "Özet kalitesi kuralları:\n"
        "- Yalnızca net, kısa ve doğru Türkçe kullan.\n"
        "- Tarih, saat, son teslim tarihi, derslik/salon/amfi, sınav veya ödev kapsamı, istenen aksiyon, "
        "ek dosya adları ve bağlantı (link) bilgilerini KESİNLİKLE atlama.\n"
        "- Gereksiz selamlaşma (Merhaba, İyi çalışmalar vb.) ve imza bloklarını çıkar.\n"
        "- professor_name alanından (GUZEM), (UZEM), (LMS) gibi platform eklerini temizle.\n\n"
        "Çıktı formatı (yalnızca tek bir JSON nesnesi):\n"
        "{\n"
        '  "is_professor": true veya false,\n'
        '  "professor_name": "Hocanın temiz adı ve varsa akademik unvanı (değilse boş string)",\n'
        '  "category": "hoca" | "genel_duyuru" | "sistem_otomatik",\n'
        '  "summary": "Eksiksiz net Türkçe özet",\n'
        '  "reason": "Kısa Türkçe karar gerekçesi"\n'
        "}"
    )

    relevant_headers = {
        k: headers[k]
        for k in ("reply-to", "in-reply-to", "references", "list-id", "subject")
        if k in headers and headers[k]
    }
    clean_email = _extract_email_address(sender_email)
    domain = clean_email.split("@", 1)[1] if "@" in clean_email else ""
    school_domain_match = any(
        domain == d.lower().lstrip("@") or domain.endswith(f".{d.lower().lstrip('@')}")
        for d in rules.school_domains
        if d.strip()
    )
    personal_school_sender = school_domain_match and _looks_like_personal_name(sender_name)
    payload = {
        "sender_name": sender_name,
        "sender_email": sender_email,
        "subject": subject,
        "body": body[:12000],
        "attachments": list(attachments),
        "relevant_headers": relevant_headers,
        "whitelisted_professor": whitelisted_professor,
        "lms_professor_candidate": lms_professor_candidate,
        "personal_school_sender": personal_school_sender,
        "rules": {
            "course_keywords": rules.course_keywords,
            "school_domains": rules.school_domains,
        },
    }
    user_prompt = (
        "Aşağıdaki okul e-postasını analiz et ve belirtilen JSON şemasıyla yanıt ver:\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
    return system_prompt, user_prompt


def _call_gemini(
    system_prompt: str,
    user_prompt: str,
    *,
    model: Optional[str] = None,
) -> str:
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY veya GOOGLE_API_KEY tanımlı değil.")

    from google import genai
    from google.genai import types as genai_types

    chosen_model = (
        model
        or os.environ.get("MAMED_SCHOOL_MAIL_MODEL")
        or os.environ.get("MAMED_STANDARD_MODEL")
        or DEFAULT_MODEL
    ).strip() or DEFAULT_MODEL

    client = genai.Client(api_key=api_key)
    config = genai_types.GenerateContentConfig(
        temperature=0.1,
        max_output_tokens=1024,
        response_mime_type="application/json",
        system_instruction=system_prompt,
    )
    response = client.models.generate_content(
        model=chosen_model,
        contents=user_prompt,
        config=config,
    )
    text = getattr(response, "text", None)
    if not text:
        raise RuntimeError("Gemini boş yanıt döndürdü.")
    return str(text)


def _extract_json_object(raw_text: str) -> Dict[str, Any]:
    cleaned = (raw_text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        parsed = json.loads(cleaned[start : end + 1])
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("Model çıktısı geçerli bir JSON nesnesi değil.")


def _fallback_analyze(
    *,
    sender_name: str,
    sender_email: str,
    subject: str,
    body: str,
    headers: Dict[str, str],
    attachments: Sequence[str],
    rules: SchoolMailRules,
    whitelisted_professor: bool,
    lms_professor_candidate: bool = False,
    fallback_reason_note: str = "",
) -> SchoolMailDecision:
    combined_norm = _tr_normalize(f"{subject} {body} {sender_name}")
    clean_email = _extract_email_address(sender_email)
    domain = clean_email.split("@", 1)[1] if "@" in clean_email else ""

    course_hits = [
        kw for kw in rules.course_keywords if kw and _tr_normalize(kw) in combined_norm
    ]
    prof_hits = [kw for kw in _FALLBACK_PROFESSOR_KEYWORDS if kw in combined_norm]
    ann_hits = [kw for kw in _FALLBACK_ANNOUNCEMENT_KEYWORDS if kw in combined_norm]
    sys_hits = [kw for kw in _FALLBACK_SYSTEM_KEYWORDS if kw in combined_norm]
    is_reply = subject.strip().lower().startswith(("re:", "ynt:")) or bool(
        headers.get("in-reply-to")
    )
    has_academic_title = _has_academic_title(sender_name)
    personal_name = _looks_like_personal_name(sender_name)
    school_domain_match = any(
        domain == d.lower().lstrip("@") or domain.endswith(f".{d.lower().lstrip('@')}")
        for d in rules.school_domains
        if d.strip()
    )

    if whitelisted_professor:
        category: CategoryType = "hoca"
        is_professor = True
        reason = "1. katman professor_whitelist önceliği ile hoca olarak işaretlendi (yedek özetleyici)."
    elif lms_professor_candidate:
        category = "hoca"
        is_professor = True
        reason = "LMS/GUZEM üzerinden hocanın kendi adıyla gönderdiği ders duyurusu yakalandı (yedek özetleyici)."
    elif sys_hits and not (prof_hits or course_hits or is_reply):
        category = "sistem_otomatik"
        is_professor = False
        reason = f"Yedek kural analizi: sistem anahtar kelimesi ({sys_hits[0]})."
    elif ann_hits and not (prof_hits or course_hits or is_reply or has_academic_title):
        category = "genel_duyuru"
        is_professor = False
        reason = f"Yedek kural analizi: genel duyuru anahtar kelimesi ({ann_hits[0]})."
    elif school_domain_match and (
        has_academic_title or (personal_name and (prof_hits or course_hits or is_reply or attachments))
    ):
        category = "hoca"
        is_professor = True
        reason = "Yedek kural analizi: okul alan adı ve kişisel hoca/ders sinyali."
    elif course_hits or prof_hits or is_reply or has_academic_title:
        category = "hoca"
        is_professor = True
        trigger = (
            course_hits[0]
            if course_hits
            else (prof_hits[0] if prof_hits else ("birebir yanıt" if is_reply else "akademik unvan"))
        )
        reason = f"Yedek kural analizi: ders/hoca sinyali ({trigger})."
    elif school_domain_match and attachments:
        category = "hoca"
        is_professor = True
        reason = "Yedek kural analizi: okul alan adı ve ek dosya sinyali."
    else:
        category = "genel_duyuru"
        is_professor = False
        reason = "Yedek kural analizi: belirgin ders/hoca sinyali bulunamadı, genel duyuru sayıldı."

    if fallback_reason_note:
        reason = f"{reason} [{fallback_reason_note}]"

    professor_name = clean_professor_name(sender_name, sender_email) if is_professor else ""
    body_clean = _strip_noise_lines(body)
    subj_clean = re.sub(r"\s+", " ", (subject or "")).strip()
    if subj_clean and body_clean:
        raw_summary = f"{subj_clean}: {body_clean[:320]}"
    else:
        raw_summary = subj_clean or body_clean[:320] or "İçerik özeti çıkarılamadı."

    summary = _ensure_attachments_and_links_in_summary(
        raw_summary,
        attachments=attachments,
        body=body,
        is_professor=is_professor,
    )
    formatted_notification = (
        format_professor_notification(professor_name, summary) if is_professor else ""
    )
    return SchoolMailDecision(
        is_professor=is_professor,
        professor_name=professor_name,
        category=category,
        summary=summary,
        formatted_notification=formatted_notification,
        reason=reason,
    )


def analyze_school_mail(
    sender_name: str = "",
    sender_email: str = "",
    subject: str = "",
    body: str = "",
    headers: Optional[Mapping[str, Any]] = None,
    attachments: Optional[Sequence[str]] = None,
    rules: Optional[Union[SchoolMailRules, Mapping[str, Any], Path, str]] = None,
    *,
    model: Optional[str] = None,
    llm_callable: Optional[Callable[[str, str], str]] = None,
) -> SchoolMailDecision:
    """Okul e-postasını iki katmanlı olarak analiz eder ve sabit ``SchoolMailDecision`` döner."""
    resolved_rules = _resolve_rules(rules)
    norm_headers = _normalize_headers(headers)
    attachment_list = [str(item).strip() for item in (attachments or []) if str(item).strip()]

    whitelisted_professor, lms_professor_candidate, early_decision = _layer1_prefilter(
        sender_name=sender_name,
        sender_email=sender_email,
        subject=subject,
        body=body,
        headers=norm_headers,
        rules=resolved_rules,
    )
    if early_decision is not None:
        return early_decision

    system_prompt, user_prompt = _build_gemini_prompts(
        sender_name=sender_name,
        sender_email=sender_email,
        subject=subject,
        body=body,
        headers=norm_headers,
        attachments=attachment_list,
        rules=resolved_rules,
        whitelisted_professor=whitelisted_professor,
        lms_professor_candidate=lms_professor_candidate,
    )

    try:
        if llm_callable is not None:
            raw_response = llm_callable(system_prompt, user_prompt)
        else:
            raw_response = _call_gemini(system_prompt, user_prompt, model=model)
        parsed = _extract_json_object(raw_response)
    except Exception as exc:
        logger.debug("2. katman Gemini analizi yedek moda geçti: %s", exc)
        return _fallback_analyze(
            sender_name=sender_name,
            sender_email=sender_email,
            subject=subject,
            body=body,
            headers=norm_headers,
            attachments=attachment_list,
            rules=resolved_rules,
            whitelisted_professor=whitelisted_professor,
            lms_professor_candidate=lms_professor_candidate,
            fallback_reason_note=str(exc),
        )

    raw_category = str(parsed.get("category", "")).strip().lower()
    if raw_category not in ("hoca", "genel_duyuru", "sistem_otomatik"):
        raw_category = "hoca" if parsed.get("is_professor") else "genel_duyuru"

    if whitelisted_professor:
        category: CategoryType = "hoca"
        is_professor = True
    else:
        category = raw_category  # type: ignore[assignment]
        is_professor = category == "hoca" or bool(parsed.get("is_professor"))
        if is_professor:
            category = "hoca"

    raw_prof_name = str(parsed.get("professor_name") or "").strip()
    if is_professor:
        professor_name = clean_professor_name(raw_prof_name or sender_name, sender_email)
    else:
        professor_name = ""

    raw_summary = str(parsed.get("summary") or "").strip()
    if not raw_summary:
        raw_summary = _build_brief_announcement_summary(subject, body)

    summary = _ensure_attachments_and_links_in_summary(
        raw_summary,
        attachments=attachment_list,
        body=body,
        is_professor=is_professor,
    )

    reason = str(parsed.get("reason") or "").strip()
    if whitelisted_professor and "whitelist" not in reason.lower():
        prefix = "1. katman professor_whitelist önceliği. "
        reason = f"{prefix}{reason}".strip()
    if not reason:
        reason = f"2. katman Gemini sınıflandırması: {category}."

    formatted_notification = (
        format_professor_notification(professor_name, summary) if is_professor else ""
    )

    return SchoolMailDecision(
        is_professor=is_professor,
        professor_name=professor_name,
        category=category,
        summary=summary,
        formatted_notification=formatted_notification,
        reason=reason,
    )


def record_school_mail_decision(
    decision: SchoolMailDecision,
    *,
    message_id: str = "",
    sender_name: str = "",
    sender_email: str = "",
    subject: str = "",
    received_at: str = "",
    attachments: Optional[Sequence[str]] = None,
    store_path: Optional[Union[Path, str]] = None,
    max_items: int = 100,
) -> Dict[str, Any]:
    """Analiz sonucunu yerel ``school_mail_summaries.json`` deposuna kaydeder."""
    target = (
        Path(store_path).expanduser()
        if store_path is not None
        else get_school_mail_store_path()
    )
    timestamp = received_at or datetime.now(timezone.utc).isoformat()
    entry: Dict[str, Any] = {
        "message_id": message_id,
        "sender_name": sender_name,
        "sender_email": _extract_email_address(sender_email),
        "subject": subject,
        "received_at": timestamp,
        "attachments": [str(a).strip() for a in (attachments or []) if str(a).strip()],
        **decision.to_dict(),
    }

    with _STORE_LOCK:
        items: List[Dict[str, Any]] = []
        if target.is_file():
            try:
                loaded = json.loads(target.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    items = [x for x in loaded if isinstance(x, dict)]
            except Exception:
                items = []

        if message_id:
            items = [item for item in items if item.get("message_id") != message_id]

        items.insert(0, entry)
        if max_items > 0:
            items = items[:max_items]

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(items, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return entry


def load_recent_school_mails(
    *,
    limit: int = 10,
    only_professor: bool = True,
    professor_query: str = "",
    store_path: Optional[Union[Path, str]] = None,
) -> List[Dict[str, Any]]:
    """Yerel depodan son okul e-postası özetlerini okur."""
    target = (
        Path(store_path).expanduser()
        if store_path is not None
        else get_school_mail_store_path()
    )
    if not target.is_file():
        return []
    with _STORE_LOCK:
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            return []
    if not isinstance(loaded, list):
        return []

    query_norm = _tr_normalize(professor_query)
    filtered: List[Dict[str, Any]] = []
    for item in loaded:
        if not isinstance(item, dict):
            continue
        if only_professor and not bool(item.get("is_professor")):
            continue
        if query_norm:
            haystack = _tr_normalize(
                " ".join(
                    str(item.get(field) or "")
                    for field in ("professor_name", "sender_name", "sender_email", "subject", "summary")
                )
            )
            if query_norm not in haystack:
                continue
        filtered.append(item)
        if limit > 0 and len(filtered) >= limit:
            break
    return filtered


__all__ = [
    "CategoryType",
    "DEFAULT_MODEL",
    "DEFAULT_RULES_FILENAME",
    "DEFAULT_STORE_FILENAME",
    "SchoolMailDecision",
    "SchoolMailRules",
    "analyze_school_mail",
    "clean_professor_name",
    "format_professor_notification",
    "get_school_mail_rules_path",
    "get_school_mail_store_path",
    "load_recent_school_mails",
    "load_school_mail_rules",
    "record_school_mail_decision",
    "save_school_mail_rules",
]

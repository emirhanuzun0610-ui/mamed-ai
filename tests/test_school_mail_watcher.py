"""Okul e-posta IMAP IDLE dinleyicisi, API uclari ve Telegram komut testleri."""

from __future__ import annotations

from email.message import EmailMessage
import importlib.util
import json
from pathlib import Path
import sys
import threading

from fastapi.testclient import TestClient
import pytest

from openjarvis.server.app import create_app
from openjarvis.server.school_mail_watcher import (
    ImapIdleWatcher,
    SchoolMailConfig,
    SchoolMailStateStore,
    mask_secrets,
    parse_raw_email,
)


def _build_raw_email(
    *,
    from_header: str,
    subject: str,
    body_plain: str = "",
    body_html: str = "",
    message_id: str = "<msg-1@gazi.edu.tr>",
    extra_headers: dict[str, str] | None = None,
    attachment_name: str | None = None,
) -> bytes:
    msg = EmailMessage()
    msg["From"] = from_header
    msg["To"] = "memirhan.uzun@gazi.edu.tr"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = "Sat, 28 Nov 2026 09:30:00 +0300"
    for k, v in (extra_headers or {}).items():
        msg[k] = v
    if body_html and not body_plain:
        msg.add_alternative(body_html, subtype="html")
    else:
        msg.set_content(body_plain or "")
        if body_html:
            msg.add_alternative(body_html, subtype="html")
    if attachment_name:
        msg.add_attachment(b"PDFDATA", maintype="application", subtype="pdf", filename=attachment_name)
    return msg.as_bytes()


class _FakeIdleSocket:
    def __init__(self, idle_lines: list[bytes]) -> None:
        self._idle_lines = idle_lines

    def has_pending_data(self, timeout: float = 0.5) -> bool:
        return bool(self._idle_lines)


class _FakeImapClient:
    def __init__(
        self,
        messages: dict[int, bytes],
        *,
        capabilities: tuple[str, ...] = ("IMAP4REV1", "IDLE", "UIDPLUS"),
        idle_push_messages: dict[int, bytes] | None = None,
        idle_lines: list[bytes] | None = None,
        uidvalidity: str = "1001",
    ) -> None:
        self.messages = dict(messages)
        self.capabilities = capabilities
        self.idle_push_messages = idle_push_messages or {}
        self._idle_lines = list(idle_lines or [])
        self._uidvalidity = uidvalidity
        self.sock = _FakeIdleSocket(self._idle_lines)
        self.sent_commands: list[bytes] = []
        self.fetch_commands: list[str] = []
        self._tag_counter = 0
        self.logged_out = False
        self._idle_pushed = False

    def login(self, user: str, password: str):
        return "OK", [b"Logged in"]

    def logout(self):
        self.logged_out = True
        return "BYE", [b"Logged out"]

    def select(self, folder: str, readonly: bool = True):
        return "OK", [str(len(self.messages)).encode("ascii")]

    def response(self, code: str):
        if code == "UIDVALIDITY":
            return "UIDVALIDITY", [self._uidvalidity.encode("ascii")]
        return code, [None]

    def capability(self):
        return "OK", [" ".join(self.capabilities).encode("ascii")]

    def noop(self):
        return "OK", [b"NOOP completed"]

    def _new_tag(self) -> bytes:
        self._tag_counter += 1
        return f"TAG{self._tag_counter}".encode("ascii")

    def send(self, data: bytes) -> None:
        self.sent_commands.append(data)
        if b"IDLE" in data and not self._idle_pushed and self.idle_push_messages:
            self._idle_pushed = True
            self.messages.update(self.idle_push_messages)
            max_seq = len(self.messages)
            self._idle_lines.extend([b"+ idling\r\n", f"* {max_seq} EXISTS\r\n".encode("ascii")])
        elif b"DONE" in data:
            last_tag = f"TAG{self._tag_counter}".encode("ascii")
            self._idle_lines.append(last_tag + b" OK IDLE terminated\r\n")

    def readline(self) -> bytes:
        if self._idle_lines:
            return self._idle_lines.pop(0)
        return b""

    def uid(self, command: str, *args):
        cmd = command.upper()
        if cmd == "SEARCH":
            criteria = args[-1]
            all_uids = sorted(self.messages.keys())
            if criteria == "ALL":
                return "OK", [" ".join(str(u) for u in all_uids).encode("ascii")]
            if isinstance(criteria, str) and criteria.startswith("UID "):
                start = int(criteria.split()[1].split(":")[0])
                # RFC 3501 davranisi: eslesen yoksa en buyuk UID doner
                matched = [u for u in all_uids if u >= start]
                if not matched and all_uids:
                    matched = [max(all_uids)]
                return "OK", [" ".join(str(u) for u in matched).encode("ascii")]
        if cmd == "FETCH":
            uid = int(args[0])
            spec = str(args[1])
            self.fetch_commands.append(spec)
            raw = self.messages.get(uid)
            if raw is None:
                return "NO", []
            return "OK", [(f"{uid} (UID {uid} BODY[] {{{len(raw)}}}".encode("ascii"), raw)]
        return "BAD", []


def test_parse_raw_email_html_and_attachments():
    raw = _build_raw_email(
        from_header='"Dr. Öğr. Üyesi Ahmet Yılmaz" <ahmet.yilmaz@gazi.edu.tr>',
        subject="Bilgisayar Ağları Vize Sınavı",
        body_html="<html><body><p>Merhaba,</p><p>Vize <b>14 Kasım 10:30</b> Amfi-2'de yapılacaktır.</p></body></html>",
        attachment_name="sinav_kapsami.pdf",
    )
    parsed = parse_raw_email(raw)
    assert "Ahmet Yılmaz" in parsed["sender_name"]
    assert parsed["sender_email"] == "ahmet.yilmaz@gazi.edu.tr"
    assert parsed["subject"] == "Bilgisayar Ağları Vize Sınavı"
    assert "14 Kasım 10:30" in parsed["body"]
    assert "<html>" not in parsed["body"]
    assert parsed["attachments"] == ["sinav_kapsami.pdf"]


def test_imap_idle_and_telegram_exact_notification(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("MAMED_API_KEYS_ENV", str(tmp_path / "missing_api_keys.env"))

    old_mail = _build_raw_email(
        from_header="Duyuru <duyuru@gazi.edu.tr>",
        subject="Eski Duyuru",
        body_plain="Eski mesaj",
        message_id="<old-1@gazi.edu.tr>",
    )
    new_prof_mail = _build_raw_email(
        from_header="Prof. Dr. Ayşe Demir <ayse.demir@gazi.edu.tr>",
        subject="Veri Yapıları Ödev Teslimi",
        body_plain="Ödev 2 son teslim tarihi 30 Kasım 23:59 olarak güncellendi. Ek dosya ektedir.",
        message_id="<prof-2@gazi.edu.tr>",
        attachment_name="odev2.pdf",
    )
    new_ann_mail = _build_raw_email(
        from_header="Öğrenci İşleri <oidb@gazi.edu.tr>",
        subject="Yemekhane Menüsü",
        body_plain="Haftalık yemekhane menüsü yayınlandı.",
        message_id="<ann-3@gazi.edu.tr>",
        extra_headers={"List-Unsubscribe": "<mailto:unsub@gazi.edu.tr>"},
    )

    fake_imap = _FakeImapClient(
        messages={10: old_mail},
        idle_push_messages={11: new_prof_mail, 12: new_ann_mail},
    )

    sent_telegram: list[tuple[str, list[str], str]] = []

    def _fake_tg(token, chat_ids, text):
        sent_telegram.append((token, list(chat_ids), text))
        return True, None

    def _fake_llm(_sys_prompt: str, user_prompt: str) -> str:
        assert "Veri Yapıları Ödev Teslimi" in user_prompt
        return json.dumps(
            {
                "is_professor": True,
                "professor_name": "Prof. Dr. Ayşe Demir",
                "category": "hoca",
                "summary": "Veri Yapıları Ödev 2 son teslim tarihi 30 Kasım 23:59 olarak güncellendi.",
                "reason": "Ders hocasından ödev duyurusu.",
            }
        )

    cfg = SchoolMailConfig(
        imap_host="imap.gazi.edu.tr",
        imap_port=993,
        username="memirhan.uzun@gazi.edu.tr",
        password="secret-password",
        telegram_bot_token="123456:FAKE_TOKEN",
        telegram_chat_ids=["987654321"],
    )
    store = SchoolMailStateStore(tmp_path / "school_mail_state.json")
    stop_event = threading.Event()

    watcher = ImapIdleWatcher(
        config=cfg,
        state_store=store,
        imap_factory=lambda _h, _p: fake_imap,
        telegram_sender=_fake_tg,
        analyzer=lambda **kw: __import__(
            "openjarvis.intelligence.school_mail_analyzer", fromlist=["analyze_school_mail"]
        ).analyze_school_mail(**kw, llm_callable=_fake_llm),
        stop_event=stop_event,
    )

    # 1) Ilk kurulum taban alma: eski mail (UID 10) icin bildirim gitmemeli
    res1 = watcher.run_once()
    assert res1["baseline_initialized"] is True
    assert res1["last_uid"] == 10
    assert sent_telegram == []

    # 2) IDLE dongusu yeni gelen 11 (hoca) ve 12 (duyuru) maillerini yakalamali
    assert fake_imap.login("u", "p")[0] == "OK"
    got_signal = watcher._wait_idle_cycle(fake_imap)
    assert got_signal is True
    res2 = watcher.sync_mailbox(fake_imap, "1001")
    assert res2["professor_count"] == 1
    assert res2["filtered_count"] == 1
    assert all(spec == "(BODY.PEEK[])" for spec in fake_imap.fetch_commands)

    assert len(sent_telegram) == 1
    assert sent_telegram[0][1] == ["987654321"]
    assert sent_telegram[0][2].startswith("Prof. Dr. Ayşe Demir hoca mail attı: ")
    assert "odev2.pdf" in sent_telegram[0][2]

    # 3) Ayni Message-ID farkli UID ile tekrar gelirse ikinci kez bildirilmemeli
    fake_imap.messages[13] = new_prof_mail
    res3 = watcher.sync_mailbox(fake_imap, "1001")
    assert res3["professor_count"] == 0
    assert len(sent_telegram) == 1

    saved = store.load()
    assert saved["total_professor_mails"] == 1
    assert saved["filtered_announcements_count"] == 1
    assert len(saved["recent_professor_mails"]) == 1


def test_poll_fallback_when_idle_not_supported(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path))

    fake_imap = _FakeImapClient(
        messages={1: _build_raw_email(from_header="a@gazi.edu.tr", subject="Test", body_plain="Test")},
        capabilities=("IMAP4REV1",),  # IDLE yok
    )
    cfg = SchoolMailConfig(
        imap_host="imap.gazi.edu.tr",
        imap_port=993,
        username="memirhan.uzun@gazi.edu.tr",
        password="secret",
    )
    store = SchoolMailStateStore(tmp_path / "school_mail_state.json")
    watcher = ImapIdleWatcher(config=cfg, state_store=store, imap_factory=lambda _h, _p: fake_imap)
    res = watcher.run_once()
    assert res["connection_type"] == "imap_poll_15s"


def test_mask_secrets():
    text = (
        "https://api.telegram.org/bot12345:AAABBBCCC/sendMessage "
        "SCHOOL_MAIL_PASSWORD=MySecretPass123 http://127.0.0.1:8000/#token=tok999"
    )
    masked = mask_secrets(text, extra_secrets=["MySecretPass123", "12345:AAABBBCCC"])
    assert "MySecretPass123" not in masked
    assert "AAABBBCCC" not in masked
    assert "tok999" not in masked
    assert "/bot***/" in masked
    assert "#token=***" in masked


def test_school_mail_rest_endpoints_and_auth(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("SCHOOL_MAIL_USERNAME", "memirhan.uzun@gazi.edu.tr")
    monkeypatch.setenv("SCHOOL_MAIL_PASSWORD", "test-pass")

    class _DummyEngine:
        pass

    app = create_app(_DummyEngine(), "gemini-3.5-flash", api_key="oj_sk_test_secret")
    client = TestClient(app)

    # Token olmadan 401 donmeli
    assert client.get("/v1/mamed/school-mail/status").status_code == 401
    assert client.get("/v1/mamed/school-mail/rules").status_code == 401

    headers = {"Authorization": "Bearer oj_sk_test_secret"}
    st = client.get("/v1/mamed/school-mail/status", headers=headers)
    assert st.status_code == 200
    assert st.json()["imap_host"] == "imap.gazi.edu.tr"
    assert "test-pass" not in st.text

    put_res = client.put(
        "/v1/mamed/school-mail/rules",
        headers=headers,
        json={
            "professor_whitelist": ["ahmet.yilmaz@gazi.edu.tr"],
            "school_domains": ["gazi.edu.tr"],
        },
    )
    assert put_res.status_code == 200
    assert put_res.json()["professor_whitelist"] == ["ahmet.yilmaz@gazi.edu.tr"]

    get_rules = client.get("/v1/mamed/school-mail/rules", headers=headers)
    assert get_rules.status_code == 200
    assert get_rules.json()["school_domains"] == ["gazi.edu.tr"]

    monkeypatch.setattr(
        "openjarvis.server.school_mail_routes.check_mailbox_once",
        lambda config=None: {"checked_count": 2, "professor_count": 1, "filtered_count": 1},
    )
    chk = client.post("/v1/mamed/school-mail/check-now", headers=headers)
    assert chk.status_code == 200
    assert chk.json()["professor_count"] == 1


def test_telegram_bot_school_mail_formatters():
    bot_path = Path(__file__).resolve().parent.parent / "scripts" / "mamed_telegram_bot.py"
    spec = importlib.util.spec_from_file_location("mamed_telegram_bot", bot_path)
    assert spec is not None and spec.loader is not None
    bot_mod = importlib.util.module_from_spec(spec)
    sys.modules["mamed_telegram_bot"] = bot_mod
    spec.loader.exec_module(bot_mod)

    status_text = bot_mod.format_school_mail_status_text(
        {
            "watcher_running": True,
            "status": "dinleniyor",
            "connection_type": "imap_idle",
            "imap_host": "imap.gazi.edu.tr",
            "imap_port": 993,
            "username": "memirhan.uzun@gazi.edu.tr",
            "last_check_at": "2026-11-28T04:50:00+03:00",
            "total_professor_mails": 2,
            "filtered_announcements_count": 5,
            "recent_professor_mails": [
                {"formatted_notification": "Ahmet Yılmaz hoca mail attı: Sınav Cuma 10:00'da."}
            ],
        }
    )
    assert "IMAP IDLE (Anlık)" in status_text
    assert "Ahmet Yılmaz hoca mail attı" in status_text

    check_text = bot_mod.format_school_mail_check_text(
        {"checked_count": 3, "professor_count": 1, "filtered_count": 2}
    )
    assert "3 yeni e-posta incelendi" in check_text
    assert "1 hoca maili" in check_text

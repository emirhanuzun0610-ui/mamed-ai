"""Okul e-posta sınıflandırma, özetleme ve Mamed aracı için kapsamlı testler."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

import pytest

from openjarvis.core.registry import ToolRegistry
from openjarvis.intelligence.school_mail_analyzer import (
    SchoolMailDecision,
    SchoolMailRules,
    analyze_school_mail,
    clean_professor_name,
    format_professor_notification,
    load_recent_school_mails,
    load_school_mail_rules,
    record_school_mail_decision,
    save_school_mail_rules,
)
from openjarvis.tools.school_mail_tool import SchoolMailRecentTool


def _make_llm_stub(payload: dict):
    calls: List[tuple[str, str]] = []

    def _stub(system_prompt: str, user_prompt: str) -> str:
        calls.append((system_prompt, user_prompt))
        return json.dumps(payload, ensure_ascii=False)

    return _stub, calls


# ---------------------------------------------------------------------------
# 1. Katman: Hızlı kural, başlıklar, genel duyuru adresleri ve whitelist önceliği
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sender_email",
    [
        "noreply@universite.edu.tr",
        "duyuru@universite.edu.tr",
        "oidb@universite.edu.tr",
        "sks@universite.edu.tr",
        "rektorluk@universite.edu.tr",
        "ogrenciisleri@universite.edu.tr",
        "bulten@universite.edu.tr",
        "kutuphane@universite.edu.tr",
        "obs@universite.edu.tr",
    ],
)
def test_layer1_generic_prefixes_are_filtered_without_llm(sender_email: str):
    llm_stub, calls = _make_llm_stub({"is_professor": True, "category": "hoca", "summary": "x"})

    decision = analyze_school_mail(
        sender_name="Üniversite Duyuru",
        sender_email=sender_email,
        subject="Genel Akademik Takvim Duyurusu",
        body="Detaylar portalda yayınlanmıştır.",
        rules=SchoolMailRules(),
        llm_callable=llm_stub,
    )

    assert decision.is_professor is False
    assert decision.category == "genel_duyuru"
    assert decision.formatted_notification == ""
    assert calls == []


@pytest.mark.parametrize(
    "headers",
    [
        {"List-Unsubscribe": "<mailto:unsubscribe@lists.edu.tr>"},
        {"Precedence": "bulk"},
        {"precedence": "list"},
        {"Auto-Submitted": "auto-generated"},
    ],
)
def test_layer1_bulk_headers_filtered_without_llm(headers: dict):
    llm_stub, calls = _make_llm_stub({"is_professor": True, "category": "hoca", "summary": "x"})

    decision = analyze_school_mail(
        sender_name="Kampüs Haberleri",
        sender_email="haberler@kampus.edu.tr",
        subject="Bahar Şenliği Başvuruları",
        body="Kulüp stantları için son gün Cuma.",
        headers=headers,
        rules=SchoolMailRules(),
        llm_callable=llm_stub,
    )

    assert decision.is_professor is False
    assert decision.category == "genel_duyuru"
    assert calls == []


def test_layer1_auto_submitted_no_is_not_filtered():
    llm_stub, calls = _make_llm_stub(
        {
            "is_professor": True,
            "professor_name": "Ahmet Yılmaz",
            "category": "hoca",
            "summary": "Vize sınavı 14 Mayıs Çarşamba saat 10:00'da B-204 dersliğinde yapılacaktır.",
            "reason": "Ders hocası sınav duyurusu.",
        }
    )

    decision = analyze_school_mail(
        sender_name="Ahmet Yılmaz",
        sender_email="ahmet.yilmaz@uni.edu.tr",
        subject="Vize Sınavı Yeri ve Saati",
        body="Merhaba arkadaşlar, vize 14 Mayıs Çarşamba 10:00'da B-204'te.",
        headers={"Auto-Submitted": "no"},
        rules=SchoolMailRules(),
        llm_callable=llm_stub,
    )

    assert len(calls) == 1
    assert decision.is_professor is True
    assert decision.category == "hoca"


def test_layer1_announcement_blacklist_filters_immediately():
    llm_stub, calls = _make_llm_stub({"is_professor": True, "category": "hoca", "summary": "x"})
    rules = SchoolMailRules(announcement_blacklist=["kariyer.merkezi@uni.edu.tr"])

    decision = analyze_school_mail(
        sender_name="Kariyer Merkezi",
        sender_email="kariyer.merkezi@uni.edu.tr",
        subject="Staj Fuarı",
        body="Standlara bekleriz.",
        rules=rules,
        llm_callable=llm_stub,
    )

    assert decision.is_professor is False
    assert decision.category == "genel_duyuru"
    assert "kara liste" in decision.reason.lower()
    assert calls == []


def test_layer1_professor_whitelist_overrides_bulk_headers_and_prefix():
    llm_stub, calls = _make_llm_stub(
        {
            "is_professor": False,
            "professor_name": "Prof. Dr. Ahmet Yılmaz",
            "category": "genel_duyuru",
            "summary": "BLG301 dersi telafisi 20 Kasım Cuma 13:30'da D-102'de yapılacaktır.",
            "reason": "Ders grubu mesajı.",
        }
    )
    rules = SchoolMailRules(professor_whitelist=["ahmet.yilmaz@uni.edu.tr"])

    decision = analyze_school_mail(
        sender_name="Prof. Dr. Ahmet Yılmaz",
        sender_email="ahmet.yilmaz@uni.edu.tr",
        subject="[BLG301] Telafi Dersi",
        body="Sevgili öğrenciler, telafi 20 Kasım Cuma saat 13:30'da D-102 dersliğinde.",
        headers={
            "List-Unsubscribe": "<mailto:blg301-leave@lists.uni.edu.tr>",
            "Precedence": "list",
            "Auto-Submitted": "auto-generated",
        },
        rules=rules,
        llm_callable=llm_stub,
    )

    assert len(calls) == 1
    assert decision.is_professor is True
    assert decision.category == "hoca"
    assert decision.professor_name == "Prof. Dr. Ahmet Yılmaz"
    assert decision.formatted_notification.startswith("Prof. Dr. Ahmet Yılmaz hoca mail attı: ")
    assert "whitelist" in decision.reason.lower()


# ---------------------------------------------------------------------------
# 2. Katman: Sahte hoca mailleri ve genel duyuru ayrımı + özet kalitesi
# ---------------------------------------------------------------------------


def test_layer2_professor_exam_change_with_attachment_and_details():
    llm_stub, _ = _make_llm_stub(
        {
            "is_professor": True,
            "professor_name": "Ahmet Yılmaz",
            "category": "hoca",
            "summary": (
                "Veri Yapıları vizesi 18 Mayıs Salı 14:00'e ertelendi; "
                "sınav A-301 amfisinde ilk 5 üniteyi kapsayacak, hesap makinesi yasak."
            ),
            "reason": "Ders hocası sınav tarihi, salon ve kapsam değişikliği bildirdi.",
        }
    )

    decision = analyze_school_mail(
        sender_name='"YILMAZ, AHMET" - Bilgisayar Mühendisliği',
        sender_email="ayilmaz@uni.edu.tr",
        subject="Veri Yapıları Vize Sınavı Hakkında",
        body=(
            "Merhaba arkadaşlar,\n"
            "Vize sınavımız 18 Mayıs Salı saat 14:00'e alınmıştır. Salon: A-301.\n"
            "Kapsam: İlk 5 ünite. Hesap makinesi getirmeyiniz.\n"
            "İyi çalışmalar,\nProf. Dr. Ahmet Yılmaz"
        ),
        attachments=["vize_konu_dagilimi.pdf"],
        rules=SchoolMailRules(school_domains=["uni.edu.tr"]),
        llm_callable=llm_stub,
    )

    assert isinstance(decision, SchoolMailDecision)
    assert decision.is_professor is True
    assert decision.category == "hoca"
    assert decision.professor_name == "Ahmet Yılmaz"
    assert "18 Mayıs Salı 14:00" in decision.summary
    assert "A-301" in decision.summary
    # LLM özette ek dosya adını atlasa bile güvenlik ağı eklemeli
    assert "vize_konu_dagilimi.pdf" in decision.summary
    assert decision.formatted_notification.startswith("Ahmet Yılmaz hoca mail attı: ")


def test_layer2_professor_homework_submission_preserves_link_and_deadline():
    llm_stub, _ = _make_llm_stub(
        {
            "is_professor": True,
            "professor_name": "Dr. Zeynep Kaya",
            "category": "hoca",
            "summary": (
                "2. ödev son teslimi 25 Kasım 23:59 olarak belirlendi; "
                "rapor ve kod zip olarak https://moodle.uni.edu.tr/mod/assign/view.php?id=42 adresine yüklenmeli."
            ),
            "reason": "Ödev teslim tarihi ve aksiyon bilgisi.",
        }
    )

    decision = analyze_school_mail(
        sender_name="Dr. Zeynep Kaya",
        sender_email="zeynep.kaya@uni.edu.tr",
        subject="Ödev 2 Teslim Duyurusu",
        body=(
            "Değerli öğrenciler, Ödev 2 için son teslim 25 Kasım 23:59.\n"
            "Yükleme: https://moodle.uni.edu.tr/mod/assign/view.php?id=42"
        ),
        attachments=["odev2_yonerge.pdf"],
        llm_callable=llm_stub,
    )

    assert decision.is_professor is True
    assert decision.category == "hoca"
    assert "25 Kasım 23:59" in decision.summary
    assert "https://moodle.uni.edu.tr/mod/assign/view.php?id=42" in decision.summary
    assert "odev2_yonerge.pdf" in decision.summary
    assert decision.formatted_notification.startswith("Dr. Zeynep Kaya hoca mail attı: ")


def test_layer2_professor_personal_reply_and_course_group():
    llm_stub, _ = _make_llm_stub(
        {
            "is_professor": True,
            "professor_name": "Mehmet Demir",
            "category": "hoca",
            "summary": "Proje önerin onaylandı; Cuma 15:00'te ofisimde 15 dakikalık ara değerlendirme yapalım.",
            "reason": "Hocanın öğrenciye birebir yanıtı.",
        }
    )

    decision = analyze_school_mail(
        sender_name="Mehmet Demir",
        sender_email="mdemir@uni.edu.tr",
        subject="Re: Bitirme Projesi Konu Onayı",
        body="Merhaba, önerin uygun. Cuma saat 15:00'te ofisime uğrayabilirsin.",
        headers={"In-Reply-To": "<msg-123@uni.edu.tr>"},
        llm_callable=llm_stub,
    )

    assert decision.is_professor is True
    assert decision.category == "hoca"
    assert decision.formatted_notification == (
        "Mehmet Demir hoca mail attı: Proje önerin onaylandı; Cuma 15:00'te ofisimde 15 dakikalık ara değerlendirme yapalım."
    )


@pytest.mark.parametrize(
    "sender_name,sender_email,subject,body",
    [
        (
            "Rektörlük İletişim",
            "iletisim@uni.edu.tr",
            "29 Ekim Cumhuriyet Bayramı Töreni",
            "Tüm öğrencilerimiz konferans salonundaki törene davetlidir.",
        ),
        (
            "IEEE Öğrenci Kolu",
            "ieee@topluluk.uni.edu.tr",
            "Yapay Zeka Atölyesi ve Pizza Gecesi",
            "Kulübümüzün etkinliğine kayıt formundan başvurabilirsiniz.",
        ),
    ],
)
def test_layer2_rectorate_and_club_classified_as_general_announcement(
    sender_name: str,
    sender_email: str,
    subject: str,
    body: str,
):
    llm_stub, _ = _make_llm_stub(
        {
            "is_professor": False,
            "professor_name": "",
            "category": "genel_duyuru",
            "summary": f"{subject}: genel kampüs duyurusu.",
            "reason": "Dersle doğrudan ilişkili hoca maili değil.",
        }
    )

    decision = analyze_school_mail(
        sender_name=sender_name,
        sender_email=sender_email,
        subject=subject,
        body=body,
        llm_callable=llm_stub,
    )

    assert decision.is_professor is False
    assert decision.professor_name == ""
    assert decision.category == "genel_duyuru"
    assert decision.formatted_notification == ""


def test_layer2_system_automatic_classification():
    llm_stub, _ = _make_llm_stub(
        {
            "is_professor": False,
            "professor_name": "",
            "category": "sistem_otomatik",
            "summary": "Tek kullanımlık doğrulama kodu bildirimi.",
            "reason": "Otomatik güvenlik doğrulama mesajı.",
        }
    )

    decision = analyze_school_mail(
        sender_name="Kampüs Kimlik Sistemi",
        sender_email="sso@uni.edu.tr",
        subject="Doğrulama Kodunuz",
        body="Tek kullanımlık şifreniz: 482910",
        llm_callable=llm_stub,
    )

    assert decision.is_professor is False
    assert decision.category == "sistem_otomatik"
    assert decision.formatted_notification == ""


def test_fallback_analysis_when_api_key_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    decision = analyze_school_mail(
        sender_name="Doç. Dr. Elif Şahin",
        sender_email="esahin@uni.edu.tr",
        subject="BLG223 Laboratuvar Telafisi",
        body=(
            "Merhaba arkadaşlar,\n"
            "Laboratuvar telafisi 12 Aralık Perşembe 10:30'da Lab-3'te yapılacaktır.\n"
            "Başarılar"
        ),
        attachments=["lab_foyu.pdf"],
        rules=SchoolMailRules(course_keywords=["BLG223"]),
    )

    assert decision.is_professor is True
    assert decision.category == "hoca"
    assert decision.professor_name == "Doç. Dr. Elif Şahin"
    assert "lab_foyu.pdf" in decision.summary
    assert decision.formatted_notification.startswith("Doç. Dr. Elif Şahin hoca mail attı: ")


# ---------------------------------------------------------------------------
# İsim temizleme, bildirim formatı, kurallar JSON ve Mamed aracı testleri
# ---------------------------------------------------------------------------


def test_name_cleaning_and_notification_formatting_no_duplicate_hoca():
    assert clean_professor_name('"YILMAZ, AHMET" <ayilmaz@uni.edu.tr>') == "Ahmet Yılmaz"
    assert clean_professor_name("Prof. Dr. Ayşe Öz - Elektrik Müh.") == "Prof. Dr. Ayşe Öz"
    assert clean_professor_name("", "fatma.demir@uni.edu.tr") == "Fatma Demir"

    assert (
        format_professor_notification("Ahmet Yılmaz", "Sınav ertelendi.")
        == "Ahmet Yılmaz hoca mail attı: Sınav ertelendi."
    )
    assert (
        format_professor_notification("Ahmet Hoca", "Sınav ertelendi.")
        == "Ahmet Hoca mail attı: Sınav ertelendi."
    )


def test_rules_file_roundtrip_with_openjarvis_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path))
    rules = SchoolMailRules(
        professor_whitelist=["ahmet.yilmaz@uni.edu.tr"],
        announcement_blacklist=["bulten@uni.edu.tr"],
        course_keywords=["BLG223", "Veri Yapıları"],
        school_domains=["uni.edu.tr"],
    )
    saved_path = save_school_mail_rules(rules)
    assert saved_path == tmp_path / "school_mail_rules.json"

    loaded = load_school_mail_rules()
    assert loaded.to_dict() == rules.to_dict()


def test_store_and_mamed_school_mail_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from openjarvis.agents.tool_resolver import ensure_registries_populated

    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path))
    ensure_registries_populated()
    assert ToolRegistry.contains("school_mail_recent")

    tool = SchoolMailRecentTool()
    empty_result = tool.execute()
    assert empty_result.success is True
    assert empty_result.metadata["count"] == 0
    assert "bulunamadı" in empty_result.content

    prof_decision = SchoolMailDecision(
        is_professor=True,
        professor_name="Ahmet Yılmaz",
        category="hoca",
        summary="Vize sınavı 18 Mayıs 14:00'te A-301'de yapılacaktır.",
        formatted_notification="Ahmet Yılmaz hoca mail attı: Vize sınavı 18 Mayıs 14:00'te A-301'de yapılacaktır.",
        reason="Sınav duyurusu.",
    )
    ann_decision = SchoolMailDecision(
        is_professor=False,
        professor_name="",
        category="genel_duyuru",
        summary="Yemekhane menüsü güncellendi.",
        formatted_notification="",
        reason="Genel duyuru.",
    )

    record_school_mail_decision(
        ann_decision,
        message_id="m-2",
        sender_name="SKS",
        sender_email="sks@uni.edu.tr",
        subject="Aylık Yemek Menüsü",
    )
    record_school_mail_decision(
        prof_decision,
        message_id="m-1",
        sender_name="Ahmet Yılmaz",
        sender_email="ayilmaz@uni.edu.tr",
        subject="Vize Sınavı",
        attachments=["sinav_listesi.pdf"],
    )

    recent_prof = load_recent_school_mails(limit=5, only_professor=True)
    assert len(recent_prof) == 1
    assert recent_prof[0]["professor_name"] == "Ahmet Yılmaz"

    prof_result = tool.execute(limit=5)
    assert prof_result.success is True
    assert prof_result.metadata["count"] == 1
    assert "Ahmet Yılmaz hoca mail attı:" in prof_result.content
    assert "Yemekhane" not in prof_result.content

    all_result = tool.execute(limit=5, include_announcements=True)
    assert all_result.success is True
    assert all_result.metadata["count"] == 2
    assert "Yemekhane" in all_result.content

    filtered_result = tool.execute(professor_query="Ahmet")
    assert filtered_result.success is True
    assert filtered_result.metadata["count"] == 1


# ---------------------------------------------------------------------------
# Gazi Üniversitesi kuralları, GUZEM/LMS hoca duyuruları ve kişisel adresler
# ---------------------------------------------------------------------------

_GAZI_BLACKLIST = [
    "duyuru@gazi.edu.tr",
    "oidb@gazi.edu.tr",
    "sks@gazi.edu.tr",
    "bidb@gazi.edu.tr",
    "rektorluk@gazi.edu.tr",
    "guzem@gazi.edu.tr",
    "kutuphane@gazi.edu.tr",
    "ogrenciisleri@gazi.edu.tr",
    "noreply@gazi.edu.tr",
    "bilgi@gazi.edu.tr",
    "gaziobs@gazi.edu.tr",
    "basin@gazi.edu.tr",
    "kariyer@gazi.edu.tr",
]


@pytest.mark.parametrize("sender_email", _GAZI_BLACKLIST)
def test_gazi_institutional_blacklist_filters_without_llm(sender_email: str):
    llm_stub, calls = _make_llm_stub({"is_professor": True, "category": "hoca", "summary": "x"})
    rules = SchoolMailRules(
        announcement_blacklist=_GAZI_BLACKLIST,
        school_domains=["gazi.edu.tr"],
    )

    decision = analyze_school_mail(
        sender_name="Gazi Üniversitesi Duyuru",
        sender_email=sender_email,
        subject="2026-2027 Akademik Yılı Genel Bilgilendirme",
        body="Kampüs kartı ve yemekhane başvuruları portal üzerinden açılmıştır.",
        rules=rules,
        llm_callable=llm_stub,
    )

    assert decision.is_professor is False
    assert decision.category == "genel_duyuru"
    assert decision.formatted_notification == ""
    assert calls == []


def test_gazi_guzem_professor_course_announcement_not_dropped():
    llm_stub, calls = _make_llm_stub(
        {
            "is_professor": True,
            "professor_name": "Prof. Dr. Ahmet Yılmaz (GUZEM)",
            "category": "hoca",
            "summary": "BM204 vize telafi sınavı 19 Mayıs Salı 14:00'te B-101 dersliğinde yapılacaktır.",
            "reason": "GUZEM üzerinden ders hocası sınav duyurusu gönderdi.",
        }
    )
    rules = SchoolMailRules(
        announcement_blacklist=_GAZI_BLACKLIST,
        school_domains=["gazi.edu.tr"],
    )

    decision = analyze_school_mail(
        sender_name="Prof. Dr. Ahmet Yılmaz (GUZEM)",
        sender_email="guzem@gazi.edu.tr",
        subject="[BM204] Vize Telafi Sınavı Duyurusu",
        body="Merhaba arkadaşlar, vize telafi sınavı 19 Mayıs Salı 14:00'te B-101 dersliğinde yapılacaktır.",
        headers={"Auto-Submitted": "auto-generated", "Precedence": "bulk"},
        rules=rules,
        llm_callable=llm_stub,
    )

    assert len(calls) == 1
    assert decision.is_professor is True
    assert decision.category == "hoca"
    assert decision.professor_name == "Prof. Dr. Ahmet Yılmaz"
    assert decision.formatted_notification == (
        "Prof. Dr. Ahmet Yılmaz hoca mail attı: BM204 vize telafi sınavı 19 Mayıs Salı 14:00'te B-101 dersliğinde yapılacaktır."
    )


def test_gazi_guzem_professor_fallback_without_api_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    rules = SchoolMailRules(
        announcement_blacklist=_GAZI_BLACKLIST,
        school_domains=["gazi.edu.tr"],
    )

    decision = analyze_school_mail(
        sender_name="Doç. Dr. Ayşe Demir via GUZEM",
        sender_email="guzem@gazi.edu.tr",
        subject="Proje Teslim Tarihi Güncellemesi",
        body="Proje teslimi 28 Mayıs Cuma 23:59'a uzatıldı.",
        headers={"List-Unsubscribe": "<mailto:leave@guzem.gazi.edu.tr>"},
        rules=rules,
    )

    assert decision.is_professor is True
    assert decision.category == "hoca"
    assert decision.professor_name == "Doç. Dr. Ayşe Demir"
    assert decision.formatted_notification.startswith("Doç. Dr. Ayşe Demir hoca mail attı: ")


@pytest.mark.parametrize(
    "sender_name,sender_email,subject,body,expected_name",
    [
        (
            "Öğr. Gör. Caner Kaya",
            "canerkaya@gazi.edu.tr",
            "Lab Telafi Saati",
            "Laboratuvar telafisi Çarşamba 15:00'te Lab-2'de yapılacak.",
            "Öğr. Gör. Caner Kaya",
        ),
        (
            "Arş. Gör. Selin Arslan",
            "selinarslan@gazi.edu.tr",
            "Ödev 3 Puanlama Anahtarı",
            "Ödev 3 için son teslim Cuma 17:00.",
            "Arş. Gör. Selin Arslan",
        ),
        (
            "Kemal Öztürk",
            "kemalozturk@gazi.edu.tr",
            "Ders İptali Hakkında",
            "Bugünkü dersimiz sağlık sebebiyle iptal edilmiştir; telafi haftaya yapılacaktır.",
            "Kemal Öztürk",
        ),
    ],
)
def test_gazi_personal_professor_addresses_caught_in_fallback(
    monkeypatch: pytest.MonkeyPatch,
    sender_name: str,
    sender_email: str,
    subject: str,
    body: str,
    expected_name: str,
):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    rules = SchoolMailRules(
        announcement_blacklist=_GAZI_BLACKLIST,
        school_domains=["gazi.edu.tr"],
    )

    decision = analyze_school_mail(
        sender_name=sender_name,
        sender_email=sender_email,
        subject=subject,
        body=body,
        rules=rules,
    )

    assert decision.is_professor is True
    assert decision.category == "hoca"
    assert decision.professor_name == expected_name
    assert decision.formatted_notification.startswith(f"{expected_name} hoca mail attı: ")

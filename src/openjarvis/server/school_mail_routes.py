"""Mamed okul e-postasi durum, kural yonetimi ve manuel kontrol API uclari."""

from __future__ import annotations

import asyncio
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from openjarvis.intelligence.school_mail_analyzer import (
    SchoolMailRules,
    load_school_mail_rules,
    save_school_mail_rules,
)
from openjarvis.server.school_mail_watcher import (
    SchoolMailConfig,
    check_mailbox_once,
    get_school_mail_status,
    mask_secrets,
)

school_mail_router = APIRouter(
    prefix="/v1/mamed/school-mail",
    tags=["mamed-school-mail"],
)


class SchoolMailRulesUpdate(BaseModel):
    professor_whitelist: Optional[List[str]] = None
    announcement_blacklist: Optional[List[str]] = None
    course_keywords: Optional[List[str]] = None
    school_domains: Optional[List[str]] = None


@school_mail_router.get("/status")
async def school_mail_status():
    """Okul e-posta dinleyici durumunu, son hoca maillerini ve istatistikleri dondurur."""
    return get_school_mail_status()


@school_mail_router.get("/rules")
async def get_school_mail_rules():
    """``school_mail_rules.json`` icindeki filtreleme kurallarini dondurur."""
    return load_school_mail_rules().to_dict()


@school_mail_router.put("/rules")
async def update_school_mail_rules(body: SchoolMailRulesUpdate):
    """Okul e-posta siniflandirma kurallarini gunceller."""
    current = load_school_mail_rules()
    merged = SchoolMailRules.from_dict(
        {
            "professor_whitelist": (
                body.professor_whitelist
                if body.professor_whitelist is not None
                else current.professor_whitelist
            ),
            "announcement_blacklist": (
                body.announcement_blacklist
                if body.announcement_blacklist is not None
                else current.announcement_blacklist
            ),
            "course_keywords": (
                body.course_keywords
                if body.course_keywords is not None
                else current.course_keywords
            ),
            "school_domains": (
                body.school_domains
                if body.school_domains is not None
                else current.school_domains
            ),
        }
    )
    save_school_mail_rules(merged)
    return {"status": "updated", **merged.to_dict()}


@school_mail_router.post("/check-now")
async def check_school_mail_now():
    """Okul posta kutusunu aninda kontrol eder ve sonucu dondurur."""
    cfg = SchoolMailConfig.from_env()
    try:
        result = await asyncio.to_thread(check_mailbox_once, config=cfg)
        return {"status": "ok", **result}
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=mask_secrets(str(exc), cfg.masked_secrets()),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Mamed okul e-posta kontrolü başarısız: {mask_secrets(str(exc), cfg.masked_secrets())}",
        ) from exc


__all__ = ["school_mail_router", "SchoolMailRulesUpdate"]

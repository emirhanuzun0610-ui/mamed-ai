"""Yerel okul e-posta özetlerini okuyan Mamed aracı."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Union

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.intelligence.school_mail_analyzer import (
    get_school_mail_store_path,
    load_recent_school_mails,
)
from openjarvis.security.capabilities import Capability
from openjarvis.tools._stubs import BaseTool, ToolSpec


@ToolRegistry.register("school_mail_recent")
class SchoolMailRecentTool(BaseTool):
    """Son hoca e-postalarını ve okul özetlerini yerel depodan okur."""

    tool_id = "school_mail_recent"
    is_local = True

    def __init__(self, store_path: Optional[Union[Path, str]] = None) -> None:
        self._store_path = Path(store_path).expanduser() if store_path is not None else None

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="school_mail_recent",
            description=(
                "Yerel önbellekten son okul e-postası özetlerini okur. Kullanıcı "
                "'Hocalardan yeni mail var mı?' veya belirli bir ders/hoca hakkında "
                "mail sorduğunda kullanılır."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Döndürülecek en fazla kayıt sayısı (varsayılan 5, 1-50).",
                    },
                    "professor_query": {
                        "type": "string",
                        "description": "İsteğe bağlı hoca adı, ders veya anahtar kelime filtresi.",
                    },
                    "include_announcements": {
                        "type": "boolean",
                        "description": "True ise genel duyuruları da dahil eder (varsayılan False).",
                    },
                },
            },
            category="school_mail",
            required_capabilities=[Capability.FILE_READ.value],
        )

    def execute(self, **params: Any) -> ToolResult:
        raw_limit = params.get("limit", 5)
        try:
            limit = max(1, min(50, int(raw_limit)))
        except (TypeError, ValueError):
            limit = 5

        professor_query = str(params.get("professor_query") or "").strip()
        include_announcements = bool(params.get("include_announcements", False))
        target = self._store_path or get_school_mail_store_path()

        items = load_recent_school_mails(
            limit=limit,
            only_professor=not include_announcements,
            professor_query=professor_query,
            store_path=target,
        )

        if not items:
            if professor_query:
                msg = f"'{professor_query}' ile eşleşen kayıtlı hoca/okul e-postası bulunamadı."
            elif include_announcements:
                msg = "Kayıtlı okul e-postası özeti bulunamadı."
            else:
                msg = "Hocalardan yeni veya kayıtlı e-posta özeti bulunamadı."
            return ToolResult(
                tool_name=self.spec.name,
                success=True,
                content=msg,
                metadata={"count": 0, "items": [], "store_path": str(target)},
            )

        lines: list[str] = []
        for idx, item in enumerate(items, start=1):
            notif = str(item.get("formatted_notification") or "").strip()
            subject = str(item.get("subject") or "").strip()
            received_at = str(item.get("received_at") or "").strip()
            summary = str(item.get("summary") or "").strip()
            category = str(item.get("category") or "genel_duyuru")

            if notif:
                header = notif
            elif subject:
                header = f"[{category}] {subject}: {summary}"
            else:
                header = f"[{category}] {summary}"

            meta_parts = [p for p in (f"Konu: {subject}" if notif and subject else "", received_at) if p]
            if meta_parts:
                lines.append(f"{idx}. {header} ({' | '.join(meta_parts)})")
            else:
                lines.append(f"{idx}. {header}")

        return ToolResult(
            tool_name=self.spec.name,
            success=True,
            content="\n".join(lines),
            metadata={
                "count": len(items),
                "items": items,
                "store_path": str(target),
            },
        )


__all__ = ["SchoolMailRecentTool"]

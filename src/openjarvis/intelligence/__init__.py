"""Intelligence primitive — the model definition and catalog."""

from __future__ import annotations

from openjarvis.intelligence.model_catalog import (
    BUILTIN_MODELS,
    merge_discovered_models,
    register_builtin_models,
    resolve_model_id_for_engine,
)
from openjarvis.intelligence.school_mail_analyzer import (
    SchoolMailDecision,
    SchoolMailRules,
    analyze_school_mail,
    format_professor_notification,
    load_recent_school_mails,
    load_school_mail_rules,
    record_school_mail_decision,
)

__all__ = [
    "BUILTIN_MODELS",
    "SchoolMailDecision",
    "SchoolMailRules",
    "analyze_school_mail",
    "format_professor_notification",
    "load_recent_school_mails",
    "load_school_mail_rules",
    "merge_discovered_models",
    "record_school_mail_decision",
    "register_builtin_models",
    "resolve_model_id_for_engine",
]

"""Mamed bagimsiz okul e-posta IMAP IDLE arka plan dinleyicisi.

Mamed sunucusu veya arayuzu kapaliyken dahi ``imap.gazi.edu.tr:993`` uzerinden
IMAP IDLE (RFC 2177) ile gelen hoca maillerini aninda algilar, Gemini ile
ozetler ve Telegram'a bildirim gonderir.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_DIR = _REPO_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from openjarvis.server.school_mail_watcher import (  # noqa: E402
    ImapIdleWatcher,
    SchoolMailConfig,
    acquire_single_instance_mutex,
    check_mailbox_once,
    get_school_mail_status,
    mask_secrets,
    release_single_instance_mutex,
    setup_watcher_logger,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Mamed okul e-posta IMAP IDLE dinleyicisi")
    parser.add_argument("--once", action="store_true", help="Tek seferlik mail kontrolu yap ve cik")
    parser.add_argument("--status", action="store_true", help="Mevcut durum JSON bilgisini yazdir ve cik")
    parser.add_argument("--no-mutex", action="store_true", help="Tek-ornek mutex kontrolunu atla")
    args = parser.parse_args()

    if args.status:
        print(json.dumps(get_school_mail_status(), ensure_ascii=False, indent=2))
        return 0

    if args.once:
        cfg = SchoolMailConfig.from_env()
        try:
            res = check_mailbox_once(config=cfg)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        except Exception as exc:
            print(f"HATA: {mask_secrets(str(exc), cfg.masked_secrets())}", file=sys.stderr)
            return 1

    mutex_handle = None
    if not args.no_mutex:
        mutex_handle = acquire_single_instance_mutex()
        if mutex_handle is None:
            setup_watcher_logger().info("Okul mail dinleyicisi zaten calisiyor; ikinci ornek sonlandirildi.")
            return 0

    try:
        watcher = ImapIdleWatcher()
        watcher.run_forever()
    finally:
        release_single_instance_mutex(mutex_handle)
    return 0


if __name__ == "__main__":
    sys.exit(main())

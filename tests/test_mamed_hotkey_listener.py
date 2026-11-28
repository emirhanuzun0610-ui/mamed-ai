"""mamed_hotkey_listener birim testleri."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import struct
import sys
import tempfile
import unittest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "mamed_hotkey_listener.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("mamed_hotkey_listener", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mamed_hotkey_listener"] = mod
    spec.loader.exec_module(mod)
    return mod


listener = _load_module()


class TestMamedHotkeyListener(unittest.TestCase):
    def test_parse_hotkey_ctrl_alt_m(self):
        mods, vk = listener.parse_hotkey("ctrl+alt+m")
        self.assertEqual(mods, listener.MOD_CONTROL | listener.MOD_ALT | listener.MOD_NOREPEAT)
        self.assertEqual(vk, ord("M"))

    def test_parse_hotkey_variants_and_invalid(self):
        mods, vk = listener.parse_hotkey("Control + Shift + F9")
        self.assertEqual(mods, listener.MOD_CONTROL | listener.MOD_SHIFT | listener.MOD_NOREPEAT)
        self.assertEqual(vk, 0x78)

        for bad in ["", "m", "ctrl+", "unknown+m", "ctrl+unknownkey"]:
            with self.assertRaises(ValueError):
                listener.parse_hotkey(bad)

    def test_is_mamed_browser_window(self):
        self.assertTrue(listener.is_mamed_browser_window("msedge.exe", "Mamed — Kişisel Yapay Zeka"))
        self.assertTrue(listener.is_mamed_browser_window("chrome", "mamed"))
        self.assertFalse(listener.is_mamed_browser_window("Antigravity.exe", "AJAN MAMED"))
        self.assertFalse(listener.is_mamed_browser_window("powershell.exe", "Mamed"))
        self.assertFalse(listener.is_mamed_browser_window("msedge.exe", "Google Arama"))

    def test_detect_nvidia_shadowplay_conflicts(self):
        mods, vk = listener.parse_hotkey("ctrl+alt+m")
        fake_reg = {
            "IsShadowPlayEnabled": b"\x01\x00\x00\x00",
            "MicToggleHKeyCount": b"\x03\x00\x00\x00",
            "MicToggleHKey0": b"\x11\x00\x00\x00",
            "MicToggleHKey1": b"\x12\x00\x00\x00",
            "MicToggleHKey2": b"\x4d\x00\x00\x00",
            "GFEOverlayHKeyV2Count": b"\x02\x00\x00\x00",
            "GFEOverlayHKeyV20": b"\x12\x00\x00\x00",
            "GFEOverlayHKeyV21": b"\x5a\x00\x00\x00",
        }
        conflicts = listener.detect_nvidia_shadowplay_conflicts(mods, vk, reg_values=fake_reg)
        self.assertEqual(len(conflicts), 1)
        self.assertIn("MicToggleHKey", conflicts[0])
        self.assertIn("Ctrl+Alt+M", conflicts[0])

        fake_reg["IsShadowPlayEnabled"] = b"\x00\x00\x00\x00"
        self.assertEqual(listener.detect_nvidia_shadowplay_conflicts(mods, vk, reg_values=fake_reg), [])

    def test_detect_lnk_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            lnk_file = Path(tmp) / "Test.lnk"
            buf = bytearray(0x4C)
            struct.pack_into("<I", buf, 0, 0x4C)
            struct.pack_into("<H", buf, 0x40, (0x06 << 8) | 0x4D)
            lnk_file.write_bytes(bytes(buf))

            mods, vk = listener.parse_hotkey("ctrl+alt+m")
            conflicts = listener.detect_lnk_conflicts(mods, vk, lnk_paths=[lnk_file])
            self.assertEqual(len(conflicts), 1)
            self.assertIn(str(lnk_file), conflicts[0])

    def test_mask_sensitive_text(self):
        raw = "URL: http://127.0.0.1:8000/#token=secret_abc123 ve OPENJARVIS_API_KEY=xyz999 Bearer tok_123"
        masked = listener.mask_sensitive_text(raw)
        self.assertNotIn("secret_abc123", masked)
        self.assertNotIn("xyz999", masked)
        self.assertNotIn("tok_123", masked)
        self.assertIn("#token=***", masked)


if __name__ == "__main__":
    unittest.main()

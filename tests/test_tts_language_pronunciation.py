"""TTS language code and pronunciation map (2026-11-22): both optional config keys.

Without the keys nothing changes: the ElevenLabs request carries no ``language_code`` and the
spoken text is not rewritten. The pronunciation rule mirrors Inari's ``voice/lib/vyslovnost.mjs``:
a stem matches at the start of a whole word, case-insensitively, and the ending is kept
(Alaignedu -> Elajndu). Only the text sent to TTS changes, never the Telegram message.

Mutations that must turn this red: drop ``language_code`` from the body in tts.synthesize;
drop the leading lookbehind in tts._stem_pattern (``nealaigned`` would be rewritten); drop the
mtime check in PronunciationMap.rules (an edited file would not be picked up).
"""
import json
import logging
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent2telegram import tts
from agent2telegram.config import Config, ConfigError
from tests.test_tts import _Opener
from tests.test_voice import _voice_bridge

RULES = [("Alaigned", "Elajnd")]


def _body(op):
    return json.loads(op.req.data.decode("utf-8"))


class LanguageCodeTests(unittest.TestCase):
    def test_no_language_means_no_language_code(self):
        op = _Opener()
        tts.synthesize("ahoj", api_key="k", voice_id="V", opener=op)
        self.assertNotIn("language_code", _body(op))
        self.assertEqual(set(_body(op)), {"text", "model_id", "voice_settings"})

    def test_language_goes_into_body(self):
        op = _Opener()
        tts.synthesize("ahoj", api_key="k", voice_id="V", opener=op, language_code="cs")
        self.assertEqual(_body(op)["language_code"], "cs")

    def test_long_passes_language_to_every_piece(self):
        with mock.patch.object(tts, "synthesize", return_value=b"mp3") as syn:
            tts.synthesize_long("Jedna věta. Druhá věta.", api_key="k", voice_id="v",
                                max_chars=12, language_code="cs")
        self.assertEqual(syn.call_count, 2)
        for call in syn.call_args_list:
            self.assertEqual(call.kwargs["language_code"], "cs")


class ApplyPronunciationTests(unittest.TestCase):
    def test_all_forms_after_stem_and_any_case(self):
        self.assertEqual(tts.apply_pronunciation("v Alaignedu, s alaignedem, ALAIGNED", RULES),
                         "v Elajndu, s Elajndem, Elajnd")

    def test_whole_word_only_not_inside(self):
        self.assertEqual(tts.apply_pronunciation("nealaigned xAlaigned Alaigned2", RULES),
                         "nealaigned xAlaigned Alaigned2")

    def test_czech_letters_count_as_word(self):
        self.assertEqual(tts.apply_pronunciation("čAlaigned Alaignedům", RULES),
                         "čAlaigned Elajndům")

    def test_no_rules_text_unchanged(self):
        self.assertEqual(tts.apply_pronunciation("Ahoj Jendo, Alaigned.", []),
                         "Ahoj Jendo, Alaigned.")

    def test_stem_with_regex_chars_is_literal(self):
        self.assertEqual(tts.apply_pronunciation("C++ a Cxx", [("C++", "cé plus plus")]),
                         "cé plus plus a Cxx")


class LoadPronunciationTests(unittest.TestCase):
    def _write(self, td, payload):
        p = Path(td) / "v.json"
        p.write_text(payload if isinstance(payload, str) else json.dumps(payload), "utf-8")
        return p

    def test_inari_format_loads(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, {"_poznamka": "x", "kmeny": [{"z": "Alaigned", "na": "Elajnd"},
                                                             {"z": "Jend", "na": "Jend"}]})
            self.assertEqual(tts.load_pronunciation(p),
                             [("Alaigned", "Elajnd"), ("Jend", "Jend")])

    def test_missing_file_empty_and_warns(self):
        with tempfile.TemporaryDirectory() as td, self.assertLogs("agent2telegram.tts", "WARNING"):
            self.assertEqual(tts.load_pronunciation(Path(td) / "neni.json"), [])

    def test_broken_json_empty_and_warns(self):
        with tempfile.TemporaryDirectory() as td, self.assertLogs("agent2telegram.tts", "WARNING"):
            self.assertEqual(tts.load_pronunciation(self._write(td, "{nejde")), [])

    def test_bad_shape_empty_and_warns(self):
        bad = [{"kmeny": [{"z": "", "na": "x"}]}, {"kmeny": "x"}, {"jine": []}, [],
               {"kmeny": [{"z": "a"}]}, {"kmeny": [{"z": "a", "na": "  "}]}]
        with tempfile.TemporaryDirectory() as td:
            for payload in bad:
                with self.subTest(payload=payload), \
                        self.assertLogs("agent2telegram.tts", "WARNING"):
                    self.assertEqual(tts.load_pronunciation(self._write(td, payload)), [])


class PronunciationMapReloadTests(unittest.TestCase):
    def test_edit_is_picked_up_without_restart(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "v.json"
            p.write_text(json.dumps({"kmeny": [{"z": "Alaigned", "na": "Elajnd"}]}), "utf-8")
            m = tts.PronunciationMap()
            self.assertEqual(m.rules(p), [("Alaigned", "Elajnd")])
            p.write_text(json.dumps({"kmeny": [{"z": "Alaigned", "na": "Elejnd"},
                                               {"z": "Jenda", "na": "Jenda"}]}), "utf-8")
            later = time.time() + 5
            os.utime(p, (later, later))
            self.assertEqual(m.rules(p), [("Alaigned", "Elejnd"), ("Jenda", "Jenda")])

    def test_atomic_replace_is_picked_up(self):
        # Same size and forced-equal mtime: only the inode tells the new file apart.
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "v.json"
            p.write_text(json.dumps({"kmeny": [{"z": "Alaigned", "na": "Elajnd"}]}), "utf-8")
            st = p.stat()
            m = tts.PronunciationMap()
            m.rules(p)
            tmp = Path(td) / "v.json.tmp"
            tmp.write_text(json.dumps({"kmeny": [{"z": "Alaigned", "na": "Elejnd"}]}), "utf-8")
            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
            keep = Path(td) / "old"            # hold the old inode so it is not reused
            os.link(p, keep)
            os.replace(tmp, p)
            self.assertEqual(m.rules(p), [("Alaigned", "Elejnd")])

    def test_unchanged_file_is_not_reread(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "v.json"
            p.write_text(json.dumps({"kmeny": [{"z": "A", "na": "B"}]}), "utf-8")
            m = tts.PronunciationMap()
            m.rules(p)
            with mock.patch.object(tts, "load_pronunciation") as load:
                self.assertEqual(m.rules(p), [("A", "B")])
            load.assert_not_called()

    def test_missing_file_warns_once_not_per_reply(self):
        with tempfile.TemporaryDirectory() as td:
            m = tts.PronunciationMap()
            with self.assertLogs("agent2telegram.tts", "WARNING") as cm:
                for _ in range(3):
                    self.assertEqual(m.rules(Path(td) / "neni.json"), [])
            self.assertEqual(len(cm.records), 1)

    def test_no_path_no_rules_no_log(self):
        m = tts.PronunciationMap()
        with self.assertNoLogs("agent2telegram.tts", "WARNING"):
            self.assertEqual(m.rules(""), [])


class ConfigTests(unittest.TestCase):
    def _cfg(self, **kw):
        return Config(agent="claude-code", token="1:abc", **kw)

    def test_defaults_are_off(self):
        c = self._cfg()
        self.assertEqual(c.tts_language, "")
        self.assertEqual(c.tts_pronunciation_file, "")
        c.validate()

    def test_language_is_normalised(self):
        c = self._cfg(tts_language=" CS ")
        c.validate()
        self.assertEqual(c.tts_language, "cs")

    def test_language_must_be_bare_iso_code(self):
        for bad in ("cs-CZ", "čeština", "c", "czech"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                self._cfg(tts_language=bad).validate()

    def test_must_be_strings(self):
        for kw in ({"tts_language": 5}, {"tts_pronunciation_file": ["x"]}):
            with self.subTest(kw=kw), self.assertRaises(ConfigError):
                self._cfg(**kw).validate()


class BridgeVoiceTests(unittest.TestCase):
    """_try_send_voice: map + language reach TTS; the Telegram text itself is untouched."""

    def _run(self, td, **cfg):
        b = _voice_bridge(td, on=True)
        for k, v in cfg.items():
            setattr(b.cfg, k, v)
        captured = {}

        def fake_long(text, **kw):
            captured["text"] = text
            captured["kw"] = kw
            raise tts.TTSError("stop here")          # no ffmpeg / no network needed

        with mock.patch("agent2telegram.attach.shutil.which", return_value="/usr/bin/ffmpeg"), \
                mock.patch.object(tts, "synthesize_long", side_effect=fake_long):
            self.assertFalse(b._try_send_voice("Pracuju pro Alaignedu."))
        return captured

    def test_without_keys_request_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            got = self._run(td)
        self.assertEqual(got["text"], "Pracuju pro Alaignedu.")
        self.assertNotIn("language_code", got["kw"])

    def test_with_keys_map_and_language_applied(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "vyslovnost.json"
            p.write_text(json.dumps({"kmeny": [{"z": "Alaigned", "na": "Elajnd"}]}), "utf-8")
            got = self._run(td, tts_language="cs", tts_pronunciation_file=str(p))
        self.assertEqual(got["text"], "Pracuju pro Elajndu.")
        self.assertEqual(got["kw"]["language_code"], "cs")

    def test_broken_map_file_still_speaks(self):
        with tempfile.TemporaryDirectory() as td:
            got = self._run(td, tts_pronunciation_file=str(Path(td) / "neni.json"))
        self.assertEqual(got["text"], "Pracuju pro Alaignedu.")


if __name__ == "__main__":
    unittest.main()

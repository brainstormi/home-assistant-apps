import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import config, g711  # noqa: E402
from app.audiosocket import FRAME_BYTES, KIND_AUDIO, KIND_UUID, pack_frame, read_frame  # noqa: E402


class G711Tests(unittest.TestCase):
    def test_silence(self):
        self.assertEqual(g711.slin_to_ulaw(b"\x00\x00"), b"\xff")
        self.assertEqual(g711.ulaw_to_slin(b"\xff"), b"\x00\x00")

    def test_roundtrip_error_is_small(self):
        import array

        samples = array.array("h", range(-32768, 32768, 37))
        back = array.array("h")
        back.frombytes(g711.ulaw_to_slin(g711.slin_to_ulaw(samples.tobytes())))
        for original, decoded in zip(samples, back):
            self.assertLessEqual(abs(original - decoded), max(40, abs(original) * 0.07))

    def test_frame_sizes(self):
        self.assertEqual(len(g711.slin_to_ulaw(bytes(FRAME_BYTES))), FRAME_BYTES // 2)
        self.assertEqual(len(g711.ulaw_to_slin(bytes(160))), FRAME_BYTES)


class AudioSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_pack_and_read(self):
        reader = asyncio.StreamReader()
        uuid_bytes = bytes(range(16))
        reader.feed_data(pack_frame(KIND_UUID, uuid_bytes) + pack_frame(KIND_AUDIO, b"\x01\x02" * 160))
        self.assertEqual(await read_frame(reader), (KIND_UUID, uuid_bytes))
        kind, payload = await read_frame(reader)
        self.assertEqual((kind, len(payload)), (KIND_AUDIO, 320))

    async def test_truncated_stream_raises(self):
        reader = asyncio.StreamReader()
        reader.feed_data(b"\x10\x01")
        reader.feed_eof()
        with self.assertRaises(asyncio.IncompleteReadError):
            await read_frame(reader)


class ConfigTests(unittest.TestCase):
    def _load(self, options):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(options, fh)
        try:
            return config.load(fh.name)
        finally:
            os.unlink(fh.name)

    def test_requires_key(self):
        with self.assertRaises(config.ConfigError):
            self._load({"pin": "1234"})

    def test_pin_required_by_default(self):
        # Without a PIN the add-on starts, but nothing can change: query only.
        cfg = self._load({"openai_api_key": "k"})
        self.assertEqual((cfg.pin_book.mode, cfg.pin_book.complete), ("global", False))
        self.assertEqual(cfg.pin_book.for_inbound().state, "locked")
        self.assertIn("ONLY answer questions", cfg.instructions)

    def test_pin_can_be_disabled(self):
        cfg = self._load({"openai_api_key": "k", "pin_enabled": False})
        self.assertEqual(cfg.pin, "")
        # A leftover PIN in the options is ignored when the PIN is disabled.
        cfg = self._load({"openai_api_key": "k", "pin_enabled": False, "pin": "1234"})
        self.assertEqual(cfg.pin, "")
        self.assertNotIn("PIN", cfg.instructions)

    def test_instructions_mention_pin_only_when_enabled(self):
        cfg = self._load({"openai_api_key": "k", "pin": "1234"})
        self.assertIn("verify_pin", cfg.instructions)

    def test_pin_must_be_digits(self):
        cfg = self._load({"openai_api_key": "k", "pin": "12ab"})
        self.assertFalse(cfg.pin_book.complete)

    def test_model_validation(self):
        for ok in ("gpt-realtime-2.1", "gpt-realtime-2.1-mini", "gpt-realtime-2"):
            self.assertEqual(self._load({"openai_api_key": "k", "pin": "1", "model": ok}).model, ok)
        with self.assertRaises(config.ConfigError) as ctx:
            self._load({"openai_api_key": "k", "pin": "1", "model": "gpt-live-1"})
        self.assertIn("GPT-Live", str(ctx.exception))
        with self.assertRaises(config.ConfigError):
            self._load({"openai_api_key": "k", "pin": "1", "model": "gpt-4o"})

    def test_fallback_agent(self):
        cfg = self._load({"openai_api_key": "k", "pin": "1"})
        self.assertEqual(cfg.fallback_agent, "")
        cfg = self._load({"openai_api_key": "k", "pin": "1", "fallback_agent": "conversation.openai_conversation"})
        self.assertEqual(cfg.fallback_agent, "conversation.openai_conversation")

    def test_defaults_and_instructions(self):
        cfg = self._load({"openai_api_key": "k", "pin": "1234", "assistant_name": "Friday", "language": "es"})
        self.assertEqual(cfg.model, "gpt-realtime-2.1")
        self.assertEqual(cfg.bind_address, "127.0.0.1")
        self.assertIn("Friday", cfg.instructions)
        self.assertIn("always speak Spanish", cfg.instructions)
        self.assertIn('"es"', cfg.instructions)

    def test_language_names(self):
        self.assertEqual(config.language_name("es"), "Spanish")
        self.assertEqual(config.language_name("es-ES"), "Spanish")
        self.assertEqual(config.language_name("pt_BR"), "Portuguese")
        self.assertEqual(config.language_name("xx"), "xx")  # unknown codes pass through

    def test_accent_is_optional(self):
        cfg = self._load({"openai_api_key": "k", "pin": "1"})
        self.assertNotIn("Accent", cfg.instructions)
        cfg = self._load({"openai_api_key": "k", "pin": "1", "language": "es",
                          "accent": "Spanish from Spain (Castilian)"})
        self.assertIn("Accent and dialect: Spanish from Spain (Castilian)", cfg.instructions)

    # ---- configurable prompt -------------------------------------------------
    def test_empty_prompt_uses_the_built_in_template(self):
        cfg = self._load({"openai_api_key": "k", "pin": "1", "prompt": ""})
        self.assertEqual(cfg.instructions, self._load({"openai_api_key": "k", "pin": "1"}).instructions)
        self.assertIn("verify_pin", cfg.instructions)

    def test_custom_prompt_uses_placeholders_from_other_options(self):
        cfg = self._load({
            "openai_api_key": "k", "pin": "1", "language": "es", "assistant_name": "Friday",
            "accent": "Castilian",
            "prompt": "Hi, I am {assistant_name}. Speak {language_name} ({language}).\n{accent_line}\n{pin_rules}\nBye.",
        })
        text = cfg.instructions
        self.assertTrue(text.startswith("Hi, I am Friday. Speak Spanish (es)."))
        self.assertIn("Accent and dialect: Castilian", text)
        self.assertIn("verify_pin", text)
        self.assertNotIn("{", text)

    def test_block_placeholders_vanish_when_not_applicable(self):
        cfg = self._load({
            "openai_api_key": "k", "pin_enabled": False,
            "prompt": "Line one.\n{accent_line}\n{pin_rules}\nLine two.",
        })
        self.assertEqual(cfg.instructions, "Line one.\nLine two.\n")

    def test_unknown_placeholders_and_braces_are_kept(self):
        cfg = self._load({"openai_api_key": "k", "pin": "1", "prompt": 'Use {unknown} and {"a": 1}.'})
        self.assertEqual(cfg.instructions, 'Use {unknown} and {"a": 1}.\n')

    def test_literal_backslash_n_becomes_a_newline(self):
        # The options form can be a single-line field.
        cfg = self._load({"openai_api_key": "k", "pin": "1", "prompt": "One.\\nTwo."})
        self.assertEqual(cfg.instructions, "One.\nTwo.\n")

    def test_extra_instructions_are_appended_after_the_prompt(self):
        cfg = self._load({"openai_api_key": "k", "pin": "1", "prompt": "Main.", "extra_instructions": "Extra."})
        self.assertEqual(cfg.instructions, "Main.\n\nAdditional instructions:\nExtra.\n")

    def test_manifest_default_prompt_matches_the_built_in_template(self):
        """The template pre-filled in config.yaml must not drift from the code."""
        path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        start = lines.index("  prompt: |") + 1
        block = []
        for line in lines[start:]:
            if line.startswith("    "):
                block.append(line[4:])
            elif line == "":
                block.append("")
            else:
                break
        self.assertEqual("\n".join(block).rstrip("\n"), config.DEFAULT_PROMPT.rstrip("\n"))

    def test_bind_address_is_configurable(self):
        cfg = self._load({"openai_api_key": "k", "pin": "1", "bind_address": "0.0.0.0"})
        self.assertEqual(cfg.bind_address, "0.0.0.0")


if __name__ == "__main__":
    unittest.main()


def test_extract_digits_handles_words_and_numerals():
    from app.session import _extract_digits

    assert _extract_digits("561 342") == "561342"
    assert _extract_digits("cinco seis uno, 3 4 2") == "561342"
    assert _extract_digits("five six one") == "561"

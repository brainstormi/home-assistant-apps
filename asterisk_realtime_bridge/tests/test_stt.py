import array
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.audiosocket import FRAME_BYTES, KIND_AUDIO, KIND_HANGUP, pack_frame  # noqa: E402
from app.stt import Segmenter, SttTest, rms, upsample  # noqa: E402

from tests.test_session import FakeWriter  # noqa: E402


def frame(value):
    return array.array("h", [value, -value] * (FRAME_BYTES // 4)).tobytes()


LOUD, QUIET = frame(3000), frame(20)


class SegmenterTests(unittest.TestCase):
    def test_utterance_is_cut_after_silence(self):
        seg = Segmenter()
        out = [seg.feed(QUIET) for _ in range(20)]
        out += [seg.feed(LOUD) for _ in range(30)]
        self.assertTrue(seg.speaking)
        out += [seg.feed(QUIET) for _ in range(40)]
        utterances = [o for o in out if o]
        self.assertEqual(len(utterances), 1)
        frames = len(utterances[0]) // FRAME_BYTES
        self.assertGreaterEqual(frames, 30, "the speech and its pre-roll")
        self.assertFalse(seg.speaking)

    def test_clicks_are_ignored(self):
        seg = Segmenter()
        out = [seg.feed(LOUD if i % 10 == 0 else QUIET) for i in range(200)]
        self.assertEqual([o for o in out if o], [])

    def test_upsample_and_rms(self):
        pcm = array.array("h", [0, 100]).tobytes()
        self.assertEqual(array.array("h", upsample(pcm)).tolist(), [0, 50, 100, 100])
        self.assertAlmostEqual(rms(frame(1000)), 1000, delta=1)


class FakeHA:
    def __init__(self):
        self.calls = []

    async def stt(self, engine_id, wav16k, language):
        self.calls.append((engine_id, wav16k[:4], language))
        return "enciende la luz del sofá"


class FakeVoice:
    def __init__(self):
        self.said = []

    async def audio(self, text):
        self.said.append(text)
        return bytes(FRAME_BYTES * 2)


class SttTestTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_call_is_transcribed_and_read_back(self):
        ha, voice = FakeHA(), FakeVoice()
        test = SttTest(ha, "stt.faster_whisper", "es", voice)
        self.assertFalse(test.take(), "not armed")
        test.arm()
        self.assertTrue(test.take())
        self.assertFalse(test.take(), "only the next call")

        reader = asyncio.StreamReader()
        run = asyncio.create_task(test.run("t1", reader, FakeWriter()))
        await asyncio.sleep(0.15)  # the greeting plays
        reader.feed_data(b"".join(pack_frame(KIND_AUDIO, f) for f in [QUIET] * 10 + [LOUD] * 25 + [QUIET] * 40))
        await asyncio.sleep(0.3)
        reader.feed_data(pack_frame(KIND_HANGUP))
        await asyncio.wait_for(run, 5)

        self.assertEqual(ha.calls[0][0], "stt.faster_whisper")
        self.assertEqual(ha.calls[0][1], b"RIFF")
        self.assertEqual(ha.calls[0][2], "es")
        self.assertEqual(test.results[0]["text"], "enciende la luz del sofá")
        self.assertIn("He entendido: enciende la luz del sofá", voice.said)


if __name__ == "__main__":
    unittest.main()

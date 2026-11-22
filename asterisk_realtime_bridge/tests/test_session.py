import asyncio
import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.audiosocket import (  # noqa: E402
    FRAME_BYTES, KIND_AUDIO, KIND_DTMF, KIND_HANGUP, pack_frame,
)
from app.config import Config  # noqa: E402
from app.session import CallSession, build_session_update, keepalive  # noqa: E402


class FakeWS:
    def __init__(self):
        self.sent = []
        self.inbox = asyncio.Queue()

    async def send(self, message):
        self.sent.append(json.loads(message))

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.inbox.get()
        if item is None:
            raise StopAsyncIteration
        return item

    def push(self, event):
        self.inbox.put_nowait(json.dumps(event))

    def of_type(self, kind):
        return [e for e in self.sent if e.get("type") == kind]

    def tool_outputs(self):
        outs = []
        for e in self.of_type("conversation.item.create"):
            item = e["item"]
            if item["type"] == "function_call_output":
                outs.append(json.loads(item["output"]))
        return outs


class FakeWriter:
    def __init__(self):
        self.chunks = []

    def write(self, data):
        self.chunks.append(data)

    async def drain(self):
        pass

    def audio_frames(self):
        """All audio frames written to Asterisk (speech and silence)."""
        return [c for c in self.chunks if c[0] == KIND_AUDIO]

    def speech_frames(self):
        """Only frames carrying actual audio (not digital silence)."""
        return [c for c in self.audio_frames() if any(c[3:])]


class FakeHome:
    def __init__(self):
        self.calls = []

    async def ask(self, text):
        self.calls.append(text)
        return {"type": "action_done", "response": "Done"}


def call(name, args, call_id):
    return {
        "type": "response.done",
        "response": {"output": [{"type": "function_call", "name": name, "call_id": call_id,
                                 "arguments": json.dumps(args)}]},
    }


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cfg = Config(openai_api_key="k", pin="1234", max_call_seconds=5)
        self.ws, self.writer, self.home = FakeWS(), FakeWriter(), FakeHome()
        self.reader = asyncio.StreamReader()
        self.session = CallSession(self.cfg, "test", self.reader, self.writer, self.ws, self.home)
        self.task = asyncio.create_task(self.session.run())
        await asyncio.sleep(0.01)

    async def asyncTearDown(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    async def ready(self):
        self.ws.push({"type": "session.updated"})
        await asyncio.sleep(0.02)

    async def test_greeting_states_the_configured_language(self):
        cfg = Config(openai_api_key="k", pin="1234", language="es", max_call_seconds=5)
        ws = FakeWS()
        task = asyncio.create_task(
            CallSession(cfg, "lang", asyncio.StreamReader(), FakeWriter(), ws, FakeHome()).run()
        )
        await asyncio.sleep(0.01)
        ws.push({"type": "session.updated"})
        await asyncio.sleep(0.02)
        hint = ws.of_type("response.create")[0]["response"]["instructions"]
        self.assertIn("Spanish", hint)
        self.assertIn("PIN", hint)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_session_update_shape(self):
        update = self.ws.sent[0]["session"]
        self.assertEqual(update["type"], "realtime")
        self.assertEqual(update["audio"]["input"]["format"], {"type": "audio/pcmu"})
        self.assertTrue(update["audio"]["input"]["turn_detection"]["interrupt_response"])
        names = [t["name"] for t in update["tools"]]
        self.assertEqual(names, ["verify_pin", "home", "hang_up"])
        no_pin = build_session_update(Config(openai_api_key="k", pin_enabled=False))
        self.assertNotIn("verify_pin", [t["name"] for t in no_pin["session"]["tools"]])

    async def test_greeting_and_uplink(self):
        self.reader.feed_data(pack_frame(KIND_AUDIO, bytes(FRAME_BYTES)))  # before the session is ready: dropped
        await asyncio.sleep(0.02)
        self.assertEqual(self.ws.of_type("input_audio_buffer.append"), [])
        await self.ready()
        self.assertEqual(len(self.ws.of_type("response.create")), 1)
        self.reader.feed_data(pack_frame(KIND_AUDIO, bytes(FRAME_BYTES)))
        await asyncio.sleep(0.02)
        appended = self.ws.of_type("input_audio_buffer.append")
        self.assertEqual(len(appended), 1)
        self.assertEqual(len(base64.b64decode(appended[0]["audio"])), FRAME_BYTES // 2)

    async def test_downlink_is_paced_and_barge_in_truncates(self):
        await self.ready()
        ulaw = b"\xd0" * 4000  # 500 ms of audio
        self.ws.push({"type": "response.output_audio.delta", "item_id": "item1",
                      "delta": base64.b64encode(ulaw).decode()})
        await asyncio.sleep(0.011)
        early = len(self.writer.speech_frames())
        await asyncio.sleep(0.12)
        played = len(self.writer.speech_frames())
        self.assertGreater(played, early)
        self.assertLess(played, 25, "audio must be paced in real time, not sent in a burst")

        self.ws.push({"type": "input_audio_buffer.speech_started"})
        await asyncio.sleep(0.03)
        count = len(self.writer.speech_frames())
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.writer.speech_frames()), count, "no more speech after a barge-in")

        trunc = self.ws.of_type("conversation.item.truncate")
        self.assertEqual(len(trunc), 1)
        self.assertEqual(trunc[0]["item_id"], "item1")
        self.assertGreaterEqual(trunc[0]["audio_end_ms"], 20)
        self.assertLessEqual(trunc[0]["audio_end_ms"], 25 * 20)

    async def test_short_audio_waits_for_the_buffer_or_the_end_of_the_item(self):
        await self.ready()
        ulaw = b"\xd0" * 800  # 100 ms: less than the prebuffer
        self.ws.push({"type": "response.output_audio.delta", "item_id": "short",
                      "delta": base64.b64encode(ulaw).decode()})
        await asyncio.sleep(0.08)
        self.assertEqual(self.writer.speech_frames(), [], "held until enough audio is queued")
        self.ws.push({"type": "response.output_audio.done", "item_id": "short"})
        await asyncio.sleep(0.2)
        self.assertEqual(len(self.writer.speech_frames()), 5, "the whole item plays once complete")
        self.assertEqual(self.session.underruns, 0)

    async def test_late_audio_is_counted_as_an_underrun_and_rebuffered(self):
        await self.ready()
        ulaw = b"\xd0" * 1600  # 200 ms: exactly the prebuffer
        self.ws.push({"type": "response.output_audio.delta", "item_id": "slow",
                      "delta": base64.b64encode(ulaw).decode()})
        await asyncio.sleep(0.35)
        self.assertEqual(len(self.writer.speech_frames()), 10)
        self.assertEqual(self.session.underruns, 1, "the item was not complete when the queue ran dry")
        # More audio, still below the prebuffer: held until the item completes.
        self.ws.push({"type": "response.output_audio.delta", "item_id": "slow",
                      "delta": base64.b64encode(b"\xd0" * 400).decode()})
        await asyncio.sleep(0.08)
        self.assertEqual(len(self.writer.speech_frames()), 10)
        self.ws.push({"type": "response.output_audio.done", "item_id": "slow"})
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.writer.speech_frames()), 13)
        self.assertEqual(self.session.underruns, 1)

    async def test_silence_is_sent_continuously_even_before_the_session_is_ready(self):
        # Asterisk hangs up after 2000 ms without activity on the AudioSocket
        # connection, so media must flow from the very first moment.
        await asyncio.sleep(0.1)
        frames = self.writer.audio_frames()
        self.assertGreaterEqual(len(frames), 3)
        self.assertEqual(self.writer.speech_frames(), [])
        self.assertTrue(all(len(f) == 3 + FRAME_BYTES for f in frames))

    async def test_keepalive_sends_silence_until_cancelled(self):
        writer = FakeWriter()
        task = asyncio.create_task(keepalive(writer))
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        count = len(writer.audio_frames())
        self.assertGreaterEqual(count, 3)
        self.assertEqual(writer.speech_frames(), [])
        await asyncio.sleep(0.05)
        self.assertEqual(len(writer.audio_frames()), count)

    async def test_no_truncate_when_nothing_pending(self):
        await self.ready()
        self.ws.push({"type": "input_audio_buffer.speech_started"})
        await asyncio.sleep(0.02)
        self.assertEqual(self.ws.of_type("conversation.item.truncate"), [])

    async def test_home_tool_is_blocked_until_pin(self):
        await self.ready()
        self.ws.push(call("home", {"request": "turn on the light"}, "c1"))
        await asyncio.sleep(0.05)
        self.assertEqual(self.home.calls, [])
        self.assertFalse(self.ws.tool_outputs()[0]["ok"])

        self.ws.push(call("verify_pin", {"pin": "0000"}, "c2"))
        await asyncio.sleep(0.05)
        self.assertFalse(self.ws.tool_outputs()[1]["ok"])

        self.ws.push(call("verify_pin", {"pin": "1 2 3 4"}, "c3"))
        await asyncio.sleep(0.05)
        self.assertTrue(self.ws.tool_outputs()[2]["ok"])

        self.ws.push(call("home", {"request": "turn on the living room light"}, "c4"))
        await asyncio.sleep(0.05)
        self.assertEqual(self.home.calls, ["turn on the living room light"])
        out = self.ws.tool_outputs()[3]
        self.assertTrue(out["ok"])
        self.assertEqual(out["response"], "Done")
        self.assertGreaterEqual(len(self.ws.of_type("response.create")), 2)

    async def test_duplicate_call_ids_run_once(self):
        await self.ready()
        self.ws.push(call("verify_pin", {"pin": "1234"}, "dup"))
        self.ws.push(call("verify_pin", {"pin": "1234"}, "dup"))
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.ws.tool_outputs()), 1)

    async def test_lockout_after_max_attempts_hangs_up(self):
        await self.ready()
        for i in range(3):
            self.ws.push(call("verify_pin", {"pin": "9999"}, f"x{i}"))
            await asyncio.sleep(0.03)
        self.ws.push({"type": "response.done", "response": {"output": []}})  # the farewell response
        await asyncio.wait_for(self.task, 3)
        self.assertEqual(self.writer.chunks[-1][0], KIND_HANGUP)
        self.assertEqual(self.home.calls, [])

    async def test_dtmf_pin(self):
        await self.ready()
        for digit in "1234":
            self.reader.feed_data(pack_frame(KIND_DTMF, digit.encode()))
        await asyncio.sleep(0.05)
        notes = [e for e in self.ws.of_type("conversation.item.create") if e["item"]["type"] == "message"]
        self.assertEqual(len(notes), 1)
        self.ws.push(call("home", {"request": "hello"}, "c9"))
        await asyncio.sleep(0.05)
        self.assertEqual(self.home.calls, ["hello"])

    async def test_hang_up_ends_call(self):
        await self.ready()
        self.ws.push(call("hang_up", {}, "bye"))
        await asyncio.wait_for(self.task, 3)
        self.assertEqual(self.writer.chunks[-1][0], KIND_HANGUP)

    async def test_asterisk_hangup_ends_session(self):
        await self.ready()
        self.reader.feed_data(pack_frame(KIND_HANGUP))
        await asyncio.wait_for(self.task, 2)

    async def test_ready_timeout_aborts(self):
        import app.session as s
        original = s.READY_TIMEOUT_S
        s.READY_TIMEOUT_S = 0.05
        try:
            ws, writer = FakeWS(), FakeWriter()
            session = CallSession(self.cfg, "t2", asyncio.StreamReader(), writer, ws, self.home)
            await asyncio.wait_for(session.run(), 2)
        finally:
            s.READY_TIMEOUT_S = original
        self.assertEqual(writer.chunks[-1][0], KIND_HANGUP)


if __name__ == "__main__":
    unittest.main()

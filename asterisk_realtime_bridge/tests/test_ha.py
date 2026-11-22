import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# aiohttp is not needed to test the agent logic: use a minimal stand-in.
fake_aiohttp = types.ModuleType("aiohttp")


class ClientConnectorError(Exception):
    pass


fake_aiohttp.ClientConnectorError = ClientConnectorError
fake_aiohttp.ClientResponseError = type("ClientResponseError", (Exception,), {})
fake_aiohttp.ClientTimeout = lambda total=None: ("timeout", total)
fake_aiohttp.ClientSession = object
sys.modules["aiohttp"] = fake_aiohttp

from app.ha import HomeAssistant, HomeAssistantError  # noqa: E402


class FakeResponse:
    def __init__(self, data, status=200, text=""):
        self._data = data
        self.status = status
        self._text = text

    async def text(self):
        return self._text

    async def read(self):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    async def json(self):
        return self._data


class FakeHttp:
    """Answers per agent_id; URLs listed in `down` are unreachable."""

    def __init__(self, replies, down=()):
        self.replies = replies
        self.down = set(down)
        self.requests = []

    def request(self, method, url, json, headers, timeout):
        assert method == "POST"
        self.requests.append((url, json["agent_id"], headers["Authorization"]))
        base = url.rsplit("/conversation/process", 1)[0]
        if base in self.down:
            raise ClientConnectorError()
        return FakeResponse(self.replies[json["agent_id"]])


def reply(kind, speech):
    return {"response": {"response_type": kind, "speech": {"plain": {"speech": speech}}}}


class HomeAssistantTests(unittest.IsolatedAsyncioTestCase):
    async def test_primary_agent_answer(self):
        http = FakeHttp({"conversation.home_assistant": reply("action_done", "Done")})
        ha = HomeAssistant(http, ("http://a",), "tok", "conversation.home_assistant", "en")
        self.assertEqual(await ha.ask("turn on the light"), {"type": "action_done", "response": "Done"})
        self.assertEqual(http.requests[0][2], "Bearer tok")

    async def test_fallback_only_when_primary_errors(self):
        replies = {
            "conversation.home_assistant": reply("error", "I did not understand"),
            "conversation.openai_conversation": reply("action_done", "Ready"),
        }
        http = FakeHttp(replies)
        ha = HomeAssistant(http, ("http://a",), "t", "conversation.home_assistant", "en",
                           "conversation.openai_conversation")
        self.assertEqual((await ha.ask("set the movie scene"))["response"], "Ready")
        self.assertEqual([r[1] for r in http.requests],
                         ["conversation.home_assistant", "conversation.openai_conversation"])

        http = FakeHttp({**replies, "conversation.home_assistant": reply("action_done", "Done")})
        ha = HomeAssistant(http, ("http://a",), "t", "conversation.home_assistant", "en",
                           "conversation.openai_conversation")
        await ha.ask("turn on the light")
        self.assertEqual([r[1] for r in http.requests], ["conversation.home_assistant"])

    async def test_no_fallback_configured_returns_error_result(self):
        http = FakeHttp({"conversation.home_assistant": reply("error", "No match")})
        ha = HomeAssistant(http, ("http://a",), "t", "conversation.home_assistant", "en")
        self.assertEqual((await ha.ask("x"))["type"], "error")

    async def test_falls_back_to_second_url_and_remembers_it(self):
        http = FakeHttp({"a": reply("action_done", "ok")}, down={"http://supervisor"})
        ha = HomeAssistant(http, ("http://supervisor", "http://172.30.32.2"), "t", "a", "en")
        await ha.ask("one")
        await ha.ask("two")
        urls = [r[0] for r in http.requests]
        self.assertEqual(urls[0], "http://supervisor/conversation/process")
        self.assertEqual(urls[1], "http://172.30.32.2/conversation/process")
        self.assertEqual(urls[2], "http://172.30.32.2/conversation/process")  # no retry of the dead URL

    async def test_all_urls_down_raises(self):
        http = FakeHttp({}, down={"http://a"})
        ha = HomeAssistant(http, ("http://a",), "t", "x", "en")
        with self.assertRaises(ConnectionError):
            await ha.ask("x")


class ErrorAndTTSTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_errors_never_carry_the_token(self):
        class Http:
            def request(self, method, url, json, headers, timeout):
                return FakeResponse({}, status=500, text="Internal Server Error")

        ha = HomeAssistant(Http(), ("http://a",), "SECRET-TOKEN", "agent", "es")
        with self.assertRaises(HomeAssistantError) as ctx:
            await ha.ask("hola")
        self.assertEqual(ctx.exception.status, 500)
        self.assertNotIn("SECRET-TOKEN", repr(ctx.exception) + str(ctx.exception))

    async def test_tts_retries_without_an_unsupported_language(self):
        calls = []

        class Http:
            def request(self, method, url, json, headers, timeout):
                calls.append((method, url, dict(json or {})))
                if url.endswith("/tts_get_url"):
                    if "language" in json:
                        return FakeResponse({}, status=500, text="Language es is not supported")
                    return FakeResponse({"path": "/api/tts_proxy/abc.wav"})
                return FakeResponse(b"RIFF...")

        ha = HomeAssistant(Http(), ("http://a",), "tok", "agent", "es")
        audio = await ha.tts_audio("tts.piper", "Hola", "es", "es_ES-davefx-medium")
        self.assertEqual(audio, b"RIFF...")
        self.assertEqual(calls[0][2]["language"], "es")
        self.assertNotIn("language", calls[1][2])
        self.assertEqual(calls[1][2]["options"]["voice"], "es_ES-davefx-medium")
        self.assertEqual(calls[1][2]["options"]["preferred_sample_rate"], 8000)
        self.assertEqual(calls[2][:2], ("GET", "http://a/tts_proxy/abc.wav"))




if __name__ == "__main__":
    unittest.main()

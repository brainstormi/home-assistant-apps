"""Minimal Home Assistant client (REST and WebSocket APIs through the Supervisor proxy)."""

from __future__ import annotations

import itertools
import logging
from typing import Any

import aiohttp

log = logging.getLogger(__name__)


class HomeAssistantError(Exception):
    """An HTTP error from Home Assistant.

    aiohttp's own exception includes the request headers in its text, and so
    the Supervisor token: never let it reach the log.
    """

    def __init__(self, status: int, path: str, detail: str = "") -> None:
        super().__init__(f"HTTP {status} from {path}" + (f": {detail}" if detail else ""))
        self.status = status


class HomeAssistant:
    """Sends natural-language requests to a Home Assistant conversation agent.

    The built-in Assist agent only acts on the entities exposed to Assist,
    which is exactly the safety boundary we want.
    """

    def __init__(
        self,
        http: aiohttp.ClientSession,
        urls: tuple[str, ...],
        token: str,
        agent_id: str,
        language: str,
        fallback_agent: str = "",
    ) -> None:
        self._http = http
        self._urls = list(urls)
        self._token = token
        self._agent_id = agent_id
        self._fallback_agent = fallback_agent
        self._language = language
        self._ids = itertools.count(1)

    async def ask(self, text: str) -> dict[str, Any]:
        """Ask the main agent first; if it cannot handle the request and a
        fallback agent is configured (e.g. an LLM), hand the request to it."""
        result = await self._converse(self._agent_id, text)
        if result["type"] == "error" and self._fallback_agent:
            log.info(
                "Agent %s could not handle the request; trying %s",
                self._agent_id,
                self._fallback_agent,
            )
            result = await self._converse(self._fallback_agent, text)
        return result

    async def converse(self, agent_id: str, text: str) -> dict[str, Any]:
        """Ask one specific agent (no fallback)."""
        return await self._converse(agent_id, text)

    async def _converse(self, agent_id: str, text: str) -> dict[str, Any]:
        payload = {"text": text, "language": self._language, "agent_id": agent_id}
        data = await self._post("conversation/process", payload)
        response = data.get("response") or {}
        speech = ((response.get("speech") or {}).get("plain") or {}).get("speech")
        return {
            "type": response.get("response_type", "unknown"),
            "response": speech or "",
        }

    async def fire_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Fire an event on the Home Assistant bus (call results for automations)."""
        await self._post(f"events/{event_type}", data)

    async def get(self, path: str) -> Any:
        """GET a REST endpoint (for example ``states``)."""
        return await self._request("GET", path)

    async def render(self, template: str) -> str:
        """Render a template in Home Assistant (its timezone, its functions)."""
        return await self._request("POST", "template", {"template": template}, kind="text")

    async def stt(self, engine_id: str, wav16k: bytes, language: str) -> str:
        """Transcribe a 16 kHz mono WAV with a Home Assistant STT engine (e.g. Whisper)."""
        header = (
            "format=wav; codec=pcm; sample_rate=16000; bit_rate=16; channel=1; "
            f"language={language}"
        )
        data = await self._request(
            "POST", f"stt/{engine_id}", raw=wav16k, extra_headers={"X-Speech-Content": header}
        )
        if not isinstance(data, dict) or data.get("result") != "success":
            raise HomeAssistantError(200, f"stt/{engine_id}", str(data)[:200])
        return str(data.get("text") or "").strip()

    async def set_state(self, entity_id: str, state: str, attributes: dict[str, Any]) -> None:
        """Create or update an entity state (lives until Home Assistant restarts)."""
        await self._request("POST", f"states/{entity_id}", {"state": state, "attributes": attributes})

    async def tts_audio(
        self, engine_id: str, message: str, language: str = "", voice: str = ""
    ) -> bytes:
        """Synthesize speech with a Home Assistant TTS engine, as an 8 kHz mono WAV.

        Home Assistant converts the audio itself (preferred_* options), so the
        bridge only has to strip the WAV header.
        """
        options: dict[str, Any] = {
            "preferred_format": "wav",
            "preferred_sample_rate": 8000,
            "preferred_sample_channels": 1,
            "preferred_sample_bytes": 2,
        }
        if voice:
            options["voice"] = voice
        payload: dict[str, Any] = {"engine_id": engine_id, "message": message, "cache": True,
                                   "options": options}
        if language:
            payload["language"] = language
        try:
            data = await self._request("POST", "tts_get_url", payload)
        except HomeAssistantError:
            if not language:
                raise
            # The engine may not know this language code (Piper wants "es_ES",
            # not "es"; Home Assistant answers 500): use the engine's default.
            payload.pop("language")
            data = await self._request("POST", "tts_get_url", payload)
        path = str((data or {}).get("path") or "")
        if path.startswith("/api/"):
            path = path[len("/api/"):]
        if not path:
            raise RuntimeError(f"tts_get_url returned no path: {data}")
        return await self._request("GET", path, kind="bytes")

    async def ws(self, command: str, **kwargs: Any) -> Any:
        """Run one WebSocket command (registries and exposure are only there)."""
        last_error: Exception | None = None
        for base in list(self._urls):
            url = base.replace("http://", "ws://").replace("https://", "wss://")
            url = url[: -len("/api")] + "/websocket" if url.endswith("/api") else url + "/websocket"
            try:
                async with self._http.ws_connect(url) as sock:
                    hello = await sock.receive_json(timeout=10)
                    if hello.get("type") == "auth_required":
                        await sock.send_json({"type": "auth", "access_token": self._token})
                        auth = await sock.receive_json(timeout=10)
                        if auth.get("type") != "auth_ok":
                            raise ConnectionError(f"Home Assistant rejected the token: {auth}")
                    msg_id = next(self._ids)
                    await sock.send_json({"id": msg_id, "type": command, **kwargs})
                    while True:
                        reply = await sock.receive_json(timeout=20)
                        if reply.get("id") == msg_id and reply.get("type") == "result":
                            break
                if not reply.get("success"):
                    raise RuntimeError(f"{command} failed: {reply.get('error')}")
                return reply.get("result")
            except aiohttp.ClientConnectorError as err:
                last_error = err
                continue
            except aiohttp.ClientResponseError as err:  # its text carries the token
                raise HomeAssistantError(err.status, command) from None
        raise ConnectionError(f"Could not reach Home Assistant: {last_error}")

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = await self._request("POST", path, payload)
        return data if isinstance(data, dict) else {}

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        kind: str = "json",
        raw: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        headers = {"Authorization": f"Bearer {self._token}", **(extra_headers or {})}
        timeout = aiohttp.ClientTimeout(total=30)
        last_error: Exception | None = None
        body: dict[str, Any] = {"data": raw} if raw is not None else {"json": payload}

        for base in list(self._urls):
            try:
                async with self._http.request(
                    method,
                    f"{base}/{path}",
                    headers=headers,
                    timeout=timeout,
                    **body,
                ) as resp:
                    if resp.status >= 400:
                        detail = (await resp.text())[:200].strip()
                        raise HomeAssistantError(resp.status, path, detail)
                    if kind == "text":
                        data = await resp.text()
                    elif kind == "bytes":
                        data = await resp.read()
                    else:
                        data = await resp.json()
            except aiohttp.ClientConnectorError as err:
                # No route to this URL: try the next one.
                last_error = err
                continue
            except aiohttp.ClientResponseError as err:  # e.g. not JSON; its text carries the token
                raise HomeAssistantError(err.status, path) from None
            # Remember the URL that works for the following requests.
            self._urls.remove(base)
            self._urls.insert(0, base)
            return data
        raise ConnectionError(f"Could not reach Home Assistant: {last_error}")

import json
import httpx
from typing import AsyncIterator
from config import OPENCODE_URL

ZEN_MODELS_URL = "https://opencode.ai/zen/v1/models"


class OpenCodeClient:
    def __init__(self, base_url: str = OPENCODE_URL):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=None)

    async def close(self):
        await self._client.aclose()

    async def health(self) -> bool:
        try:
            r = await self._client.get(f"{self.base_url}/app")
            return r.status_code == 200
        except Exception:
            return False

    async def fetch_free_zen_models(self) -> list[str]:
        try:
            r = await self._client.get(ZEN_MODELS_URL, timeout=10.0)
            r.raise_for_status()
            data = r.json()
            ids = [m["id"] for m in data.get("data", [])]
            free = [i for i in ids if "-free" in i or i == "big-pickle"]
            return [f"opencode/{i}" for i in sorted(free)]
        except Exception:
            return []

    async def create_session(self, title: str = "tg") -> str:
        r = await self._client.post(f"{self.base_url}/session", json={"title": title})
        r.raise_for_status()
        return r.json()["id"]

    async def list_sessions(self) -> list[dict]:
        r = await self._client.get(f"{self.base_url}/session")
        r.raise_for_status()
        return r.json()

    async def send_message(self, session_id: str, text: str, model: str | None = None) -> AsyncIterator[dict]:
        payload = {"parts": [{"type": "text", "text": text}]}
        if model:
            provider_id, model_id = model.split("/", 1)
            payload["model"] = {"providerID": provider_id, "modelID": model_id}

        async with self._client.stream("POST", f"{self.base_url}/session/{session_id}/message", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                if line.startswith("data: "):
                    line = line[6:]
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    async def abort(self, session_id: str) -> None:
        await self._client.post(f"{self.base_url}/session/{session_id}/abort")


client = OpenCodeClient()

import json
import httpx
from typing import AsyncIterator
from config import ZEN_BASE_URL, ZEN_API_KEY

class ZenClient:
    """OpenAI-compatible client pointed at a local zen-proxy instance."""

    def __init__(self, base_url: str = ZEN_BASE_URL, api_key: str = ZEN_API_KEY):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=None,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def close(self):
        await self._client.aclose()

    async def health(self) -> bool:
        try:
            r = await self._client.get(f"{self.base_url}/models")
            return r.status_code == 200
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        """Live free model list from Zen via proxy."""
        try:
            r = await self._client.get(f"{self.base_url}/models", timeout=10.0)
            r.raise_for_status()
            data = r.json()
            ids = [m["id"] for m in data.get("data", [])]
            # zen-proxy already filters to free models
            return sorted(ids)
        except Exception:
            return []

    async def stream_chat(
        self,
        messages: list[dict],
        model: str,
    ) -> AsyncIterator[dict]:
        """Stream OpenAI chat completion chunks."""
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
        }

        async with self._client.stream(
            "POST",
            f"{self.base_url}/chat/completions",
            json=payload,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                if line.startswith("data: "):
                    line = line[6:]
                if line == "[DONE]":
                    break
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


# Singleton used by bot.py
client = ZenClient()

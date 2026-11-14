"""Chat-completion client for an OpenAI-compatible endpoint (LiteLLM, vLLM).

Used for one thing: asking a locally hosted model to rewrite a NOTAM. The
endpoint and the model name come from the configuration; the API key lives in
the environment (HANGAR_LLM_API_KEY) and is never logged, returned or stored.
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional

import httpx

MAX_TOKENS = 700            # a rewrite is one line; this bounds a runaway answer
SCHEMA_NAME = "notam_rewrite"


class ModelError(RuntimeError):
    """The endpoint could not be used or did not answer usefully."""


def api_key() -> str:
    return os.environ.get("HANGAR_LLM_API_KEY", "")


class ChatModel:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self._no_schema: set = set()    # (base_url, model) pairs that refuse a JSON schema

    @staticmethod
    def _headers() -> Dict[str, str]:
        key = api_key()
        return {"Authorization": f"Bearer {key}"} if key else {}

    async def _send(self, method: str, url: str, timeout_s: float, **kw) -> httpx.Response:
        try:
            return await self.client.request(method, url, headers=self._headers(), timeout=timeout_s, **kw)
        except httpx.TimeoutException:
            raise ModelError(f"the model endpoint did not answer within {timeout_s:g} s") from None
        except httpx.HTTPError as exc:
            raise ModelError(f"the model endpoint is unreachable: {exc.__class__.__name__}") from None

    @staticmethod
    def _refused(resp: httpx.Response) -> Optional[str]:
        if resp.status_code in (401, 403):
            return f"the model endpoint rejected the API key (HTTP {resp.status_code})"
        if resp.status_code == 404:
            return "the model endpoint has no such path or model (HTTP 404)"
        if resp.status_code >= 400:
            return f"the model endpoint answered HTTP {resp.status_code}"
        return None

    async def models(self, base_url: str, timeout_s: float = 15.0) -> List[str]:
        """The model names the endpoint offers."""
        resp = await self._send("GET", base_url.rstrip("/") + "/models", timeout_s)
        problem = self._refused(resp)
        if problem:
            raise ModelError(problem)
        try:
            return [str(m["id"]) for m in resp.json().get("data", []) if isinstance(m, dict) and m.get("id")]
        except (ValueError, AttributeError):
            raise ModelError("the model endpoint's model list is not JSON") from None

    async def complete(self, base_url: str, model: str, messages: List[Dict[str, str]], timeout_s: float,
                       schema: Optional[Dict] = None) -> str:
        """The model's reply to the messages, as text. The reply is asked for at
        temperature 0 and, where the endpoint supports it, constrained to the
        JSON schema; an endpoint that refuses the schema is asked without it."""
        url = base_url.rstrip("/") + "/chat/completions"
        body: Dict = {"model": model, "messages": messages, "temperature": 0, "max_tokens": MAX_TOKENS}
        constrained = schema is not None and (base_url, model) not in self._no_schema
        if constrained:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": SCHEMA_NAME, "strict": True, "schema": schema},
            }
        resp = await self._send("POST", url, timeout_s, json=body)
        if resp.status_code in (400, 422) and constrained:
            self._no_schema.add((base_url, model))
            body.pop("response_format")
            resp = await self._send("POST", url, timeout_s, json=body)
        problem = self._refused(resp)
        if problem:
            raise ModelError(problem)
        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise ModelError("the model endpoint's reply is not a chat completion") from None
        if isinstance(content, list):      # content given as parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not isinstance(content, str) or not content.strip():
            raise ModelError("the model returned no text")
        return content

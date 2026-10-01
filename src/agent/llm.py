"""LLM providers for the agent.

The agent depends on the LLMProvider protocol, not on any vendor SDK, so the
underlying model is swappable via the AGENT_LLM environment variable:

- AGENT_LLM=echo   (default) deterministic offline provider, no network, no
                    API key; extractive answer from retrieved context.
- AGENT_LLM=http   OpenAI-compatible /chat/completions client using stdlib
                    urllib; configured with AGENT_LLM_URL, AGENT_LLM_API_KEY,
                    AGENT_LLM_MODEL.

Tests inject their own stub provider, which is the main reason the protocol
exists: the agent loop can be tested without network or paid APIs.
"""

import json
import os
import urllib.request
from typing import Protocol, runtime_checkable

import common.logging_utils  # noqa: F401  (ensures logging is configured)


@runtime_checkable
class LLMProvider(Protocol):
    def complete(self, system: str, user: str) -> str: ...


class EchoLLM:
    """Deterministic extractive provider: answers only from the given context.

    No network calls. If the context has no evidence, it says so explicitly
    instead of inventing an answer, which keeps offline demos and tests
    honest about what the retrieval layer found.
    """

    def complete(self, system: str, user: str) -> str:
        context, question = EchoLLM._split_prompt(user)
        if not context.strip() or "(no evidence)" in context:
            return "No indexed evidence matched this question."
        first_line = next(
            (ln for ln in context.splitlines() if ln.strip().startswith("[1]")),
            context.splitlines()[0] if context.strip() else "",
        )
        return f"Based on the indexed evidence: {first_line.strip()} — this answers '{question}'."

    @staticmethod
    def _split_prompt(user: str) -> tuple[str, str]:
        """Split the rendered prompt back into (evidence, question)."""
        marker = "Question:"
        context, _, question = user.rpartition(marker)
        lines = [ln for ln in context.splitlines() if ln.strip()]
        evidence_lines = [ln for ln in lines if not ln.strip().startswith("Evidence:")]
        return "\n".join(evidence_lines), question.strip().splitlines()[0] if question.strip() else ""


class HttpLLM:
    """Minimal OpenAI-compatible chat client (stdlib only)."""

    def __init__(
        self,
        url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 20.0,
    ) -> None:
        if not url or not api_key or not model:
            raise ValueError("HttpLLM requires AGENT_LLM_URL, AGENT_LLM_API_KEY and AGENT_LLM_MODEL")
        self._url = url
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds

    def complete(self, system: str, user: str) -> str:
        payload = json.dumps(
            {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0,
            }
        ).encode()
        request = urllib.request.Request(
            self._url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            body = json.loads(response.read())
        return str(body["choices"][0]["message"]["content"])


def get_llm() -> LLMProvider:
    """Factory: pick the provider from AGENT_LLM (default: echo)."""
    kind = os.environ.get("AGENT_LLM", "echo").lower()
    if kind == "http":
        return HttpLLM(
            url=os.environ.get("AGENT_LLM_URL", ""),
            api_key=os.environ.get("AGENT_LLM_API_KEY", ""),
            model=os.environ.get("AGENT_LLM_MODEL", ""),
        )
    return EchoLLM()

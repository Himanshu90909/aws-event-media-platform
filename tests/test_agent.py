"""Agent layer tests: retrieval, prompt rendering, LLM swap, failure paths.

All AWS access is stubbed; the agent loop is tested end-to-end offline.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from agent.agent import MediaJobsAgent
from agent.documents import job_item_to_document
from agent.index import SearchIndex
from agent.llm import EchoLLM, HttpLLM, get_llm
from agent.prompts import render_query_prompt
from agent.tools import Tool, ToolRegistry, build_tools

FIXTURE = json.loads(Path("tests/fixtures/job_items.json").read_text())["Items"]


def make_agent(items: list[dict[str, Any]], llm: Any = None) -> MediaJobsAgent:
    index = SearchIndex()
    index.ingest([job_item_to_document(i) for i in items])
    tools = build_tools(index, job_lookup=lambda jid: next((i for i in items if i["jobId"] == jid), None))
    return MediaJobsAgent(tools, llm=llm or EchoLLM())


class TestSearchIndex:
    def test_ingest_and_retrieve_ranks_relevant_doc_first(self):
        index = SearchIndex()
        index.ingest([job_item_to_document(i) for i in FIXTURE])
        hits = index.retrieve("which job had a decode error", top_k=3)
        assert len(hits) > 0
        assert hits[0].document.doc_id == "job:j-002"

    def test_retrieve_with_empty_index_returns_no_hits(self):
        assert SearchIndex().retrieve("anything") == []

    def test_reingest_replaces_document(self):
        index = SearchIndex()
        index.ingest([job_item_to_document(FIXTURE[0])])
        changed = {**FIXTURE[0], "status": "FAILED", "error": "decode error"}
        index.ingest([job_item_to_document(changed)])
        assert len(index) == 1
        assert index.retrieve("decode error")[0].document.doc_id == "job:j-001"


class TestAgentLoop:
    def test_happy_path_returns_citations_and_trace(self):
        result = make_agent(FIXTURE).answer("which jobs failed?")
        assert result.answer
        assert "job:" in result.citations[0]
        assert result.tool_trace == ["search_jobs"]
        assert result.latency_ms >= 0

    def test_no_hits_returns_honest_answer_with_empty_citations(self):
        result = make_agent([]).answer("which jobs failed?")
        assert result.citations == []
        assert "No indexed evidence" in result.answer
        assert "search_jobs:0_hits" in result.tool_trace

    def test_llm_is_swappable(self):
        class CannedLLM:
            def complete(self, system: str, user: str) -> str:
                return "CANNED ANSWER [1]"

        result = make_agent(FIXTURE, llm=CannedLLM()).answer("any failed jobs?")
        assert result.answer == "CANNED ANSWER [1]"
        assert result.citations  # evidence still collected by the loop


class TestTools:
    def test_registry_rejects_duplicates_and_unknown_names(self):
        registry = ToolRegistry()
        registry.register(Tool("a", "desc", lambda: 1))
        with pytest.raises(ValueError, match="Duplicate"):
            registry.register(Tool("a", "other", lambda: 2))
        with pytest.raises(KeyError, match="Unknown tool"):
            registry.get("nope")

    def test_get_job_tool_uses_injected_lookup(self):
        seen: list[str] = []
        tools = build_tools(SearchIndex(), job_lookup=lambda jid: seen.append(jid) or {"jobId": jid})
        assert tools.execute("get_job", job_id="j-009") == {"jobId": "j-009"}
        assert seen == ["j-009"]


class TestPromptsAndLLM:
    def test_rendered_prompt_contains_numbered_evidence(self):
        system, user = render_query_prompt("q?", ["evidence one", "evidence two"])
        assert "[1] evidence one" in user and "[2] evidence two" in user
        assert "Question: q?" in user
        assert "cite" in system.lower()

    def test_echo_llm_says_no_evidence_without_context(self):
        assert "No indexed evidence" in EchoLLM().complete("sys", "Evidence:\n\nQuestion: x")

    def test_get_llm_defaults_to_echo_and_http_needs_config(self, monkeypatch):
        assert isinstance(get_llm(), EchoLLM)
        monkeypatch.setenv("AGENT_LLM", "http")
        with pytest.raises(ValueError, match="AGENT_LLM_URL"):
            get_llm()

    def test_http_llm_parses_chat_completion(self, monkeypatch):
        import agent.llm as llm_mod

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self) -> bytes:
                return json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode()

        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["auth"] = request.headers.get("Authorization")
            return FakeResponse()

        monkeypatch.setattr(llm_mod.urllib.request, "urlopen", fake_urlopen)
        provider = HttpLLM(url="https://llm.internal/v1/chat/completions", api_key="k", model="m")
        assert provider.complete("s", "u") == "hi"
        assert captured["auth"] == "Bearer k"

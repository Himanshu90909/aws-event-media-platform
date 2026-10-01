"""Agent Lambda handler tests (API Gateway event shape, mocked DynamoDB)."""

import json
from unittest.mock import Mock

import agent.app as agent_app


def api_event(question: str | None) -> dict:
    body = json.dumps({"question": question}) if question is not None else None
    return {"body": body}


class TestAgentHandler:
    def test_query_returns_answer_with_citations(self, monkeypatch):
        table = Mock()
        table.scan.return_value = {
            "Items": [
                {
                    "jobId": "j-1",
                    "fileName": "a.wav",
                    "contentType": "audio/wav",
                    "status": "FAILED",
                    "error": "decode error",
                }
            ]
        }
        monkeypatch.setattr(agent_app, "get_table", lambda: table)
        response = agent_app.handler(api_event("what failed?"), None)
        assert response["statusCode"] == 200
        payload = json.loads(response["body"])
        assert payload["citations"]
        assert payload["latencyMs"] >= 0

    def test_missing_question_is_400(self, monkeypatch):
        response = agent_app.handler(api_event(None), None)
        assert response["statusCode"] == 400

    def test_empty_index_returns_honest_answer(self, monkeypatch):
        table = Mock()
        table.scan.return_value = {"Items": []}
        monkeypatch.setattr(agent_app, "get_table", lambda: table)
        response = agent_app.handler(api_event("anything"), None)
        payload = json.loads(response["body"])
        assert payload["citations"] == []

    def test_scan_paginates_until_limit(self, monkeypatch):
        table = Mock()
        page1 = {"Items": [{"jobId": f"j-{i}"} for i in range(100)], "LastEvaluatedKey": {"jobId": "j-99"}}
        page2 = {"Items": [{"jobId": "j-100"}]}
        table.scan.side_effect = [page1, page2]
        monkeypatch.setattr(agent_app, "get_table", lambda: table)
        items = agent_app.scan_recent_job_items(limit=101)
        assert len(items) == 101
        assert table.scan.call_count == 2
        second_call = table.scan.call_args_list[1]
        assert second_call.kwargs["ExclusiveStartKey"] == {"jobId": "j-99"}

    def test_dynamo_error_returns_500_not_crash(self, monkeypatch):
        table = Mock()
        table.scan.side_effect = RuntimeError("db down")
        monkeypatch.setattr(agent_app, "get_table", lambda: table)
        response = agent_app.handler(api_event("q"), None)
        assert response["statusCode"] == 500
        assert "failed" in json.loads(response["body"])["error"]

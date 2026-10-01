"""EMF metric emitter tests: valid CloudWatch Embedded Metric Format output."""

import json
import logging

from common.metrics import emit_metric, metric_timer


def test_emit_metric_produces_valid_emf_records(caplog):
    with caplog.at_level(logging.INFO, logger="metrics"):
        emit_metric("JobOutcome", 1, outcome="completed")
    record = json.loads(caplog.records[0].getMessage())
    assert record["JobOutcome"] == 1
    assert record["outcome"] == "completed"
    cw = record["_aws"]["CloudWatchMetrics"][0]
    assert cw["Namespace"] == "EventMediaPlatform"
    assert cw["Metrics"][0]["Name"] == "JobOutcome"
    assert cw["Dimensions"] == [["outcome"]]


def test_metric_timer_emits_duration_and_dimensions(caplog):
    with caplog.at_level(logging.INFO, logger="metrics"), metric_timer("JobLatency", jobId="j-1"):
        pass
    record = json.loads(caplog.records[-1].getMessage())
    assert record["JobLatency"] >= 0
    assert record["jobId"] == "j-1"
    assert record["_aws"]["CloudWatchMetrics"][0]["Metrics"][0]["Unit"] == "Milliseconds"

"""CloudWatch metrics via Embedded Metric Format (EMF).

Emits structured JSON log lines that CloudWatch picks up as metrics with zero
extra infrastructure: no SDK dependency, no PutMetricData API calls, and the
same lines work in local development (they are plain JSON logs).

Usage:
    from common.metrics import emit_metric, metric_timer

    emit_metric("JobOutcome", 1, outcome="completed")
    with metric_timer("JobLatency", jobId=jid):
        ...
"""

import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger("metrics")


def emit_metric(
    name: str,
    value: float,
    unit: str = "Count",
    namespace: str = "EventMediaPlatform",
    **dimensions: str | int | bool,
) -> None:
    """Write one EMF record. Dimensions become CloudWatch dimensions."""
    dims = list(dimensions)
    record: dict[str, Any] = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    "Dimensions": [dims] if dims else [],
                    "Metrics": [{"Name": name, "Unit": unit}],
                }
            ],
        },
        name: value,
    }
    record.update(dimensions)
    logger.info(json.dumps(record, sort_keys=True, default=str))


@contextmanager
def metric_timer(name: str, unit: str = "Milliseconds", **dimensions: str | int | bool) -> Iterator[None]:
    """Time a block and emit the duration as a metric."""
    start = time.perf_counter()
    try:
        yield
    finally:
        emit_metric(name, round((time.perf_counter() - start) * 1000, 2), unit=unit, **dimensions)

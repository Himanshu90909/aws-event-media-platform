# Operations Runbook

## Signals

Track API request count/latency/errors, ECS CPU/memory, SQS visible messages and age, DLQ depth, completed/failed jobs, processing duration and deployment version. The SAM stack already enables X-Ray tracing and structured Lambda logs; the ECS layer writes JSON-compatible container logs to `/ecs/<project>-<environment>`.

## First response

1. Check `/health` and `/ready`.
2. Inspect the request ID/correlation ID in API and CloudWatch logs.
3. Check SQS queue age and DLQ depth.
4. Read the DynamoDB job item and compare its state with the worker log.
5. For repeated failures, preserve the DLQ payload before replaying.

## Local commands

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
ruff check src tests
python docker/server.py
curl http://localhost:8080/health
```

## Production alarms to configure

- ECS CPU or memory above 70% for 10 minutes.
- ALB/API 5xx above 2% for 5 minutes.
- SQS age above the processing SLO.
- Any DLQ message in production.
- No successful jobs while queue depth is non-zero.

Thresholds are starting points and must be tuned with observed workload; this repository contains no fabricated production metrics.

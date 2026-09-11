# Architecture Notes

## Request lifecycle

The ingest Lambda validates the filename and MIME type, generates a UUID, writes a `QUEUED` record, creates a private S3 placeholder object, and publishes a small SQS message containing the job ID. The API returns `202` without waiting for the worker.

The worker loads the job, conditionally claims it, performs a deterministic metadata inspection of the S3 object, and conditionally records the result. A second delivery sees `PROCESSING` or `COMPLETED` and cannot corrupt the state.

## Partial failures

The current ordering makes the job durable before queue publication. If S3 or SQS fails after the DynamoDB write, the ingest handler returns `503`; a production hardening step is an outbox/reconciliation process that scans for old `QUEUED` records and republishes missing events. If worker failure occurs, the raised exception causes SQS retry. If the failure update cannot be written, the message still remains retryable.

## Security boundaries

Function-specific SAM policies deliberately separate read and write access. The bucket is retained on stack deletion to reduce accidental data loss and has public access blocks, encryption, versioning, and ownership controls. GitHub Actions receives short-lived credentials through the standard GitHub OIDC provider and a branch-restricted trust policy.

## Operational runbook

For a stuck job, query DynamoDB by job ID and correlate the timestamp with the worker CloudWatch log stream. For repeated worker failures, inspect the SQS message receive count and then the DLQ. For API 503 responses, inspect ingest logs for the AWS client error and verify table, bucket, and queue environment variables. For deployment failures, run `sam validate --lint`, then inspect the CloudFormation events for the stack.

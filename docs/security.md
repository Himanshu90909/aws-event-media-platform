# Security Checklist and Threat Model

## Implemented or defined

- Private, encrypted, versioned S3 bucket with public access blocks in SAM.
- DynamoDB server-side encryption and point-in-time recovery in SAM.
- SQS managed encryption and DLQ retry policy.
- Lambda-specific SAM policies rather than one shared application role.
- GitHub Actions OIDC trust instead of long-lived AWS keys.
- Non-root, read-only-rootfs container profile with dropped Linux capabilities.
- ECR immutable tags and scan-on-push.
- Terraform task role scoped to its CloudWatch log group.
- Input validation for safe filenames, MIME length and callback URLs.
- Structured logs avoid request bodies and credentials.

## Threat model

| Threat | Control | Residual work |
|---|---|---|
| Unauthorized API access | API boundary and optional gateway auth | Add JWT/Cognito authorizer before multi-tenant use |
| Malicious uploads | safe object keys, private S3, content type validation | malware scanning, size limits, media codec sandbox |
| Credential leakage | OIDC, ignored env files, secret manager recommendation | configure repository/environment protections |
| Excessive IAM | split function/task roles | narrow bootstrap deployment role further |
| Queue poisoning | schema validation, retries, DLQ, idempotent transitions | alert and runbook automation |
| Sensitive logs | structured, metadata-only logging | formal log redaction tests |
| Insecure deployments | PR validation, SHA tags, protected environments | enable manual production approval |
| Public exposure | private subnets, SG egress-only service | attach controlled ALB/API Gateway integration |

This document is a design control checklist, not a regulatory certification or claim of comprehensive security compliance.

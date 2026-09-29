# Cost and Performance Analysis Template

No live AWS benchmark or billing data is included. Complete this table with the AWS Pricing Calculator and measured load-test results for the target region.

| Dimension | Existing serverless path | Hybrid target | Measurement / assumption |
|---|---|---|---|
| Compute | Lambda per invocation/duration | Fargate task-hours plus Lambda workers | request rate, task count, CPU/memory |
| API handling | API Gateway + Lambda | ALB/API Gateway + ECS | p50/p95 latency under same payload |
| Storage | S3 + DynamoDB on-demand | same managed stores | GB-month, requests, retention |
| Queueing | SQS + DLQ | same SQS boundary | messages/month and average size |
| Observability | CloudWatch/X-Ray | CloudWatch/container logs | GB logs/month, retention |
| Network | AWS service transfer | private-subnet NAT/endpoint/ALB transfer | region and AZ topology |
| Operations | low infrastructure overhead | more deployment and capacity controls | on-call/runbook effort |
| Scaling | request/event driven | API and worker capacity independently scaled | saturation point and recovery time |

Use workload, region, test duration, image version, concurrency, and limitations in every benchmark report. Do not describe a cost saving or performance improvement until it is measured or transparently estimated.

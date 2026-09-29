# Rollback and Recovery

## ECS application rollback

Keep the last known-good image URI and task definition revision. Stop traffic to the new route or set the service task definition back to the prior revision, then rerun `scripts/migration/migrate.sh smoke`. ECS deployment settings retain healthy capacity during a normal rolling update, but zero downtime is not claimed until a live routing test proves it.

```bash
export PREVIOUS_IMAGE=<ecr-uri>@<digest>
./scripts/migration/migrate.sh rollback
# update the Terraform container_image to PREVIOUS_IMAGE
terraform plan
terraform apply
./scripts/migration/migrate.sh smoke
```

## Serverless fallback

If the container path is unhealthy, route the API integration back to the existing SAM/API Gateway deployment. Do not destroy the SAM stack during migration. Preserve S3/DynamoDB data and keep the SQS/DLQ available for replay after investigating poison messages.

## Data recovery

DynamoDB point-in-time recovery and S3 versioning are enabled in the SAM template. Recovery actions require an operator change review and must be tested in a non-production account before use. No live recovery was executed for this portfolio repository.

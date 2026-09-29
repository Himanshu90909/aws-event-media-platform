terraform {
  required_version = ">= 1.6.0, < 2.0.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
  # Configure an S3 backend per environment, for example:
  # backend "s3" { bucket = "org-terraform-state" key = "event-media-platform/dev.tfstate" region = "us-east-1" dynamodb_table = "terraform-locks" encrypt = true }
}
provider "aws" { region = var.aws_region }

variable "aws_region" {
  type    = string
  default = "us-east-1"
}
variable "project" {
  type    = string
  default = "event-media-platform"
}
variable "environment" {
  type    = string
  default = "dev"
}
variable "container_image" {
  type        = string
  description = "Immutable ECR image URI including commit SHA"
}
variable "vpc_id" { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "alb_subnet_ids" {
  type    = list(string)
  default = []
}
variable "desired_count" {
  type    = number
  default = 2
}
variable "container_port" {
  type    = number
  default = 8080
}
variable "health_check_path" {
  type    = string
  default = "/health"
}
variable "tags" {
  type    = map(string)
  default = {}
}

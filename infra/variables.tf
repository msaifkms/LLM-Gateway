# --- General ---------------------------------------------------------------

variable "project" {
  description = "Name prefix for every resource, and the value of the project tag. The deploy scripts assume the default."
  type        = string
  default     = "llm-gateway"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.project))
    error_message = "Use 2 to 21 lowercase letters, digits or hyphens, starting with a letter."
  }
}

variable "aws_region" {
  description = "AWS region. The project is restricted to us-east-2."
  type        = string
  default     = "us-east-2"

  validation {
    condition     = var.aws_region == "us-east-2"
    error_message = "This project deploys only to us-east-2."
  }
}

# --- Network ---------------------------------------------------------------

variable "vpc_cidr" {
  description = "CIDR block of the VPC. Subnets are carved from it as /24s."
  type        = string
  default     = "10.40.0.0/16"
}

variable "allowed_ingress_cidrs" {
  description = "Who may reach the load balancer. Set it to your own IP (x.x.x.x/32) for a private demo."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "certificate_arn" {
  description = "ACM certificate ARN for HTTPS. Empty means HTTP only (fine for a short demo, not for real keys)."
  type        = string
  default     = ""
}

# --- Gateway service -------------------------------------------------------

variable "image_tag" {
  description = "Image tag the first task definition points at. Later deploys (infra/scripts/deploy.sh) register new revisions with the commit SHA."
  type        = string
  default     = "bootstrap"
}

variable "cpu_architecture" {
  description = "ARM64 (Graviton, about 20% cheaper) or X86_64. The image must be built for the same architecture."
  type        = string
  default     = "ARM64"

  validation {
    condition     = contains(["ARM64", "X86_64"], var.cpu_architecture)
    error_message = "cpu_architecture must be ARM64 or X86_64."
  }
}

variable "task_cpu" {
  description = "Fargate task CPU units (256 = 0.25 vCPU, the smallest)."
  type        = number
  default     = 256
}

variable "task_memory" {
  description = "Fargate task memory in MiB (512 is the smallest for 0.25 vCPU)."
  type        = number
  default     = 512
}

variable "desired_count" {
  description = "Number of gateway tasks. One is enough for a demo; two or more spread over both AZs."
  type        = number
  default     = 1
}

variable "log_retention_days" {
  description = "How long CloudWatch keeps container logs."
  type        = number
  default     = 7
}

variable "log_level" {
  description = "Gateway LOG_LEVEL."
  type        = string
  default     = "INFO"
}

variable "openai_model" {
  description = "Model the OpenAI provider calls."
  type        = string
  default     = "gpt-4.1-mini"
}

variable "anthropic_model" {
  description = "Model the Anthropic provider calls."
  type        = string
  default     = "claude-haiku-5-5"
}

# --- Database --------------------------------------------------------------

variable "db_instance_class" {
  description = "RDS instance class. db.t4g.micro is the smallest Graviton class."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_engine_version" {
  description = "PostgreSQL major version. AWS picks the current minor version."
  type        = string
  default     = "16"
}

variable "db_allocated_storage" {
  description = "Database storage in GiB (20 is the gp3 minimum)."
  type        = number
  default     = 20
}

variable "db_backup_retention_days" {
  description = "Automated backup retention in days. 1 is the shortest that still allows point-in-time restore; 0 turns backups off."
  type        = number
  default     = 1
}

variable "db_name" {
  description = "Database name."
  type        = string
  default     = "gateway"
}

variable "db_username" {
  description = "Master user name. The password is generated and kept by RDS in Secrets Manager, never in Terraform."
  type        = string
  default     = "gateway_admin"
}

variable "db_pool_max" {
  description = "Postgres connections per gateway task (DB_POOL_MAX)."
  type        = number
  default     = 10
}

# --- Redis (optional) ------------------------------------------------------

variable "enable_redis" {
  description = "Create an ElastiCache Redis node. Off: no response cache, no cache savings, and no rate limits (quotas still apply)."
  type        = bool
  default     = false
}

variable "redis_node_type" {
  description = "ElastiCache node type when enable_redis is true."
  type        = string
  default     = "cache.t4g.micro"
}

# --- CI/CD -----------------------------------------------------------------

variable "enable_github_oidc" {
  description = "Create a GitHub OIDC provider and a deploy role for GitHub Actions. Off by default: the AWS free plan's SCP denies the IAM OIDC actions, so deploys run from a laptop (infra/scripts/deploy.sh)."
  type        = bool
  default     = false
}

variable "create_github_oidc_provider" {
  description = "With enable_github_oidc: create the OIDC provider (false if the account already has one for token.actions.githubusercontent.com)."
  type        = bool
  default     = true
}

variable "github_repository" {
  description = "owner/name of the GitHub repository allowed to deploy (used only with enable_github_oidc)."
  type        = string
  default     = ""

  validation {
    condition     = var.github_repository == "" || can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "github_repository must look like owner/name."
  }
}

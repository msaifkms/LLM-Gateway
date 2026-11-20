variable "project" {
  description = "Name used for tags and as the bucket name prefix."
  type        = string
  default     = "llm-gateway"
}

variable "aws_region" {
  description = "Region for the state bucket. The project is restricted to us-east-2."
  type        = string
  default     = "us-east-2"

  validation {
    condition     = var.aws_region == "us-east-2"
    error_message = "This project deploys only to us-east-2."
  }
}

variable "force_destroy" {
  description = <<-EOT
    Let "terraform destroy" delete the bucket even though it still holds state
    versions. True suits a deploy-test-destroy demo; set false to protect a
    long-lived environment's state.
  EOT
  type        = bool
  default     = true
}

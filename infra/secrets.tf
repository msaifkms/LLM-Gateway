# Provider API keys, injected into the task as environment variables by ECS.
#
# Terraform creates each parameter with a placeholder and then ignores its
# value, so the real key is set outside Terraform and never enters code, plans
# or state:
#   aws ssm put-parameter --name /llm-gateway/providers/openai-api-key \
#     --type SecureString --overwrite --value "$OPENAI_API_KEY"
# Until then, OpenAI and Anthropic calls fail (503); the Mock provider works.
#
# The database password is not here: RDS manages it (see database.tf).

locals {
  provider_keys = {
    openai    = "OPENAI_API_KEY"
    anthropic = "ANTHROPIC_API_KEY"
  }
}

resource "aws_ssm_parameter" "provider_key" {
  for_each = local.provider_keys

  name        = "${local.ssm_prefix}/providers/${each.key}-api-key"
  description = "${each.value} for the ${local.name} gateway (set outside Terraform)"
  type        = "SecureString" # encrypted with the account's aws/ssm KMS key
  tier        = "Standard"     # free
  value       = "placeholder-set-me-with-aws-ssm-put-parameter"

  lifecycle {
    ignore_changes = [value]
  }
}

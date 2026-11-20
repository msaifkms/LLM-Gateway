output "alb_url" {
  description = "Base URL of the gateway."
  value       = "${local.https ? "https" : "http"}://${aws_lb.main.dns_name}"
}

output "ecr_repository_url" {
  description = "Where deploys push the gateway image."
  value       = aws_ecr_repository.gateway.repository_url
}

output "cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "service_name" {
  value = aws_ecs_service.gateway.name
}

output "log_group_name" {
  description = "CloudWatch Logs group for the gateway and one-off tasks."
  value       = aws_cloudwatch_log_group.gateway.name
}

output "admin_task_role_arn" {
  description = "Role that one-off admin tasks run with (infra/scripts/run-task.sh)."
  value       = aws_iam_role.admin_task.arn
}

output "issued_key_ssm_path" {
  description = "SSM path that issue-key --to-ssm may write under."
  value       = "${local.ssm_key_path}/"
}

output "provider_key_parameters" {
  description = "SSM parameters to fill with the real provider API keys."
  value       = { for k, p in aws_ssm_parameter.provider_key : k => p.name }
}

output "db_endpoint" {
  description = "Postgres address (reachable only from the gateway tasks)."
  value       = aws_db_instance.main.address
}

output "redis_enabled" {
  value = var.enable_redis
}

output "github_deploy_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE_ARN repository variable to turn on CI deploys (only with enable_github_oidc)."
  value       = var.enable_github_oidc ? aws_iam_role.github_deploy[0].arn : null
}

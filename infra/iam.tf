# Three roles for ECS, each with only what it needs:
#   execution  - used by ECS itself to start a task: pull the image, write logs,
#                read the secrets it injects.
#   task       - the gateway's own identity. It calls no AWS APIs, so it has no
#                permissions at all.
#   admin_task - for one-off admin tasks (infra/scripts/run-task.sh): may only
#                create parameters under /llm-gateway/keys/ (issue-key --to-ssm).

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }

    # Only ECS acting for this account may assume these roles (confused deputy).
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${local.partition}:ecs:${var.aws_region}:${local.account_id}:*"]
    }
  }
}

# --- execution role ---------------------------------------------------------

resource "aws_iam_role" "execution" {
  name               = "${local.name}-execution"
  description        = "ECS starts ${local.name} tasks with this role"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "execution" {
  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # this action has no resource-level permissions
  }

  statement {
    sid = "PullGatewayImage"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [aws_ecr_repository.gateway.arn]
  }

  statement {
    sid       = "WriteLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.gateway.arn}:*"]
  }

  statement {
    sid       = "ReadProviderKeys"
    actions   = ["ssm:GetParameters"]
    resources = [for p in aws_ssm_parameter.provider_key : p.arn]
  }

  statement {
    sid       = "ReadDatabasePassword"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [local.db_secret_arn]
  }
}

resource "aws_iam_role_policy" "execution" {
  name   = "start-gateway-tasks"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution.json
}

# --- task role (the running gateway) ---------------------------------------

resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  description        = "Identity of running ${local.name} tasks; deliberately has no permissions"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

# --- admin task role (one-off admin commands) --------------------------------

resource "aws_iam_role" "admin_task" {
  name               = "${local.name}-admin-task"
  description        = "One-off ${local.name} admin tasks: may store issued API keys in SSM"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "admin_task" {
  statement {
    sid = "StoreIssuedKeys"
    # PutParameter only: the CLI creates (never overwrites, never reads) a key.
    # Encryption uses the AWS managed aws/ssm key, whose key policy already
    # allows SSM to use it for principals in this account.
    actions   = ["ssm:PutParameter"]
    resources = ["arn:${local.partition}:ssm:${var.aws_region}:${local.account_id}:parameter${local.ssm_key_path}/*"]
  }
}

resource "aws_iam_role_policy" "admin_task" {
  name   = "store-issued-keys"
  role   = aws_iam_role.admin_task.id
  policy = data.aws_iam_policy_document.admin_task.json
}

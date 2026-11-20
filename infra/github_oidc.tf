# Optional (enable_github_oidc, off by default): lets GitHub Actions deploy with
# short-lived credentials from OIDC instead of stored AWS keys.
#
# Off by default because the AWS free plan's service control policy denies the
# IAM OIDC provider actions (iam:ListOpenIDConnectProviders was denied when this
# was built). Deploys then run from a laptop with infra/scripts/deploy.sh, and
# the CI deploy job skips itself.

locals {
  github_oidc_url = "token.actions.githubusercontent.com"
}

resource "aws_iam_openid_connect_provider" "github" {
  count = var.enable_github_oidc && var.create_github_oidc_provider ? 1 : 0

  url            = "https://${local.github_oidc_url}"
  client_id_list = ["sts.amazonaws.com"]
}

locals {
  github_oidc_provider_arn = (
    var.enable_github_oidc && var.create_github_oidc_provider
    ? aws_iam_openid_connect_provider.github[0].arn
    : "arn:${local.partition}:iam::${local.account_id}:oidc-provider/${local.github_oidc_url}"
  )
}

data "aws_iam_policy_document" "github_assume" {
  count = var.enable_github_oidc ? 1 : 0

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.github_oidc_url}:aud"
      values   = ["sts.amazonaws.com"]
    }

    # Only workflows running on main of this one repository.
    condition {
      test     = "StringEquals"
      variable = "${local.github_oidc_url}:sub"
      values   = ["repo:${var.github_repository}:ref:refs/heads/main"]
    }
  }
}

resource "aws_iam_role" "github_deploy" {
  count = var.enable_github_oidc ? 1 : 0

  name                 = "${local.name}-github-deploy"
  description          = "GitHub Actions deploys ${local.name} from main"
  assume_role_policy   = data.aws_iam_policy_document.github_assume[0].json
  max_session_duration = 3600

  lifecycle {
    precondition {
      condition     = var.github_repository != ""
      error_message = "Set github_repository (owner/name) when enable_github_oidc is true."
    }
  }
}

data "aws_iam_policy_document" "github_deploy" {
  count = var.enable_github_oidc ? 1 : 0

  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "PushGatewayImage"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:DescribeRepositories",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [aws_ecr_repository.gateway.arn]
  }

  statement {
    sid = "TaskDefinitions"
    # These two actions do not support resource-level permissions.
    actions   = ["ecs:DescribeTaskDefinition", "ecs:RegisterTaskDefinition"]
    resources = ["*"]
  }

  statement {
    sid       = "TagTaskDefinitions"
    actions   = ["ecs:TagResource"]
    resources = ["arn:${local.partition}:ecs:${var.aws_region}:${local.account_id}:task-definition/${local.name}:*"]
  }

  statement {
    sid       = "RunMigrations"
    actions   = ["ecs:RunTask"]
    resources = ["arn:${local.partition}:ecs:${var.aws_region}:${local.account_id}:task-definition/${local.name}:*"]

    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.main.arn]
    }
  }

  statement {
    sid       = "WatchTasks"
    actions   = ["ecs:DescribeTasks"]
    resources = ["arn:${local.partition}:ecs:${var.aws_region}:${local.account_id}:task/${local.name}/*"]
  }

  statement {
    sid       = "UpdateService"
    actions   = ["ecs:DescribeServices", "ecs:UpdateService"]
    resources = [aws_ecs_service.gateway.id]
  }

  statement {
    sid       = "PassTaskRoles"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.execution.arn, aws_iam_role.task.arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }

  statement {
    sid       = "ReadMigrationLogs"
    actions   = ["logs:GetLogEvents"]
    resources = ["${aws_cloudwatch_log_group.gateway.arn}:*"]
  }

  statement {
    sid       = "FindLoadBalancer"
    actions   = ["elasticloadbalancing:DescribeLoadBalancers"]
    resources = ["*"] # describe calls have no resource-level permissions
  }
}

resource "aws_iam_role_policy" "github_deploy" {
  count = var.enable_github_oidc ? 1 : 0

  name   = "deploy-gateway"
  role   = aws_iam_role.github_deploy[0].id
  policy = data.aws_iam_policy_document.github_deploy[0].json
}

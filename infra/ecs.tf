resource "aws_cloudwatch_log_group" "gateway" {
  name              = "/ecs/${local.name}"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_cluster" "main" {
  name = local.name

  # Container Insights adds per-metric charges; the gateway has /metrics.
  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

locals {
  # The same check as the image's HEALTHCHECK (ECS ignores the Dockerfile's).
  healthcheck = "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${local.container_port}/healthz', timeout=3)"

  container_environment = {
    LOG_FORMAT      = "json"
    LOG_LEVEL       = var.log_level
    AWS_REGION      = var.aws_region
    REDIS_URL       = local.redis_url
    DB_HOST         = aws_db_instance.main.address
    DB_PORT         = tostring(aws_db_instance.main.port)
    DB_NAME         = var.db_name
    DB_USER         = var.db_username
    DB_SSLMODE      = "require"
    DB_POOL_MAX     = tostring(var.db_pool_max)
    OPENAI_MODEL    = var.openai_model
    ANTHROPIC_MODEL = var.anthropic_model
  }

  container_secrets = merge(
    { for provider, env in local.provider_keys : env => aws_ssm_parameter.provider_key[provider].arn },
    # One key out of the JSON secret RDS manages: "<arn>:<json-key>:<stage>:<version>"
    { DB_PASSWORD = "${local.db_secret_arn}:password::" },
  )
}

resource "aws_ecs_task_definition" "gateway" {
  family                   = local.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([
    {
      name      = local.container_name
      image     = "${aws_ecr_repository.gateway.repository_url}:${var.image_tag}"
      essential = true

      portMappings = [
        { containerPort = local.container_port, protocol = "tcp" },
      ]

      environment = [
        for name in sort(keys(local.container_environment)) :
        { name = name, value = local.container_environment[name] }
      ]
      secrets = [
        for name in sort(keys(local.container_secrets)) :
        { name = name, valueFrom = local.container_secrets[name] }
      ]

      # The image already runs as uid 10001; the root filesystem is read-only
      # on top of that, since the gateway writes nothing to disk.
      user                   = "10001:10001"
      readonlyRootFilesystem = true
      linuxParameters = {
        initProcessEnabled = true # forwards signals and reaps zombies
      }

      healthCheck = {
        command     = ["CMD", "python", "-c", local.healthcheck]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 15
      }

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.gateway.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "gateway"
          # Never block the app on a slow log pipeline.
          mode            = "non-blocking"
          max-buffer-size = "4m"
        }
      }
    },
  ])
}

resource "aws_ecs_service" "gateway" {
  name             = local.name
  cluster          = aws_ecs_cluster.main.id
  task_definition  = aws_ecs_task_definition.gateway.arn
  desired_count    = var.desired_count
  launch_type      = "FARGATE"
  platform_version = "LATEST"

  # Zero-downtime rollouts; a deployment that never gets healthy rolls back.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  health_check_grace_period_seconds = 30

  # Not relied on (the free plan may block the SSM session service); admin
  # commands run as one-off tasks instead.
  enable_execute_command = false

  enable_ecs_managed_tags = true
  propagate_tags          = "SERVICE"

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = true # no NAT Gateway; see network.tf
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.gateway.arn
    container_name   = local.container_name
    container_port   = local.container_port
  }

  lifecycle {
    # Deploys (infra/scripts/deploy.sh) register new revisions with a new image.
    # Terraform must not roll the service back to its own revision on the next
    # apply. A Terraform change to the task definition is picked up by the next
    # deploy, which copies the newest revision and swaps only the image.
    ignore_changes = [task_definition]
  }

  depends_on = [aws_lb_listener.http, aws_lb_listener.https]
}

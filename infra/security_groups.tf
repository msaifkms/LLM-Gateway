# Each tier accepts traffic only from the tier in front of it:
#   internet -> ALB (80/443) -> tasks (8000) -> Postgres (5432) / Redis (6379)

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Load balancer: HTTP(S) in from allowed CIDRs, out only to gateway tasks"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-alb" }
}

resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  for_each = toset(var.allowed_ingress_cidrs)

  security_group_id = aws_security_group.alb.id
  description       = local.https ? "HTTP (redirected to HTTPS)" : "HTTP"
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  for_each = local.https ? toset(var.allowed_ingress_cidrs) : toset([])

  security_group_id = aws_security_group.alb.id
  description       = "HTTPS"
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "alb_to_tasks" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Forward to gateway tasks"
  referenced_security_group_id = aws_security_group.tasks.id
  ip_protocol                  = "tcp"
  from_port                    = local.container_port
  to_port                      = local.container_port
}

resource "aws_security_group" "tasks" {
  name        = "${local.name}-tasks"
  description = "Gateway tasks: in only from the ALB; out to HTTPS, Postgres and Redis"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-tasks" }
}

resource "aws_vpc_security_group_ingress_rule" "tasks_from_alb" {
  security_group_id            = aws_security_group.tasks.id
  description                  = "Gateway port, from the load balancer only"
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = local.container_port
  to_port                      = local.container_port
}

# HTTPS out: ECR, CloudWatch Logs, SSM, Secrets Manager, OpenAI and Anthropic.
# Without a NAT or proxy, these have no fixed IP ranges to narrow this to.
# DNS to the VPC resolver is not subject to security group rules.
resource "aws_vpc_security_group_egress_rule" "tasks_https" {
  security_group_id = aws_security_group.tasks.id
  description       = "HTTPS to AWS APIs and LLM providers"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "tasks_to_db" {
  security_group_id            = aws_security_group.tasks.id
  description                  = "Postgres"
  referenced_security_group_id = aws_security_group.db.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_egress_rule" "tasks_to_redis" {
  count = var.enable_redis ? 1 : 0

  security_group_id            = aws_security_group.tasks.id
  description                  = "Redis"
  referenced_security_group_id = aws_security_group.redis[0].id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
}

resource "aws_security_group" "db" {
  name        = "${local.name}-db"
  description = "Postgres: in only from gateway tasks; no outbound rules"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-db" }
}

resource "aws_vpc_security_group_ingress_rule" "db_from_tasks" {
  security_group_id            = aws_security_group.db.id
  description                  = "Postgres from gateway tasks"
  referenced_security_group_id = aws_security_group.tasks.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_security_group" "redis" {
  count = var.enable_redis ? 1 : 0

  name        = "${local.name}-redis"
  description = "Redis: in only from gateway tasks; no outbound rules"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-redis" }
}

resource "aws_vpc_security_group_ingress_rule" "redis_from_tasks" {
  count = var.enable_redis ? 1 : 0

  security_group_id            = aws_security_group.redis[0].id
  description                  = "Redis from gateway tasks"
  referenced_security_group_id = aws_security_group.tasks.id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
}

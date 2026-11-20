resource "aws_lb" "main" {
  name               = local.name
  load_balancer_type = "application"
  internal           = false
  subnets            = aws_subnet.public[*].id
  security_groups    = [aws_security_group.alb.id]

  # Reject requests with malformed headers instead of passing them on.
  drop_invalid_header_fields = true
  idle_timeout               = 60
  enable_deletion_protection = false
}

resource "aws_lb_target_group" "gateway" {
  name        = local.name
  vpc_id      = aws_vpc.main.id
  target_type = "ip" # Fargate tasks register by IP
  protocol    = "HTTP"
  port        = local.container_port

  # Old tasks drain for 30 s instead of the default 300 s, so deploys finish faster.
  deregistration_delay = 30

  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

# Without a certificate: plain HTTP that forwards to the gateway.
# With one: HTTP only redirects to HTTPS, and HTTPS forwards.
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = local.https ? "redirect" : "forward"
    target_group_arn = local.https ? null : aws_lb_target_group.gateway.arn

    dynamic "redirect" {
      for_each = local.https ? [1] : []
      content {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }
}

resource "aws_lb_listener" "https" {
  count = local.https ? 1 : 0

  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.gateway.arn
  }
}

# /metrics is for an internal scraper only, never the internet. The load
# balancer answers it with a 404 itself, so the request never reaches a task.
# "/metrics*" also covers "/metrics/" and anything after it.
resource "aws_lb_listener_rule" "block_metrics" {
  listener_arn = local.https ? aws_lb_listener.https[0].arn : aws_lb_listener.http.arn
  priority     = 1

  condition {
    path_pattern {
      values = ["/metrics", "/metrics*"]
    }
  }

  action {
    type = "fixed-response"

    fixed_response {
      content_type = "application/json"
      status_code  = "404"
      message_body = jsonencode({ detail = "Not Found" })
    }
  }
}

# Optional (enable_redis, off by default). The gateway runs without it; what is
# lost is listed in docs/deploy.md: the response cache and its savings, and
# per-key rate limits.

resource "aws_elasticache_subnet_group" "main" {
  count = var.enable_redis ? 1 : 0

  name        = local.name
  description = "Private subnets for ${local.name} Redis"
  subnet_ids  = aws_subnet.private[*].id
}

resource "aws_elasticache_replication_group" "main" {
  count = var.enable_redis ? 1 : 0

  replication_group_id = local.name
  description          = "${local.name} response cache and rate limits"
  engine               = "redis"
  engine_version       = "7.1"
  parameter_group_name = "default.redis7"
  node_type            = var.redis_node_type
  port                 = 6379

  # One node, no replica: the cache is disposable and the gateway fails open.
  num_cache_clusters         = 1
  automatic_failover_enabled = false
  multi_az_enabled           = false

  subnet_group_name  = aws_elasticache_subnet_group.main[0].name
  security_group_ids = [aws_security_group.redis[0].id]

  # TLS in transit (the gateway connects with rediss://) and encryption at rest.
  # No AUTH token, so no secret ends up in state; only the task security group
  # can reach the node.
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true

  snapshot_retention_limit   = 0
  apply_immediately          = true
  auto_minor_version_upgrade = true
}

locals {
  redis_url = (
    var.enable_redis
    ? "rediss://${aws_elasticache_replication_group.main[0].primary_endpoint_address}:6379/0"
    : "none"
  )
}

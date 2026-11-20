resource "aws_db_subnet_group" "main" {
  name        = local.name
  description = "Private subnets for ${local.name} Postgres"
  subnet_ids  = aws_subnet.private[*].id
}

resource "aws_db_parameter_group" "postgres" {
  name        = "${local.name}-postgres${var.db_engine_version}"
  family      = "postgres${var.db_engine_version}"
  description = "${local.name}: TLS required"

  # Already the default for Postgres 15+, set here so it is explicit.
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
}

resource "aws_db_instance" "main" {
  identifier     = local.name
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  allocated_storage = var.db_allocated_storage
  storage_type      = "gp3"
  storage_encrypted = true # with the account's aws/rds KMS key

  db_name  = var.db_name
  username = var.db_username
  # RDS generates the password and keeps it in a Secrets Manager secret it
  # manages, so the password never appears in Terraform code, plans or state.
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  parameter_group_name   = aws_db_parameter_group.postgres.name
  publicly_accessible    = false
  multi_az               = false
  port                   = 5432

  backup_retention_period  = var.db_backup_retention_days
  backup_window            = "07:00-07:30"
  maintenance_window       = "sun:08:00-sun:08:30"
  copy_tags_to_snapshot    = true
  delete_automated_backups = true

  # Demo settings, so "terraform destroy" removes everything in one go.
  deletion_protection = false
  skip_final_snapshot = true

  auto_minor_version_upgrade   = true
  apply_immediately            = true
  performance_insights_enabled = false
  monitoring_interval          = 0
}

locals {
  db_secret_arn = aws_db_instance.main.master_user_secret[0].secret_arn
}

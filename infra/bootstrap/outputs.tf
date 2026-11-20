output "state_bucket" {
  description = "Name of the S3 bucket that holds the main config's state."
  value       = aws_s3_bucket.state.bucket
}

output "backend_config" {
  description = "Contents for infra/backend.hcl (gitignored)."
  value       = <<-EOT
    bucket       = "${aws_s3_bucket.state.bucket}"
    key          = "${var.project}/terraform.tfstate"
    region       = "${var.aws_region}"
    use_lockfile = true
    encrypt      = true
  EOT
}

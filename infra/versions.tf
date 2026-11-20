terraform {
  # 1.11+ for S3-native state locking (use_lockfile).
  required_version = ">= 1.11.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Partial configuration: the bucket name lives in backend.hcl (gitignored),
  # generated from the bootstrap config's output. See docs/deploy.md.
  #   terraform init -backend-config=backend.hcl
  backend "s3" {}
}

provider "aws" {
  region = var.aws_region

  # Every taggable resource gets these, so cost and cleanup can be checked by tag.
  default_tags {
    tags = {
      project    = var.project
      managed-by = "terraform"
    }
  }
}

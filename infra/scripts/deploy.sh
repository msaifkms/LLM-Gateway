#!/usr/bin/env bash
# Build the gateway image, push it to ECR, run database migrations, and roll the
# ECS service onto it. The same script runs from a laptop (the usual path: the
# AWS free plan blocks GitHub OIDC) and from the CI deploy job.
#
#   infra/scripts/deploy.sh                    # deploy the current commit
#   infra/scripts/deploy.sh --skip-if-missing  # exit 0 if nothing is deployed
#
# Environment: PROJECT (default llm-gateway), AWS_REGION (default us-east-2),
# IMAGE_TAG (default: the commit SHA, with -dirty-<time> for uncommitted work).
# Needs: aws (v2), docker (with buildx), jq, git, curl.
set -euo pipefail
cd "$(dirname "$0")/../.."
# shellcheck source=infra/scripts/lib.sh
source infra/scripts/lib.sh

skip_if_missing=false
case "${1:-}" in
  "") ;;
  --skip-if-missing) skip_if_missing=true ;;
  *) die "usage: $0 [--skip-if-missing]" ;;
esac

require_tools aws docker jq git curl

# 1. Is there anything to deploy to?
if [[ "$(service_state)" != "active" ]]; then
  if $skip_if_missing; then
    log "ECS service $SERVICE not found in $AWS_REGION: the infrastructure is not up. Nothing to deploy."
    exit 0
  fi
  die "ECS service $SERVICE not found in $AWS_REGION. Run terraform apply first (docs/deploy.md)."
fi

# 2. The image tag: the commit SHA. Tags are immutable in ECR, so uncommitted
#    changes get a unique suffix rather than overwriting a real commit's image.
if [[ -z "${IMAGE_TAG:-}" ]]; then
  IMAGE_TAG=$(git rev-parse HEAD)
  if [[ -n "$(git status --porcelain)" ]]; then
    IMAGE_TAG="$IMAGE_TAG-dirty-$(date -u +%Y%m%d%H%M%S)"
    log "uncommitted changes: tagging the image $IMAGE_TAG"
  fi
fi

repository_uri=$(aws ecr describe-repositories --repository-names "$REPOSITORY" \
  --query 'repositories[0].repositoryUri' --output text)
image="$repository_uri:$IMAGE_TAG"

# The newest revision of the family is the base: it carries Terraform's latest
# settings (environment, secrets, sizes); only the image changes.
base=$(aws ecs describe-task-definition --task-definition "$FAMILY" --include TAGS --output json)
case "$(jq -r '.taskDefinition.runtimePlatform.cpuArchitecture // "X86_64"' <<<"$base")" in
  ARM64) platform=linux/arm64 ;;
  *) platform=linux/amd64 ;;
esac

# 3. Build and push, unless this exact tag is already in ECR.
if aws ecr describe-images --repository-name "$REPOSITORY" \
  --image-ids "imageTag=$IMAGE_TAG" >/dev/null 2>&1; then
  log "image $IMAGE_TAG is already in ECR; not rebuilding"
else
  log "building $image for $platform"
  aws ecr get-login-password | docker login --username AWS --password-stdin "${repository_uri%%/*}" >/dev/null
  # No provenance or SBOM attestations: they add untagged manifests to ECR.
  docker buildx build --platform "$platform" --provenance=false --sbom=false \
    --tag "$image" --push .
fi

# 4. Register a task definition revision with the new image.
new_definition=$(jq --arg c "$CONTAINER" --arg image "$image" '
  .tags as $tags
  | .taskDefinition
  | .containerDefinitions |= map(if .name == $c then .image = $image else . end)
  | del(.taskDefinitionArn, .revision, .status, .requiresAttributes,
        .compatibilities, .registeredAt, .registeredBy, .deregisteredAt)
  | if ($tags | length) > 0 then .tags = $tags else . end' <<<"$base")
task_definition=$(aws ecs register-task-definition --cli-input-json "$new_definition" \
  --query 'taskDefinition.taskDefinitionArn' --output text)
log "registered ${task_definition##*/}"

# 5. Migrations first, as a one-off task with the new image. If they fail, the
#    running service is left alone.
log "running database migrations"
if ! run_one_off "$task_definition" "" "deploy-migrate" python -m app.db.migrate; then
  die "migrations failed; the service was not updated"
fi

# 6. Roll the service and wait. The deployment circuit breaker rolls back a
#    revision that never becomes healthy, so check which one ended up running.
log "updating service $SERVICE"
aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" \
  --task-definition "$task_definition" >/dev/null
log "waiting for the service to become stable (a few minutes)"
aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE" ||
  die "the service did not become stable; see: aws ecs describe-services --cluster $CLUSTER --services $SERVICE"

running=$(service_task_definition)
[[ "$running" == "$task_definition" ]] ||
  die "the deployment was rolled back to ${running##*/}; check the task logs in $LOG_GROUP"

# 7. A quick check through the load balancer.
alb=$(aws elbv2 describe-load-balancers --names "$PROJECT" \
  --query 'LoadBalancers[0].DNSName' --output text)
if curl -fsSL --max-time 10 "http://$alb/healthz" >/dev/null; then
  log "deployed $IMAGE_TAG: http://$alb is healthy"
else
  die "deployed $IMAGE_TAG, but http://$alb/healthz did not answer"
fi

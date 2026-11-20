#!/usr/bin/env bash
# After "terraform destroy": list anything from this project that is still in
# the account, billed or not. Read-only. Prints "nothing left" when clean.
#
#   infra/scripts/leftovers.sh
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 1
# shellcheck source=infra/scripts/lib.sh
source infra/scripts/lib.sh
require_tools aws jq

found=0
report() {
  local what="$1" items
  read -r -a items <<<"$2"
  if [[ ${#items[@]} -gt 0 && "${items[0]}" != "None" ]]; then
    found=1
    printf '%s:\n' "$what"
    printf '  %s\n' "${items[@]}"
  fi
}

report "Tagged resources (project=$PROJECT)" "$(aws resourcegroupstaggingapi get-resources \
  --tag-filters "Key=project,Values=$PROJECT" --query 'ResourceTagMappingList[].ResourceARN' --output text)"
report "ECS clusters" "$(aws ecs describe-clusters --clusters "$CLUSTER" \
  --query "clusters[?status=='ACTIVE'].clusterName" --output text)"
report "ECS task definitions (free, but listed)" "$(aws ecs list-task-definitions \
  --family-prefix "$FAMILY" --query 'taskDefinitionArns' --output text)"
report "ECR repositories" "$(aws ecr describe-repositories --repository-names "$REPOSITORY" \
  --query 'repositories[].repositoryName' --output text 2>/dev/null)"
report "Load balancers" "$(aws elbv2 describe-load-balancers --names "$PROJECT" \
  --query 'LoadBalancers[].LoadBalancerName' --output text 2>/dev/null)"
report "RDS instances" "$(aws rds describe-db-instances --db-instance-identifier "$PROJECT" \
  --query 'DBInstances[].DBInstanceIdentifier' --output text 2>/dev/null)"
report "RDS snapshots" "$(aws rds describe-db-snapshots --db-instance-identifier "$PROJECT" \
  --query 'DBSnapshots[].DBSnapshotIdentifier' --output text 2>/dev/null)"
report "ElastiCache" "$(aws elasticache describe-replication-groups --replication-group-id "$PROJECT" \
  --query 'ReplicationGroups[].ReplicationGroupId' --output text 2>/dev/null)"
report "SSM parameters under /$PROJECT" "$(aws ssm get-parameters-by-path --path "/$PROJECT" \
  --recursive --query 'Parameters[].Name' --output text)"
report "Secrets (RDS-managed)" "$(aws secretsmanager list-secrets \
  --query "SecretList[?starts_with(Name, 'rds!')].Name" --output text)"
report "Log groups" "$(aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" \
  --query 'logGroups[].logGroupName' --output text)"
report "VPCs tagged $PROJECT" "$(aws ec2 describe-vpcs --filters "Name=tag:project,Values=$PROJECT" \
  --query 'Vpcs[].VpcId' --output text)"
report "Elastic IPs (billed when idle)" "$(aws ec2 describe-addresses --query 'Addresses[].PublicIp' --output text)"
report "Network interfaces tagged $PROJECT" "$(aws ec2 describe-network-interfaces \
  --filters "Name=tag:project,Values=$PROJECT" --query 'NetworkInterfaces[].NetworkInterfaceId' --output text)"
report "Terraform state buckets" "$(aws s3api list-buckets \
  --query "Buckets[?starts_with(Name, '$PROJECT-tfstate-')].Name" --output text)"

if [[ $found -eq 0 ]]; then
  echo "nothing left for $PROJECT in $AWS_REGION"
fi

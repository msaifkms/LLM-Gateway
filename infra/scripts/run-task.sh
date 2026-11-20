#!/usr/bin/env bash
# Run one command in a one-off ECS task, with the image the service is running
# now, and print its output. For migrations and admin CLI commands against the
# cloud database, which is reachable only from inside the VPC.
#
#   infra/scripts/run-task.sh python -m app.db.migrate
#   infra/scripts/run-task.sh python -m app.cli create-user alice
#   infra/scripts/run-task.sh python -m app.cli issue-key alice --to-ssm /llm-gateway/keys/alice
#   infra/scripts/run-task.sh python -m app.cli kill-switch on --reason "incident"
#
# The task runs as the admin task role, which may only create parameters under
# /llm-gateway/keys/. Its output goes to CloudWatch Logs, so issue-key is
# refused unless --to-ssm is given: a printed key would be stored in the logs.
# Needs: aws (v2), jq.
set -euo pipefail
cd "$(dirname "$0")/../.."
# shellcheck source=infra/scripts/lib.sh
source infra/scripts/lib.sh

[[ $# -gt 0 ]] || die "usage: $0 COMMAND [ARGS...]   (for example: python -m app.cli list-keys)"
require_tools aws jq

args=" $* "
if [[ "$args" == *" issue-key "* && "$args" != *" --to-ssm "* ]]; then
  die "issue-key would print the key into CloudWatch Logs. Add --to-ssm /$PROJECT/keys/<name>."
fi

[[ "$(service_state)" == "active" ]] ||
  die "ECS service $SERVICE not found in $AWS_REGION. Run terraform apply and deploy first."

account_id=$(aws sts get-caller-identity --query Account --output text)
admin_role="arn:aws:iam::$account_id:role/$PROJECT-admin-task"

run_one_off "$(service_task_definition)" "$admin_role" "run-task" "$@"

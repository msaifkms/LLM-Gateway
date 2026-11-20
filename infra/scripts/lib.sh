# shellcheck shell=bash
# Shared helpers for deploy.sh, run-task.sh and leftovers.sh. Source, don't run.
#
# Resources are found by name (the Terraform "project" variable, default
# llm-gateway), so the scripts work without Terraform state: from a laptop or
# from CI. Needs: aws (v2), jq.

PROJECT="${PROJECT:-llm-gateway}"
export AWS_REGION="${AWS_REGION:-us-east-2}"
export AWS_DEFAULT_REGION="$AWS_REGION"
export AWS_PAGER=""

# Names Terraform gives the resources (used by the scripts that source this).
# shellcheck disable=SC2034
{
  CLUSTER="$PROJECT"
  SERVICE="$PROJECT"
  FAMILY="$PROJECT"
  REPOSITORY="$PROJECT"
  CONTAINER="gateway"
  LOG_GROUP="/ecs/$PROJECT"
}

log() { printf '==> %s\n' "$*" >&2; }
die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

require_tools() {
  local tool
  for tool in "$@"; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is required but not installed"
  done
}

# Prints "active" or "missing". Any other failure (credentials, permissions) is
# an error, so a real problem is never mistaken for "nothing deployed".
service_state() {
  local out
  if ! out=$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
    --query 'services[0].status' --output text 2>&1); then
    if grep -q 'ClusterNotFoundException' <<<"$out"; then
      echo missing
      return
    fi
    die "could not describe the ECS service: $out"
  fi
  if [[ "$out" == "ACTIVE" ]]; then echo active; else echo missing; fi
}

# The service's network settings, reused for one-off tasks (same subnets and
# security group, so they can reach the database).
service_network_config() {
  aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
    --query 'services[0].networkConfiguration' --output json
}

service_task_definition() {
  aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
    --query 'services[0].taskDefinition' --output text
}

# Print a stopped task's container output from CloudWatch Logs.
print_task_logs() {
  local task_id="$1" stream attempt messages
  stream="gateway/$CONTAINER/$task_id"
  for attempt in 1 2 3 4 5; do
    if messages=$(aws logs get-log-events --log-group-name "$LOG_GROUP" \
      --log-stream-name "$stream" --start-from-head --output json 2>/dev/null |
      jq -r '.events[].message') && [[ -n "$messages" ]]; then
      printf '%s\n' "$messages"
      return
    fi
    sleep $((attempt * 2)) # logs can lag the task by a few seconds
  done
  log "no log output found in $LOG_GROUP/$stream"
}

# run_one_off TASK_DEFINITION TASK_ROLE_ARN STARTED_BY COMMAND...
# Runs COMMAND once in a new task, waits for it, prints its output and returns
# its exit code. An empty TASK_ROLE_ARN keeps the task definition's role.
run_one_off() {
  local task_definition="$1" role="$2" started_by="$3"
  shift 3

  local network overrides result task_arn task_id exit_code reason
  network=$(service_network_config)
  # The command arrives NUL-separated on stdin, so arguments such as "-m" can
  # never be read as jq options, and spaces inside an argument survive.
  overrides=$(printf '%s\0' "$@" | jq -cRs --arg c "$CONTAINER" --arg role "$role" \
    '{containerOverrides: [{name: $c, command: split("\u0000")[:-1]}]}
     + (if $role == "" then {} else {taskRoleArn: $role} end)')

  result=$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$task_definition" \
    --launch-type FARGATE --platform-version LATEST \
    --network-configuration "$network" --overrides "$overrides" \
    --started-by "$started_by" --propagate-tags TASK_DEFINITION --output json)
  task_arn=$(jq -r '.tasks[0].taskArn // empty' <<<"$result")
  [[ -n "$task_arn" ]] || die "the task did not start: $(jq -c '.failures' <<<"$result")"
  task_id="${task_arn##*/}"

  log "task $task_id started; waiting for it to finish"
  aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$task_arn" ||
    die "task $task_id did not stop in time; check: aws ecs describe-tasks --cluster $CLUSTER --tasks $task_id"

  result=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task_arn" --output json)
  exit_code=$(jq -r --arg c "$CONTAINER" \
    '.tasks[0].containers[] | select(.name == $c) | .exitCode // empty' <<<"$result")
  reason=$(jq -r '.tasks[0].stoppedReason // ""' <<<"$result")

  log "output of task $task_id:"
  print_task_logs "$task_id"

  if [[ -z "$exit_code" ]]; then
    log "the container never ran: $reason"
    return 1
  fi
  log "task $task_id exited with status $exit_code"
  return "$exit_code"
}

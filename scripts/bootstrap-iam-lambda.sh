#!/usr/bin/env bash
# IAM for the alarm -> DevOps Agent investigation bridge (Lambda execution role).
# Run from your own terminal with credentials for the demo account:
#   bash scripts/bootstrap-iam-lambda.sh
set -euo pipefail

ROLE=checkout-demo-alarm-to-investigation
AGENT_SPACE_ID=$(cat "$(dirname "$0")/../.agent-space-id")
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=us-east-1

TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

if aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  echo "role $ROLE already exists"
else
  aws iam create-role --role-name "$ROLE" --assume-role-policy-document "$TRUST" \
    --description "Lambda: CloudWatch alarm -> AWS DevOps Agent CreateBacklogTask" \
    --query 'Role.Arn' --output text
fi

aws iam attach-role-policy --role-name "$ROLE" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

# Least privilege: only open investigations in this one Agent Space
aws iam put-role-policy --role-name "$ROLE" --policy-name create-investigation --policy-document "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [{
    \"Effect\": \"Allow\",
    \"Action\": [\"aidevops:CreateBacklogTask\"],
    \"Resource\": \"arn:aws:aidevops:${REGION}:${ACCOUNT}:agentspace/${AGENT_SPACE_ID}\"
  }]
}"

echo "done: $(aws iam get-role --role-name "$ROLE" --query 'Role.Arn' --output text)"

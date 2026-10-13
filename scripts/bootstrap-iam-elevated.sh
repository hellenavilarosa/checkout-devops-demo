#!/usr/bin/env bash
# Elevated role for AWS DevOps Agent "directed actions" (Agent Actions) in the demo.
#
# Directed actions are defense-in-depth: (1) preference enabled on the Agent Space,
# (2) THIS role registered on the AWS account association, (3) operator approval
# per action in chat. The role is the CEILING of what the agent can ever do here;
# each approved action gets credentials narrowed to that one operation/resource.
#
# We deliberately do NOT attach AIDevOpsAgentActionsPolicy (broad). The demo only
# needs the agent to silence/restore the checkout-api alarms while a fix is in PR.
#
# Run from your own terminal with credentials for the demo account:
#   bash scripts/bootstrap-iam-elevated.sh
# Then register the printed ARN in the DevOps Agent console:
#   Agent Space -> Access (associações) -> conta AWS -> Elevated role -> colar ARN
set -euo pipefail

ROLE=DevOpsAgent-ElevatedAction-checkout-demo
REGION=us-east-1
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

TRUST=$(cat <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
  "Principal":{"Service":"aidevops.amazonaws.com"},
  "Action":["sts:AssumeRole","sts:SetSourceIdentity","sts:TagSession"],
  "Condition":{"StringEquals":{"aws:SourceAccount":"${ACCOUNT}"},
               "ArnLike":{"aws:SourceArn":"arn:aws:aidevops:${REGION}:${ACCOUNT}:agentspace/*"}}}]}
EOF
)

POLICY=$(cat <<EOF
{"Version":"2012-10-17","Statement":[
  {"Sid":"SilenceOrRestoreDemoAlarms","Effect":"Allow",
   "Action":["cloudwatch:DisableAlarmActions","cloudwatch:EnableAlarmActions","cloudwatch:SetAlarmState"],
   "Resource":"arn:aws:cloudwatch:${REGION}:${ACCOUNT}:alarm:checkout-api-*"},
  {"Sid":"PauseOrResumeAutoInvestigation","Effect":"Allow",
   "Action":["events:DisableRule","events:EnableRule"],
   "Resource":"arn:aws:events:${REGION}:${ACCOUNT}:rule/checkout-demo-alarm-to-investigation"}
]}
EOF
)

if aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  echo "role $ROLE already exists; updating trust + policy"
  aws iam update-assume-role-policy --role-name "$ROLE" --policy-document "$TRUST"
else
  aws iam create-role --role-name "$ROLE" --assume-role-policy-document "$TRUST" \
    --description "AWS DevOps Agent elevated role (directed actions) - demo scope: checkout-api alarms + investigation rule" \
    --query 'Role.Arn' --output text
fi
aws iam put-role-policy --role-name "$ROLE" --policy-name checkout-demo-directed-actions --policy-document "$POLICY"

echo
echo "ARN para registrar no console do DevOps Agent (Access -> conta -> Elevated role):"
aws iam get-role --role-name "$ROLE" --query 'Role.Arn' --output text

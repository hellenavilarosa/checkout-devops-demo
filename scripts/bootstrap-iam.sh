#!/usr/bin/env bash
# One-time IAM bootstrap for the demo. Creates the EKS Pod Identity role used by
# the amazon-cloudwatch-observability add-on (Container Insights + pod logs to
# CloudWatch), which is what gives AWS DevOps Agent visibility into the cluster.
#
# Run from your own terminal with credentials for the demo account:
#   bash scripts/bootstrap-iam.sh
set -euo pipefail

ROLE=eks-agentic-cloudwatch-agent

TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"pods.eks.amazonaws.com"},"Action":["sts:AssumeRole","sts:TagSession"]}]}'

if aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  echo "role $ROLE already exists"
else
  aws iam create-role \
    --role-name "$ROLE" \
    --assume-role-policy-document "$TRUST" \
    --description "EKS Pod Identity role for amazon-cloudwatch-observability add-on" \
    --query 'Role.Arn' --output text
fi

aws iam attach-role-policy \
  --role-name "$ROLE" \
  --policy-arn arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy

echo "done: $(aws iam get-role --role-name "$ROLE" --query 'Role.Arn' --output text)"

#!/usr/bin/env bash
set -euo pipefail
CONTEXT="${KUBE_CONTEXT:-match-infra-tunnel}"
NAMESPACE="${KUBE_NAMESPACE:-daiki-ai-passport}"
SECRET_NAME="daiki-ai-passport-secrets"
BOOTSTRAP_JOB="keycloak-bootstrap-v6"
SENDER="ch.yutthapichai@gmail.com"

command -v kubectl >/dev/null 2>&1 || { echo "ERROR: kubectl not found" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "ERROR: jq not found" >&2; exit 1; }
command -v openssl >/dev/null 2>&1 || { echo "ERROR: openssl not found" >&2; exit 1; }
kubectl --context "$CONTEXT" -n "$NAMESPACE" get secret "$SECRET_NAME" >/dev/null
kubectl --context "$CONTEXT" -n "$NAMESPACE" get job "$BOOTSTRAP_JOB" >/dev/null

printf 'Gmail App Password for %s: ' "$SENDER"
IFS= read -r -s GMAIL_APP_PASSWORD
printf '\n'
GMAIL_APP_PASSWORD="${GMAIL_APP_PASSWORD// /}"
GMAIL_APP_PASSWORD="${GMAIL_APP_PASSWORD//$'\r'/}"
if [[ ${#GMAIL_APP_PASSWORD} -lt 16 ]]; then
  echo "ERROR: Gmail App Password looks too short. Use a Google App Password, not your normal Gmail password." >&2
  exit 1
fi
PASS_B64="$(printf '%s' "$GMAIL_APP_PASSWORD" | openssl base64 -A)"
FROM_B64="$(printf '%s' "$SENDER" | openssl base64 -A)"
kubectl --context "$CONTEXT" -n "$NAMESPACE" patch secret "$SECRET_NAME" --type merge \
  -p "{\"data\":{\"gmail-app-password\":\"$PASS_B64\",\"admin-email\":\"$FROM_B64\"}}" >/dev/null
unset GMAIL_APP_PASSWORD PASS_B64 FROM_B64

echo "Gmail SMTP credential stored in Kubernetes Secret."
RUN_JOB="keycloak-bootstrap-smtp-$(date +%s)"
kubectl --context "$CONTEXT" -n "$NAMESPACE" get job "$BOOTSTRAP_JOB" -o json | \
  jq --arg name "$RUN_JOB" 'del(.metadata.creationTimestamp,.metadata.generation,.metadata.resourceVersion,.metadata.uid,.metadata.managedFields,.status,.spec.selector,.spec.template.metadata.creationTimestamp,.spec.template.metadata.labels["controller-uid"],.spec.template.metadata.labels["batch.kubernetes.io/controller-uid"],.spec.template.metadata.labels["batch.kubernetes.io/job-name"],.spec.template.metadata.labels["job-name"]) | .metadata.name=$name | .metadata.labels=(.metadata.labels//{}) | .spec.template.metadata.labels=(.spec.template.metadata.labels//{})' | \
  kubectl --context "$CONTEXT" -n "$NAMESPACE" create -f - >/dev/null
kubectl --context "$CONTEXT" -n "$NAMESPACE" wait --for=condition=complete "job/$RUN_JOB" --timeout=300s >/dev/null
if ! kubectl --context "$CONTEXT" -n "$NAMESPACE" logs "job/$RUN_JOB" | grep -q '^gmail-smtp-configured$'; then
  echo "ERROR: Keycloak Gmail SMTP configuration did not complete." >&2
  kubectl --context "$CONTEXT" -n "$NAMESPACE" logs "job/$RUN_JOB" | grep -E 'gmail-smtp-|bootstrap-complete' || true
  exit 1
fi
echo "PASS: Keycloak Gmail SMTP is configured."
echo "Sender: Daiki AI Passport <$SENDER>"
echo "Reset page: https://daiki-aipass.matchchemical.co/forgot-password"

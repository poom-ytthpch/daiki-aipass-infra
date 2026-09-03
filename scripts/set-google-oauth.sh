#!/usr/bin/env bash
set -euo pipefail

CONTEXT="${KUBE_CONTEXT:-match-infra-tunnel}"
NAMESPACE="${KUBE_NAMESPACE:-daiki-ai-passport}"
SECRET_NAME="daiki-ai-passport-secrets"
BOOTSTRAP_JOB="keycloak-bootstrap-v10"

command -v kubectl >/dev/null 2>&1 || { echo "ERROR: kubectl not found" >&2; exit 1; }
command -v openssl >/dev/null 2>&1 || { echo "ERROR: openssl not found" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "ERROR: jq not found" >&2; exit 1; }

kubectl --context "$CONTEXT" -n "$NAMESPACE" get secret "$SECRET_NAME" >/dev/null
kubectl --context "$CONTEXT" -n "$NAMESPACE" get job "$BOOTSTRAP_JOB" >/dev/null

printf 'Google Client ID: '
IFS= read -r GOOGLE_CLIENT_ID
printf 'Google Client Secret: '
IFS= read -r -s GOOGLE_CLIENT_SECRET
printf '\n'

GOOGLE_CLIENT_ID="${GOOGLE_CLIENT_ID//$'\r'/}"
GOOGLE_CLIENT_SECRET="${GOOGLE_CLIENT_SECRET//$'\r'/}"

if [[ -z "$GOOGLE_CLIENT_ID" || ! "$GOOGLE_CLIENT_ID" =~ \.apps\.googleusercontent\.com$ ]]; then
  echo "ERROR: Client ID must end with .apps.googleusercontent.com" >&2
  exit 1
fi
if [[ -z "$GOOGLE_CLIENT_SECRET" || ${#GOOGLE_CLIENT_SECRET} -lt 12 ]]; then
  echo "ERROR: Client Secret looks empty or too short" >&2
  exit 1
fi

ID_B64="$(printf '%s' "$GOOGLE_CLIENT_ID" | openssl base64 -A)"
SECRET_B64="$(printf '%s' "$GOOGLE_CLIENT_SECRET" | openssl base64 -A)"

kubectl --context "$CONTEXT" -n "$NAMESPACE" patch secret "$SECRET_NAME" \
  --type merge \
  -p "{\"data\":{\"google-client-id\":\"${ID_B64}\",\"google-client-secret\":\"${SECRET_B64}\"}}" >/dev/null

unset GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET ID_B64 SECRET_B64

echo "Google OAuth credentials stored in Kubernetes Secret."

RUN_JOB="keycloak-bootstrap-google-$(date +%s)"
kubectl --context "$CONTEXT" -n "$NAMESPACE" get job "$BOOTSTRAP_JOB" -o json |
  jq --arg name "$RUN_JOB" '''
    del(.metadata.creationTimestamp,.metadata.generation,.metadata.resourceVersion,.metadata.uid,.metadata.managedFields,.status,
        .spec.selector,.spec.template.metadata.creationTimestamp,
        .spec.template.metadata.labels["controller-uid"],
        .spec.template.metadata.labels["batch.kubernetes.io/controller-uid"],
        .spec.template.metadata.labels["batch.kubernetes.io/job-name"],
        .spec.template.metadata.labels["job-name"]) |
    .metadata.name=$name
  ''' | kubectl --context "$CONTEXT" -n "$NAMESPACE" create -f - >/dev/null

echo "Running Keycloak Google IdP bootstrap: $RUN_JOB"
kubectl --context "$CONTEXT" -n "$NAMESPACE" wait \
  --for=condition=complete "job/$RUN_JOB" --timeout=300s >/dev/null

if ! kubectl --context "$CONTEXT" -n "$NAMESPACE" logs "job/$RUN_JOB" | grep -q 'google-idp-configured'; then
  echo "ERROR: Keycloak bootstrap completed but Google IdP was not configured." >&2
  kubectl --context "$CONTEXT" -n "$NAMESPACE" logs "job/$RUN_JOB" | grep -E 'google-idp-|bootstrap-complete' || true
  exit 1
fi

echo "Keycloak Google IdP configured."

kubectl --context "$CONTEXT" -n "$NAMESPACE" rollout restart deployment/frontend >/dev/null
kubectl --context "$CONTEXT" -n "$NAMESPACE" rollout status deployment/frontend --timeout=180s >/dev/null

echo "Frontend restarted with Google Client ID."

LOCATION="$(curl -fsSI 'https://daiki-aipass.matchchemical.co/api/auth/login?provider=google' | awk 'BEGIN{IGNORECASE=1} /^location:/{sub(/\r$/,""); print substr($0,index($0,$2)); exit}')"
if [[ "$LOCATION" != *"kc_idp_hint=google"* ]]; then
  echo "ERROR: Google login route did not redirect to Keycloak Google IdP." >&2
  echo "Redirect target: ${LOCATION:-<missing>}" >&2
  exit 1
fi

echo "PASS: Google OAuth is active."
echo "Login URL: https://daiki-aipass.matchchemical.co/login"

#!/bin/sh
# ---------------------------------------------------------------------------
# MinIO bootstrap — idempotent.
#
# Creates:
#   1. Bucket            $S3_BUCKET_NAME
#   2. Policy            mas-app  (least-privilege: only this bucket)
#   3. Service account   $MINIO_APP_ACCESS_KEY / $MINIO_APP_SECRET_KEY
#                        owned by root, bound to mas-app policy
#
# Used by the `minio-bootstrap` init container (see docker-compose.yml).
# Runs as `mc` from minio/mc image. Safe to re-run any number of times.
#
# Source of truth:
#   - docs/06-security.md sec.12  (least-privilege rationale)
#   - docs/07-deployment.md sec.12 (env contract, bootstrap order)
# ---------------------------------------------------------------------------

set -eu

# ---- required env -----------------------------------------------------------
: "${MINIO_ROOT_USER:?MINIO_ROOT_USER must be set}"
: "${MINIO_ROOT_PASSWORD:?MINIO_ROOT_PASSWORD must be set}"
: "${MINIO_APP_ACCESS_KEY:?MINIO_APP_ACCESS_KEY must be set}"
: "${MINIO_APP_SECRET_KEY:?MINIO_APP_SECRET_KEY must be set}"
: "${S3_BUCKET_NAME:?S3_BUCKET_NAME must be set}"

MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://minio:9000}"
ALIAS="local"
POLICY_NAME="mas-app"
POLICY_FILE="/tmp/mas-app-policy.json"

log() {
    # Plain stdout — JSON logging is for the app, this is one-shot infra.
    printf '[minio-bootstrap] %s\n' "$*"
}

# ---- 1. wait until MinIO actually accepts our root credentials --------------
# `depends_on: condition: service_healthy` already waits for /health/live,
# but admin operations need the root login to be *registered*, which can lag
# the live endpoint by a fraction of a second on cold start. Retry briefly.
log "waiting for MinIO at ${MINIO_ENDPOINT} ..."
i=0
until mc alias set "$ALIAS" "$MINIO_ENDPOINT" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -ge 30 ]; then
        log "ERROR: MinIO never accepted root credentials after 30 attempts"
        exit 1
    fi
    sleep 2
done
log "alias '${ALIAS}' configured"

# ---- 2. create bucket (idempotent) ------------------------------------------
log "ensuring bucket '${S3_BUCKET_NAME}' exists"
mc mb --ignore-existing "${ALIAS}/${S3_BUCKET_NAME}"

# ---- 3. write least-privilege policy ----------------------------------------
# Only ops the app needs (storage.py contract):
#   - GetObject / PutObject / DeleteObject — attachment CRUD
#   - ListBucket                            — for orphan-scan tooling later
#   - GetBucketLocation                     — boto3 client startup probe
# Everything else (admin, other buckets, IAM) — denied by absence.
log "writing policy file -> ${POLICY_FILE}"
cat > "$POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AppBucketObjectsRW",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject"
      ],
      "Resource": [
        "arn:aws:s3:::${S3_BUCKET_NAME}/*"
      ]
    },
    {
      "Sid": "AppBucketList",
      "Effect": "Allow",
      "Action": [
        "s3:ListBucket",
        "s3:GetBucketLocation"
      ],
      "Resource": [
        "arn:aws:s3:::${S3_BUCKET_NAME}"
      ]
    }
  ]
}
EOF

# ---- 4. create or update policy (idempotent) --------------------------------
# `policy create` errors if the policy exists, `policy update` errors if it
# doesn't — try create, fall back to update. Both end states are equivalent.
log "creating/updating policy '${POLICY_NAME}'"
if ! mc admin policy create "$ALIAS" "$POLICY_NAME" "$POLICY_FILE" >/dev/null 2>&1; then
    mc admin policy update "$ALIAS" "$POLICY_NAME" "$POLICY_FILE"
fi

# ---- 5. create or update service account (idempotent) -----------------------
# `svcacct info` returns 0 if the account exists, non-zero otherwise.
# - exists -> edit (rotates secret + re-binds policy)
# - absent -> add  (creates owned by root with the explicit access/secret pair)
log "ensuring service account '${MINIO_APP_ACCESS_KEY}'"
if mc admin user svcacct info "$ALIAS" "$MINIO_APP_ACCESS_KEY" >/dev/null 2>&1; then
    log "  -> exists, updating secret + policy"
    mc admin user svcacct edit "$ALIAS" "$MINIO_APP_ACCESS_KEY" \
        --secret-key "$MINIO_APP_SECRET_KEY" \
        --policy "$POLICY_FILE"
else
    log "  -> creating new service account"
    mc admin user svcacct add "$ALIAS" "$MINIO_ROOT_USER" \
        --access-key "$MINIO_APP_ACCESS_KEY" \
        --secret-key "$MINIO_APP_SECRET_KEY" \
        --policy "$POLICY_FILE"
fi

# ---- 6. cleanup -------------------------------------------------------------
# Policy file lives in tmpfs of this short-lived container; remove anyway
# so that if someone re-mounts the container interactively it's gone.
rm -f "$POLICY_FILE"

log "done — bucket=${S3_BUCKET_NAME}, policy=${POLICY_NAME}, svcacct=${MINIO_APP_ACCESS_KEY}"
exit 0

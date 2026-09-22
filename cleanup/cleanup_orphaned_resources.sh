#!/usr/bin/env bash
# Delete orphaned Compute Engine resources (unattached disks, old snapshots,
# reserved-but-unused static IPs) older than N days.
#
# Safety model:
#   * Dry-run is the default. Nothing is deleted unless --execute is passed.
#   * Resources labelled keep=true (configurable) are always skipped.
#   * Disks are snapshotted before deletion unless --no-snapshot is given.
#   * Every decision is appended to a log file for audit.
#
# Usage:
#   cleanup_orphaned_resources.sh --project example-project-dev [--min-age-days 30]
#       [--exclude-label keep=true] [--execute] [--no-snapshot] [--log-dir ./out]
#       [--only disks|snapshots|addresses]
set -euo pipefail

PROJECT="${GOOGLE_CLOUD_PROJECT:-}"
MIN_AGE_DAYS="${CLEANUP_MIN_AGE_DAYS:-30}"
EXCLUDE_LABEL="${CLEANUP_EXCLUDE_LABEL:-keep=true}"
LOG_DIR="${CLEANUP_LOG_DIR:-./out}"
EXECUTE=false
SNAPSHOT_BEFORE_DELETE=true
ONLY=""

usage() { sed -n '2,20p' "$0"; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT="$2"; shift 2 ;;
    --min-age-days) MIN_AGE_DAYS="$2"; shift 2 ;;
    --exclude-label) EXCLUDE_LABEL="$2"; shift 2 ;;
    --log-dir) LOG_DIR="$2"; shift 2 ;;
    --only) ONLY="$2"; shift 2 ;;
    --execute) EXECUTE=true; shift ;;
    --no-snapshot) SNAPSHOT_BEFORE_DELETE=false; shift ;;
    -h|--help) usage 0 ;;
    *) echo "Unknown argument: $1" >&2; usage 1 ;;
  esac
done

[[ -n "$PROJECT" ]] || { echo "ERROR: --project or GOOGLE_CLOUD_PROJECT is required" >&2; exit 1; }
command -v gcloud >/dev/null || { echo "ERROR: gcloud not found" >&2; exit 1; }
[[ "$MIN_AGE_DAYS" =~ ^[0-9]+$ ]] || { echo "ERROR: --min-age-days must be an integer" >&2; exit 1; }

EXCLUDE_KEY="${EXCLUDE_LABEL%%=*}"
EXCLUDE_VAL="${EXCLUDE_LABEL#*=}"
mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_DIR}/cleanup-$(date -u +%Y%m%dT%H%M%SZ).log"
MODE="DRY-RUN"; $EXECUTE && MODE="EXECUTE"

# Cut-off timestamp in RFC3339, portable across GNU and BSD date.
if date -u -d "-${MIN_AGE_DAYS} days" +%Y-%m-%dT%H:%M:%SZ >/dev/null 2>&1; then
  CUTOFF="$(date -u -d "-${MIN_AGE_DAYS} days" +%Y-%m-%dT%H:%M:%SZ)"
else
  CUTOFF="$(date -u -v-"${MIN_AGE_DAYS}"d +%Y-%m-%dT%H:%M:%SZ)"
fi

log() { printf '%s %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "[$MODE]" "$*" | tee -a "$LOG_FILE"; }

run() {
  # Executes the command only in --execute mode; always logs it.
  if $EXECUTE; then
    log "RUN  $*"
    "$@" >>"$LOG_FILE" 2>&1 || log "FAIL $*"
  else
    log "PLAN $*"
  fi
}

declare -i CANDIDATES=0 SKIPPED=0

cleanup_disks() {
  log "== Unattached disks (project=$PROJECT, unattached since before $CUTOFF)"
  # Fields: name zone lastDetach(or creation) keepLabel sizeGb
  gcloud compute disks list --project "$PROJECT" \
    --filter="-users:* AND (lastDetachTimestamp<${CUTOFF} OR (NOT lastDetachTimestamp:* AND creationTimestamp<${CUTOFF}))" \
    --format="value(name,zone.basename(),sizeGb,labels.${EXCLUDE_KEY})" |
  while IFS=$'\t' read -r name zone size keep; do
    [[ -n "$name" ]] || continue
    if [[ "${keep:-}" == "$EXCLUDE_VAL" ]]; then
      log "SKIP disk $zone/$name (${size}GB) labelled ${EXCLUDE_LABEL}"; SKIPPED+=1; continue
    fi
    CANDIDATES+=1
    if $SNAPSHOT_BEFORE_DELETE; then
      run gcloud compute snapshots create "pre-delete-${name}-$(date -u +%Y%m%d)" \
        --project "$PROJECT" --source-disk "$name" --source-disk-zone "$zone" \
        --labels "source=cleanup,origin-disk=${name}" --quiet
    fi
    run gcloud compute disks delete "$name" --project "$PROJECT" --zone "$zone" --quiet
  done
}

cleanup_snapshots() {
  log "== Snapshots created before $CUTOFF"
  gcloud compute snapshots list --project "$PROJECT" \
    --filter="creationTimestamp<${CUTOFF} AND NOT labels.source=cleanup" \
    --format="value(name,storageBytes,labels.${EXCLUDE_KEY})" |
  while IFS=$'\t' read -r name bytes keep; do
    [[ -n "$name" ]] || continue
    if [[ "${keep:-}" == "$EXCLUDE_VAL" ]]; then
      log "SKIP snapshot $name labelled ${EXCLUDE_LABEL}"; SKIPPED+=1; continue
    fi
    CANDIDATES+=1
    run gcloud compute snapshots delete "$name" --project "$PROJECT" --quiet
  done
}

cleanup_addresses() {
  log "== Reserved static addresses not in use, created before $CUTOFF"
  gcloud compute addresses list --project "$PROJECT" \
    --filter="status=RESERVED AND creationTimestamp<${CUTOFF}" \
    --format="value(name,region.basename(),address,labels.${EXCLUDE_KEY})" |
  while IFS=$'\t' read -r name region addr keep; do
    [[ -n "$name" ]] || continue
    if [[ "${keep:-}" == "$EXCLUDE_VAL" ]]; then
      log "SKIP address $name ($addr) labelled ${EXCLUDE_LABEL}"; SKIPPED+=1; continue
    fi
    CANDIDATES+=1
    if [[ -z "$region" || "$region" == "global" ]]; then
      run gcloud compute addresses delete "$name" --project "$PROJECT" --global --quiet
    else
      run gcloud compute addresses delete "$name" --project "$PROJECT" --region "$region" --quiet
    fi
  done
}

log "Starting cleanup project=$PROJECT min_age_days=$MIN_AGE_DAYS exclude=$EXCLUDE_LABEL snapshot_first=$SNAPSHOT_BEFORE_DELETE"
case "$ONLY" in
  "")        cleanup_disks; cleanup_snapshots; cleanup_addresses ;;
  disks)     cleanup_disks ;;
  snapshots) cleanup_snapshots ;;
  addresses) cleanup_addresses ;;
  *) echo "ERROR: --only must be disks|snapshots|addresses" >&2; exit 1 ;;
esac
log "Done. Log: $LOG_FILE"
if ! $EXECUTE; then
  log "Dry-run only. Re-run with --execute to apply."
fi

#!/usr/bin/env bash
# Scale non-production GKE node pools to zero (evening) or back to their
# configured size (morning). Intended to run from Cloud Scheduler -> Cloud Run
# Job or a CI cron, with the target pools declared in a small config file.
#
# Config file format (whitespace separated, # comments allowed):
#   <project> <cluster> <location> <node-pool> <daytime-node-count>
#   example-project-dev  example-dev-gke  asia-south1  general  1
#
# Usage:
#   gke-scale-down-nonprod.sh --config pools.conf down          # scale to 0
#   gke-scale-down-nonprod.sh --config pools.conf up            # restore daytime size
#   gke-scale-down-nonprod.sh --config pools.conf down --execute
#
# Schedule (Cloud Scheduler, Asia/Kolkata):
#   down: "0 21 * * 1-5"   up: "30 7 * * 1-5"
# Alternative: the Kubernetes-native kube-downscaler (kubernetes/kube-downscaler-values.yaml).
set -euo pipefail

CONFIG=""
ACTION=""
EXECUTE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --execute) EXECUTE=true; shift ;;
    up|down) ACTION="$1"; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

[[ -n "$CONFIG" && -r "$CONFIG" ]] || { echo "ERROR: --config <file> is required and must be readable" >&2; exit 1; }
[[ "$ACTION" == "up" || "$ACTION" == "down" ]] || { echo "ERROR: action must be 'up' or 'down'" >&2; exit 1; }
command -v gcloud >/dev/null || { echo "ERROR: gcloud not found" >&2; exit 1; }

MODE="DRY-RUN"; $EXECUTE && MODE="EXECUTE"
log() { printf '%s [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$MODE" "$*"; }

while read -r project cluster location pool day_count; do
  [[ -z "$project" || "$project" == \#* ]] && continue
  if [[ "$ACTION" == "down" ]]; then target=0; else target="$day_count"; fi

  # Refuse to touch anything that looks like production.
  if [[ "$project" == *prod* || "$cluster" == *prod* ]]; then
    log "REFUSE $project/$cluster/$pool matches 'prod'"; continue
  fi

  # Autoscaled pools: adjust min so the autoscaler cannot bring nodes back overnight.
  autoscaling="$(gcloud container node-pools describe "$pool" --project "$project" --cluster "$cluster" --location "$location" --format='value(autoscaling.enabled)' 2>/dev/null || echo "")"
  cmd_resize=(gcloud container clusters resize "$cluster" --project "$project" --location "$location" --node-pool "$pool" --num-nodes "$target" --quiet)
  if [[ "$autoscaling" == "True" ]]; then
    if [[ "$ACTION" == "down" ]]; then
      cmd_auto=(gcloud container clusters update "$cluster" --project "$project" --location "$location" --node-pool "$pool" --enable-autoscaling --min-nodes 0 --max-nodes 0 --quiet)
    else
      cmd_auto=(gcloud container clusters update "$cluster" --project "$project" --location "$location" --node-pool "$pool" --enable-autoscaling --min-nodes "$day_count" --max-nodes "$((day_count * 4))" --quiet)
    fi
    log "PLAN ${cmd_auto[*]}"
    $EXECUTE && "${cmd_auto[@]}"
  fi
  log "PLAN ${cmd_resize[*]}"
  $EXECUTE && "${cmd_resize[@]}"
done < "$CONFIG"

$EXECUTE || log "Dry-run only. Re-run with --execute to apply."

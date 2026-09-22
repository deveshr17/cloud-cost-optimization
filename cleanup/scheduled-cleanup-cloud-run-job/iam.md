# Service accounts for the scheduled job

Two dedicated service accounts, least privilege, no keys.

| Account | Purpose | Roles |
| --- | --- | --- |
| `finops-job-runner@…` | Identity of the Cloud Run Job | `roles/compute.viewer`, `roles/bigquery.jobUser`, `roles/bigquery.resourceViewer`, `roles/storage.objectViewer` (inventory), `roles/storage.objectCreator` on the reports bucket, `roles/monitoring.viewer`, `roles/secretmanager.secretAccessor` on the Slack secret only |
| `finops-scheduler-invoker@…` | Cloud Scheduler caller | `roles/run.invoker` on the job only |

```bash
PROJECT=example-project-dev
gcloud iam service-accounts create finops-job-runner --project "$PROJECT"
gcloud iam service-accounts create finops-scheduler-invoker --project "$PROJECT"

for role in roles/compute.viewer roles/bigquery.jobUser roles/bigquery.resourceViewer roles/monitoring.viewer; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member "serviceAccount:finops-job-runner@${PROJECT}.iam.gserviceaccount.com" --role "$role"
done

gcloud run jobs add-iam-policy-binding finops-weekly-report --region asia-south1 \
  --member "serviceAccount:finops-scheduler-invoker@${PROJECT}.iam.gserviceaccount.com" --role roles/run.invoker
```

Destructive cleanup (`cleanup_orphaned_resources.sh --execute`) runs under a
separate, human-approved identity with `roles/compute.storageAdmin`; the job
account deliberately cannot delete anything.

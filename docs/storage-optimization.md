# Cloud Storage optimization

Storage cost = bytes x class price + operations + retrieval + egress.
Optimising only the first term while ignoring the others is how a
lifecycle policy ends up costing more than it saves.

## Storage classes

| Class | Relative price | Min duration | Retrieval fee | Fits |
| --- | --- | --- | --- | --- |
| Standard | 1.0x | none | none | Serving, hot data, anything read weekly |
| Nearline | ~0.55x | 30 days | yes | Read about once a month: recent backups, monthly reports |
| Coldline | ~0.25x | 90 days | higher | Read about once a quarter: DR copies, older logs |
| Archive | ~0.1x | 365 days | highest | Read about once a year: compliance retention |

Deleting or transitioning before the minimum duration is billed as if the
object had stayed for the full period. Never add an `Archive` rule to a
bucket with a 30-day retention need.

## Decision procedure (what `storage_optimizer.py` does)

1. Get the age distribution of bytes (Storage Insights inventory report, or an aggregated object listing).
2. Get the 30-day access pattern: Class A/B operations and egress from Cloud Monitoring or the billing export.
3. Classify the bucket:
   - **Hot serving** (tens of millions of reads or > 1 TB egress a month): no transitions; look at Cloud CDN instead.
   - **Warm** (median object read within 30 days): Standard for 90 days, then Nearline, then Coldline.
   - **Cold**: Nearline at 30, Coldline at 90, Archive at 365 days.
4. Estimate retrieval fees from the read volume and skip a transition when they eat the saving.
5. Versioned buckets: delete noncurrent versions after N days and keep at most M newer versions.
6. Emit lifecycle JSON and the `gcloud storage buckets update` command.

## Lifecycle rule patterns

```json
{
  "lifecycle": {
    "rule": [
      {"action": {"type": "SetStorageClass", "storageClass": "NEARLINE"},
       "condition": {"age": 30, "matchesStorageClass": ["STANDARD"]}},
      {"action": {"type": "SetStorageClass", "storageClass": "COLDLINE"},
       "condition": {"age": 90, "matchesStorageClass": ["STANDARD", "NEARLINE"]}},
      {"action": {"type": "Delete"},
       "condition": {"isLive": false, "daysSinceNoncurrentTime": 30}},
      {"action": {"type": "Delete"},
       "condition": {"isLive": false, "numNewerVersions": 3}},
      {"action": {"type": "AbortIncompleteMultipartUpload"},
       "condition": {"age": 7}}
    ]
  }
}
```

Conditions inside one rule are ANDed; use separate rules for OR. `matchesPrefix`
scopes a rule to a path (`logs/`), which is how one bucket can hold hot and
cold data without splitting.

## Other levers

- **Autoclass**: bucket-level automatic transitions based on access, with no retrieval fees but a small per-object management fee. Good default for buckets with unknown patterns; not for tiny objects (fee dominates) or hot serving buckets.
- **Soft delete** (default 7 days) bills deleted bytes for the window; set to 0 for scratch buckets, keep for anything that matters.
- **Dual/multi-region** costs roughly 2x single-region; use only where an RTO requires it.
- **Egress**: Cloud CDN in front of public buckets; same-region compute for processing; VPC Service Controls do not affect cost but Private Google Access avoids NAT charges.
- **Object count**: millions of small objects make Class A operations and lifecycle evaluation expensive; batch into larger files (Avro/Parquet).

## Verification

Storage class changes show up in the billing export as different SKUs within
a day. Compare `storage.googleapis.com/storage/total_bytes` grouped by
`storage_class` before and after (Cloud Monitoring dashboard panel).

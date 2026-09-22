# Billing export schema notes

Reference for the columns used by `queries/*.sql`. Full schema:
<https://cloud.google.com/billing/docs/how-to/export-data-bigquery-tables/detailed-usage>.

## Time columns

| Column | Meaning | Use |
| --- | --- | --- |
| `_PARTITIONTIME` | Ingestion partition (day) | **Always filter on this** for cost control; it is what prunes the scan |
| `usage_start_time` / `usage_end_time` | Usage window of the line item | Bucketing by day/month |
| `export_time` | When the row was exported | Detecting late-arriving rows (billing can restate up to several days) |
| `invoice.month` | `YYYYMM` string | Reconciling to the invoice |

Because rows for a given day keep arriving for ~3 days, the anomaly
detection query excludes the two most recent days from its baseline.

## Identity columns

| Column | Notes |
| --- | --- |
| `billing_account_id` | Same for every row in one export |
| `project.id`, `project.name`, `project.number` | `project.id` is the stable key |
| `project.labels` (REPEATED key/value) | Project-level labels such as `env`, `team`, `cost-center` |
| `labels` (REPEATED key/value) | Resource-level labels; includes GKE cost allocation keys `k8s-namespace`, `k8s-workload-name`(*) |
| `system_labels` (REPEATED) | GCP-populated, e.g. `compute.googleapis.com/machine_spec`, `compute.googleapis.com/cores` |
| `resource.name` | Short resource name (detailed export only) |
| `resource.global_name` | Fully-qualified name, e.g. `//compute.googleapis.com/projects/p/zones/z/disks/d` |

(*) Workload labels require GKE cost allocation to be enabled; only
`k8s-namespace` is present on all clusters with the feature on.

## Money columns

| Column | Notes |
| --- | --- |
| `cost` | List-price cost **before** credits, in `currency` |
| `credits` (REPEATED) | Each `{name, amount, full_name, id, type}`; `amount` is negative |
| `credits.type` | `COMMITTED_USAGE_DISCOUNT`, `SUSTAINED_USAGE_DISCOUNT`, `DISCOUNT`, `PROMOTION`, `FREE_TIER`, `SPENDING_BASED_DISCOUNT`, `RESELLER_MARGIN`, `COMMITTED_USAGE_DISCOUNT_DOLLAR_BASE` |
| `cost_type` | `regular`, `tax`, `adjustment`, `rounding_error` |
| `cost_at_list` | Price before any negotiated discounts (present when pricing export is enabled) |
| `price.effective_price` | Unit price actually charged |

**Net cost** is therefore:

```sql
cost + IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)
```

## Usage columns

| Column | Notes |
| --- | --- |
| `usage.amount` | Quantity in `usage.unit` (e.g. `byte-seconds`) |
| `usage.amount_in_pricing_units` | Quantity in `usage.pricing_unit` (e.g. `gibibyte month`) |
| `sku.description` | Human SKU name; the substring filters in the queries rely on these (e.g. `'Storage PD Capacity'`, `'Network Egress'`) |

SKU descriptions are stable but not contractual. Pin filters by `sku.id`
once you know which SKUs matter to you.

## Gotchas

* `cost` is in the billing account currency; multi-currency accounts need `currency_conversion_rate`.
* `project.id` is NULL for account-level charges (support, taxes). Coalesce to `'(account)'`.
* Label values are case-sensitive; enforce lowercase via org policy or CI.
* Disk SKUs do not tell you if the disk is attached. That is why
  `unattached-disks-cost.sql` joins the export against a Cloud Asset
  Inventory export table.

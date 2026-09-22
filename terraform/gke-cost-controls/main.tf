locals {
  name_prefix = "${var.org}-${var.env}"
  labels      = merge(var.labels, { env = var.env, managed-by = "terraform" })
}

# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
resource "google_service_account" "gke_nodes" {
  account_id   = "${local.name_prefix}-gke-nodes"
  display_name = "GKE node service account (${var.env})"
}

resource "google_project_iam_member" "gke_nodes" {
  for_each = toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/monitoring.viewer",
    "roles/stackdriver.resourceMetadata.writer",
    "roles/artifactregistry.reader",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.gke_nodes.email}"
}

# --------------------------------------------------------------------------- #
# Cluster: cost allocation + node auto-provisioning + optimize-utilization
# --------------------------------------------------------------------------- #
resource "google_container_cluster" "primary" {
  name     = "${local.name_prefix}-gke"
  location = var.region

  network    = var.network
  subnetwork = var.subnetwork

  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = var.env == "prod"

  release_channel {
    channel = var.release_channel
  }

  ip_allocation_policy {
    cluster_secondary_range_name  = var.pods_range_name
    services_secondary_range_name = var.services_range_name
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  # Populates k8s-namespace / k8s-workload labels in the billing export.
  cost_management_config {
    enabled = true
  }

  vertical_pod_autoscaling {
    enabled = true
  }

  cluster_autoscaling {
    enabled             = true
    autoscaling_profile = "OPTIMIZE_UTILIZATION"

    resource_limits {
      resource_type = "cpu"
      minimum       = 4
      maximum       = var.nap_cpu_max
    }
    resource_limits {
      resource_type = "memory"
      minimum       = 16
      maximum       = var.nap_memory_max_gb
    }

    auto_provisioning_defaults {
      service_account = google_service_account.gke_nodes.email
      oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
      disk_type       = "pd-balanced"
      disk_size       = 50

      management {
        auto_repair  = true
        auto_upgrade = true
      }
      shielded_instance_config {
        enable_secure_boot          = true
        enable_integrity_monitoring = true
      }
    }
  }

  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS", "DEPLOYMENT", "STATEFULSET", "DAEMONSET", "HPA", "POD"]
    managed_prometheus {
      enabled = true
    }
  }

  resource_labels = local.labels

  lifecycle {
    ignore_changes = [initial_node_count]
  }
}

# --------------------------------------------------------------------------- #
# Node pools
# --------------------------------------------------------------------------- #
resource "google_container_node_pool" "general" {
  name     = "general"
  cluster  = google_container_cluster.primary.id
  location = var.region

  autoscaling {
    min_node_count  = var.general_min_nodes
    max_node_count  = var.general_max_nodes
    location_policy = "BALANCED"
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  upgrade_settings {
    max_surge       = 1
    max_unavailable = 0
  }

  node_config {
    machine_type    = var.general_machine_type
    disk_type       = "pd-balanced"
    disk_size_gb    = 50
    service_account = google_service_account.gke_nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    labels          = merge(local.labels, { workload-class = "general" })
    resource_labels = local.labels

    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }
    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }
}

resource "google_container_node_pool" "batch_spot" {
  name     = "batch-spot"
  cluster  = google_container_cluster.primary.id
  location = var.region

  autoscaling {
    min_node_count  = 0
    max_node_count  = var.spot_max_nodes
    location_policy = "ANY" # take capacity wherever spot is available
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    spot            = true
    machine_type    = var.spot_machine_type
    disk_type       = "pd-balanced"
    disk_size_gb    = 50
    service_account = google_service_account.gke_nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    labels          = merge(local.labels, { workload-class = "batch" })
    resource_labels = local.labels

    taint {
      key    = "cloud.google.com/gke-spot"
      value  = "true"
      effect = "NO_SCHEDULE"
    }

    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }
    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }
}

# --------------------------------------------------------------------------- #
# Budget with Pub/Sub + email notifications
# --------------------------------------------------------------------------- #
resource "google_pubsub_topic" "budget_alerts" {
  name   = "${local.name_prefix}-budget-alerts"
  labels = local.labels
}

resource "google_monitoring_notification_channel" "email" {
  display_name = "FinOps email (${var.env})"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }
}

resource "google_billing_budget" "project" {
  provider        = google-beta
  billing_account = var.billing_account_id
  display_name    = "${local.name_prefix} monthly budget"

  budget_filter {
    projects               = ["projects/${var.project_id}"]
    credit_types_treatment = "INCLUDE_ALL_CREDITS"
    calendar_period        = "MONTH"
  }

  amount {
    specified_amount {
      currency_code = var.budget_currency_code
      units         = tostring(floor(var.monthly_budget_amount))
    }
  }

  dynamic "threshold_rules" {
    for_each = var.budget_thresholds
    content {
      threshold_percent = threshold_rules.value
      spend_basis       = "CURRENT_SPEND"
    }
  }

  dynamic "threshold_rules" {
    for_each = var.forecast_thresholds
    content {
      threshold_percent = threshold_rules.value
      spend_basis       = "FORECASTED_SPEND"
    }
  }

  all_updates_rule {
    pubsub_topic                     = google_pubsub_topic.budget_alerts.id
    schema_version                   = "1.0"
    monitoring_notification_channels = [google_monitoring_notification_channel.email.id]
    disable_default_iam_recipients   = true
  }
}

# --------------------------------------------------------------------------- #
# Spend anomaly alert on a daily-cost metric.
# The metric is written by the daily-anomaly-detection scheduled query via a
# small Cloud Function / Cloud Run job (custom.googleapis.com/finops/daily_cost).
# --------------------------------------------------------------------------- #
resource "google_monitoring_alert_policy" "spend_anomaly" {
  display_name = "${local.name_prefix} daily spend anomaly"
  combiner     = "OR"
  severity     = "WARNING"

  conditions {
    display_name = "Daily net cost above threshold"
    condition_threshold {
      filter          = "metric.type=\"custom.googleapis.com/finops/daily_cost\" AND resource.type=\"global\" AND metric.label.project_id=\"${var.project_id}\""
      comparison      = "COMPARISON_GT"
      threshold_value = var.anomaly_daily_spend_threshold
      duration        = "0s"
      aggregations {
        alignment_period   = "86400s"
        per_series_aligner = "ALIGN_MAX"
      }
      trigger {
        count = 1
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.id]

  alert_strategy {
    auto_close = "172800s"
  }

  documentation {
    mime_type = "text/markdown"
    content   = <<-MD
      Daily net cost for `${var.project_id}` exceeded ${var.anomaly_daily_spend_threshold} ${var.budget_currency_code}.
      Run `billing/queries/daily-anomaly-detection.sql` to find the service and project responsible,
      then check `reports/` for recent cleanup or scaling changes.
    MD
  }

  user_labels = local.labels
}

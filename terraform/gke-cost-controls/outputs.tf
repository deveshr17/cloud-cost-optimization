output "cluster_name" {
  description = "GKE cluster name."
  value       = google_container_cluster.primary.name
}

output "cluster_location" {
  description = "GKE cluster region."
  value       = google_container_cluster.primary.location
}

output "node_service_account" {
  description = "Service account used by node pools and NAP."
  value       = google_service_account.gke_nodes.email
}

output "spot_node_pool" {
  description = "Name of the spot batch node pool (taint cloud.google.com/gke-spot=true:NoSchedule)."
  value       = google_container_node_pool.batch_spot.name
}

output "budget_pubsub_topic" {
  description = "Pub/Sub topic receiving budget notifications (subscribe a Slack notifier here)."
  value       = google_pubsub_topic.budget_alerts.id
}

output "budget_name" {
  description = "Resource name of the billing budget."
  value       = google_billing_budget.project.name
}

output "anomaly_alert_policy" {
  description = "Monitoring alert policy for daily spend anomalies."
  value       = google_monitoring_alert_policy.spend_anomaly.name
}

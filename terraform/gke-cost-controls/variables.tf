variable "project_id" {
  description = "GCP project that hosts the GKE cluster and monitoring resources."
  type        = string
}

variable "billing_account_id" {
  description = "Billing account ID (format 012345-6789AB-CDEF01) the budget is attached to."
  type        = string
  validation {
    condition     = can(regex("^[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}$", var.billing_account_id))
    error_message = "billing_account_id must look like 012345-6789AB-CDEF01."
  }
}

variable "org" {
  description = "Short organisation prefix used in resource names."
  type        = string
  default     = "example"
}

variable "env" {
  description = "Environment name (dev, uat, prod)."
  type        = string
  validation {
    condition     = contains(["dev", "uat", "prod"], var.env)
    error_message = "env must be one of dev, uat, prod."
  }
}

variable "region" {
  description = "Region for the regional GKE cluster."
  type        = string
  default     = "asia-south1"
}

variable "network" {
  description = "VPC network self-link or name for the cluster."
  type        = string
}

variable "subnetwork" {
  description = "Subnetwork self-link or name for the cluster."
  type        = string
}

variable "pods_range_name" {
  description = "Secondary range name for pods."
  type        = string
  default     = "pods"
}

variable "services_range_name" {
  description = "Secondary range name for services."
  type        = string
  default     = "services"
}

variable "release_channel" {
  description = "GKE release channel."
  type        = string
  default     = "REGULAR"
}

variable "general_machine_type" {
  description = "Machine type for the on-demand general-purpose node pool."
  type        = string
  default     = "e2-standard-4"
}

variable "general_min_nodes" {
  description = "Minimum nodes per zone for the general pool."
  type        = number
  default     = 1
}

variable "general_max_nodes" {
  description = "Maximum nodes per zone for the general pool."
  type        = number
  default     = 4
}

variable "spot_machine_type" {
  description = "Machine type for the spot batch pool."
  type        = string
  default     = "e2-standard-8"
}

variable "spot_max_nodes" {
  description = "Maximum nodes per zone for the spot batch pool (min is always 0)."
  type        = number
  default     = 10
}

variable "nap_cpu_max" {
  description = "Node auto-provisioning ceiling for total vCPUs."
  type        = number
  default     = 128
}

variable "nap_memory_max_gb" {
  description = "Node auto-provisioning ceiling for total memory in GB."
  type        = number
  default     = 512
}

variable "monthly_budget_amount" {
  description = "Monthly budget in billing-account currency units for the project."
  type        = number
  validation {
    condition     = var.monthly_budget_amount > 0
    error_message = "monthly_budget_amount must be positive."
  }
}

variable "budget_currency_code" {
  description = "ISO 4217 currency of the budget amount."
  type        = string
  default     = "USD"
}

variable "budget_thresholds" {
  description = "Threshold fractions (0-1) at which budget notifications fire on actual spend."
  type        = list(number)
  default     = [0.5, 0.8, 0.9, 1.0]
}

variable "forecast_thresholds" {
  description = "Threshold fractions at which forecasted spend triggers notifications."
  type        = list(number)
  default     = [1.0, 1.2]
}

variable "alert_email" {
  description = "Email for budget and anomaly alerts (placeholder)."
  type        = string
  default     = "finops@example.com"
}

variable "anomaly_daily_spend_threshold" {
  description = "Daily spend (currency units) above which the anomaly alert fires. Set to ~3x the normal daily average."
  type        = number
  default     = 500
}

variable "labels" {
  description = "Labels applied to all resources; env/team/cost-center/app are the org tagging standard."
  type        = map(string)
  default = {
    team        = "platform"
    cost-center = "cc-1001"
    app         = "gke-platform"
  }
}

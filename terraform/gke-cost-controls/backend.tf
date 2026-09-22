# Remote state. Bucket name is a placeholder; create it out of band with
# versioning enabled. `prefix` is per environment.
terraform {
  backend "gcs" {
    bucket = "example-org-tfstate"
    prefix = "cost-controls/dev"
  }
}

variable "cluster_name" {
  description = "Name of the kind cluster (also the Docker network / kubeconfig context suffix)."
  type        = string
  default     = "finbot"
}

output "cluster_name" {
  description = "The kind cluster name."
  value       = kind_cluster.finbot.name
}

output "kubeconfig_path" {
  description = "Path to the generated kubeconfig for this cluster."
  value       = kind_cluster.finbot.kubeconfig_path
}

output "endpoint" {
  description = "Kubernetes API server endpoint."
  value       = kind_cluster.finbot.endpoint
}

output "kubectl_context" {
  description = "kubectl context to select this cluster."
  value       = "kind-${kind_cluster.finbot.name}"
}

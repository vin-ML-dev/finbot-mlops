terraform {
  required_version = ">= 1.5"
  required_providers {
    kind = {
      source  = "tehcyx/kind"
      version = "~> 0.9"
    }
  }
}

provider "kind" {}

resource "kind_cluster" "finbot" {
  name           = var.cluster_name
  wait_for_ready = true

  kind_config {
    kind        = "Cluster"
    api_version = "kind.x-k8s.io/v1alpha4"

    node {
      role = "control-plane"
      labels = {
        "node-role" = "infra"
      }
      extra_port_mappings {
        container_port = 80
        host_port      = 8080
      }
      extra_port_mappings {
        container_port = 443
        host_port      = 8443
      }
    }

    node {
      role = "worker"
      labels = {
        "node-role" = "model"
      }
    }

    node {
      role = "worker"
      labels = {
        "node-role" = "agent"
      }
    }
  }
}

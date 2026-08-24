# Pin the provider so `terraform init` is reproducible across machines.
# (required_providers is declared in main.tf; this file documents the intent
# and is where you'd add a backend block if you ever remote-state this.)

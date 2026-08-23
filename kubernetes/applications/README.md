# Kubernetes applications

Each application has a reusable `base` and one or more cluster-specific
`overlays`. Cluster directories do not copy these resources; they contain Flux
`Kustomization` objects that point to the appropriate overlay.


# Applications

Each directory contains one application and everything needed to build and test
its container image. Kubernetes deployment configuration lives separately under
`kubernetes/applications/<name>` so the same image can be configured differently
for each cluster.

To add another application:

1. Add its source under `applications/<name>`.
2. Add reusable manifests under `kubernetes/applications/<name>/base`.
3. Add an overlay under `kubernetes/applications/<name>/overlays/<cluster>`.
4. Add a Flux `Kustomization` under `kubernetes/clusters/<cluster>/applications`.


# Hello CRUD

A deliberately small JSON API for exercising the build and GitOps deployment
path. Items are stored in SQLite. Kubernetes mounts the database from a 1 Gi
persistent volume so data survives pod restarts and application redeployments.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | Hello-world response |
| `GET` | `/healthz` | Health check |
| `GET` | `/items` | List items |
| `POST` | `/items` | Create an item |
| `GET` | `/items/{id}` | Read an item |
| `PUT` | `/items/{id}` | Update an item |
| `DELETE` | `/items/{id}` | Delete an item |

Create and manipulate an item:

```bash
curl -X POST http://localhost:8080/items \
  -H 'Content-Type: application/json' \
  -d '{"name":"hello"}'
curl http://localhost:8080/items
curl -X PUT http://localhost:8080/items/1 \
  -H 'Content-Type: application/json' \
  -d '{"name":"updated"}'
curl -X DELETE http://localhost:8080/items/1
```

## Develop

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
python app.py
```

## Build for Kind

The dev overlay expects image
`ghcr.io/griffinseibold/hello-crud:0.2.0`. Build it locally and load it into
every node in the Kind cluster before pushing the GitOps manifests:

```bash
docker build \
  -t ghcr.io/griffinseibold/hello-crud:0.2.0 \
  applications/hello-crud
kind load docker-image \
  ghcr.io/griffinseibold/hello-crud:0.2.0 \
  --name homelab-dev
```

Kind's default local-path provisioner stores the volume inside the node. The
data survives pod and node-container restarts, but deleting and recreating the
entire Kind cluster deletes it. Durable storage across cluster rebuilds is a
separate roadmap step for model files and other important data.

After Flux deploys it, use port forwarding until a Gateway is installed:

```bash
kubectl --context kind-homelab-dev \
  -n hello-crud port-forward service/hello-crud 8080:80
```

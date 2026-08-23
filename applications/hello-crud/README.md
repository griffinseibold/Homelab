# Hello CRUD

A deliberately small JSON API for exercising the build and GitOps deployment
path. Data is held in memory and is lost whenever the process restarts.

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
`ghcr.io/griffinseibold/hello-crud:0.1.0`. Build it locally and load it into
every node in the Kind cluster before pushing the GitOps manifests:

```bash
docker build \
  -t ghcr.io/griffinseibold/hello-crud:0.1.0 \
  applications/hello-crud
kind load docker-image \
  ghcr.io/griffinseibold/hello-crud:0.1.0 \
  --name homelab-dev
```

After Flux deploys it, use port forwarding until a Gateway is installed:

```bash
kubectl --context kind-homelab-dev \
  -n hello-crud port-forward service/hello-crud 8080:80
```


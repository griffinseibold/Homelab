from flask import Flask, jsonify, request


app = Flask(__name__)
items = {}
next_id = 1


def read_name():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return None

    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        return None

    return name.strip()


@app.get("/")
def hello():
    return jsonify(message="Hello, world!", resource="/items")


@app.get("/healthz")
def health():
    return jsonify(status="ok")


@app.get("/items")
def list_items():
    return jsonify(list(items.values()))


@app.post("/items")
def create_item():
    global next_id

    name = read_name()
    if name is None:
        return jsonify(error="name must be a non-empty string"), 400

    item = {"id": next_id, "name": name}
    items[next_id] = item
    next_id += 1
    return jsonify(item), 201


@app.get("/items/<int:item_id>")
def get_item(item_id):
    item = items.get(item_id)
    if item is None:
        return jsonify(error="item not found"), 404
    return jsonify(item)


@app.put("/items/<int:item_id>")
def update_item(item_id):
    if item_id not in items:
        return jsonify(error="item not found"), 404

    name = read_name()
    if name is None:
        return jsonify(error="name must be a non-empty string"), 400

    items[item_id]["name"] = name
    return jsonify(items[item_id])


@app.delete("/items/<int:item_id>")
def delete_item(item_id):
    if items.pop(item_id, None) is None:
        return jsonify(error="item not found"), 404
    return "", 204


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)


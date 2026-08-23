import os
import sqlite3

from flask import Flask, g, jsonify, request


app = Flask(__name__)
app.config["DATABASE_PATH"] = os.environ.get(
    "DATABASE_PATH",
    "/tmp/hello-crud.db",
)


def get_database():
    if "database" not in g:
        g.database = sqlite3.connect(app.config["DATABASE_PATH"])
        g.database.row_factory = sqlite3.Row

    return g.database


@app.teardown_appcontext
def close_database(_exception=None):
    database = g.pop("database", None)
    if database is not None:
        database.close()


def initialize_database():
    with sqlite3.connect(app.config["DATABASE_PATH"]) as database:
        database.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL
            )
            """
        )


initialize_database()


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
    rows = get_database().execute(
        "SELECT id, name FROM items ORDER BY id"
    ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.post("/items")
def create_item():
    name = read_name()
    if name is None:
        return jsonify(error="name must be a non-empty string"), 400

    database = get_database()
    cursor = database.execute("INSERT INTO items (name) VALUES (?)", (name,))
    database.commit()
    item = {"id": cursor.lastrowid, "name": name}
    return jsonify(item), 201


@app.get("/items/<int:item_id>")
def get_item(item_id):
    row = get_database().execute(
        "SELECT id, name FROM items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if row is None:
        return jsonify(error="item not found"), 404
    return jsonify(dict(row))


@app.put("/items/<int:item_id>")
def update_item(item_id):
    name = read_name()
    if name is None:
        return jsonify(error="name must be a non-empty string"), 400

    database = get_database()
    cursor = database.execute(
        "UPDATE items SET name = ? WHERE id = ?",
        (name, item_id),
    )
    database.commit()
    if cursor.rowcount == 0:
        return jsonify(error="item not found"), 404

    return jsonify(id=item_id, name=name)


@app.delete("/items/<int:item_id>")
def delete_item(item_id):
    database = get_database()
    cursor = database.execute("DELETE FROM items WHERE id = ?", (item_id,))
    database.commit()
    if cursor.rowcount == 0:
        return jsonify(error="item not found"), 404
    return "", 204


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)

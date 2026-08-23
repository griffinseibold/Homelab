import unittest

import app as hello_crud


class HelloCrudTestCase(unittest.TestCase):
    def setUp(self):
        hello_crud.app.config.update(TESTING=True)
        hello_crud.items.clear()
        hello_crud.next_id = 1
        self.client = hello_crud.app.test_client()

    def test_hello_and_health(self):
        self.assertEqual(
            self.client.get("/").get_json(),
            {"message": "Hello, world!", "resource": "/items"},
        )
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_crud_lifecycle(self):
        created = self.client.post("/items", json={"name": "first"})
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.get_json(), {"id": 1, "name": "first"})

        self.assertEqual(
            self.client.get("/items").get_json(),
            [{"id": 1, "name": "first"}],
        )
        self.assertEqual(self.client.get("/items/1").status_code, 200)

        updated = self.client.put("/items/1", json={"name": "updated"})
        self.assertEqual(updated.get_json(), {"id": 1, "name": "updated"})

        self.assertEqual(self.client.delete("/items/1").status_code, 204)
        self.assertEqual(self.client.get("/items/1").status_code, 404)

    def test_rejects_invalid_input(self):
        response = self.client.post("/items", json={"name": "  "})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()


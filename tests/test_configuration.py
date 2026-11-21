import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from input_to_cmd.api import app
from input_to_cmd.config_store import ConfigurationStore, ConfigurationUpdate


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "nested" / "config.json"
        self.store = ConfigurationStore(self.path)
        store_patch = patch("input_to_cmd.api.configuration_store", self.store)
        store_patch.start()
        self.addCleanup(store_patch.stop)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def test_partial_updates_persist_across_processes(self):
        self.assertEqual(self.client.get("/configuration").json(), {
            "device_type": "kasten", "iframe_url": "http://192.168.5.203:5000/",
        })
        self.assertEqual(self.client.patch("/configuration", json={
            "device_type": "raeder",
        }).json()["device_type"], "raeder")
        response = self.client.patch("/configuration", json={
            "iframe_url": "https://example.test/stream?view=1",
        })
        self.assertEqual(response.status_code, 200)
        expected = {
            "device_type": "raeder", "iframe_url": "https://example.test/stream?view=1",
        }
        self.assertEqual(response.json(), expected)

        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        output = subprocess.run(
            [
                sys.executable, "-c",
                "import json,sys; from pathlib import Path; "
                "from input_to_cmd.config_store import ConfigurationStore; "
                "print(json.dumps(ConfigurationStore(Path(sys.argv[1])).read().model_dump(mode='json')))",
                str(self.path),
            ],
            env=env, check=True, capture_output=True, text=True,
        )
        self.assertEqual(json.loads(output.stdout), expected)

    def test_invalid_updates_do_not_change_saved_configuration(self):
        expected = self.client.get("/configuration").json()
        for update in (
            {}, {"device_type": None}, {"device_type": "unknown"},
            {"iframe_url": None}, {"iframe_url": "javascript:alert(1)"},
            {"iframe_url": "ftp://example.test/file"},
            {"iframe_url": "https://user:password@example.test/"},
            {"iframe_url": "https://example.test/" + "a" * 2048},
            {"unexpected": True},
        ):
            with self.subTest(update=update):
                self.assertEqual(self.client.patch("/configuration", json=update).status_code, 422)
        self.assertEqual(self.client.get("/configuration").json(), expected)

    def test_corrupt_storage_is_reported_instead_of_replaced(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertLogs("input_to_cmd", level="ERROR"):
            self.assertEqual(self.client.get("/configuration").status_code, 500)
            self.assertEqual(
                self.client.patch("/configuration", json={"device_type": "raeder"}).status_code,
                500,
            )
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{broken")

    def test_parallel_partial_updates_preserve_both_fields(self):
        barrier = threading.Barrier(2)

        def update(data):
            barrier.wait(timeout=2)
            self.store.update(ConfigurationUpdate.model_validate(data))

        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [
                pool.submit(update, {"device_type": "raeder"}),
                pool.submit(update, {"iframe_url": "http://device.test:5000/view"}),
            ]
            for task in tasks:
                task.result(timeout=5)

        self.assertEqual(self.store.read().model_dump(mode="json"), {
            "device_type": "raeder", "iframe_url": "http://device.test:5000/view",
        })

    def test_configuration_patch_cors_preflight(self):
        response = self.client.options("/configuration", headers={
            "Origin": "http://frontend.test",
            "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "content-type",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("PATCH", response.headers["access-control-allow-methods"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


class TrainingManifestTests(unittest.TestCase):
    def test_writer_preserves_release_license_and_provenance(self) -> None:
        from ml.manifest import write_model_manifest

        with tempfile.TemporaryDirectory(prefix="qidao-model-manifest-") as temporary:
            output = Path(temporary) / "vision_models.json"
            report = write_model_manifest(
                output,
                {
                    "schema": 1,
                    "license": "missing",
                    "trainingScript": "missing",
                    "syntheticData": "missing",
                },
            )
            written = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(report, written)
        self.assertEqual(written["license"], "MIT")
        self.assertEqual(written["trainingScript"], "vision/ml/train.py")
        self.assertIn("synthetic board data", written["syntheticData"])
        self.assertIn("no third-party model weights or datasets", written["syntheticData"])


if __name__ == "__main__":
    unittest.main()

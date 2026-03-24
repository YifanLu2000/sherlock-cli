from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sherlock_cli.state import StateStore


class StateTests(unittest.TestCase):
    def test_record_submission_and_tunnel(self):
        with TemporaryDirectory() as tmpdir:
            store = StateStore(Path(tmpdir) / "state.json")
            store.record_submission(
                job_id="123",
                job_name="GPU-jupyterlab",
                preset_id="xiaojie-gpu",
                notebook_dir="/oak/demo",
                remote_port=56790,
                remote_stdout="/home/user/job-123.out",
                remote_stderr="/home/user/job-123.err",
                remote_template="/home/user/template.sbatch",
            )
            store.update_tunnel("123", 999, 56790, "http://localhost:56790/lab?token=abc")

            reloaded = StateStore(Path(tmpdir) / "state.json")
            metadata = reloaded.get("123")
            self.assertIsNotNone(metadata)
            self.assertEqual(metadata.tunnel_pid, 999)
            self.assertEqual(metadata.local_port, 56790)


if __name__ == "__main__":
    unittest.main()

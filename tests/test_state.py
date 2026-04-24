from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sherlock_cli.models import RemoteSessionMetadata
from sherlock_cli.state import StateStore


class StateTests(unittest.TestCase):
    def test_record_submission_and_tunnel(self):
        with TemporaryDirectory() as tmpdir:
            resource = "sherlock"
            path = Path(tmpdir) / "state.json"
            store = StateStore(path, resource)
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

            reloaded = StateStore(path, resource)
            metadata = reloaded.get("123")
            self.assertIsNotNone(metadata)
            self.assertEqual(metadata.tunnel_pid, 999)
            self.assertEqual(metadata.local_port, 56790)

    def test_remote_session_round_trip(self):
        with TemporaryDirectory() as tmpdir:
            resource = "sherlock"
            path = Path(tmpdir) / "state.json"
            store = StateStore(path, resource)
            store.set_remote_session(
                RemoteSessionMetadata(
                    job_id="4321",
                    node="sh03-01",
                    port=40022,
                    session_type="attached",
                    last_verified_at="2026-04-02T00:00:00+00:00",
                )
            )

            reloaded = StateStore(path, resource)
            session = reloaded.get_remote_session()
            self.assertIsNotNone(session)
            self.assertEqual(session.job_id, "4321")
            self.assertEqual(session.port, 40022)

    def test_cluster_namespaces_do_not_overlap(self):
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            sherlock = StateStore(path, "sherlock")
            marlowe = StateStore(path, "marlowe")

            sherlock.record_submission(
                job_id="123",
                job_name="Sherlock job",
                preset_id="gpu",
                notebook_dir="/oak/demo",
                remote_port=56790,
                remote_stdout="/tmp/a.out",
                remote_stderr="/tmp/a.err",
                remote_template="/tmp/a.sbatch",
            )
            marlowe.record_submission(
                job_id="123",
                job_name="Marlowe job",
                preset_id="cpu",
                notebook_dir="/scratch/demo",
                remote_port=56791,
                remote_stdout="/tmp/b.out",
                remote_stderr="/tmp/b.err",
                remote_template="/tmp/b.sbatch",
            )

            self.assertEqual(StateStore(path, "sherlock").get("123").job_name, "Sherlock job")
            self.assertEqual(StateStore(path, "marlowe").get("123").job_name, "Marlowe job")

    def test_legacy_state_migrates_into_current_resource_namespace(self):
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            path.write_text(
                """{
  "jobs": {
    "123": {
      "created_at": "2026-04-02T00:00:00+00:00",
      "job_name": "Legacy job",
      "jupyter_url": null,
      "local_port": null,
      "notebook_dir": "/oak/demo",
      "preset_id": "gpu",
      "remote_port": 56790,
      "remote_stderr": "/tmp/a.err",
      "remote_stdout": "/tmp/a.out",
      "remote_template": "/tmp/a.sbatch",
      "tunnel_pid": null,
      "job_id": "123"
    }
  },
  "remote_session": null
}"""
            )

            store = StateStore(path, "sherlock")
            self.assertEqual(store.get("123").job_name, "Legacy job")
            store.save()

            reloaded = path.read_text()
            self.assertIn('"version": 2', reloaded)
            self.assertIn('"clusters"', reloaded)
            self.assertIn('"sherlock"', reloaded)


if __name__ == "__main__":
    unittest.main()

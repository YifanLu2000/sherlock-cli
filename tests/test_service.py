from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
import unittest

from sherlock_cli.config import load_config
from sherlock_cli.models import SubmissionRequest
from sherlock_cli.service import SherlockError, SherlockService
from sherlock_cli.state import StateStore


class FakeRemoteShell:
    def __init__(self):
        self.commands = []
        self.uploads = []
        self.closed = False

    def run(self, command, check=True):
        self.commands.append((command, check))
        if "printf '%s' \"$HOME\"" in command:
            return "/home/demo"
        if "mkdir -p" in command:
            return ""
        if "sbatch --parsable" in command:
            return "4321"
        if "squeue -u" in command:
            return (
                "4321|GPU-jupyterlab|RUNNING|gpu|sh03-01|00:03:12\n"
                "9999|other|PENDING|normal|(Priority)|00:00"
            )
        if "scontrol show job -o 4321" in command:
            return (
                "JobId=4321 JobName=GPU-jupyterlab JobState=RUNNING Partition=gpu "
                "NodeList=sh03-01 Reason=None RunTime=00:03:12 "
                "StdOut=/home/demo/forward-util/GPU-jupyterlab-4321.out "
                "StdErr=/home/demo/forward-util/GPU-jupyterlab-4321.err"
            )
        if "tail -n 200" in command or "tail -n 60" in command:
            return "http://sh03-01:56793/lab?token=abc123"
        if "scancel 4321" in command:
            return ""
        if "squeue -j 4321" in command:
            return "4321|GPU-jupyterlab|RUNNING|gpu|sh03-01|00:03:12"
        if "sacct -j 4321" in command:
            return ""
        return ""

    def write_text(self, remote_path, content):
        self.uploads.append((remote_path, content))

    def close(self):
        self.closed = True


class FakeSessionFactory:
    def __init__(self):
        self.sessions = []

    def __call__(self):
        session = FakeRemoteShell()
        self.sessions.append(session)
        return session


class FakePopen:
    def __init__(self, args, stdout=None, stderr=None, stdin=None):
        self.args = args
        self.pid = 7777


class ServiceTests(unittest.TestCase):
    def make_service(self, tmpdir: str):
        config = load_config()
        config.state_path = Path(tmpdir) / "state.json"
        session_factory = FakeSessionFactory()
        service = SherlockService(
            config,
            StateStore(config.state_path),
            popen_factory=FakePopen,
            ssh_session_factory=session_factory,
            sleeper=lambda _seconds: None,
            browser_opener=lambda _url: None,
        )
        return service, session_factory

    def test_submit_job_records_state(self):
        with TemporaryDirectory() as tmpdir:
            service, session_factory = self.make_service(tmpdir)
            preset = service.config.presets["xiaojie-gpu"]
            request = SubmissionRequest(
                preset=preset,
                job_name=preset.job_name,
                notebook_dir="/oak/demo",
                port=preset.port,
                partition=preset.partition,
                mem=preset.mem,
                time=preset.time,
                cpus=preset.cpus,
                account=preset.account,
                gpus=preset.gpus,
                nodelist=preset.nodelist,
                constraint=preset.constraint,
                gres_flags=preset.gres_flags,
                gpu_cmode=preset.gpu_cmode,
                isolated_compute_node=preset.isolated_compute_node,
            )
            job_id = service.submit_job(request)
            self.assertEqual(job_id, "4321")
            metadata = service.state.get("4321")
            self.assertIsNotNone(metadata)
            self.assertEqual(len(session_factory.sessions), 1)
            session = session_factory.sessions[0]
            self.assertEqual(len(session.uploads), 1)
            remote_path, content = session.uploads[0]
            self.assertTrue(remote_path.endswith("/GPU-jupyterlab.sbatch"))
            self.assertIn("#!/bin/bash", content)
            self.assertTrue(any("sbatch --parsable" in command for command, _check in session.commands))
            self.assertFalse(any(" -A " in command for command, _check in session.commands))

    def test_submit_job_includes_account_when_configured(self):
        with TemporaryDirectory() as tmpdir:
            service, session_factory = self.make_service(tmpdir)
            preset = service.config.presets["xiaojie-gpu"]
            request = SubmissionRequest(
                preset=preset,
                job_name=preset.job_name,
                notebook_dir="/oak/demo",
                port=preset.port,
                partition=preset.partition,
                mem=preset.mem,
                time=preset.time,
                cpus=preset.cpus,
                account="demo-account",
                gpus=preset.gpus,
                nodelist=preset.nodelist,
                constraint=preset.constraint,
                gres_flags=preset.gres_flags,
                gpu_cmode=preset.gpu_cmode,
                isolated_compute_node=preset.isolated_compute_node,
            )
            service.submit_job(request)

            session = session_factory.sessions[0]
            submit_command = next(command for command, _check in session.commands if "sbatch --parsable" in command)
            self.assertIn(" -A demo-account ", submit_command)

    def test_list_and_connect_job_reuses_single_session(self):
        with TemporaryDirectory() as tmpdir:
            service, session_factory = self.make_service(tmpdir)
            service.state.record_submission(
                job_id="4321",
                job_name="GPU-jupyterlab",
                preset_id="xiaojie-gpu",
                notebook_dir="/oak/demo",
                remote_port=56793,
                remote_stdout="/home/demo/forward-util/GPU-jupyterlab-4321.out",
                remote_stderr="/home/demo/forward-util/GPU-jupyterlab-4321.err",
                remote_template="/home/demo/forward-util/GPU-jupyterlab.sbatch",
            )

            jobs = service.list_jobs()
            self.assertEqual(len(jobs), 2)
            self.assertFalse(jobs[0].connected)
            with mock.patch.object(service, "_port_available", return_value=True):
                info = service.connect_job("4321", open_browser=False)
            self.assertIsInstance(info.local_port, int)
            self.assertGreater(info.local_port, 0)
            self.assertEqual(info.local_url, f"http://localhost:{info.local_port}/lab?token=abc123")
            with mock.patch.object(service, "_pid_alive", return_value=True):
                refreshed_jobs = service.list_jobs()
            connected_job = next(job for job in refreshed_jobs if job.job_id == "4321")
            self.assertTrue(connected_job.connected)
            self.assertEqual(len(session_factory.sessions), 1)
            session = session_factory.sessions[0]
            commands = [command for command, _check in session.commands]
            self.assertTrue(any("squeue -u" in command for command in commands))
            self.assertTrue(any("scontrol show job -o 4321" in command for command in commands))

    def test_connect_job_uses_recorded_port_without_waiting_for_logs(self):
        with TemporaryDirectory() as tmpdir:
            service, _session_factory = self.make_service(tmpdir)
            service.state.record_submission(
                job_id="4321",
                job_name="GPU-jupyterlab",
                preset_id="xiaojie-gpu",
                notebook_dir="/oak/demo",
                remote_port=56793,
                remote_stdout="/home/demo/forward-util/GPU-jupyterlab-4321.out",
                remote_stderr="/home/demo/forward-util/GPU-jupyterlab-4321.err",
                remote_template="/home/demo/forward-util/GPU-jupyterlab.sbatch",
            )

            with (
                mock.patch.object(service, "_port_available", return_value=True),
                mock.patch.object(service, "wait_for_jupyter", side_effect=AssertionError("should not wait")),
            ):
                info = service.connect_job("4321", open_browser=False)

            self.assertEqual(info.port, 56793)
            self.assertEqual(info.local_url, "http://localhost:56793/")

    def test_close_disposes_current_session(self):
        with TemporaryDirectory() as tmpdir:
            service, session_factory = self.make_service(tmpdir)
            service.list_jobs()
            self.assertEqual(len(session_factory.sessions), 1)
            session = session_factory.sessions[0]
            self.assertFalse(session.closed)
            service.close()
            self.assertTrue(session.closed)
            service.list_jobs()
            self.assertEqual(len(session_factory.sessions), 2)

    def test_connect_job_errors_when_local_port_is_busy(self):
        with TemporaryDirectory() as tmpdir:
            service, _session_factory = self.make_service(tmpdir)
            service.state.record_submission(
                job_id="4321",
                job_name="GPU-jupyterlab",
                preset_id="xiaojie-gpu",
                notebook_dir="/oak/demo",
                remote_port=56793,
                remote_stdout="/home/demo/forward-util/GPU-jupyterlab-4321.out",
                remote_stderr="/home/demo/forward-util/GPU-jupyterlab-4321.err",
                remote_template="/home/demo/forward-util/GPU-jupyterlab.sbatch",
            )

            with mock.patch.object(service, "_port_available", return_value=False):
                with self.assertRaises(SherlockError) as ctx:
                    service.connect_job("4321", open_browser=False)

            self.assertEqual(str(ctx.exception), "Local port 56793 is already in use.")

    def test_kill_job_clears_tunnel(self):
        with TemporaryDirectory() as tmpdir:
            service, _session_factory = self.make_service(tmpdir)
            service.state.record_submission(
                job_id="4321",
                job_name="GPU-jupyterlab",
                preset_id="xiaojie-gpu",
                notebook_dir="/oak/demo",
                remote_port=56793,
                remote_stdout="/home/demo/forward-util/GPU-jupyterlab-4321.out",
                remote_stderr="/home/demo/forward-util/GPU-jupyterlab-4321.err",
                remote_template="/home/demo/forward-util/GPU-jupyterlab.sbatch",
            )
            service.state.update_tunnel("4321", 999999, 56793, "http://localhost:56793/lab?token=abc123")
            job_id = service.kill_job("4321")
            self.assertEqual(job_id, "4321")
            metadata = service.state.get("4321")
            self.assertIsNone(metadata.tunnel_pid)


    def test_connect_external_job_adopts_into_state(self):
        with TemporaryDirectory() as tmpdir:
            service, _session_factory = self.make_service(tmpdir)
            # No record_submission — job 4321 is "external"
            self.assertIsNone(service.state.get("4321"))

            with mock.patch.object(service, "_port_available", return_value=True):
                info = service.connect_job("4321", open_browser=False)

            self.assertIsInstance(info.local_port, int)
            self.assertEqual(info.local_url, "http://localhost:56793/")

            # External job should now be adopted into state with tunnel info
            metadata = service.state.get("4321")
            self.assertIsNotNone(metadata)
            self.assertEqual(metadata.job_name, "GPU-jupyterlab")
            self.assertEqual(metadata.remote_port, 56793)
            self.assertEqual(metadata.tunnel_pid, 7777)
            self.assertEqual(metadata.preset_id, "")

    def test_connect_external_job_uses_preset_port_without_waiting_for_logs(self):
        with TemporaryDirectory() as tmpdir:
            service, _session_factory = self.make_service(tmpdir)

            with (
                mock.patch.object(service, "_port_available", return_value=True),
                mock.patch.object(service, "wait_for_jupyter", side_effect=AssertionError("should not wait")),
            ):
                info = service.connect_job("4321", open_browser=False)

            self.assertEqual(info.port, 56793)
            self.assertEqual(info.local_url, "http://localhost:56793/")


if __name__ == "__main__":
    unittest.main()

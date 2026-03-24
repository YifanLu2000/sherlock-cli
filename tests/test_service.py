from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from sherlock_cli.config import load_config
from sherlock_cli.models import SubmissionRequest
from sherlock_cli.service import SherlockService
from sherlock_cli.state import StateStore


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.fail_next_control_socket_call = False

    def __call__(self, args, capture_output=True, text=True):
        self.calls.append(args)
        command = " ".join(args)
        if self.fail_next_control_socket_call and args[0] == "ssh":
            self.fail_next_control_socket_call = False
            return SimpleNamespace(
                returncode=255,
                stdout="",
                stderr="Control socket connect(/tmp/sherlock.sock): Connection refused",
            )
        if "printf '%s' \"$HOME\"" in command:
            return SimpleNamespace(returncode=0, stdout="/home/demo", stderr="")
        if "mkdir -p" in command:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "scp":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "sbatch --parsable" in command:
            return SimpleNamespace(returncode=0, stdout="4321\n", stderr="")
        if "squeue -u" in command:
            return SimpleNamespace(
                returncode=0,
                stdout="4321|GPU-jupyterlab|RUNNING|gpu|sh03-01|00:03:12\n9999|other|PENDING|normal|(Priority)|00:00\n",
                stderr="",
            )
        if "scontrol show job -o 4321" in command:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "JobId=4321 JobName=GPU-jupyterlab JobState=RUNNING Partition=gpu "
                    "NodeList=sh03-01 Reason=None RunTime=00:03:12 "
                    "StdOut=/home/demo/forward-util/GPU-jupyterlab-4321.out "
                    "StdErr=/home/demo/forward-util/GPU-jupyterlab-4321.err"
                ),
                stderr="",
            )
        if "tail -n 200" in command or "tail -n 60" in command:
            return SimpleNamespace(
                returncode=0,
                stdout="http://sh03-01:56793/lab?token=abc123\n",
                stderr="",
            )
        if "scancel 4321" in command:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "squeue -j 4321" in command:
            return SimpleNamespace(
                returncode=0,
                stdout="4321|GPU-jupyterlab|RUNNING|gpu|sh03-01|00:03:12\n",
                stderr="",
            )
        if "sacct -j 4321" in command:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class FakePopen:
    def __init__(self, args, stdout=None, stderr=None, stdin=None):
        self.args = args
        self.pid = 7777


class ServiceTests(unittest.TestCase):
    def make_service(self, tmpdir: str):
        config = load_config()
        config.state_path = Path(tmpdir) / "state.json"
        runner = FakeRunner()
        service = SherlockService(
            config,
            StateStore(config.state_path),
            runner=runner,
            popen_factory=FakePopen,
            sleeper=lambda _seconds: None,
            browser_opener=lambda _url: None,
        )
        return service, runner

    def test_submit_job_records_state(self):
        with TemporaryDirectory() as tmpdir:
            service, runner = self.make_service(tmpdir)
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
            self.assertTrue(any(call[0] == "scp" for call in runner.calls))
            scp_call = next(call for call in runner.calls if call[0] == "scp")
            self.assertIn("ControlMaster=auto", scp_call)
            self.assertTrue(any(part.startswith("ControlPath=") for part in scp_call))

    def test_list_and_connect_job(self):
        with TemporaryDirectory() as tmpdir:
            service, _runner = self.make_service(tmpdir)
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
            info = service.connect_job("4321", open_browser=False)
            self.assertIsInstance(info.local_port, int)
            self.assertGreater(info.local_port, 0)
            self.assertEqual(info.local_url, f"http://localhost:{info.local_port}/lab?token=abc123")

    def test_stale_control_socket_retries_once(self):
        with TemporaryDirectory() as tmpdir:
            service, runner = self.make_service(tmpdir)
            runner.fail_next_control_socket_call = True

            jobs = service.list_jobs()

            self.assertEqual(len(jobs), 2)
            ssh_calls = [call for call in runner.calls if call[0] == "ssh"]
            self.assertGreaterEqual(len(ssh_calls), 2)

    def test_kill_job_clears_tunnel(self):
        with TemporaryDirectory() as tmpdir:
            service, _runner = self.make_service(tmpdir)
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


if __name__ == "__main__":
    unittest.main()

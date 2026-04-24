from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sherlock_cli.config import load_config
from sherlock_cli.remote import RemoteService, SshConfigManager
from sherlock_cli.service import SherlockError
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
        if "echo __sherlock_connected__" in command:
            return "__sherlock_connected__"
        if "mkdir -p" in command:
            return ""
        if "authorized_keys" in command:
            return ""
        if "chmod +x" in command:
            return ""
        if "ssh-keygen -t ed25519" in command:
            return ""
        if "for p in /usr/libexec/openssh/sftp-server" in command:
            return "/usr/libexec/openssh/sftp-server"
        if "test -x" in command:
            return "yes"
        if "scontrol show job -o 4321" in command:
            return (
                "JobId=4321 JobName=GPU-jupyterlab JobState=RUNNING Partition=gpu "
                "NodeList=sh03-01 Reason=None RunTime=00:03:12 "
                "StdOut=/home/demo/forward-util/GPU-jupyterlab-4321.out "
                "StdErr=/home/demo/forward-util/GPU-jupyterlab-4321.err"
            )
        if "cat /home/demo/.cursor-remote/sherlock/session " in command:
            return "sh03-01 40022 4321"
        if "cat /home/demo/.cursor-remote/sherlock/session_type" in command:
            return "attached"
        if "printf '%s\\n' attached >" in command:
            return ""
        if "rm -f /home/demo/.cursor-remote/sherlock/session " in command:
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


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, args, check=True):
        self.calls.append((args, check))


class RemoteServiceTests(unittest.TestCase):
    def make_service(self, tmpdir: str):
        config = load_config()
        config.state_path = Path(tmpdir) / "state.json"
        runner = FakeRunner()
        session_factory = FakeSessionFactory()
        ssh_config = SshConfigManager(Path(tmpdir) / "ssh_config")
        service = RemoteService(
            config,
            StateStore(config.state_path, config.connection.resource),
            runner=runner,
            ssh_session_factory=session_factory,
            sleeper=lambda _seconds: None,
            browser_opener=lambda _url: None,
            ssh_config_manager=ssh_config,
        )
        return service, runner, session_factory, ssh_config

    def test_setup_remote_uploads_assets_and_profiles(self):
        with TemporaryDirectory() as tmpdir:
            ssh_dir = Path.home() / ".ssh"
            ssh_dir.mkdir(parents=True, exist_ok=True)
            pubkey = ssh_dir / "id_ed25519.pub"
            original = pubkey.read_text() if pubkey.exists() else None
            pubkey.write_text("ssh-ed25519 AAAATESTKEY demo@test\n")
            try:
                service, _runner, session_factory, ssh_config = self.make_service(tmpdir)
                service.setup_remote()
            finally:
                if original is None:
                    pubkey.unlink(missing_ok=True)
                else:
                    pubkey.write_text(original)

            self.assertTrue(ssh_config.path.exists())
            session = session_factory.sessions[0]
            uploaded_paths = [path for path, _content in session.uploads]
            self.assertIn("/home/demo/.cursor-remote/sherlock/start_sshd.sh", uploaded_paths)
            self.assertIn("/home/demo/.cursor-remote/sherlock/sshd_config", uploaded_paths)
            self.assertIn("/home/demo/.cursor-remote/sherlock/cursor_cpu.slurm", uploaded_paths)
            self.assertIn("/home/demo/.cursor-remote/sherlock/cursor_gpu.slurm", uploaded_paths)
            self.assertIn("/home/demo/.cursor-remote/sherlock/cursor_owners.slurm", uploaded_paths)

    def test_attach_updates_local_state_and_runner_commands(self):
        with TemporaryDirectory() as tmpdir:
            ssh_dir = Path.home() / ".ssh"
            ssh_dir.mkdir(parents=True, exist_ok=True)
            pubkey = ssh_dir / "id_ed25519.pub"
            original = pubkey.read_text() if pubkey.exists() else None
            pubkey.write_text("ssh-ed25519 AAAATESTKEY demo@test\n")
            try:
                service, runner, _session_factory, _ssh_config = self.make_service(tmpdir)
                session = service.attach("4321")
            finally:
                if original is None:
                    pubkey.unlink(missing_ok=True)
                else:
                    pubkey.write_text(original)

            self.assertEqual(session.job_id, "4321")
            self.assertEqual(session.port, 40022)
            saved = service.state.get_remote_session()
            self.assertIsNotNone(saved)
            self.assertEqual(saved.session_type, "attached")
            self.assertTrue(
                any("srun --jobid=4321 --overlap" in call[-1] for call, _check in runner.calls if call[0] == "ssh")
            )
            self.assertTrue(
                any(call[:3] == ["ssh", "-o", "ConnectTimeout=10"] for call, _check in runner.calls)
            )

    def test_stop_attached_session_clears_local_state(self):
        with TemporaryDirectory() as tmpdir:
            ssh_dir = Path.home() / ".ssh"
            ssh_dir.mkdir(parents=True, exist_ok=True)
            pubkey = ssh_dir / "id_ed25519.pub"
            original = pubkey.read_text() if pubkey.exists() else None
            pubkey.write_text("ssh-ed25519 AAAATESTKEY demo@test\n")
            try:
                service, runner, _session_factory, _ssh_config = self.make_service(tmpdir)
                service.stop()
            finally:
                if original is None:
                    pubkey.unlink(missing_ok=True)
                else:
                    pubkey.write_text(original)

            self.assertIsNone(service.state.get_remote_session())
            self.assertTrue(any(call[0:2] == ["ssh", "sherlock-compute"] for call, _check in runner.calls))

    def test_start_without_profiles_errors(self):
        with TemporaryDirectory() as tmpdir:
            config = load_config("marlowe_presets.toml")
            config.state_path = Path(tmpdir) / "state.json"
            service = RemoteService(
                config,
                StateStore(config.state_path, config.connection.resource),
                runner=FakeRunner(),
            )
            with self.assertRaises(SherlockError):
                service.resolve_remote_profile(None)

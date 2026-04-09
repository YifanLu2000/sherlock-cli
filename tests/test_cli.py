import os
from unittest import mock
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from rich.text import Text

from sherlock_cli.cli import (
    ESC_KEY,
    InterruptTracker,
    MENU_ACTIONS,
    SWITCH_SENTINEL,
    _read_raw_key,
    build_jobs_table,
    choose_cluster,
    choose_job,
    main,
    select_remote_profile,
    select_preset,
    should_exit_on_interrupt,
)
from sherlock_cli.config import discover_configs, load_config
from sherlock_cli.models import ConnectionConfig, JobInfo, Preset
from sherlock_cli.remote import RemoteService
from sherlock_cli.state import StateStore


class DummyLive:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def update(self, *args, **kwargs):
        return None


class CliTests(unittest.TestCase):
    def test_first_interrupt_does_not_exit(self):
        tracker = InterruptTracker()
        self.assertFalse(should_exit_on_interrupt(tracker, now=10.0))

    def test_second_interrupt_within_window_exits(self):
        tracker = InterruptTracker()
        self.assertFalse(should_exit_on_interrupt(tracker, now=10.0))
        self.assertTrue(should_exit_on_interrupt(tracker, now=11.0))

    def test_second_interrupt_after_window_does_not_exit(self):
        tracker = InterruptTracker()
        self.assertFalse(should_exit_on_interrupt(tracker, now=10.0, window_seconds=2.0))
        self.assertFalse(should_exit_on_interrupt(tracker, now=12.5, window_seconds=2.0))

    def test_raw_ctrl_c_raises_keyboard_interrupt(self):
        fake_stdin = mock.Mock()
        fake_stdin.fileno.return_value = 0
        with (
            mock.patch("sherlock_cli.cli.sys.stdin", fake_stdin),
            mock.patch("sherlock_cli.cli.os.read", return_value=b"\x03"),
            mock.patch("sherlock_cli.cli.termios.tcgetattr", return_value=object()),
            mock.patch("sherlock_cli.cli.termios.tcsetattr"),
            mock.patch("sherlock_cli.cli.tty.setraw"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                _read_raw_key()

    def test_raw_escape_returns_escape_key(self):
        fake_stdin = mock.Mock()
        fake_stdin.fileno.return_value = 0
        with (
            mock.patch("sherlock_cli.cli.sys.stdin", fake_stdin),
            mock.patch("sherlock_cli.cli.os.read", return_value=b"\x1b"),
            mock.patch("sherlock_cli.cli.select.select", side_effect=[([0], [], []), ([], [], [])]),
            mock.patch("sherlock_cli.cli.termios.tcgetattr", return_value=object()),
            mock.patch("sherlock_cli.cli.termios.tcsetattr"),
            mock.patch("sherlock_cli.cli.tty.setraw"),
        ):
            self.assertEqual(_read_raw_key(timeout=1.0), ESC_KEY)

    def test_raw_arrow_key_returns_full_escape_sequence(self):
        fake_stdin = mock.Mock()
        fake_stdin.fileno.return_value = 0
        with (
            mock.patch("sherlock_cli.cli.sys.stdin", fake_stdin),
            mock.patch(
                "sherlock_cli.cli.os.read",
                side_effect=[b"\x1b", b"[", b"C"],
            ),
            mock.patch(
                "sherlock_cli.cli.select.select",
                side_effect=[([0], [], []), ([0], [], []), ([0], [], []), ([], [], [])],
            ),
            mock.patch("sherlock_cli.cli.termios.tcgetattr", return_value=object()),
            mock.patch("sherlock_cli.cli.termios.tcsetattr"),
            mock.patch("sherlock_cli.cli.tty.setraw"),
        ):
            self.assertEqual(_read_raw_key(timeout=1.0), "\x1b[C")

    def test_build_jobs_table_marks_connected_jobs(self):
        jobs = [
            JobInfo(
                job_id="123",
                name="GPU-jupyterlab",
                state="RUNNING",
                partition="gpu",
                remote_port=56793,
                reason_or_node="sh03-01",
                elapsed="01:23",
                origin="CLI-managed",
                connected=True,
            )
        ]

        table = build_jobs_table(jobs)

        name_cell = table.columns[1]._cells[0]
        state_cell = table.columns[2]._cells[0]
        port_cell = table.columns[4]._cells[0]
        origin_cell = table.columns[7]._cells[0]
        self.assertIsInstance(name_cell, Text)
        self.assertEqual(name_cell.plain, "● GPU-jupyterlab")
        self.assertEqual(str(name_cell.style), "bold green")
        self.assertIsInstance(state_cell, Text)
        self.assertEqual(state_cell.plain, "RUNNING")
        self.assertEqual(str(state_cell.style), "bold green")
        self.assertEqual(port_cell, "56793")
        self.assertIsInstance(origin_cell, Text)
        self.assertEqual(origin_cell.plain, "CLI-managed, connected")
        self.assertEqual(str(origin_cell.style), "bold green")

    def test_choose_job_escape_returns_none(self):
        jobs = [
            JobInfo(
                job_id="123",
                name="GPU-jupyterlab",
                state="RUNNING",
                partition="gpu",
                reason_or_node="sh03-01",
                elapsed="01:23",
                origin="CLI-managed",
            )
        ]
        with (
            mock.patch("sherlock_cli.cli.sys.stdin.isatty", return_value=True),
            mock.patch("sherlock_cli.cli._read_raw_key", return_value=ESC_KEY),
            mock.patch("sherlock_cli.cli.Live", DummyLive),
        ):
            self.assertIsNone(choose_job(jobs, action_label="Connect"))

    def test_select_preset_escape_returns_none(self):
        service = mock.Mock()
        service.config.presets = {
            "gpu": Preset(
                id="gpu",
                label="GPU",
                job_name="GPU-jupyterlab",
                sbatch_script="sbatches/GPU-jupyterlab.sbatch",
                partition="gpu",
                cpus=8,
                mem="64G",
                time="24:00:00",
                port=56793,
            )
        }
        service.config.connection = ConnectionConfig(
            name="Sherlock",
            resource="sherlock",
            domain_name="login.sherlock.stanford.edu",
            forward_username="demo",
            email="demo@example.com",
            machine_prefix="sh",
            default_notebook_dir="/oak/demo",
            remote_util_dir="~/forward-util",
            use_kerberos=True,
        )
        with (
            mock.patch("sherlock_cli.cli.sys.stdin.isatty", return_value=True),
            mock.patch("sherlock_cli.cli._read_raw_key", return_value=ESC_KEY),
            mock.patch("sherlock_cli.cli.Live", DummyLive),
        ):
            self.assertIsNone(select_preset(service))


    def test_choose_cluster_enter_selects(self):
        clusters = [("Sherlock", "/tmp/a.toml"), ("Marlowe", "/tmp/b.toml")]
        with (
            mock.patch("sherlock_cli.cli.sys.stdin.isatty", return_value=True),
            mock.patch("sherlock_cli.cli._read_raw_key", return_value="\r"),
            mock.patch("sherlock_cli.cli.Live", DummyLive),
        ):
            result = choose_cluster(clusters)
            self.assertEqual(result, ("Sherlock", "/tmp/a.toml"))

    def test_choose_cluster_q_returns_none(self):
        clusters = [("Sherlock", "/tmp/a.toml"), ("Marlowe", "/tmp/b.toml")]
        with (
            mock.patch("sherlock_cli.cli.sys.stdin.isatty", return_value=True),
            mock.patch("sherlock_cli.cli._read_raw_key", return_value="q"),
            mock.patch("sherlock_cli.cli.Live", DummyLive),
        ):
            self.assertIsNone(choose_cluster(clusters))

    def test_discover_configs_finds_toml_files(self):
        configs = discover_configs()
        names = [name for name, _path in configs]
        self.assertIn("Sherlock", names)
        self.assertIn("Marlowe", names)

    def test_switch_sentinel_is_string(self):
        self.assertIsInstance(SWITCH_SENTINEL, str)

    def test_main_menu_contains_remote_action(self):
        self.assertIn(("m", "Remote"), MENU_ACTIONS)

    def test_select_remote_profile_escape_returns_none(self):
        with TemporaryDirectory() as tmpdir:
            config = load_config()
            config.state_path = Path(tmpdir) / "state.json"
            service = RemoteService(config, StateStore(config.state_path))
            with (
                mock.patch("sherlock_cli.cli.sys.stdin.isatty", return_value=True),
                mock.patch("sherlock_cli.cli._read_raw_key", return_value=ESC_KEY),
                mock.patch("sherlock_cli.cli.Live", DummyLive),
            ):
                self.assertIsNone(select_remote_profile(service))

    def test_setup_command_writes_user_config(self):
        with TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            target = home / ".config" / "sherlock-cli" / "sherlock.toml"
            with (
                mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False),
                mock.patch(
                    "sherlock_cli.cli.Prompt.ask",
                    side_effect=["demo-user", "demo-user@stanford.edu", "/oak/demo-user"],
                ),
            ):
                exit_code = main(["setup", "sherlock"])

            self.assertEqual(exit_code, 0)
            config = load_config(str(target))
            self.assertEqual(config.connection.forward_username, "demo-user")
            self.assertEqual(config.connection.email, "demo-user@stanford.edu")
            self.assertEqual(config.connection.default_notebook_dir, "/oak/demo-user")


if __name__ == "__main__":
    unittest.main()

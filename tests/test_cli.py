from unittest import mock
import unittest

from rich.text import Text

from sherlock_cli.cli import (
    ESC_KEY,
    InterruptTracker,
    _read_raw_key,
    build_jobs_table,
    choose_job,
    select_preset,
    should_exit_on_interrupt,
)
from sherlock_cli.models import ConnectionConfig, JobInfo, Preset


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
        fake_stdin.read.return_value = "\x03"
        with (
            mock.patch("sherlock_cli.cli.sys.stdin", fake_stdin),
            mock.patch("sherlock_cli.cli.termios.tcgetattr", return_value=object()),
            mock.patch("sherlock_cli.cli.termios.tcsetattr"),
            mock.patch("sherlock_cli.cli.tty.setraw"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                _read_raw_key()

    def test_raw_escape_returns_escape_key(self):
        fake_stdin = mock.Mock()
        fake_stdin.fileno.return_value = 0
        fake_stdin.read.return_value = ESC_KEY
        with (
            mock.patch("sherlock_cli.cli.sys.stdin", fake_stdin),
            mock.patch("sherlock_cli.cli.select.select", side_effect=[([0], [], []), ([], [], [])]),
            mock.patch("sherlock_cli.cli.termios.tcgetattr", return_value=object()),
            mock.patch("sherlock_cli.cli.termios.tcsetattr"),
            mock.patch("sherlock_cli.cli.tty.setraw"),
        ):
            self.assertEqual(_read_raw_key(timeout=1.0), ESC_KEY)

    def test_raw_arrow_key_returns_full_escape_sequence(self):
        fake_stdin = mock.Mock()
        fake_stdin.fileno.return_value = 0
        fake_stdin.read.side_effect = [ESC_KEY, "[", "C"]
        with (
            mock.patch("sherlock_cli.cli.sys.stdin", fake_stdin),
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
                reason_or_node="sh03-01",
                elapsed="01:23",
                origin="CLI-managed",
                connected=True,
            )
        ]

        table = build_jobs_table(jobs)

        name_cell = table.columns[1]._cells[0]
        state_cell = table.columns[2]._cells[0]
        origin_cell = table.columns[6]._cells[0]
        self.assertIsInstance(name_cell, Text)
        self.assertEqual(name_cell.plain, "● GPU-jupyterlab")
        self.assertEqual(str(name_cell.style), "bold green")
        self.assertIsInstance(state_cell, Text)
        self.assertEqual(state_cell.plain, "RUNNING")
        self.assertEqual(str(state_cell.style), "bold green")
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


if __name__ == "__main__":
    unittest.main()

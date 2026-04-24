import os
from unittest import mock
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from rich.text import Text
from rich.table import Table

from sherlock_cli.cli import (
    ClusterContext,
    DISCONNECT_SENTINEL,
    ESC_KEY,
    InterruptTracker,
    MENU_ACTIONS,
    SWITCH_SENTINEL,
    _read_raw_key,
    build_cluster_jobs_table,
    build_jobs_table,
    build_multi_cluster_dashboard,
    build_multi_cluster_main_view,
    choose_cluster,
    choose_job,
    choose_action,
    choose_multi_cluster_action,
    choose_remote_action,
    interactive_menu,
    interactive_multi_cluster_menu,
    interactive_remote_menu,
    main,
    prompt_local_connect_port,
    resolve_cluster_job_target,
    resolve_remote_session_context,
    select_remote_profile,
    select_preset,
    should_exit_on_interrupt,
)
from sherlock_cli.config import discover_configs, load_config
from sherlock_cli.models import ConnectionConfig, JobInfo, Preset, RemoteSessionMetadata
from sherlock_cli.remote import RemoteService
from sherlock_cli.service import PersistentSessionLostError, SherlockError
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


class FakeMultiClusterService:
    def __init__(self, name: str, resource: str, jobs=None, *, has_remote_session: bool = False):
        self.config = mock.Mock()
        self.config.connection = ConnectionConfig(
            name=name,
            resource=resource,
            domain_name=f"{resource}.example.com",
            forward_username="demo",
            email="demo@example.com",
            machine_prefix=resource[:2],
            default_notebook_dir="/tmp",
            remote_util_dir="~/forward-util",
            use_kerberos=True,
        )
        self.config.config_path = Path(f"/tmp/{resource}.toml")
        self.jobs = list(jobs or [])
        self.state = mock.Mock()
        self.state.get_remote_session.return_value = (
            RemoteSessionMetadata(
                job_id="999",
                node="node-1",
                port=40022,
                session_type="attached",
                last_verified_at="2026-04-02T00:00:00+00:00",
            )
            if has_remote_session
            else None
        )
        self.connect_job = mock.Mock()
        self.close = mock.Mock()

    def list_jobs(self):
        return list(self.jobs)


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

    def test_build_cluster_jobs_table_shows_error_row(self):
        context = ClusterContext(
            "Sherlock",
            Path("/tmp/sherlock.toml"),
            FakeMultiClusterService("Sherlock", "sherlock"),
            load_error="SSH failed",
        )

        table = build_cluster_jobs_table(context)

        self.assertEqual(table.title, "Sherlock Jobs")
        self.assertEqual(table.columns[1]._cells[0], "Cluster unavailable")
        state_cell = table.columns[2]._cells[0]
        self.assertIsInstance(state_cell, Text)
        self.assertEqual(state_cell.plain, "ERROR")
        self.assertEqual(str(state_cell.style), "bold red")
        self.assertEqual(table.columns[5]._cells[0], "SSH failed")

    def test_build_multi_cluster_dashboard_creates_one_table_per_cluster(self):
        sherlock_job = JobInfo(
            job_id="123",
            name="job-a",
            state="RUNNING",
            partition="gpu",
            reason_or_node="sh03-01",
            elapsed="00:10",
            origin="CLI-managed",
        )
        contexts = [
            ClusterContext(
                "Sherlock",
                Path("/tmp/sherlock.toml"),
                FakeMultiClusterService("Sherlock", "sherlock", [sherlock_job]),
                jobs=[sherlock_job],
            ),
            ClusterContext("Marlowe", Path("/tmp/marlowe.toml"), FakeMultiClusterService("Marlowe", "marlowe")),
        ]

        dashboard = build_multi_cluster_dashboard(contexts, active_index=1)

        self.assertEqual(len(dashboard.renderables), 2)
        self.assertTrue(all(isinstance(renderable, Table) for renderable in dashboard.renderables))
        self.assertEqual([table.title for table in dashboard.renderables], ["Sherlock Jobs", "Marlowe Jobs"])
        self.assertEqual(dashboard.renderables[0].border_style, None)
        self.assertEqual(dashboard.renderables[1].border_style, "cyan")
        self.assertEqual(dashboard.renderables[1].title_style, "bold cyan")

    def test_build_multi_cluster_main_view_stacks_tables_above_actions(self):
        contexts = [
            ClusterContext("Sherlock", Path("/tmp/sherlock.toml"), FakeMultiClusterService("Sherlock", "sherlock")),
            ClusterContext("Marlowe", Path("/tmp/marlowe.toml"), FakeMultiClusterService("Marlowe", "marlowe")),
        ]

        view = build_multi_cluster_main_view(contexts, active_index=0, action_index=0)

        self.assertEqual(len(view.renderables), 2)
        self.assertEqual(len(view.renderables[0].renderables), 2)
        self.assertEqual([table.title for table in view.renderables[0].renderables], ["Sherlock Jobs", "Marlowe Jobs"])

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
            service = RemoteService(config, StateStore(config.state_path, config.connection.resource))
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

    def test_choose_action_returns_disconnect_sentinel_when_refresh_loses_session(self):
        with (
            mock.patch("sherlock_cli.cli.sys.stdin.isatty", return_value=True),
            mock.patch("sherlock_cli.cli._read_raw_key", return_value=None),
            mock.patch("sherlock_cli.cli.Live", DummyLive),
        ):
            action = choose_action(
                [],
                refresh_callback=mock.Mock(
                    side_effect=PersistentSessionLostError("Persistent SSH session disconnected.")
                ),
            )

        self.assertEqual(action, DISCONNECT_SENTINEL)

    def test_choose_remote_action_returns_disconnect_sentinel_when_refresh_loses_session(self):
        service = mock.Mock()
        service.read_remote_session.return_value = None
        with (
            mock.patch("sherlock_cli.cli.sys.stdin.isatty", return_value=True),
            mock.patch("sherlock_cli.cli._read_raw_key", return_value=None),
            mock.patch("sherlock_cli.cli.Live", DummyLive),
        ):
            action = choose_remote_action(
                service,
                [],
                refresh_callback=mock.Mock(
                    side_effect=PersistentSessionLostError("Persistent SSH session disconnected.")
                ),
            )

        self.assertEqual(action, DISCONNECT_SENTINEL)

    def test_interactive_menu_retries_after_disconnect_without_pause_prompt(self):
        service = mock.Mock()
        with (
            mock.patch(
                "sherlock_cli.cli.show_splash_and_load",
                side_effect=[
                    PersistentSessionLostError("Persistent SSH session disconnected."),
                    [],
                ],
            ),
            mock.patch("sherlock_cli.cli.choose_disconnect_action", return_value="r"),
            mock.patch("sherlock_cli.cli.choose_action", return_value="q"),
            mock.patch("sherlock_cli.cli.Prompt.ask") as prompt_ask,
        ):
            result = interactive_menu(service)

        self.assertEqual(result, 0)
        service.close.assert_called_once()
        prompt_ask.assert_not_called()

    def test_prompt_local_connect_port_returns_blank_as_none_when_remote_port_unknown(self):
        job = JobInfo(
            job_id="123",
            name="job-a",
            state="RUNNING",
            partition="gpu",
            reason_or_node="sh03-01",
            elapsed="00:10",
            origin="CLI-managed",
            remote_port=None,
        )

        with mock.patch("sherlock_cli.cli.Prompt.ask", return_value=""):
            self.assertIsNone(prompt_local_connect_port(job))

    def test_interactive_menu_prompts_for_local_port_on_connect(self):
        job = JobInfo(
            job_id="123",
            name="job-a",
            state="RUNNING",
            partition="gpu",
            reason_or_node="sh03-01",
            elapsed="00:10",
            origin="CLI-managed",
            remote_port=56793,
        )
        service = mock.Mock()
        service.list_jobs.return_value = [job]
        service.connect_job.return_value = mock.Mock(local_port=56790, local_url="http://localhost:56790/")

        with (
            mock.patch("sherlock_cli.cli.show_splash_and_load", return_value=[job]),
            mock.patch("sherlock_cli.cli.choose_action", side_effect=["c", "q"]),
            mock.patch("sherlock_cli.cli.choose_job", return_value=job),
            mock.patch("sherlock_cli.cli.prompt_local_connect_port", return_value=56790),
            mock.patch("sherlock_cli.cli.render_jupyter_forwarding"),
            mock.patch("sherlock_cli.cli.pause_for_continue"),
        ):
            result = interactive_menu(service)

        self.assertEqual(result, 0)
        service.connect_job.assert_called_once_with("123", open_browser=True, local_port=56790)

    def test_choose_multi_cluster_action_moves_active_panel(self):
        contexts = [
            ClusterContext("Sherlock", Path("/tmp/sherlock.toml"), FakeMultiClusterService("Sherlock", "sherlock")),
            ClusterContext("Marlowe", Path("/tmp/marlowe.toml"), FakeMultiClusterService("Marlowe", "marlowe")),
        ]
        with (
            mock.patch("sherlock_cli.cli.sys.stdin.isatty", return_value=True),
            mock.patch("sherlock_cli.cli._read_raw_key", side_effect=["\x1b[B", "\r"]),
            mock.patch("sherlock_cli.cli.Live", DummyLive),
        ):
            action, active_index = choose_multi_cluster_action(contexts, active_index=0)

        self.assertEqual(action, "r")
        self.assertEqual(active_index, 1)

    def test_resolve_cluster_job_target_requires_prefix_when_job_id_is_ambiguous(self):
        sherlock_job = JobInfo(
            job_id="123",
            name="job-a",
            state="RUNNING",
            partition="gpu",
            reason_or_node="sh03-01",
            elapsed="00:10",
            origin="CLI-managed",
        )
        marlowe_job = JobInfo(
            job_id="123",
            name="job-b",
            state="RUNNING",
            partition="gpu",
            reason_or_node="mr01",
            elapsed="00:10",
            origin="CLI-managed",
        )
        contexts = [
            ClusterContext("Sherlock", Path("/tmp/sherlock.toml"), FakeMultiClusterService("Sherlock", "sherlock", [sherlock_job])),
            ClusterContext("Marlowe", Path("/tmp/marlowe.toml"), FakeMultiClusterService("Marlowe", "marlowe", [marlowe_job])),
        ]

        with self.assertRaisesRegex(SherlockError, "matched multiple clusters"):
            resolve_cluster_job_target(contexts, "123", running_only=True)

    def test_resolve_cluster_job_target_accepts_cluster_prefix(self):
        sherlock_job = JobInfo(
            job_id="123",
            name="job-a",
            state="RUNNING",
            partition="gpu",
            reason_or_node="sh03-01",
            elapsed="00:10",
            origin="CLI-managed",
        )
        marlowe_job = JobInfo(
            job_id="123",
            name="job-b",
            state="RUNNING",
            partition="gpu",
            reason_or_node="mr01",
            elapsed="00:10",
            origin="CLI-managed",
        )
        contexts = [
            ClusterContext("Sherlock", Path("/tmp/sherlock.toml"), FakeMultiClusterService("Sherlock", "sherlock", [sherlock_job])),
            ClusterContext("Marlowe", Path("/tmp/marlowe.toml"), FakeMultiClusterService("Marlowe", "marlowe", [marlowe_job])),
        ]

        context, job = resolve_cluster_job_target(contexts, "marlowe:123", running_only=True)
        self.assertEqual(context.name, "Marlowe")
        self.assertEqual(job.name, "job-b")

    def test_resolve_remote_session_context_requires_single_cluster(self):
        contexts = [
            ClusterContext(
                "Sherlock",
                Path("/tmp/sherlock.toml"),
                FakeMultiClusterService("Sherlock", "sherlock", has_remote_session=True),
            ),
            ClusterContext(
                "Marlowe",
                Path("/tmp/marlowe.toml"),
                FakeMultiClusterService("Marlowe", "marlowe", has_remote_session=True),
            ),
        ]

        with self.assertRaisesRegex(SherlockError, "Multiple clusters have remote sessions"):
            resolve_remote_session_context(contexts)

    def test_interactive_multi_cluster_menu_routes_connect_to_active_cluster(self):
        sherlock_job = JobInfo(
            job_id="123",
            name="job-a",
            state="RUNNING",
            partition="gpu",
            reason_or_node="sh03-01",
            elapsed="00:10",
            origin="CLI-managed",
        )
        marlowe_job = JobInfo(
            job_id="456",
            name="job-b",
            state="RUNNING",
            partition="gpu",
            reason_or_node="mr01",
            elapsed="00:10",
            origin="CLI-managed",
        )
        sherlock_service = FakeMultiClusterService("Sherlock", "sherlock", [sherlock_job])
        marlowe_service = FakeMultiClusterService("Marlowe", "marlowe", [marlowe_job])
        marlowe_service.connect_job.return_value = mock.Mock(port=56793, local_port=56793, local_url="http://localhost:56793/")
        contexts = [
            ClusterContext("Sherlock", Path("/tmp/sherlock.toml"), sherlock_service, jobs=[sherlock_job]),
            ClusterContext("Marlowe", Path("/tmp/marlowe.toml"), marlowe_service, jobs=[marlowe_job]),
        ]

        with (
            mock.patch("sherlock_cli.cli.show_multi_cluster_splash_and_load"),
            mock.patch("sherlock_cli.cli.choose_multi_cluster_action", side_effect=[("c", 1), ("q", 1)]),
            mock.patch("sherlock_cli.cli.choose_job", return_value=marlowe_job),
            mock.patch("sherlock_cli.cli.prompt_local_connect_port", return_value=56790),
            mock.patch("sherlock_cli.cli.render_jupyter_forwarding"),
            mock.patch("sherlock_cli.cli.refresh_cluster_context"),
            mock.patch("sherlock_cli.cli.pause_for_continue"),
        ):
            result = interactive_multi_cluster_menu(contexts)

        self.assertEqual(result, 0)
        marlowe_service.connect_job.assert_called_once_with("456", open_browser=True, local_port=56790)
        sherlock_service.connect_job.assert_not_called()

    def test_main_without_config_uses_multi_cluster_interactive_menu(self):
        contexts = [
            ClusterContext("Sherlock", Path("/tmp/sherlock.toml"), FakeMultiClusterService("Sherlock", "sherlock"))
        ]
        with (
            mock.patch("sherlock_cli.cli.build_cluster_contexts", return_value=contexts),
            mock.patch("sherlock_cli.cli.interactive_multi_cluster_menu", return_value=0) as interactive_mock,
            mock.patch("sherlock_cli.cli.choose_cluster") as choose_cluster_mock,
        ):
            result = main([])

        self.assertEqual(result, 0)
        interactive_mock.assert_called_once_with(contexts)
        choose_cluster_mock.assert_not_called()

    def test_main_connect_passes_local_port_to_single_cluster_service(self):
        service = mock.Mock()
        service.connect_job.return_value = mock.Mock(local_port=56790, local_url="http://localhost:56790/")

        with (
            mock.patch("sherlock_cli.cli.make_service", return_value=service),
            mock.patch("sherlock_cli.cli.render_jupyter_forwarding"),
        ):
            result = main(["--config", "/tmp/marlowe.toml", "connect", "123", "--local-port", "56790"])

        self.assertEqual(result, 0)
        service.connect_job.assert_called_once_with("123", open_browser=True, local_port=56790)

    def test_main_connect_passes_local_port_to_multi_cluster_service(self):
        sherlock_service = FakeMultiClusterService("Sherlock", "sherlock")
        marlowe_job = JobInfo(
            job_id="456",
            name="job-b",
            state="RUNNING",
            partition="gpu",
            reason_or_node="mr01",
            elapsed="00:10",
            origin="CLI-managed",
        )
        marlowe_service = FakeMultiClusterService("Marlowe", "marlowe", [marlowe_job])
        marlowe_service.connect_job.return_value = mock.Mock(local_port=56790, local_url="http://localhost:56790/")
        contexts = [
            ClusterContext("Sherlock", Path("/tmp/sherlock.toml"), sherlock_service),
            ClusterContext("Marlowe", Path("/tmp/marlowe.toml"), marlowe_service),
        ]

        with (
            mock.patch("sherlock_cli.cli.build_cluster_contexts", return_value=contexts),
            mock.patch("sherlock_cli.cli.render_jupyter_forwarding"),
        ):
            result = main(["connect", "marlowe:456", "--local-port", "56790"])

        self.assertEqual(result, 0)
        marlowe_service.connect_job.assert_called_once_with("456", open_browser=True, local_port=56790)
        sherlock_service.connect_job.assert_not_called()

    def test_main_list_without_config_uses_multi_cluster_command(self):
        with mock.patch("sherlock_cli.cli.run_multi_cluster_command", return_value=0) as command_mock:
            result = main(["list"])

        self.assertEqual(result, 0)
        command_mock.assert_called_once()

    def test_interactive_remote_menu_quits_on_disconnect_without_pause_prompt(self):
        service = mock.Mock()
        service.list_jobs.side_effect = PersistentSessionLostError("Persistent SSH session disconnected.")

        with (
            mock.patch("sherlock_cli.cli.choose_remote_disconnect_action", return_value="q"),
            mock.patch("sherlock_cli.cli.Prompt.ask") as prompt_ask,
        ):
            result = interactive_remote_menu(service)

        self.assertEqual(result, 0)
        service.close.assert_called_once()
        prompt_ask.assert_not_called()


if __name__ == "__main__":
    unittest.main()

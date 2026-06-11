from __future__ import annotations

import argparse
import os
import re
import select
import sys
import termios
import time
import tty
from dataclasses import dataclass, field
from dataclasses import replace
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.spinner import Spinner
from rich.text import Text
from rich.table import Table

from .config import (
    default_user_config_path,
    discover_configs,
    find_config_path,
    is_bundled_config_path,
    load_config,
    write_config,
)
from .models import JobInfo, JobLogs, Preset, RemoteProfile, SubmissionRequest
from .remote import RemoteService
from .service import PersistentSessionLostError, SherlockError, SherlockService
from .state import StateStore


console = Console()
MENU_ACTIONS = [
    ("r", "Refresh"),
    ("n", "New"),
    ("c", "Connect"),
    ("w", "Watch"),
    ("k", "Kill"),
    ("l", "Logs"),
    ("m", "Remote"),
    ("s", "Servers"),
    ("q", "Quit"),
]
REMOTE_ACTIONS = [
    ("a", "Attach"),
    ("t", "Start"),
    ("c", "Connect"),
    ("s", "Stop"),
    ("u", "Setup"),
    ("n", "Clean"),
    ("b", "Back"),
]
MULTI_CLUSTER_MENU_ACTIONS = [action for action in MENU_ACTIONS if action[0] != "s"]
INTERRUPT_EXIT_WINDOW_SECONDS = 2.0
DISCONNECT_SENTINEL = "__disconnect__"


@dataclass
class InterruptTracker:
    last_interrupt_at: float | None = None


@dataclass
class ClusterContext:
    name: str
    config_path: Path
    service: RemoteService
    jobs: list[JobInfo] = field(default_factory=list)
    load_error: str | None = None


def should_exit_on_interrupt(
    tracker: InterruptTracker,
    *,
    now: float | None = None,
    window_seconds: float = INTERRUPT_EXIT_WINDOW_SECONDS,
) -> bool:
    current = time.monotonic() if now is None else now
    if tracker.last_interrupt_at is None or current - tracker.last_interrupt_at > window_seconds:
        tracker.last_interrupt_at = current
        return False
    tracker.last_interrupt_at = None
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sherlock-cli", description="Manage Slurm Jupyter jobs")
    parser.add_argument("--config", help="Path to a cluster config TOML")

    subparsers = parser.add_subparsers(dest="command")

    setup_parser = subparsers.add_parser("setup", help="Create or update a user config")
    setup_parser.add_argument("cluster", nargs="?", help="Cluster name or resource id")
    setup_parser.add_argument("--output", help="Write the configured TOML to this path")

    list_parser = subparsers.add_parser("list", help="List pending and running jobs")
    list_parser.add_argument("--logs", action="store_true", help="Also show log paths")

    new_parser = subparsers.add_parser("new", help="Submit a new JupyterLab job")
    new_parser.add_argument("--preset", help="Preset id")
    new_parser.add_argument("--job-name", help="Slurm job name")
    new_parser.add_argument("--notebook-dir", help="Notebook working directory")
    new_parser.add_argument("--port", type=int, help="Remote Jupyter port")
    new_parser.add_argument("--partition", help="Override partition")
    new_parser.add_argument("--account", help="Override Slurm account")
    new_parser.add_argument("--mem", help="Override memory")
    new_parser.add_argument("--time", help="Override time")
    new_parser.add_argument("--cpus", type=int, help="Override CPU count")
    new_parser.add_argument("--gpus", type=int, help="Override GPU count")
    new_parser.add_argument("--nodelist", help="Override nodelist")
    new_parser.add_argument("--constraint", help="Override Slurm constraint")
    new_parser.add_argument("--no-browser", action="store_true", help="Do not open browser after connection")

    connect_parser = subparsers.add_parser("connect", help="Forward a running Jupyter job to localhost")
    connect_parser.add_argument("job", help="Job id or job name")
    connect_parser.add_argument("--no-browser", action="store_true")
    connect_parser.add_argument(
        "--local-port",
        type=int,
        help="Local port for SSH forwarding (defaults to the remote Jupyter port)",
    )

    watch_parser = subparsers.add_parser("watch", help="Watch a job until it starts or exits")
    watch_parser.add_argument("job", help="Job id or job name")
    watch_parser.add_argument("--connect-on-run", action="store_true")

    kill_parser = subparsers.add_parser("kill", help="Kill a job")
    kill_parser.add_argument("job", help="Job id or job name")

    remote_parser = subparsers.add_parser("remote", help="Manage Cursor/SSH remote sessions")
    remote_subparsers = remote_parser.add_subparsers(dest="remote_command")
    remote_subparsers.required = True

    remote_setup_parser = remote_subparsers.add_parser("setup", help="Set up remote session files on the cluster")
    remote_setup_parser.add_argument("--full", action="store_true", help="Also set up local SSH config")

    remote_subparsers.add_parser("setup-local", help="Set up local SSH config only")

    remote_attach_parser = remote_subparsers.add_parser("attach", help="Attach remote sshd to a running job")
    remote_attach_parser.add_argument("job", nargs="?", help="Running job id or job name")

    remote_start_parser = remote_subparsers.add_parser("start", help="Start a dedicated remote job")
    remote_start_parser.add_argument("profile", nargs="?", help="Remote profile id")

    remote_subparsers.add_parser("connect", help="Reconnect to an existing remote session")
    remote_subparsers.add_parser("list", help="List jobs and remote session status")
    remote_subparsers.add_parser("stop", help="Stop the current remote session")
    remote_subparsers.add_parser("clean", help="Remove the local compute-node SSH config block")

    return parser


def make_service(config_path: str | None = None) -> SherlockService:
    config = load_config(config_path)
    state = StateStore(config.state_path, config.connection.resource)
    return SherlockService(config, state)


def make_remote_service(config_path: str | None = None) -> RemoteService:
    config = load_config(config_path)
    state = StateStore(config.state_path, config.connection.resource)
    return RemoteService(config, state)


def cluster_name(service: SherlockService) -> str:
    return service.config.connection.name


def cluster_aliases(service: SherlockService) -> set[str]:
    config_path = service.config.config_path
    return {
        service.config.connection.name.lower(),
        service.config.connection.resource.lower(),
        config_path.stem.lower(),
        config_path.stem.removesuffix("_presets").lower(),
    }


def build_cluster_contexts(reuse_services: dict[Path, RemoteService] | None = None) -> list[ClusterContext]:
    reusable = {
        Path(config_path).resolve(): service
        for config_path, service in (reuse_services or {}).items()
    }
    contexts: list[ClusterContext] = []
    for name, config_path in discover_configs():
        path = Path(config_path)
        service = reusable.get(path.resolve()) or make_remote_service(str(config_path))
        contexts.append(ClusterContext(name=name, config_path=path, service=service))
    return contexts


def close_cluster_contexts(contexts: list[ClusterContext]) -> None:
    for context in contexts:
        context.service.close()


def refresh_cluster_context(context: ClusterContext, *, ensure_connected: bool = False) -> None:
    try:
        if ensure_connected:
            context.service.ensure_connected()
        context.jobs = context.service.list_jobs()
        context.load_error = None
    except SherlockError as exc:
        context.jobs = []
        context.load_error = str(exc)
        context.service.close()


def refresh_all_cluster_contexts(contexts: list[ClusterContext], *, ensure_connected: bool = False) -> None:
    for context in contexts:
        refresh_cluster_context(context, ensure_connected=ensure_connected)


def split_qualified_target(target: str) -> tuple[str | None, str]:
    if ":" not in target:
        return None, target
    prefix, value = target.split(":", 1)
    if not prefix or not value:
        raise SherlockError(f"Invalid target '{target}'. Use cluster:job or a bare job identifier.")
    return prefix, value


def find_cluster_context(contexts: list[ClusterContext], alias: str) -> ClusterContext:
    wanted = alias.strip().lower()
    for context in contexts:
        if wanted in cluster_aliases(context.service):
            return context
    raise SherlockError(f"Unknown cluster '{alias}'.")


def resolve_job_from_list(jobs: list[JobInfo], target: str) -> JobInfo | None:
    exact_id = [job for job in jobs if job.job_id == target]
    if exact_id:
        return exact_id[0]
    exact_name = [job for job in jobs if job.name == target]
    if len(exact_name) == 1:
        return exact_name[0]
    if len(exact_name) > 1:
        raise SherlockError(f"Multiple active jobs match name '{target}'. Use a job id.")
    return None


def resolve_cluster_job_target(
    contexts: list[ClusterContext],
    target: str,
    *,
    running_only: bool = False,
) -> tuple[ClusterContext, JobInfo]:
    alias, raw_target = split_qualified_target(target)
    candidate_contexts = [find_cluster_context(contexts, alias)] if alias else contexts
    matches: list[tuple[ClusterContext, JobInfo]] = []
    errors: list[str] = []
    for context in candidate_contexts:
        try:
            jobs = context.service.list_jobs()
        except SherlockError as exc:
            context.load_error = str(exc)
            errors.append(f"{context.name}: {exc}")
            continue
        context.jobs = jobs
        context.load_error = None
        visible_jobs = [job for job in jobs if job.state == "RUNNING"] if running_only else jobs
        match = resolve_job_from_list(visible_jobs, raw_target)
        if match is not None:
            matches.append((context, match))
    if not matches:
        if errors and alias:
            raise SherlockError(errors[0])
        kind = "running job" if running_only else "active job"
        raise SherlockError(f"{kind.capitalize()} '{raw_target}' not found.")
    if len(matches) == 1:
        return matches[0]
    available = ", ".join(sorted(context.name for context, _job in matches))
    raise SherlockError(
        f"Target '{raw_target}' matched multiple clusters: {available}. Use cluster:job or --config."
    )


def choose_cluster_job(contexts: list[ClusterContext], *, action_label: str) -> tuple[ClusterContext, JobInfo] | None:
    options: list[tuple[ClusterContext, JobInfo]] = []
    for context in contexts:
        for job in context.jobs:
            options.append((context, job))
    if not options:
        raise SherlockError(f"No jobs available for {action_label.lower()}.")
    if not sys.stdin.isatty():
        table = Table(title=f"Select Job To {action_label}")
        table.add_column("Cluster")
        table.add_column("Job ID", style="cyan")
        table.add_column("Name")
        for context, job in options:
            table.add_row(context.name, job.job_id, job.name)
        console.print(table)
        choices = [f"{context.service.config.connection.resource}:{job.job_id}" for context, job in options]
        selected = Prompt.ask("Job", choices=choices, default=choices[0])
        cluster_alias, job_id = selected.split(":", 1)
        context = find_cluster_context(contexts, cluster_alias)
        return context, next(job for candidate_context, job in options if candidate_context is context and job.job_id == job_id)

    selected_index = 0

    def build_view() -> Group:
        table = Table(title=f"Select Job To {action_label}")
        table.add_column("", no_wrap=True, width=2)
        table.add_column("Cluster")
        table.add_column("Job ID", style="cyan")
        table.add_column("Name")
        table.add_column("State", style="bold")
        for index, (context, job) in enumerate(options):
            style = "bold black on cyan" if index == selected_index else None
            table.add_row(
                ">" if index == selected_index else "",
                context.name,
                job.job_id,
                job.name,
                job.state,
                style=style,
            )
        help_text = Text("Use up/down arrows or j/k, Enter to select, Esc to go back.", style="dim")
        return Group(table, help_text)

    with Live(build_view(), console=console, auto_refresh=False) as live:
        while True:
            key = _read_raw_key()
            if key == "\x1b[B" or key.lower() == "j":
                selected_index = (selected_index + 1) % len(options)
                live.update(build_view(), refresh=True)
            elif key == "\x1b[A" or key.lower() == "k":
                selected_index = (selected_index - 1) % len(options)
                live.update(build_view(), refresh=True)
            elif key == ESC_KEY:
                return None
            elif key in {"\r", "\n"}:
                return options[selected_index]


def resolve_remote_session_context(contexts: list[ClusterContext]) -> ClusterContext:
    matches = [context for context in contexts if context.service.state.get_remote_session() is not None]
    if not matches:
        raise SherlockError("No remote session info found in any configured cluster.")
    if len(matches) == 1:
        return matches[0]
    names = ", ".join(sorted(context.name for context in matches))
    raise SherlockError(f"Multiple clusters have remote sessions: {names}. Use --config.")


def suggest_notebook_dir(path: str, old_username: str, new_username: str) -> str:
    if not path or not old_username or old_username == new_username:
        return path
    return re.sub(re.escape(old_username), new_username, path, flags=re.IGNORECASE)


def resolve_setup_source(args) -> Path:
    if args.config:
        return Path(args.config).expanduser()
    if args.cluster:
        config_path = find_config_path(args.cluster)
        if config_path is None:
            raise SherlockError(f"Unknown cluster: {args.cluster}")
        return config_path

    configs = discover_configs()
    if not configs:
        raise SherlockError("No *_presets.toml config files found.")
    if len(configs) == 1:
        return configs[0][1]
    chosen = choose_cluster(configs)
    if chosen is None:
        raise SherlockError("No cluster selected.")
    return chosen[1]


def resolve_setup_output_path(args, config) -> Path:
    if args.output:
        return Path(args.output).expanduser()
    if args.config and not is_bundled_config_path(config.config_path):
        return config.config_path
    return default_user_config_path(config.connection.resource)


def run_setup(args) -> int:
    source_path = resolve_setup_source(args)
    config = load_config(str(source_path))
    output_path = resolve_setup_output_path(args, config)

    username_default = os.environ.get("USER") or config.connection.forward_username
    email_domain = config.connection.email.partition("@")[2]
    email_default = config.connection.email
    if email_domain:
        email_default = f"{username_default}@{email_domain}"
    notebook_dir_default = suggest_notebook_dir(
        config.connection.default_notebook_dir,
        config.connection.forward_username,
        username_default,
    )

    console.print(f"Configuring [bold]{config.connection.name}[/bold].")
    forward_username = Prompt.ask("Cluster username", default=username_default).strip()
    email = Prompt.ask("Notification email", default=f"{forward_username}@{email_domain}" if email_domain else email_default).strip()
    default_notebook_dir = Prompt.ask("Default notebook dir", default=notebook_dir_default).strip()

    written_path = write_config(
        source_path,
        output_path,
        forward_username=forward_username,
        email=email,
        default_notebook_dir=default_notebook_dir,
    )
    console.print(f"Wrote config to [green]{written_path}[/green].")
    return 0


def build_jobs_table(jobs, *, title: str = "Jobs", selected_index: int | None = None):
    table = Table(title=title)
    if selected_index is not None:
        table.add_column("", no_wrap=True, width=2)
    table.add_column("Job ID", style="cyan", no_wrap=True)
    table.add_column("Name")
    table.add_column("State", style="bold")
    table.add_column("Partition")
    table.add_column("Port", no_wrap=True)
    table.add_column("Reason / Node")
    table.add_column("Elapsed")
    table.add_column("Origin")
    for index, job in enumerate(jobs):
        name_cell = Text(job.name)
        state_cell = Text(job.state)
        origin_cell = Text(job.origin)
        if job.connected:
            name_cell = Text(f"● {job.name}", style="bold green")
            state_cell = Text(job.state, style="bold green")
            origin_cell = Text(f"{job.origin}, connected", style="bold green")
        row = [
            job.job_id,
            name_cell,
            state_cell,
            job.partition,
            str(job.remote_port) if job.remote_port is not None else "-",
            job.reason_or_node,
            job.elapsed,
            origin_cell,
        ]
        style = None
        if selected_index is not None:
            row.insert(0, ">" if index == selected_index else "")
            style = "bold black on cyan" if index == selected_index else None
        table.add_row(*row, style=style)
    if not jobs:
        empty_row = ["-", "No active jobs", "-", "-", "-", "-", "-", "-"]
        if selected_index is not None:
            empty_row.insert(0, "")
        table.add_row(*empty_row)
    return table


def render_jupyter_forwarding(jupyter) -> None:
    console.print(
        f"Forwarded remote port [cyan]{jupyter.port}[/cyan] to local port [cyan]{jupyter.local_port}[/cyan]."
    )
    console.print(f"Open: [green]{jupyter.local_url}[/green]")


def render_jobs_table(jobs, *, title: str = "Jobs", selected_index: int | None = None):
    table = build_jobs_table(jobs, title=title, selected_index=selected_index)
    console.print(table)
    return jobs


def render_logs(logs: JobLogs) -> None:
    console.rule("stdout")
    console.print(logs.stdout or "<empty>")
    console.rule("stderr")
    console.print(logs.stderr or "<empty>")


def build_shortcut_selector(title: str, actions, selected_index: int):
    text = Text(f"{title}: ")
    for index, (key, label) in enumerate(actions):
        if index > 0:
            text.append("  ")
        if index == selected_index:
            text.append(f" {label} ", style="bold black on cyan")
        else:
            text.append(f" {label} ", style="white on rgb(60,60,60)")
        text.append(f" [{key}]", style="dim")
    return text


def build_action_selector(selected_index: int):
    text = build_shortcut_selector("Actions", MENU_ACTIONS, selected_index)
    help_text = Text("Use left/right arrows, or press the shortcut key, then Enter.", style="dim")
    return Group(text, help_text)


def build_main_menu_view(jobs, selected_index: int):
    return Group(
        build_jobs_table(jobs),
        build_action_selector(selected_index),
    )


def build_multi_cluster_action_selector(selected_index: int):
    text = build_shortcut_selector("Actions", MULTI_CLUSTER_MENU_ACTIONS, selected_index)
    help_text = Text(
        "Use up/down to move between clusters, left/right to choose an action, then Enter.",
        style="dim",
    )
    return Group(text, help_text)


def build_cluster_jobs_table(context: ClusterContext, *, active: bool = False) -> Table:
    title = f"{context.name} Jobs"
    if context.load_error:
        table = Table(title=title)
        table.add_column("Job ID", style="cyan", no_wrap=True)
        table.add_column("Name")
        table.add_column("State", style="bold")
        table.add_column("Partition")
        table.add_column("Port", no_wrap=True)
        table.add_column("Reason / Node")
        table.add_column("Elapsed")
        table.add_column("Origin")
        table.add_row(
            "-",
            "Cluster unavailable",
            Text("ERROR", style="bold red"),
            "-",
            "-",
            context.load_error,
            "-",
            "-",
        )
    else:
        table = build_jobs_table(context.jobs, title=title)
    if active:
        table.border_style = "cyan"
        table.title_style = "bold cyan"
    return table


def build_multi_cluster_dashboard(contexts: list[ClusterContext], *, active_index: int | None = None):
    if not contexts:
        empty = Table(title="Jobs")
        empty.add_column("Job ID", style="cyan", no_wrap=True)
        empty.add_column("Name")
        empty.add_column("State", style="bold")
        empty.add_column("Partition")
        empty.add_column("Port", no_wrap=True)
        empty.add_column("Reason / Node")
        empty.add_column("Elapsed")
        empty.add_column("Origin")
        empty.add_row("-", "No configured clusters", "-", "-", "-", "-", "-", "-")
        return empty
    return Group(
        *[
            build_cluster_jobs_table(context, active=active_index is not None and index == active_index)
            for index, context in enumerate(contexts)
        ]
    )


def build_multi_cluster_jobs_table(
    contexts: list[ClusterContext],
    *,
    title: str = "Jobs",
    active_index: int | None = None,
) -> Table:
    table = Table(title=title)
    table.add_column("Cluster", style="bold", no_wrap=True)
    table.add_column("Job ID", style="cyan", no_wrap=True)
    table.add_column("Name")
    table.add_column("State", style="bold")
    table.add_column("Partition")
    table.add_column("Port", no_wrap=True)
    table.add_column("Reason / Node")
    table.add_column("Elapsed")
    table.add_column("Origin")

    row_count = 0
    for index, context in enumerate(contexts):
        row_style = "bold black on cyan" if active_index is not None and index == active_index else None
        header_style = "bold cyan" if index == active_index else "bold white"
        table.add_row(
            Text(context.name, style=header_style),
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            style=row_style,
        )
        row_count += 1
        if context.load_error:
            table.add_row(
                "",
                "-",
                "Cluster unavailable",
                Text("ERROR", style="bold red"),
                "-",
                "-",
                context.load_error,
                "-",
                "-",
                style=row_style,
            )
            row_count += 1
            continue
        if not context.jobs:
            table.add_row(
                "",
                "-",
                "No active jobs",
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
                style=row_style,
            )
            row_count += 1
            continue
        for job in context.jobs:
            name_cell = Text(job.name)
            state_cell = Text(job.state)
            origin_cell = Text(job.origin)
            if job.connected:
                name_cell = Text(f"● {job.name}", style="bold green")
                state_cell = Text(job.state, style="bold green")
                origin_cell = Text(f"{job.origin}, connected", style="bold green")
            table.add_row(
                "",
                job.job_id,
                name_cell,
                state_cell,
                job.partition,
                str(job.remote_port) if job.remote_port is not None else "-",
                job.reason_or_node,
                job.elapsed,
                origin_cell,
                style=row_style,
            )
            row_count += 1

    if row_count == 0:
        table.add_row("-", "-", "No configured clusters", "-", "-", "-", "-", "-", "-")
    return table


def build_multi_cluster_main_view(contexts: list[ClusterContext], active_index: int, action_index: int):
    return Group(
        build_multi_cluster_dashboard(contexts, active_index=active_index),
        build_multi_cluster_action_selector(action_index),
    )


def build_job_selector_view(jobs, selected_index: int, *, action_label: str):
    help_text = Text("Use up/down arrows or j/k, Enter to select, Esc to go back.", style="dim")
    return Group(
        build_jobs_table(jobs, title=f"Select Job To {action_label}", selected_index=selected_index),
        help_text,
    )


def build_remote_action_selector(selected_index: int):
    text = build_shortcut_selector("Remote", REMOTE_ACTIONS, selected_index)
    help_text = Text("Use left/right arrows, Enter to run, Esc or Back to return.", style="dim")
    return Group(text, help_text)


def build_remote_menu_view(service: RemoteService, jobs, selected_index: int):
    session = service.read_remote_session()
    if session is None:
        status = Text("Remote session: none", style="yellow")
    else:
        status = Text(
            f"Remote session: job {session.job_id} on {session.node}:{session.port} ({session.session_type})",
            style="green",
        )
    running_jobs = [job for job in jobs if job.state == "RUNNING"]
    title = f"{cluster_name(service)} Remote Jobs"
    if running_jobs:
        return Group(
            build_jobs_table(running_jobs, title=title),
            status,
            build_remote_action_selector(selected_index),
        )
    empty = Table(title=title)
    empty.add_column("Status")
    empty.add_row("No RUNNING jobs available for remote attach.")
    return Group(empty, status, build_remote_action_selector(selected_index))


def _read_raw_key(*, timeout: float | None = None) -> str | None:
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        if timeout is not None:
            ready, _, _ = select.select([fd], [], [], timeout)
            if not ready:
                return None
        # Use os.read instead of sys.stdin.read to bypass Python's internal
        # buffering.  sys.stdin.read(1) may pull all available bytes from the
        # kernel fd into its buffer, leaving select() unable to see the
        # remaining bytes of an escape sequence (it checks the kernel fd).
        first = os.read(fd, 1).decode()
        if first == "\x03":
            raise KeyboardInterrupt
        if first == "\x1b":
            ready, _, _ = select.select([fd], [], [], 0.1)
            if not ready:
                return ESC_KEY

            second = os.read(fd, 1).decode()
            if second not in {"[", "O"}:
                return f"{first}{second}"

            sequence = [first, second]
            ready, _, _ = select.select([fd], [], [], 0.1)
            while ready and len(sequence) < 8:
                sequence.append(os.read(fd, 1).decode())
                # Arrow keys and similar terminal controls often arrive as ESC [ X or ESC O X.
                # Once we have the final alphabetic byte, stop instead of leaking it into the next read.
                if sequence[-1].isalpha() or sequence[-1] == "~":
                    break
                ready, _, _ = select.select([fd], [], [], 0.1)
            return "".join(sequence)
        return first
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


AUTO_REFRESH_SECONDS = 10.0
ESC_KEY = "\x1b"


def choose_action(jobs, *, refresh_callback=None) -> str:
    if not sys.stdin.isatty():
        render_jobs_table(jobs)
        return Prompt.ask("Choose action", choices=[key for key, _label in MENU_ACTIONS], default="r").lower()

    selected_index = 0
    with Live(build_main_menu_view(jobs, selected_index), console=console, auto_refresh=False) as live:
        while True:
            key = _read_raw_key(timeout=AUTO_REFRESH_SECONDS)
            if key is None:
                if refresh_callback:
                    try:
                        jobs = refresh_callback()
                    except PersistentSessionLostError:
                        return DISCONNECT_SENTINEL
                    live.update(build_main_menu_view(jobs, selected_index), refresh=True)
                continue
            if key == "\x1b[C":
                selected_index = (selected_index + 1) % len(MENU_ACTIONS)
                live.update(build_main_menu_view(jobs, selected_index), refresh=True)
            elif key == "\x1b[D":
                selected_index = (selected_index - 1) % len(MENU_ACTIONS)
                live.update(build_main_menu_view(jobs, selected_index), refresh=True)
            elif key in {"\r", "\n"}:
                return MENU_ACTIONS[selected_index][0]
            else:
                lowered = key.lower()
                for index, (shortcut, _label) in enumerate(MENU_ACTIONS):
                    if lowered == shortcut:
                        return shortcut
                if lowered == "h":
                    selected_index = (selected_index - 1) % len(MENU_ACTIONS)
                    live.update(build_main_menu_view(jobs, selected_index), refresh=True)


def choose_multi_cluster_action(
    contexts: list[ClusterContext],
    *,
    active_index: int,
    refresh_callback=None,
) -> tuple[str, int]:
    if not sys.stdin.isatty():
        console.print(build_multi_cluster_main_view(contexts, active_index, 0))
        choices = [key for key, _label in MULTI_CLUSTER_MENU_ACTIONS]
        action = Prompt.ask("Choose action", choices=choices, default="r").lower()
        return action, active_index

    action_index = 0
    with Live(build_multi_cluster_main_view(contexts, active_index, action_index), console=console, auto_refresh=False) as live:
        while True:
            key = _read_raw_key(timeout=AUTO_REFRESH_SECONDS)
            if key is None:
                if refresh_callback:
                    refresh_callback()
                    live.update(build_multi_cluster_main_view(contexts, active_index, action_index), refresh=True)
                continue
            if key == "\x1b[B" or key.lower() == "j":
                active_index = (active_index + 1) % len(contexts)
                live.update(build_multi_cluster_main_view(contexts, active_index, action_index), refresh=True)
            elif key == "\x1b[A" or key.lower() == "k":
                active_index = (active_index - 1) % len(contexts)
                live.update(build_multi_cluster_main_view(contexts, active_index, action_index), refresh=True)
            elif key == "\x1b[C" or key.lower() == "l":
                action_index = (action_index + 1) % len(MULTI_CLUSTER_MENU_ACTIONS)
                live.update(build_multi_cluster_main_view(contexts, active_index, action_index), refresh=True)
            elif key == "\x1b[D" or key.lower() == "h":
                action_index = (action_index - 1) % len(MULTI_CLUSTER_MENU_ACTIONS)
                live.update(build_multi_cluster_main_view(contexts, active_index, action_index), refresh=True)
            elif key in {"\r", "\n"}:
                return MULTI_CLUSTER_MENU_ACTIONS[action_index][0], active_index
            else:
                lowered = key.lower()
                for shortcut, _label in MULTI_CLUSTER_MENU_ACTIONS:
                    if lowered == shortcut:
                        return shortcut, active_index


def choose_remote_action(service: RemoteService, jobs, *, refresh_callback=None) -> str | None:
    if not sys.stdin.isatty():
        console.print(build_remote_menu_view(service, jobs, 0))
        choices = [key for key, _label in REMOTE_ACTIONS]
        return Prompt.ask("Choose remote action", choices=choices, default="a").lower()

    selected_index = 0
    with Live(build_remote_menu_view(service, jobs, selected_index), console=console, auto_refresh=False) as live:
        while True:
            key = _read_raw_key(timeout=AUTO_REFRESH_SECONDS)
            if key is None:
                if refresh_callback:
                    try:
                        jobs = refresh_callback()
                    except PersistentSessionLostError:
                        return DISCONNECT_SENTINEL
                    live.update(build_remote_menu_view(service, jobs, selected_index), refresh=True)
                continue
            if key == ESC_KEY:
                return "b"
            if key == "\x1b[C":
                selected_index = (selected_index + 1) % len(REMOTE_ACTIONS)
                live.update(build_remote_menu_view(service, jobs, selected_index), refresh=True)
            elif key == "\x1b[D":
                selected_index = (selected_index - 1) % len(REMOTE_ACTIONS)
                live.update(build_remote_menu_view(service, jobs, selected_index), refresh=True)
            elif key in {"\r", "\n"}:
                return REMOTE_ACTIONS[selected_index][0]
            else:
                lowered = key.lower()
                for shortcut, _label in REMOTE_ACTIONS:
                    if lowered == shortcut:
                        return shortcut


def choose_disconnect_action(*, allow_servers: bool) -> str:
    choices = ["r", "q"]
    prompt = "Connection lost. Retry or quit"
    if allow_servers:
        choices.insert(1, "s")
        prompt = "Connection lost. Retry, open servers, or quit"
    return Prompt.ask(prompt, choices=choices, default="r").lower()


def choose_remote_disconnect_action() -> str:
    return Prompt.ask(
        "Connection lost. Retry, go back, or quit",
        choices=["r", "b", "q"],
        default="r",
    ).lower()


def pause_for_continue() -> None:
    console.input("Press Enter to continue: ")


def choose_job(jobs, *, action_label: str):
    if not jobs:
        raise SherlockError(f"No jobs available for {action_label.lower()}.")
    if not sys.stdin.isatty():
        render_jobs_table(jobs, title=f"Select Job To {action_label}")
        choices = [job.job_id for job in jobs]
        job_id = Prompt.ask("Job id", choices=choices, default=choices[0])
        return next(job for job in jobs if job.job_id == job_id)

    selected_index = 0
    with Live(
        build_job_selector_view(jobs, selected_index, action_label=action_label),
        console=console,
        auto_refresh=False,
    ) as live:
        while True:
            key = _read_raw_key()
            if key == "\x1b[B" or key.lower() == "j":
                selected_index = (selected_index + 1) % len(jobs)
                live.update(build_job_selector_view(jobs, selected_index, action_label=action_label), refresh=True)
            elif key == "\x1b[A" or key.lower() == "k":
                selected_index = (selected_index - 1) % len(jobs)
                live.update(build_job_selector_view(jobs, selected_index, action_label=action_label), refresh=True)
            elif key == ESC_KEY:
                return None
            elif key in {"\r", "\n"}:
                return jobs[selected_index]


def build_preset_table(presets: list[Preset], *, selected_index: int | None = None) -> Table:
    table = Table(title="Select Preset")
    if selected_index is not None:
        table.add_column("", no_wrap=True, width=2)
    table.add_column("Preset", style="cyan")
    table.add_column("Partition")
    table.add_column("Resources")
    table.add_column("Default Port")
    for idx, preset in enumerate(presets):
        resources = f"{preset.cpus} CPU"
        if preset.gpus:
            resources += f", {preset.gpus} GPU"
        row = [f"{preset.id} ({preset.label})", preset.partition, resources, str(preset.port)]
        style = None
        if selected_index is not None:
            row.insert(0, ">" if idx == selected_index else "")
            style = "bold black on cyan" if idx == selected_index else None
        table.add_row(*row, style=style)
    return table


def build_preset_selector_view(presets: list[Preset], selected_index: int):
    help_text = Text("Use up/down arrows or j/k, Enter to select, Esc to go back.", style="dim")
    return Group(build_preset_table(presets, selected_index=selected_index), help_text)


def select_preset(service: SherlockService) -> Preset | None:
    presets = list(service.config.presets.values())
    if not presets:
        raise SherlockError("No presets configured.")

    if not sys.stdin.isatty():
        console.print(build_preset_table(presets))
        choice = IntPrompt.ask("Select preset (number)", default=1)
        if choice < 1 or choice > len(presets):
            raise SherlockError("Invalid preset selection.")
        return presets[choice - 1]

    selected_index = 0
    with Live(
        build_preset_selector_view(presets, selected_index),
        console=console,
        auto_refresh=False,
    ) as live:
        while True:
            key = _read_raw_key()
            if key == "\x1b[B" or key.lower() == "j":
                selected_index = (selected_index + 1) % len(presets)
                live.update(build_preset_selector_view(presets, selected_index), refresh=True)
            elif key == "\x1b[A" or key.lower() == "k":
                selected_index = (selected_index - 1) % len(presets)
                live.update(build_preset_selector_view(presets, selected_index), refresh=True)
            elif key == ESC_KEY:
                return None
            elif key in {"\r", "\n"}:
                return presets[selected_index]


def build_remote_profile_table(profiles: list[RemoteProfile], *, selected_index: int | None = None) -> Table:
    table = Table(title="Select Remote Profile")
    if selected_index is not None:
        table.add_column("", no_wrap=True, width=2)
    table.add_column("Profile", style="cyan")
    table.add_column("Partition")
    table.add_column("Resources")
    table.add_column("Time")
    for idx, profile in enumerate(profiles):
        resources = f"{profile.cpus} CPU"
        if profile.gpus:
            resources += f", {profile.gpus} GPU"
        label = profile.label or profile.job_name
        row = [f"{profile.id} ({label})", profile.partition, resources, profile.time]
        style = None
        if selected_index is not None:
            row.insert(0, ">" if idx == selected_index else "")
            style = "bold black on cyan" if idx == selected_index else None
        table.add_row(*row, style=style)
    return table


def build_remote_profile_selector_view(profiles: list[RemoteProfile], selected_index: int):
    help_text = Text("Use up/down arrows or j/k, Enter to select, Esc to go back.", style="dim")
    return Group(build_remote_profile_table(profiles, selected_index=selected_index), help_text)


def select_remote_profile(service: RemoteService) -> RemoteProfile | None:
    profiles = list(service.config.remote_profiles.values())
    if not profiles:
        raise SherlockError(f"No remote profiles configured for {cluster_name(service)}.")

    if not sys.stdin.isatty():
        console.print(build_remote_profile_table(profiles))
        choice = IntPrompt.ask("Select remote profile (number)", default=1)
        if choice < 1 or choice > len(profiles):
            raise SherlockError("Invalid remote profile selection.")
        return profiles[choice - 1]

    selected_index = 0
    with Live(
        build_remote_profile_selector_view(profiles, selected_index),
        console=console,
        auto_refresh=False,
    ) as live:
        while True:
            key = _read_raw_key()
            if key == "\x1b[B" or key.lower() == "j":
                selected_index = (selected_index + 1) % len(profiles)
                live.update(build_remote_profile_selector_view(profiles, selected_index), refresh=True)
            elif key == "\x1b[A" or key.lower() == "k":
                selected_index = (selected_index - 1) % len(profiles)
                live.update(build_remote_profile_selector_view(profiles, selected_index), refresh=True)
            elif key == ESC_KEY:
                return None
            elif key in {"\r", "\n"}:
                return profiles[selected_index]


def build_submission_request(service: SherlockService, args) -> SubmissionRequest | None:
    preset = service.config.presets.get(args.preset) if getattr(args, "preset", None) else None
    if preset is None:
        preset = select_preset(service)
        if preset is None:
            return None

    job_name = getattr(args, "job_name", None) or Prompt.ask("Job name", default=preset.job_name)
    notebook_dir = getattr(args, "notebook_dir", None) or Prompt.ask(
        "Notebook dir",
        default=preset.notebook_dir or service.config.connection.default_notebook_dir,
    )

    request = SubmissionRequest(
        preset=preset,
        job_name=job_name,
        notebook_dir=notebook_dir,
        port=getattr(args, "port", None) or preset.port,
        partition=getattr(args, "partition", None) or preset.partition,
        mem=getattr(args, "mem", None) or preset.mem,
        time=getattr(args, "time", None) or preset.time,
        cpus=getattr(args, "cpus", None) or preset.cpus,
        account=getattr(args, "account", None) or preset.account,
        gpus=getattr(args, "gpus", None) if getattr(args, "gpus", None) is not None else preset.gpus,
        nodelist=getattr(args, "nodelist", None) or preset.nodelist,
        constraint=getattr(args, "constraint", None) or preset.constraint,
        gres_flags=preset.gres_flags,
        gpu_cmode=preset.gpu_cmode,
        isolated_compute_node=preset.isolated_compute_node,
    )

    if not getattr(args, "preset", None) and Confirm.ask("Edit advanced resource settings?", default=False):
        request = replace(
            request,
            port=IntPrompt.ask("Port", default=request.port),
            partition=Prompt.ask("Partition", default=request.partition),
            account=Prompt.ask("Account", default=request.account or ""),
            mem=Prompt.ask("Memory", default=request.mem),
            time=Prompt.ask("Time", default=request.time),
            cpus=IntPrompt.ask("CPUs", default=request.cpus),
            gpus=IntPrompt.ask("GPUs", default=request.gpus),
            nodelist=Prompt.ask("Nodelist", default=request.nodelist or ""),
            constraint=Prompt.ask("Constraint", default=request.constraint or ""),
        )
        request = replace(
            request,
            account=request.account or None,
            nodelist=request.nodelist or None,
            constraint=request.constraint or None,
        )
    return request


def run_new(service: SherlockService, args) -> int:
    request = build_submission_request(service, args)
    if request is None:
        return 0
    job_id = service.submit_job(request)
    console.print(f"Submitted [cyan]{job_id}[/cyan] as [bold]{request.job_name}[/bold].")
    status = service.watch_job(job_id, connect_on_run=False, console=console)
    if status:
        console.print(f"Final watch state: [bold]{status.state}[/bold]")
    if status and status.state == "RUNNING":
        jupyter = service.connect_job(job_id, open_browser=not args.no_browser)
        render_jupyter_forwarding(jupyter)
    return 0


def prompt_local_connect_port(job: JobInfo) -> int | None:
    if job.remote_port is not None:
        return IntPrompt.ask("Local port", default=job.remote_port)
    while True:
        value = Prompt.ask("Local port (blank = remote port)", default="").strip()
        if not value:
            return None
        try:
            port = int(value)
        except ValueError:
            console.print("[red]Port must be an integer.[/red]")
            continue
        if 1 <= port <= 65535:
            return port
        console.print("[red]Port must be between 1 and 65535.[/red]")


def choose_remote_job(service: RemoteService, target: str | None):
    if target:
        job = service.resolve_job(target)
        if job.state != "RUNNING":
            raise SherlockError(f"Job {job.job_id} is not RUNNING. Current state: {job.state}")
        return job.job_id
    jobs = service.list_running_jobs()
    if not jobs:
        raise SherlockError("No RUNNING jobs available for remote attach.")
    chosen = choose_job(jobs, action_label="Attach Remote")
    if chosen is None:
        raise SherlockError("No job selected.")
    return chosen.job_id


def render_remote_session(service: RemoteService, session) -> None:
    console.print(
        f"Remote session ready on [cyan]{session.node}:{session.port}[/cyan] "
        f"(job [cyan]{session.job_id}[/cyan], type [bold]{session.session_type}[/bold])."
    )
    console.print(f"Cursor: [green]cursor --remote ssh-remote+{service.compute_host_alias} {service.remote_home}[/green]")
    console.print(f"SSH: [green]ssh {service.compute_host_alias}[/green]")


def run_remote(service: RemoteService, args) -> int:
    if args.remote_command == "setup-local":
        key_path = service.setup_local()
        console.print(f"Local SSH config ready. Using key [green]{key_path}[/green].")
        return 0
    if args.remote_command == "setup":
        if args.full:
            service.setup_full()
            console.print(f"Local and remote setup complete for [bold]{cluster_name(service)}[/bold].")
        else:
            service.setup_remote()
            console.print(f"Remote setup complete for [bold]{cluster_name(service)}[/bold].")
        return 0
    if args.remote_command == "attach":
        session = service.attach(choose_remote_job(service, args.job))
        render_remote_session(service, session)
        return 0
    if args.remote_command == "start":
        session = service.start(args.profile)
        render_remote_session(service, session)
        return 0
    if args.remote_command == "connect":
        session = service.connect_remote()
        render_remote_session(service, session)
        return 0
    if args.remote_command == "list":
        jobs = service.list_jobs()
        render_jobs_table(jobs, title=f"{cluster_name(service)} Jobs")
        session = service.read_remote_session()
        if session is None:
            console.print("[yellow]No remote session found.[/yellow]")
            return 0
        status = service.get_job_status(session.job_id)
        state = status.state if status else "gone"
        console.print(
            f"Remote session: job [cyan]{session.job_id}[/cyan] on [cyan]{session.node}:{session.port}[/cyan] "
            f"([bold]{session.session_type}[/bold], state [bold]{state}[/bold])."
        )
        return 0
    if args.remote_command == "stop":
        session = service.stop()
        console.print(
            f"Stopped remote session for job [cyan]{session.job_id}[/cyan] "
            f"([bold]{session.session_type}[/bold])."
        )
        return 0
    if args.remote_command == "clean":
        service.clean()
        console.print(f"Removed local SSH config for [bold]{service.compute_host_alias}[/bold].")
        return 0
    raise SherlockError(f"Unknown remote command: {args.remote_command}")


def interactive_remote_menu(service: RemoteService) -> int | None:
    while True:
        try:
            jobs = service.list_jobs()
            action = choose_remote_action(service, jobs, refresh_callback=service.list_jobs)
            if action == DISCONNECT_SENTINEL:
                raise PersistentSessionLostError("Persistent SSH session disconnected.")
            if action in {None, "b"}:
                return None
            if action == "a":
                target = choose_job([job for job in jobs if job.state == "RUNNING"], action_label="Attach Remote")
                if target is None:
                    continue
                session = service.attach(target.job_id)
                render_remote_session(service, session)
            elif action == "t":
                profile = select_remote_profile(service)
                if profile is None:
                    continue
                session = service.start(profile.id)
                render_remote_session(service, session)
            elif action == "c":
                session = service.connect_remote()
                render_remote_session(service, session)
            elif action == "s":
                session = service.stop()
                console.print(
                    f"Stopped remote session for job [cyan]{session.job_id}[/cyan] "
                    f"([bold]{session.session_type}[/bold])."
                )
            elif action == "u":
                service.setup_full()
                console.print(f"Local and remote setup complete for [bold]{cluster_name(service)}[/bold].")
            elif action == "n":
                service.clean()
                console.print(f"Removed local SSH config for [bold]{service.compute_host_alias}[/bold].")
            pause_for_continue()
        except PersistentSessionLostError as exc:
            console.print(f"[red]{exc}[/red]")
            service.close()
            follow_up = choose_remote_disconnect_action()
            if follow_up == "q":
                return 0
            if follow_up == "b":
                return None


LOGO = """\
[bold cyan]\
   ___  _         _           _
  / _ \\(_)_   _  | |    __ _| |__
 | | | | | | | | | |   / _` | '_ \\
 | |_| | | |_| | | |__| (_| | |_) |
  \\__\\_\\_|\\__,_| |_____\\__,_|_.__/
  ____
 / ___|  ___ _ ____   _____ _ __
 \\___ \\ / _ \\ '__\\ \\ / / _ \\ '__|
  ___) |  __/ |   \\ V /  __/ |
 |____/ \\___|_|    \\_/ \\___|_|[/bold cyan]
"""

SERVERS_SENTINEL = "__servers__"


def build_cluster_selector(clusters: list[tuple[str, object]], selected_index: int):
    text = Text("Select cluster: ")
    for index, (name, _path) in enumerate(clusters):
        if index > 0:
            text.append("  ")
        if index == selected_index:
            text.append(f" {name} ", style="bold black on cyan")
        else:
            text.append(f" {name} ", style="white on rgb(60,60,60)")
    help_text = Text("Use left/right arrows, then Enter.", style="dim")
    return Group(text, help_text)


def build_cluster_selection_view(clusters: list[tuple[str, object]], selected_index: int):
    return Group(
        Text.from_markup(LOGO),
        build_cluster_selector(clusters, selected_index),
    )


def choose_cluster(clusters: list[tuple[str, object]]) -> tuple[str, object] | None:
    if not sys.stdin.isatty():
        for idx, (name, _path) in enumerate(clusters, 1):
            console.print(f"  {idx}. {name}")
        choice = IntPrompt.ask("Select cluster", default=1)
        if choice < 1 or choice > len(clusters):
            raise SherlockError("Invalid cluster selection.")
        return clusters[choice - 1]

    selected_index = 0
    with Live(
        build_cluster_selection_view(clusters, selected_index),
        console=console,
        auto_refresh=False,
    ) as live:
        while True:
            key = _read_raw_key()
            if key == "\x1b[C":
                selected_index = (selected_index + 1) % len(clusters)
                live.update(build_cluster_selection_view(clusters, selected_index), refresh=True)
            elif key == "\x1b[D":
                selected_index = (selected_index - 1) % len(clusters)
                live.update(build_cluster_selection_view(clusters, selected_index), refresh=True)
            elif key in {"\r", "\n"}:
                return clusters[selected_index]
            elif key.lower() == "q":
                return None


def show_splash_and_load(service: SherlockService) -> list:
    import threading

    # Truncate old log so we only see fresh output for this connection attempt.
    log_path = service.ssh_log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        log_path.write_text("")
    except OSError:
        pass

    # Phase 1: establish SSH session WITHOUT Live so that password / 2FA
    # prompts (written to /dev/tty by SSH) are visible and interactive.
    console.print(Text.from_markup(LOGO))
    console.print(f"[dim]Connecting to {cluster_name(service)}...[/dim]")
    console.print(f"[dim italic]Log: {log_path}[/dim italic]")
    service.ensure_connected()

    # Phase 2: SSH is up — fetch jobs with a spinner.
    result = []
    error = []

    def fetch():
        try:
            result.extend(service.list_jobs())
        except Exception as exc:
            error.append(exc)

    thread = threading.Thread(target=fetch, daemon=True)
    thread.start()

    spinner = Spinner("dots", text=f"[dim]Loading jobs from {cluster_name(service)}...[/dim]")
    with Live(spinner, console=console, auto_refresh=True, refresh_per_second=10):
        thread.join()

    if error:
        raise error[0]
    return result


def show_multi_cluster_splash_and_load(contexts: list[ClusterContext]) -> None:
    console.print(Text.from_markup(LOGO))
    for context in contexts:
        log_path = context.service.ssh_log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            log_path.write_text("")
        except OSError:
            pass
        console.print(f"[dim]Connecting to {context.name}...[/dim]")
        console.print(f"[dim italic]Log: {log_path}[/dim italic]")
        refresh_cluster_context(context, ensure_connected=True)


def render_cluster_sections(
    contexts: list[ClusterContext],
    *,
    include_remote_session: bool = False,
    logs: bool = False,
) -> None:
    console.print(build_multi_cluster_jobs_table(contexts))
    if include_remote_session:
        for context in contexts:
            session = context.service.state.get_remote_session()
            if session is None:
                console.print(f"{context.name}: [yellow]Remote session: none[/yellow]")
            else:
                status = next((job.state for job in context.jobs if job.job_id == session.job_id), "gone")
                console.print(
                    f"{context.name}: Remote session: job [cyan]{session.job_id}[/cyan] "
                    f"on [cyan]{session.node}:{session.port}[/cyan] "
                    f"([bold]{session.session_type}[/bold], state [bold]{status}[/bold])"
                )
    if logs:
        for context in contexts:
            if context.load_error:
                continue
            for job in context.jobs:
                detail = context.service.get_job_detail(job.job_id)
                console.print(
                    f"{context.name} {job.job_id} stdout={detail.stdout_path or '-'} stderr={detail.stderr_path or '-'}"
                )


def require_single_cluster_scope(args, verb: str) -> None:
    if args.config:
        return
    raise SherlockError(f"`{verb}` requires --config when multiple clusters are enabled.")


def run_multi_cluster_remote(contexts: list[ClusterContext], args) -> int:
    if args.remote_command == "list":
        refresh_all_cluster_contexts(contexts)
        render_cluster_sections(contexts, include_remote_session=True)
        return 0
    if args.remote_command == "attach":
        if args.job:
            context, job = resolve_cluster_job_target(contexts, args.job, running_only=True)
        else:
            refresh_all_cluster_contexts(contexts)
            running_contexts = [
                ClusterContext(
                    name=context.name,
                    config_path=context.config_path,
                    service=context.service,
                    jobs=[job for job in context.jobs if job.state == "RUNNING"],
                    load_error=context.load_error,
                )
                for context in contexts
            ]
            chosen = choose_cluster_job(running_contexts, action_label="Attach Remote")
            if chosen is None:
                return 0
            context, job = chosen
        session = context.service.attach(job.job_id)
        render_remote_session(context.service, session)
        return 0
    if args.remote_command == "connect":
        context = resolve_remote_session_context(contexts)
        session = context.service.connect_remote()
        render_remote_session(context.service, session)
        return 0
    if args.remote_command == "stop":
        context = resolve_remote_session_context(contexts)
        session = context.service.stop()
        console.print(
            f"Stopped remote session for job [cyan]{session.job_id}[/cyan] "
            f"([bold]{session.session_type}[/bold])."
        )
        return 0
    if args.remote_command == "clean":
        context = resolve_remote_session_context(contexts)
        context.service.clean()
        console.print(f"Removed local SSH config for [bold]{context.service.compute_host_alias}[/bold].")
        return 0
    require_single_cluster_scope(args, f"remote {args.remote_command}")
    raise SherlockError(f"Unknown remote command: {args.remote_command}")


def run_multi_cluster_command(args) -> int:
    contexts = build_cluster_contexts()
    if not contexts:
        raise SherlockError("No *_presets.toml config files found.")
    try:
        if args.command == "list":
            refresh_all_cluster_contexts(contexts)
            render_cluster_sections(contexts, logs=args.logs)
            return 0
        if args.command == "connect":
            context, job = resolve_cluster_job_target(contexts, args.job, running_only=True)
            info = context.service.connect_job(
                job.job_id,
                open_browser=not args.no_browser,
                local_port=args.local_port,
            )
            render_jupyter_forwarding(info)
            return 0
        if args.command == "watch":
            context, job = resolve_cluster_job_target(contexts, args.job)
            status = context.service.watch_job(job.job_id, connect_on_run=args.connect_on_run, console=console)
            console.print(f"{status.job_id} {status.name} {status.state}")
            return 0
        if args.command == "kill":
            context, job = resolve_cluster_job_target(contexts, args.job)
            job_id = context.service.kill_job(job.job_id)
            console.print(f"Killed [cyan]{job_id}[/cyan]")
            return 0
        if args.command == "remote":
            return run_multi_cluster_remote(contexts, args)
        require_single_cluster_scope(args, args.command)
        raise SherlockError(f"Unknown command: {args.command}")
    finally:
        close_cluster_contexts(contexts)


def interactive_menu(service: RemoteService) -> int | str:
    interrupt_tracker = InterruptTracker()
    first_load = True
    while True:
        try:
            if first_load:
                jobs = show_splash_and_load(service)
                first_load = False
            else:
                jobs = service.list_jobs()
            action = choose_action(jobs, refresh_callback=service.list_jobs)
            if action == DISCONNECT_SENTINEL:
                raise PersistentSessionLostError("Persistent SSH session disconnected.")
            should_pause = action not in {"r", "refresh", "q", "quit"}
            if action in {"q", "quit"}:
                return 0
            if action == SERVERS_SENTINEL:
                return SERVERS_SENTINEL
            if action in {"r", "refresh"}:
                continue
            if action in {"n", "new"}:
                args = argparse.Namespace(
                    preset=None,
                    job_name=None,
                    notebook_dir=None,
                    port=None,
                    partition=None,
                    account=None,
                    mem=None,
                    time=None,
                    cpus=None,
                    gpus=None,
                    nodelist=None,
                    constraint=None,
                    no_browser=False,
                )
                request = build_submission_request(service, args)
                if request is None:
                    should_pause = False
                    continue
                job_id = service.submit_job(request)
                console.print(f"Submitted [cyan]{job_id}[/cyan] as [bold]{request.job_name}[/bold].")
                should_pause = False
            elif action in {"c", "connect"}:
                running_jobs = [job for job in jobs if job.state == "RUNNING"]
                target = choose_job(running_jobs, action_label="Connect")
                if target is None:
                    should_pause = False
                    continue
                local_port = prompt_local_connect_port(target)
                info = service.connect_job(target.job_id, open_browser=True, local_port=local_port)
                render_jupyter_forwarding(info)
            elif action in {"w", "watch"}:
                target = choose_job(jobs, action_label="Watch")
                if target is None:
                    should_pause = False
                    continue
                status = service.watch_job(target.job_id, console=console)
                console.print(f"Watch finished with [bold]{status.state}[/bold]")
            elif action in {"k", "kill"}:
                target = choose_job(jobs, action_label="Kill")
                if target is None:
                    should_pause = False
                    continue
                job_id = service.kill_job(target.job_id)
                console.print(f"Killed [cyan]{job_id}[/cyan]")
            elif action in {"l", "logs"}:
                target = choose_job(jobs, action_label="View Logs")
                if target is None:
                    should_pause = False
                    continue
                render_logs(service.get_job_logs(target.job_id))
            elif action in {"m", "remote"}:
                result = interactive_remote_menu(service)
                if result == 0:
                    return 0
                should_pause = False
            elif action in {"s", "servers", "clusters", "add"}:
                return SERVERS_SENTINEL
            else:
                console.print("[red]Unknown action.[/red]")
        except PersistentSessionLostError as exc:
            console.print(f"[red]{exc}[/red]")
            service.close()
            follow_up = choose_disconnect_action(allow_servers=True)
            if follow_up == "q":
                return 0
            if follow_up == "s":
                return SERVERS_SENTINEL
            first_load = True
            continue
        except SherlockError as exc:
            console.print(f"[red]{exc}[/red]")
            should_pause = True
        except KeyboardInterrupt:
            console.print()
            if should_exit_on_interrupt(interrupt_tracker):
                console.print("[yellow]Exiting on second Ctrl+C.[/yellow]")
                return 130
            console.print("[yellow]Press Ctrl+C again within 2 seconds to exit.[/yellow]")
            should_pause = False

        if should_pause:
            pause_for_continue()


def interactive_multi_cluster_menu(contexts: list[ClusterContext], *, active_index: int = 0) -> int:
    if not contexts:
        raise SherlockError("No cluster configs available.")
    interrupt_tracker = InterruptTracker()
    active_index = max(0, min(active_index, len(contexts) - 1))
    first_load = True
    while True:
        should_pause = False
        try:
            if first_load:
                show_multi_cluster_splash_and_load(contexts)
                first_load = False
            action, active_index = choose_multi_cluster_action(
                contexts,
                active_index=active_index,
                refresh_callback=lambda: refresh_all_cluster_contexts(contexts),
            )
            active = contexts[active_index]
            service = active.service
            jobs = active.jobs
            should_pause = action not in {"r", "refresh", "q", "quit"}
            if action in {"q", "quit"}:
                return 0
            if action in {"r", "refresh"}:
                refresh_all_cluster_contexts(contexts)
                continue
            if action in {"n", "new"}:
                args = argparse.Namespace(
                    preset=None,
                    job_name=None,
                    notebook_dir=None,
                    port=None,
                    partition=None,
                    account=None,
                    mem=None,
                    time=None,
                    cpus=None,
                    gpus=None,
                    nodelist=None,
                    constraint=None,
                    no_browser=False,
                )
                request = build_submission_request(service, args)
                if request is None:
                    should_pause = False
                    continue
                job_id = service.submit_job(request)
                console.print(f"Submitted [cyan]{job_id}[/cyan] on [bold]{active.name}[/bold] as [bold]{request.job_name}[/bold].")
                refresh_cluster_context(active)
                should_pause = False
            elif action in {"c", "connect"}:
                running_jobs = [job for job in jobs if job.state == "RUNNING"]
                target = choose_job(running_jobs, action_label=f"Connect ({active.name})")
                if target is None:
                    should_pause = False
                    continue
                local_port = prompt_local_connect_port(target)
                info = service.connect_job(target.job_id, open_browser=True, local_port=local_port)
                render_jupyter_forwarding(info)
                refresh_cluster_context(active)
            elif action in {"w", "watch"}:
                target = choose_job(jobs, action_label=f"Watch ({active.name})")
                if target is None:
                    should_pause = False
                    continue
                status = service.watch_job(target.job_id, console=console)
                console.print(f"Watch finished with [bold]{status.state}[/bold]")
                refresh_cluster_context(active)
            elif action in {"k", "kill"}:
                target = choose_job(jobs, action_label=f"Kill ({active.name})")
                if target is None:
                    should_pause = False
                    continue
                job_id = service.kill_job(target.job_id)
                console.print(f"Killed [cyan]{job_id}[/cyan] on [bold]{active.name}[/bold]")
                refresh_cluster_context(active)
            elif action in {"l", "logs"}:
                target = choose_job(jobs, action_label=f"View Logs ({active.name})")
                if target is None:
                    should_pause = False
                    continue
                render_logs(service.get_job_logs(target.job_id))
            elif action in {"m", "remote"}:
                result = interactive_remote_menu(service)
                if result == 0:
                    return 0
                refresh_cluster_context(active)
                should_pause = False
            else:
                console.print("[red]Unknown action.[/red]")
        except SherlockError as exc:
            console.print(f"[red]{exc}[/red]")
            should_pause = True
        except KeyboardInterrupt:
            console.print()
            if should_exit_on_interrupt(interrupt_tracker):
                console.print("[yellow]Exiting on second Ctrl+C.[/yellow]")
                return 130
            console.print("[yellow]Press Ctrl+C again within 2 seconds to exit.[/yellow]")
            should_pause = False

        if should_pause:
            pause_for_continue()


def interactive_cluster_selection_loop() -> int:
    while True:
        clusters = discover_configs()
        if not clusters:
            console.print("[red]No *_presets.toml config files found.[/red]")
            return 1
        if len(clusters) == 1:
            chosen = clusters[0]
        else:
            chosen = choose_cluster(clusters)
            if chosen is None:
                return 0
        _name, config_path = chosen
        service = make_remote_service(str(config_path))
        service_handed_off = False
        try:
            result = interactive_menu(service)
            if result == SERVERS_SENTINEL:
                contexts = build_cluster_contexts(reuse_services={Path(config_path): service})
                service_handed_off = any(context.service is service for context in contexts)
                active_index = next(
                    (
                        index
                        for index, context in enumerate(contexts)
                        if context.config_path == Path(config_path)
                    ),
                    0,
                )
                try:
                    return interactive_multi_cluster_menu(contexts, active_index=active_index)
                finally:
                    close_cluster_contexts(contexts)
            return result if isinstance(result, int) else 0
        finally:
            if not service_handed_off:
                service.close()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "setup":
        try:
            return run_setup(args)
        except SherlockError as exc:
            console.print(f"[red]{exc}[/red]")
            return 1
        except KeyboardInterrupt:
            console.print()
            console.print("[yellow]Interrupted.[/yellow]")
            return 130

    if args.command is not None:
        if not args.config:
            try:
                return run_multi_cluster_command(args)
            except SherlockError as exc:
                console.print(f"[red]{exc}[/red]")
                return 1
            except KeyboardInterrupt:
                console.print()
                console.print("[yellow]Interrupted.[/yellow]")
                return 130

        service = make_remote_service(args.config) if args.command == "remote" else make_service(args.config)
        try:
            if args.command == "list":
                jobs = service.list_jobs()
                render_jobs_table(jobs, title=f"{cluster_name(service)} Jobs")
                if args.logs:
                    for job in jobs:
                        detail = service.get_job_detail(job.job_id)
                        console.print(
                            f"{job.job_id} stdout={detail.stdout_path or '-'} stderr={detail.stderr_path or '-'}"
                        )
                return 0
            if args.command == "new":
                return run_new(service, args)
            if args.command == "connect":
                info = service.connect_job(
                    args.job,
                    open_browser=not args.no_browser,
                    local_port=args.local_port,
                )
                render_jupyter_forwarding(info)
                return 0
            if args.command == "watch":
                status = service.watch_job(args.job, connect_on_run=args.connect_on_run, console=console)
                console.print(f"{status.job_id} {status.name} {status.state}")
                return 0
            if args.command == "kill":
                job_id = service.kill_job(args.job)
                console.print(f"Killed [cyan]{job_id}[/cyan]")
                return 0
            if args.command == "remote":
                return run_remote(service, args)
            return 0
        except SherlockError as exc:
            console.print(f"[red]{exc}[/red]")
            return 1
        except KeyboardInterrupt:
            console.print()
            console.print("[yellow]Interrupted.[/yellow]")
            return 130
        finally:
            service.close()

    try:
        if args.config:
            service = make_remote_service(args.config)
            try:
                return interactive_menu(service)
            finally:
                service.close()
        return interactive_cluster_selection_loop()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        return 130

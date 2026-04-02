from __future__ import annotations

import argparse
import os
import select
import sys
import termios
import time
import tty
from dataclasses import dataclass
from dataclasses import replace

from rich.console import Console, Group
from rich.live import Live
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.spinner import Spinner
from rich.text import Text
from rich.table import Table

from .config import discover_configs, load_config
from .models import JobLogs, Preset, SubmissionRequest
from .remote import RemoteService
from .service import SherlockError, SherlockService
from .state import StateStore


console = Console()
MENU_ACTIONS = [
    ("r", "Refresh"),
    ("n", "New"),
    ("c", "Connect"),
    ("w", "Watch"),
    ("k", "Kill"),
    ("l", "Logs"),
    ("s", "Switch"),
    ("q", "Quit"),
]
INTERRUPT_EXIT_WINDOW_SECONDS = 2.0


@dataclass
class InterruptTracker:
    last_interrupt_at: float | None = None


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
    state = StateStore(config.state_path)
    return SherlockService(config, state)


def make_remote_service(config_path: str | None = None) -> RemoteService:
    config = load_config(config_path)
    state = StateStore(config.state_path)
    return RemoteService(config, state)


def cluster_name(service: SherlockService) -> str:
    return service.config.connection.name


def build_jobs_table(jobs, *, title: str = "Jobs", selected_index: int | None = None):
    table = Table(title=title)
    if selected_index is not None:
        table.add_column("", no_wrap=True, width=2)
    table.add_column("Job ID", style="cyan", no_wrap=True)
    table.add_column("Name")
    table.add_column("State", style="bold")
    table.add_column("Partition")
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
        empty_row = ["-", "No active jobs", "-", "-", "-", "-", "-"]
        if selected_index is not None:
            empty_row.insert(0, "")
        table.add_row(*empty_row)
    return table


def render_jobs_table(jobs, *, title: str = "Jobs", selected_index: int | None = None):
    table = build_jobs_table(jobs, title=title, selected_index=selected_index)
    console.print(table)
    return jobs


def render_logs(logs: JobLogs) -> None:
    console.rule("stdout")
    console.print(logs.stdout or "<empty>")
    console.rule("stderr")
    console.print(logs.stderr or "<empty>")


def build_action_selector(selected_index: int):
    text = Text("Actions: ")
    for index, (key, label) in enumerate(MENU_ACTIONS):
        if index > 0:
            text.append("  ")
        if index == selected_index:
            text.append(f" {label} ", style="bold black on cyan")
        else:
            text.append(f" {label} ", style="white on rgb(60,60,60)")
        text.append(f" [{key}]", style="dim")
    help_text = Text("Use left/right arrows, or press the shortcut key, then Enter.", style="dim")
    return Group(text, help_text)


def build_main_menu_view(jobs, selected_index: int):
    return Group(
        build_jobs_table(jobs),
        build_action_selector(selected_index),
    )


def build_job_selector_view(jobs, selected_index: int, *, action_label: str):
    help_text = Text("Use up/down arrows or j/k, Enter to select, Esc to go back.", style="dim")
    return Group(
        build_jobs_table(jobs, title=f"Select Job To {action_label}", selected_index=selected_index),
        help_text,
    )


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
                    jobs = refresh_callback()
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
        console.print(f"Forwarded to [green]{jupyter.local_url}[/green]")
    return 0


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

SWITCH_SENTINEL = "__switch__"


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


def interactive_menu(service: SherlockService) -> int | str:
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
            should_pause = action not in {"r", "refresh", "q", "quit"}
            if action in {"q", "quit"}:
                return 0
            if action == SWITCH_SENTINEL:
                return SWITCH_SENTINEL
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
                info = service.connect_job(target.job_id, open_browser=True)
                console.print(f"Forwarded to [green]{info.local_url}[/green]")
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
            elif action in {"s", "switch"}:
                return SWITCH_SENTINEL
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
            Prompt.ask("Press Enter to continue", default="")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Non-interactive subcommands: use a single service (--config or default).
    if args.command is not None:
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
                info = service.connect_job(args.job, open_browser=not args.no_browser)
                console.print(f"Forwarded to [green]{info.local_url}[/green]")
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
        except SherlockError as exc:
            console.print(f"[red]{exc}[/red]")
            return 1
        except KeyboardInterrupt:
            console.print()
            console.print("[yellow]Interrupted.[/yellow]")
            return 130
        finally:
            service.close()
        return 0

    # Interactive mode: cluster selection loop.
    if args.config:
        # --config given: skip cluster selection, go straight to menu.
        service = make_service(args.config)
        try:
            return interactive_menu(service)
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/yellow]")
            return 130
        finally:
            service.close()

    clusters = discover_configs()
    if not clusters:
        console.print("[red]No *_presets.toml config files found.[/red]")
        return 1

    try:
        while True:
            if len(clusters) == 1:
                chosen = clusters[0]
            else:
                chosen = choose_cluster(clusters)
                if chosen is None:
                    return 0
            _name, config_path = chosen
            service = make_service(str(config_path))
            try:
                result = interactive_menu(service)
            finally:
                service.close()
            if result != SWITCH_SENTINEL:
                return result if isinstance(result, int) else 0
            # SWITCH_SENTINEL: loop back to cluster selection
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        return 130

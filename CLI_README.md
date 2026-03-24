# Sherlock CLI

`sherlock-cli` is a Python CLI for submitting and managing Sherlock JupyterLab jobs.

## Install

```bash
python3 -m pip install -e .
```

After installation, the command is:

```bash
sherlock-cli
```

## What It Does

- Shows all of your current Sherlock `PENDING` and `RUNNING` jobs in a table
- Marks jobs as `CLI-managed` or `external`
- Lets you submit a new JupyterLab job from a preset
- Watches a job until it starts or reaches a terminal state
- Connects a running Jupyter job to localhost with SSH port forwarding
- Opens the forwarded Jupyter URL in your browser
- Kills a job and cleans up its local SSH tunnel if the CLI created one
- Reads remote stdout and stderr logs for a selected job

## Interactive Mode

Running `sherlock-cli` with no subcommand opens the interactive menu.

The home screen shows:

- job id
- job name
- state
- partition
- reason or node
- elapsed time
- origin

Available actions:

- `Refresh`
- `New`
- `Connect`
- `Watch`
- `Kill`
- `Logs`
- `Quit`

Menu controls:

- left and right arrow keys move between actions
- Enter confirms the selected action
- shortcut keys also work: `r`, `n`, `c`, `w`, `k`, `l`, `q`
- in interactive mode, press `Ctrl+C` twice within 2 seconds to exit

The menu now fetches Sherlock job status once per screen refresh. Moving left and right does not trigger a new SSH query.
This experimental branch also keeps a single long-lived `ssh ... bash -l` session open for the lifetime of the CLI process, so repeated remote queries and sbatch template uploads avoid reconnecting for each action.

When an action needs a specific job, the CLI opens a second selector:

- up and down arrow keys move between jobs
- `j` and `k` also work
- Enter selects the highlighted job

For `Connect`, the selector only shows jobs that are already `RUNNING`.

## Subcommands

### `sherlock-cli list`

Show all current `PENDING` and `RUNNING` jobs for the configured Sherlock user.

```bash
sherlock-cli list
```

Show log paths too:

```bash
sherlock-cli list --logs
```

### `sherlock-cli new`

Submit a new JupyterLab job from a preset.

```bash
sherlock-cli new
```

Use a specific preset:

```bash
sherlock-cli new --preset xiaojie-gpu
```

Override common resource settings:

```bash
sherlock-cli new \
  --preset general-cpu \
  --job-name my-jupyter \
  --notebook-dir /oak/stanford/groups/xiaojie/yifan \
  --time 08:00:00 \
  --mem 128G \
  --cpus 16
```

Supported flags:

- `--preset`
- `--job-name`
- `--notebook-dir`
- `--port`
- `--partition`
- `--mem`
- `--time`
- `--cpus`
- `--gpus`
- `--nodelist`
- `--constraint`
- `--no-browser`

Behavior:

- uploads the configured `.sbatch` template to Sherlock
- submits the Slurm job with stable stdout and stderr file names
- records the job in the local state file
- watches until the job starts or exits
- when the job reaches `RUNNING`, parses Jupyter logs and starts local port forwarding

### `sherlock-cli connect <job>`

Connect a running Jupyter job to localhost.

```bash
sherlock-cli connect 123456
```

You can pass either:

- a job id
- a unique active job name

The CLI:

- queries `scontrol show job`
- finds the compute node
- reads remote logs
- extracts the Jupyter URL and token if available
- starts SSH port forwarding
- opens the local URL in your browser unless `--no-browser` is used

### `sherlock-cli watch <job>`

Watch a job until it becomes `RUNNING` or reaches a terminal state.

```bash
sherlock-cli watch 123456
```

Option:

- `--connect-on-run`: automatically connect once the job becomes `RUNNING`

### `sherlock-cli kill <job>`

Cancel a job with `scancel`.

```bash
sherlock-cli kill 123456
```

If the CLI created a local SSH tunnel for that job, it also terminates the local tunnel process.

## Presets

The unified preset configuration lives in `sherlock_presets.toml`.

Current presets:

- `xiaojie-cpu`
- `xiaojie-gpu`
- `general-cpu`
- `general-gpu`
- `owner-cpu`
- `owner-gpu`
- `bigmem`
- `stanford-gpu`

Each preset defines:

- Slurm partition
- CPU count
- GPU count
- memory
- time limit
- default port
- optional nodelist
- optional constraint
- sbatch template path

## Configuration

By default the CLI reads:

- repo config: `sherlock_presets.toml`
- local state: `~/.local/state/sherlock-cli/state.json`

You can point to a different config file with:

```bash
sherlock-cli --config /path/to/config.toml list
```

The config controls:

- Sherlock login host
- Sherlock username
- email
- default notebook directory
- remote utility directory
- watch polling interval
- startup timeout
- all preset definitions

## Local State

The local state file stores metadata for jobs created by the CLI, including:

- job id
- job name
- preset id
- notebook directory
- remote port
- remote stdout path
- remote stderr path
- uploaded sbatch template path
- local tunnel pid
- local forwarded port
- local Jupyter URL

This is used to:

- label jobs as `CLI-managed`
- reconnect more reliably
- clean up local tunnels on `kill`

## Typical Workflow

### Start a new GPU Jupyter job

```bash
sherlock-cli new --preset xiaojie-gpu
```

### Check current jobs

```bash
sherlock-cli list
```

### Reconnect to a running job

```bash
sherlock-cli connect 123456
```

### Read recent logs

Use the interactive `Logs` action from the main menu.

### Kill a job you no longer want

```bash
sherlock-cli kill 123456
```

## Notes

- The CLI is centered on JupyterLab jobs.
- Existing shell scripts are still in the repo, but the Python CLI is the main interface now.
- External Sherlock jobs are shown in the table, but CLI state tracking only exists for jobs created by this CLI.
- For external jobs, connect behavior depends on whether the job logs and node information are available and Jupyter can be detected from logs.

# sherlock-cli

`sherlock-cli` is a Python CLI for two related Slurm workflows:

- submitting and managing JupyterLab jobs
- opening Cursor, VS Code, or plain SSH remote sessions on compute nodes

The repository ships with working configs for Stanford Sherlock and Marlowe, but the CLI is organized around editable TOML configs so the same workflow can be adapted to similar clusters.

## Why use it

`sherlock-cli` combines the cluster actions that are usually spread across `sbatch`, `squeue`, `scancel`, SSH config edits, port forwarding, and ad hoc notes:

- submit Jupyter jobs from named presets
- list `PENDING` and `RUNNING` jobs in one table
- watch a job until it starts
- connect a running Jupyter job to localhost and open it in a browser
- set up and manage compute-node remote sessions for Cursor or SSH
- keep local state for CLI-managed jobs so reconnect and cleanup are easier

## Install

Requirements:

- Python 3.9+
- SSH access to the target cluster
- a working cluster account for the selected config

Install from source:

```bash
python3 -m pip install -e .
```

Then create your user config:

```bash
sherlock-cli setup sherlock
sherlock-cli setup marlowe
```

The setup flow prompts for values such as your cluster username, notification email, and default notebook directory, then writes a user-specific config to `~/.config/sherlock-cli/<cluster>.toml`.

That installs these commands:

- `sherlock-cli`
- `sherlock-compute`
- `marlowe-compute`

You can also use the helper installer:

```bash
./install.sh sherlock
./install.sh marlowe
```

`install.sh` installs the package, runs `sherlock-cli setup` for the chosen cluster, and then runs `remote setup --full` with the generated user config.

## Quick Start

### Sherlock: submit a Jupyter job

If this is your first time using the CLI on Sherlock, run:

```bash
sherlock-cli setup sherlock
```

Submit from a preset:

```bash
sherlock-cli new --preset xiaojie-gpu
```

Or launch the interactive selector:

```bash
sherlock-cli new
```

List current jobs across all discovered cluster configs:

```bash
sherlock-cli list
```

Watch a job until it starts:

```bash
sherlock-cli watch 123456
```

Connect a running job to localhost:

```bash
sherlock-cli connect 123456
sherlock-cli connect marlowe:123456
sherlock-cli connect marlowe:123456 --local-port 56790
```

Cancel a job:

```bash
sherlock-cli kill 123456
```

### Marlowe: use the Marlowe config

Create the Marlowe user config first:

```bash
sherlock-cli setup marlowe
```

The default config is Sherlock. To use Marlowe, pass `--config ~/.config/sherlock-cli/marlowe.toml`:

```bash
sherlock-cli --config ~/.config/sherlock-cli/marlowe.toml new --preset marlowe-batch-gpu
```

You can do the same for any other subcommand:

```bash
sherlock-cli --config ~/.config/sherlock-cli/marlowe.toml list
```

### Remote compute-node session

Set up cluster-side assets and local SSH config:

```bash
sherlock-cli remote setup --full
```

Attach a remote session to an existing running job:

```bash
sherlock-cli remote attach 123456
```

Or start a dedicated remote session job from a remote profile:

```bash
sherlock-cli remote start gpu
```

Reconnect to the current remote session later:

```bash
sherlock-cli remote connect
```

Show jobs together with remote session status:

```bash
sherlock-cli remote list
```

Stop the current remote session:

```bash
sherlock-cli remote stop
```

### Connect from Cursor or SSH

After `remote attach` or `remote start`, connect to the compute node with:

```bash
cursor --remote ssh-remote+sherlock-compute /home/users/<sunetid>
ssh sherlock-compute
```

For Marlowe:

```bash
cursor --remote ssh-remote+marlowe-compute /home/users/<sunetid>
ssh marlowe-compute
```

## Common Workflows

### 1. Jupyter workflow

The normal Jupyter path is:

1. `sherlock-cli new`
2. `sherlock-cli watch <job>` if you want to follow startup explicitly
3. `sherlock-cli connect <job>`
4. `sherlock-cli kill <job>` when you are done

When you submit with `new`, the CLI:

- uploads the configured `.sbatch` template to the target cluster
- submits the job with stable stdout and stderr paths
- stores job metadata in the local state file
- watches the job until it starts or exits
- automatically starts SSH port forwarding when the job reaches `RUNNING`

Use resource overrides when you want to keep the preset but change a few fields:

```bash
sherlock-cli new \
  --preset general-cpu \
  --job-name my-jupyter \
  --notebook-dir /path/to/notebooks \
  --time 08:00:00 \
  --mem 128G \
  --cpus 16
```

### 2. Remote workflow

There are two remote-session modes:

- `remote attach`: start a user-mode `sshd` inside an already running job
- `remote start`: submit a separate dedicated remote session job from a `[[remote_profiles]]` entry

`remote stop` behaves differently for the two modes:

- attached sessions stop only the user-mode `sshd`
- dedicated sessions cancel the dedicated Slurm job

Sherlock ships built-in remote profiles:

- `cpu`
- `gpu`
- `owners`

Marlowe does not ship default remote start profiles, so the usual path there is `remote attach` unless you add your own `[[remote_profiles]]`.

## Interactive Mode

Running `sherlock-cli` with no subcommand opens the interactive menu.
Without `--config`, it first asks which discovered cluster to use, then opens that cluster's interactive menu.
Use `Servers` to open a multi-cluster dashboard with every discovered cluster shown together.
In that dashboard, up and down arrows choose which cluster receives the selected action.

Home screen actions:

- `Refresh`
- `New`
- `Connect`
- `Watch`
- `Kill`
- `Logs`
- `Remote`
- `Servers`
- `Quit`

Useful controls:

- left and right arrows move between actions
- `Enter` confirms the current action
- `r`, `n`, `c`, `w`, `k`, `l`, `m`, `s`, `q` work as shortcuts
- `Ctrl+C` twice within 2 seconds exits interactive mode

When the CLI needs a specific job, it opens a selector:

- up and down arrows move between jobs
- `j` and `k` also work
- `Enter` selects the highlighted job

The `Remote` action opens a second menu for `setup`, `attach`, `start`, `connect`, `stop`, and `clean`.

## Command Reference

### `sherlock-cli list`

Show current `PENDING` and `RUNNING` jobs for all discovered clusters by default.
Use `--config` to limit the output to one cluster.

```bash
sherlock-cli list
sherlock-cli --config ~/.config/sherlock-cli/marlowe.toml list
sherlock-cli list --logs
```

### `sherlock-cli new`

Submit a new JupyterLab job from a preset.

```bash
sherlock-cli new
sherlock-cli new --preset xiaojie-gpu
```

Common flags:

- `--preset`
- `--job-name`
- `--notebook-dir`
- `--port`
- `--partition`
- `--account`
- `--mem`
- `--time`
- `--cpus`
- `--gpus`
- `--nodelist`
- `--constraint`
- `--no-browser`

### `sherlock-cli connect <job>`

Connect a running Jupyter job to localhost.

```bash
sherlock-cli connect 123456
sherlock-cli connect marlowe:123456
sherlock-cli connect marlowe:123456 --local-port 56790
```

`<job>` can be either:

- a job id
- a unique active job name

Without `--config`, the CLI searches every discovered cluster. If a bare target matches more than one cluster, rerun the command with `cluster:job` or `--config`.

The CLI resolves the compute node, reads the remote logs, extracts the Jupyter URL when possible, and starts SSH port forwarding.
Use `--local-port` when you want the localhost side to differ from the remote Jupyter port. If omitted, the CLI keeps the current default and uses the remote Jupyter port locally.
In the interactive menu, `Connect` prompts for the local port and pre-fills the known remote Jupyter port when available.

### `sherlock-cli watch <job>`

Watch a job until it becomes `RUNNING` or reaches a terminal state.

```bash
sherlock-cli watch 123456
sherlock-cli watch 123456 --connect-on-run
sherlock-cli watch sherlock:123456
```

### `sherlock-cli kill <job>`

Cancel a job with `scancel`.

```bash
sherlock-cli kill 123456
sherlock-cli kill marlowe:123456
```

If the CLI created a local SSH tunnel for that job, it also terminates the local tunnel process.

### `sherlock-cli remote setup`

Prepare remote-session files and, optionally, local SSH config.

```bash
sherlock-cli remote setup
sherlock-cli remote setup --full
```

This command can:

- ensure the login-node SSH alias exists locally
- upload `remote/start_sshd.sh`
- create remote `sshd_config` and host keys
- write cluster-side Slurm launch scripts for configured remote profiles

If you only want the local SSH alias, use:

```bash
sherlock-cli remote setup-local
```

### `sherlock-cli remote attach [job]`

Attach a compute-node remote session to an existing running job.

```bash
sherlock-cli remote attach 123456
sherlock-cli remote attach marlowe:123456
```

If no job is provided, the CLI prompts you to choose from current `RUNNING` jobs.

### `sherlock-cli remote start [profile]`

Start a dedicated remote session job from a configured remote profile.

```bash
sherlock-cli remote start
sherlock-cli remote start gpu
```

With no profile argument, the CLI uses the default remote profile when one is configured.

### `sherlock-cli remote connect`

Reconnect to the current remote session by refreshing the local compute-node SSH config.

```bash
sherlock-cli remote connect
```

Without `--config`, this works only when exactly one configured cluster has saved remote session metadata.

### `sherlock-cli remote list`

Show cluster jobs together with remote session metadata for every discovered cluster by default.

```bash
sherlock-cli remote list
```

### `sherlock-cli remote stop`

Stop the current remote session.

```bash
sherlock-cli remote stop
```

### `sherlock-cli remote clean`

Remove the local compute-node SSH block from `~/.ssh/config`.

```bash
sherlock-cli remote clean
```

## Configuration and Presets

By default the CLI discovers every available config. Interactive mode starts with one selected cluster and can open the `Servers` dashboard when needed; non-interactive `list` / `connect` / `watch` / `kill` / `remote` commands can search across discovered clusters. Use `--config` whenever you want to scope a command to one cluster:

```bash
sherlock-cli --config /path/to/config.toml list
```

You can also set `SHERLOCK_CONFIG` if you want a different default config file.

The repo currently ships:

- `sherlock_presets.toml`
- `marlowe_presets.toml`

Two config concepts matter:

- `[[presets]]`: resource templates for Jupyter job submission
- `[[remote_profiles]]`: resource templates for dedicated remote session jobs

Bundled Jupyter presets:

- `xiaojie-cpu`
- `xiaojie-gpu`
- `general-cpu`
- `general-gpu`
- `owner-cpu`
- `owner-gpu`
- `bigmem`
- `stanford-gpu`
- `marlowe-batch-gpu`
- `marlowe-batch-cpu`

Each preset defines the job template and runtime defaults such as partition, CPU count, GPU count, memory, time limit, port, and optional Slurm fields like account, nodelist, or constraint.

## Compatibility Commands

The package also installs two wrapper commands:

```bash
sherlock-compute ...
marlowe-compute ...
```

They are convenience entrypoints for remote-session workflows:

- `sherlock-compute ...` forwards to `sherlock-cli --config ~/.config/sherlock-cli/sherlock.toml remote ...` when that file exists, otherwise it falls back to the bundled Sherlock preset file
- `marlowe-compute ...` forwards to `sherlock-cli --config ~/.config/sherlock-cli/marlowe.toml remote ...` when that file exists, otherwise it falls back to the bundled Marlowe preset file

The main documentation path is still `sherlock-cli`.

## Local State

The CLI stores local metadata for CLI-managed jobs in a state file. By default:

- Sherlock: `~/.local/state/sherlock-cli/state.json`
- Marlowe: `~/.local/state/sherlock-cli/marlowe-state.json`

This state is used to:

- label jobs as CLI-managed
- reconnect more reliably
- track uploaded templates and remote log paths
- clean up local SSH tunnels on `kill`

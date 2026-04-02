# sherlock-cli

Python CLI for submitting and managing Sherlock and Marlowe jobs.

The repo now unifies two workflows:

- Jupyter job submission and port forwarding
- Cursor/VS Code/SSH remote sessions on compute nodes

Detailed CLI documentation lives in `CLI_README.md`.

## Install

```bash
python3 -m pip install -e .
```

Or use the thin installer:

```bash
./install.sh sherlock
./install.sh marlowe
```

## Usage

Sherlock (default config):

Interactive mode:

```bash
sherlock-cli
```

The interactive home screen now includes a `Remote` action. Enter it to run `setup`, `attach`, `start`, `connect`, `stop`, or `clean` without typing the full subcommand. For `Attach`, the CLI shows running jobs and lets you choose with the up/down keys.

Marlowe:

```bash
sherlock-cli --config marlowe_presets.toml
```

Subcommands:

```bash
sherlock-cli list
sherlock-cli new --preset xiaojie-gpu
sherlock-cli connect 123456
sherlock-cli watch 123456
sherlock-cli kill 123456
sherlock-cli remote setup --full
sherlock-cli remote attach 123456
sherlock-cli remote start gpu
sherlock-cli remote connect
```

Compatibility wrappers are also installed:

```bash
sherlock-compute attach 123456
marlowe-compute connect
```

## Quick Start

### 1. Jupyter workflow

Submit a new Jupyter job on Sherlock:

```bash
sherlock-cli new --preset xiaojie-gpu
```

List current jobs:

```bash
sherlock-cli list
```

Watch a job until it starts:

```bash
sherlock-cli watch 123456
```

Forward a running Jupyter job to localhost and open it in your browser:

```bash
sherlock-cli connect 123456
```

Cancel a job:

```bash
sherlock-cli kill 123456
```

For Marlowe, pass the Marlowe config:

```bash
sherlock-cli --config marlowe_presets.toml new --preset marlowe-batch-gpu
```

### 2. Remote compute-node workflow

Set up local SSH config and remote `sshd` assets:

```bash
sherlock-cli remote setup --full
```

Attach a remote session to an existing running job:

```bash
sherlock-cli remote attach 123456
```

Start a dedicated Sherlock remote session:

```bash
sherlock-cli remote start gpu
```

Reconnect to the most recent remote session:

```bash
sherlock-cli remote connect
```

Show jobs plus remote session status:

```bash
sherlock-cli remote list
```

Stop the remote session:

```bash
sherlock-cli remote stop
```

Behavior:

- `remote attach` starts a user-mode `sshd` inside an existing running job and keeps the job itself alive on `stop`.
- `remote start` submits a dedicated remote job from a configured remote profile and `stop` cancels that dedicated job.
- Sherlock ships built-in remote profiles: `cpu`, `gpu`, `owners`.
- Marlowe does not ship built-in remote start profiles; use `remote attach` unless you add your own `[[remote_profiles]]`.

### 3. Connect from Cursor or SSH

After `remote attach` or `remote start` succeeds, connect with:

```bash
cursor --remote ssh-remote+sherlock-compute /home/users/<sunetid>
ssh sherlock-compute
```

Or for Marlowe:

```bash
cursor --remote ssh-remote+marlowe-compute /home/users/<sunetid>
ssh marlowe-compute
```

If you prefer the compatibility wrappers, these are equivalent entrypoints:

```bash
sherlock-compute setup --full
sherlock-compute attach 123456
sherlock-compute start gpu

marlowe-compute setup --full
marlowe-compute attach 123456
```

## Presets

The repo includes:

- `sherlock_presets.toml`
- `marlowe_presets.toml`

Default presets:

- `xiaojie-cpu`
- `xiaojie-gpu`
- `general-cpu`
- `general-gpu`
- `owner-cpu`
- `owner-gpu`
- `bigmem`
- `stanford-gpu`
- `marlowe-batch-gpu`

Sherlock also ships remote session profiles:

- `cpu`
- `gpu`
- `owners`

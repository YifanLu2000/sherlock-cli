# sherlock-cli

Python CLI for submitting and managing Sherlock JupyterLab jobs.

The repo now includes cluster configs for both Sherlock and Marlowe.

Detailed CLI documentation lives in `CLI_README.md`.

## Install

```bash
python3 -m pip install -e .
```

## Usage

Sherlock (default config):

Interactive mode:

```bash
sherlock-cli
```

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

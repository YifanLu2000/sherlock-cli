# sherlock-cli

Python CLI for submitting and managing Sherlock JupyterLab jobs.

Detailed CLI documentation lives in `CLI_README.md`.

## Install

```bash
python3 -m pip install -e .
```

## Usage

Interactive mode:

```bash
sherlock-cli
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

The unified preset config lives in `sherlock_presets.toml`.

Default presets:

- `xiaojie-cpu`
- `xiaojie-gpu`
- `general-cpu`
- `general-gpu`
- `owner-cpu`
- `owner-gpu`
- `bigmem`
- `stanford-gpu`

# Repository Guidelines

## Project Structure & Module Organization
`sherlock_cli/` contains the Python package. Key modules include `cli.py` for argument parsing and interactive menus, `service.py` and `remote.py` for Slurm/SSH workflows, `config.py` for TOML loading, and `state.py` for local state. Tests live in `tests/` and follow the package layout by behavior (`test_cli.py`, `test_remote.py`, `test_config.py`). Cluster assets live outside the package: `sbatches/` stores Slurm templates, `remote/start_sshd.sh` bootstraps remote sessions, and `sherlock_presets.toml` plus `marlowe_presets.toml` define bundled configs.

## Build, Test, and Development Commands
Install in editable mode with:

```bash
python3 -m pip install -e .
```

Run the full test suite with:

```bash
python3 -m pytest
```

Run a focused test file while iterating:

```bash
python3 -m pytest tests/test_remote.py -q
```

Smoke-check the CLI entrypoint with:

```bash
python3 -m sherlock_cli --help
```

Use `./install.sh sherlock` or `./install.sh marlowe` only when you intentionally want local install plus cluster setup side effects.

## Coding Style & Naming Conventions
Target Python 3.9+ and match the existing standard-library-first import grouping. Use 4-space indentation, snake_case for functions and variables, and PascalCase for classes. Prefer small dataclasses and typed function signatures where the surrounding code already uses them. Keep user-facing CLI text concise and update `README.md` when commands or flags change. There is no formatter configured in `pyproject.toml`, so keep edits close to existing style rather than introducing a new tool-driven format.

## Testing Guidelines
Tests are discovered by `pytest`, but most suites use `unittest.TestCase` plus `unittest.mock`. Add new tests under `tests/test_<area>.py` and name methods `test_<behavior>`. Favor isolated unit tests with mocks for SSH, subprocess, browser, and filesystem behavior; avoid depending on a real cluster account. No coverage gate is configured, so contributors should add regression tests for every behavior change.

## Commit & Pull Request Guidelines
Recent history uses short, imperative commit subjects such as `Add interactive remote menu to CLI` and `Unify remote cluster access into sherlock-cli`. Follow that pattern: one sentence, present tense, focused scope. Pull requests should explain user-visible CLI changes, list the commands used for validation, and call out any config, SSH, or Slurm template updates. Include screenshots or terminal captures when changing the interactive menu flow.

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from sherlock_cli.config import discover_configs, load_config, write_config


class ConfigTests(unittest.TestCase):
    def test_load_config(self):
        config = load_config()
        self.assertIn("xiaojie-cpu", config.presets)
        self.assertEqual(config.presets["general-cpu"].partition, "normal")
        self.assertEqual(config.connection.name, "Sherlock")
        self.assertEqual(config.connection.resource, "sherlock")
        self.assertEqual(config.remote.compute_host_alias, "sherlock-compute")
        self.assertIn("gpu", config.remote_profiles)
        self.assertEqual(config.remote_default_profile_id, "cpu")

    def test_load_marlowe_config_cpu_template_exists(self):
        config = load_config("marlowe_presets.toml")
        self.assertIn("marlowe-batch-cpu", config.presets)
        template_path = config.repo_root / config.presets["marlowe-batch-cpu"].sbatch_script
        self.assertTrue(template_path.exists())

    def test_custom_config_path(self):
        with TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(
                """
[connection]
name = "Demo"
resource = "demo"
domain_name = "login.demo"
forward_username = "user"
email = "user@example.com"
machine_prefix = "d"
default_notebook_dir = "/tmp"
remote_util_dir = "~/forward-util"

[state]
path = "~/.demo-state.json"

[remote]
compute_host_alias = "demo-compute"

[[remote_profiles]]
id = "cpu"
job_name = "demo-remote"
partition = "normal"
cpus = 2
mem = "4G"
time = "01:00:00"
default = true

[[presets]]
id = "demo"
label = "Demo"
job_name = "demo-job"
sbatch_script = "sbatches/CPU-jupyterlab.sbatch"
partition = "normal"
cpus = 2
mem = "4G"
time = "01:00:00"
port = 9999
account = "demo-account"
"""
            )
            config = load_config(str(config_path))
            self.assertEqual(config.connection.name, "Demo")
            self.assertEqual(config.connection.resource, "demo")
            self.assertEqual(config.presets["demo"].port, 9999)
            self.assertEqual(config.presets["demo"].account, "demo-account")
            self.assertEqual(config.remote.compute_host_alias, "demo-compute")
            self.assertEqual(config.remote_default_profile_id, "cpu")

    def test_load_config_prefers_user_override_for_default_sherlock(self):
        with TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            target = home / ".config" / "sherlock-cli" / "sherlock.toml"
            source = Path(__file__).resolve().parent.parent / "sherlock_presets.toml"
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                write_config(
                    source,
                    target,
                    forward_username="override-user",
                    email="override-user@stanford.edu",
                    default_notebook_dir="/oak/override-user",
                )
                config = load_config()
            self.assertEqual(config.connection.forward_username, "override-user")
            self.assertEqual(config.connection.email, "override-user@stanford.edu")
            self.assertEqual(config.connection.default_notebook_dir, "/oak/override-user")
            self.assertEqual(config.config_path, target.resolve())

    def test_discover_configs_prefers_user_override(self):
        with TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            target = home / ".config" / "sherlock-cli" / "sherlock.toml"
            source = Path(__file__).resolve().parent.parent / "sherlock_presets.toml"
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                write_config(
                    source,
                    target,
                    forward_username="override-user",
                    email="override-user@stanford.edu",
                    default_notebook_dir="/oak/override-user",
                )
                configs = discover_configs()
            sherlock_path = dict(configs)["Sherlock"]
            self.assertEqual(sherlock_path, target.resolve())


if __name__ == "__main__":
    unittest.main()

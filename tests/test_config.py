from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sherlock_cli.config import load_config


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


if __name__ == "__main__":
    unittest.main()

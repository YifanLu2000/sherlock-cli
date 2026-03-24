from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sherlock_cli.service import PersistentSSHSession, SherlockError


class PersistentSSHSessionTests(unittest.TestCase):
    def test_run_reads_stdout_without_newline(self):
        session = PersistentSSHSession(command=["bash"])
        try:
            self.assertEqual(session.run("printf 'hello'"), "hello")
        finally:
            session.close()

    def test_run_raises_with_stderr_on_failure(self):
        session = PersistentSSHSession(command=["bash"])
        try:
            with self.assertRaises(SherlockError) as ctx:
                session.run("echo bad >&2\nfalse")
            self.assertIn("bad", str(ctx.exception))
        finally:
            session.close()

    def test_write_text_writes_file_through_session(self):
        session = PersistentSSHSession(command=["bash"])
        try:
            with TemporaryDirectory() as tmpdir:
                target = Path(tmpdir) / "remote.sbatch"
                session.write_text(str(target), "#!/bin/bash\necho hello\n")
                self.assertEqual(target.read_text(), "#!/bin/bash\necho hello\n")
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()

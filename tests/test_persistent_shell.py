import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sherlock_cli.service import PersistentSSHSession, SherlockError


class FakeProcess:
    def __init__(self):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("")
        self.stderr = None
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


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

    def test_starts_process_with_inherited_stderr(self):
        captured = {}

        def fake_popen(command, stdin=None, stdout=None, stderr=None, text=None, bufsize=None):
            captured["command"] = command
            captured["stdin"] = stdin
            captured["stdout"] = stdout
            captured["stderr"] = stderr
            captured["text"] = text
            captured["bufsize"] = bufsize
            return FakeProcess()

        session = PersistentSSHSession(command=["ssh", "demo"], popen_factory=fake_popen)
        process = session._ensure_started()

        self.assertIsInstance(process, FakeProcess)
        self.assertIsNone(captured["stderr"])


if __name__ == "__main__":
    unittest.main()

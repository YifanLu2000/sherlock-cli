from unittest import mock
import unittest

from sherlock_cli.cli import InterruptTracker, _read_raw_key, should_exit_on_interrupt


class CliTests(unittest.TestCase):
    def test_first_interrupt_does_not_exit(self):
        tracker = InterruptTracker()
        self.assertFalse(should_exit_on_interrupt(tracker, now=10.0))

    def test_second_interrupt_within_window_exits(self):
        tracker = InterruptTracker()
        self.assertFalse(should_exit_on_interrupt(tracker, now=10.0))
        self.assertTrue(should_exit_on_interrupt(tracker, now=11.0))

    def test_second_interrupt_after_window_does_not_exit(self):
        tracker = InterruptTracker()
        self.assertFalse(should_exit_on_interrupt(tracker, now=10.0, window_seconds=2.0))
        self.assertFalse(should_exit_on_interrupt(tracker, now=12.5, window_seconds=2.0))

    def test_raw_ctrl_c_raises_keyboard_interrupt(self):
        fake_stdin = mock.Mock()
        fake_stdin.fileno.return_value = 0
        fake_stdin.read.return_value = "\x03"
        with (
            mock.patch("sherlock_cli.cli.sys.stdin", fake_stdin),
            mock.patch("sherlock_cli.cli.termios.tcgetattr", return_value=object()),
            mock.patch("sherlock_cli.cli.termios.tcsetattr"),
            mock.patch("sherlock_cli.cli.tty.setraw"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                _read_raw_key()


if __name__ == "__main__":
    unittest.main()

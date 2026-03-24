import unittest

from sherlock_cli.parsing import (
    parse_job_detail,
    parse_jupyter_info,
    parse_sacct_job,
    parse_sbatch_submission,
    parse_squeue_jobs,
    rewrite_local_jupyter_url,
)


class ParsingTests(unittest.TestCase):
    def test_parse_sbatch_submission(self):
        self.assertEqual(parse_sbatch_submission("Submitted batch job 12345"), "12345")
        self.assertEqual(parse_sbatch_submission("12345;cluster"), "12345")

    def test_parse_squeue_jobs(self):
        jobs = parse_squeue_jobs("123|GPU-jupyterlab|RUNNING|gpu|sh03-01|01:23\n", {"123"})
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].origin, "CLI-managed")
        self.assertEqual(jobs[0].reason_or_node, "sh03-01")

    def test_parse_sacct_job(self):
        job = parse_sacct_job("123|GPU-jupyterlab|COMPLETED|gpu|sh03-01|02:00:00\n", set())
        self.assertIsNotNone(job)
        self.assertEqual(job.state, "COMPLETED")

    def test_parse_job_detail(self):
        detail = parse_job_detail(
            "JobId=123 JobName=GPU-jupyterlab JobState=RUNNING Partition=gpu "
            "NodeList=sh03-01 Reason=None RunTime=00:03:12 "
            "StdOut=/home/user/forward-util/job-123.out StdErr=/home/user/forward-util/job-123.err"
        )
        self.assertEqual(detail.job_id, "123")
        self.assertEqual(detail.stdout_path, "/home/user/forward-util/job-123.out")

    def test_parse_jupyter_info(self):
        info = parse_jupyter_info(
            "Or copy and paste one of these URLs:\n"
            "http://sh03-01:56790/lab?token=abcd1234\n"
        )
        self.assertIsNotNone(info)
        self.assertEqual(info.port, 56790)
        self.assertEqual(info.token, "abcd1234")

    def test_rewrite_local_url(self):
        local = rewrite_local_jupyter_url("http://sh03-01:56790/lab?token=abcd", 8888)
        self.assertEqual(local, "http://localhost:8888/lab?token=abcd")


if __name__ == "__main__":
    unittest.main()

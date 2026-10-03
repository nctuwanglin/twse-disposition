import subprocess
import unittest
from unittest.mock import patch

from scripts import push_update


BASE = 'a' * 40
NEWER = 'b' * 40
HEAD = 'c' * 40


def result(code=0, stdout='', stderr=''):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class PushUpdateTests(unittest.TestCase):
    def run_push(self, responses):
        def respond(command, **kwargs):
            if command == ['git', 'rev-parse', '--verify', 'HEAD']:
                return result(stdout=HEAD + '\n')
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        with patch.object(push_update.subprocess, 'run', side_effect=respond) as run, \
                patch.object(push_update.time, 'sleep'):
            try:
                push_update.push_update(BASE)
            finally:
                self.commands = [call.args[0] for call in run.call_args_list
                                 if call.args[0][1] != 'rev-parse']

    def test_success_uses_only_plain_push_after_remote_check(self):
        self.run_push([result(stdout=f'{BASE}\trefs/heads/main\n'), result()])
        self.assertEqual(self.commands, [
            ['git', 'ls-remote', '--exit-code', 'origin', 'refs/heads/main'],
            ['git', 'push', 'origin', f'{HEAD}:refs/heads/main'],
        ])

    def test_lost_push_acknowledgement_accepts_exact_intended_head(self):
        self.run_push([result(stdout=f'{BASE}\trefs/heads/main\n'),
                       subprocess.TimeoutExpired(['git', 'push'], 60),
                       result(stdout=f'{HEAD}\trefs/heads/main\n')])
        self.assertEqual([c[1] for c in self.commands],
                         ['ls-remote', 'push', 'ls-remote'])

    def test_lost_push_acknowledgement_rejects_other_remote_head(self):
        with self.assertRaisesRegex(push_update.PushError, 'fresh checkout'):
            self.run_push([result(stdout=f'{BASE}\trefs/heads/main\n'),
                           subprocess.TimeoutExpired(['git', 'push'], 60),
                           result(stdout=f'{NEWER}\trefs/heads/main\n')])
        self.assertEqual([c[1] for c in self.commands].count('push'), 1)

    def test_final_push_timeout_can_be_confirmed_without_fourth_push(self):
        self.run_push([result(stdout=f'{BASE}\trefs/heads/main\n'),
                       subprocess.TimeoutExpired(['git', 'push'], 60)] * 3
                      + [result(stdout=f'{HEAD}\trefs/heads/main\n')])
        self.assertEqual([c[1] for c in self.commands].count('push'), 3)

    def test_newer_remote_stops_before_push(self):
        with self.assertRaisesRegex(push_update.PushError, 'fresh checkout'):
            self.run_push([result(stdout=f'{NEWER}\trefs/heads/main\n')])
        self.assertEqual(len(self.commands), 1)

    def test_transient_failure_rechecks_base_before_retry(self):
        self.run_push([result(stdout=f'{BASE}\trefs/heads/main\n'),
                       result(128, stderr='Connection timed out'),
                       result(stdout=f'{BASE}\trefs/heads/main\n'), result()])
        self.assertEqual([c[1] for c in self.commands],
                         ['ls-remote', 'push', 'ls-remote', 'push'])

    def test_remote_advance_after_transient_failure_stops_retry(self):
        with self.assertRaisesRegex(push_update.PushError, 'fresh checkout'):
            self.run_push([result(stdout=f'{BASE}\trefs/heads/main\n'),
                           result(128, stderr='Connection reset by peer'),
                           result(stdout=f'{NEWER}\trefs/heads/main\n')])
        self.assertEqual([c[1] for c in self.commands].count('push'), 1)

    def test_permanent_failure_is_not_retried(self):
        with self.assertRaisesRegex(push_update.PushError, 'Permission denied'):
            self.run_push([result(stdout=f'{BASE}\trefs/heads/main\n'),
                           result(128, stderr='Permission denied (publickey)')])
        self.assertEqual(len(self.commands), 2)

    def test_transient_failure_has_three_attempt_bound(self):
        with self.assertRaisesRegex(push_update.PushError, '3 attempts'):
            self.run_push([result(stdout=f'{BASE}\trefs/heads/main\n'),
                           result(128, stderr='Connection timed out')] * 3
                          + [result(stdout=f'{BASE}\trefs/heads/main\n')])
        self.assertEqual([c[1] for c in self.commands].count('push'), 3)

    def test_failed_remote_check_never_pushes(self):
        with self.assertRaises(push_update.PushError):
            self.run_push([result(128, stderr='Could not resolve host')] * 3)
        self.assertTrue(all(c[1] == 'ls-remote' for c in self.commands))

    def test_timeout_is_bounded_and_never_skips_remote_check(self):
        with self.assertRaises(push_update.PushError):
            self.run_push([subprocess.TimeoutExpired(['git'], 30)] * 3)
        self.assertEqual(len(self.commands), 3)

    def test_invalid_expected_base_fails_without_git(self):
        with patch.object(push_update.subprocess, 'run') as run:
            with self.assertRaises(push_update.PushError):
                push_update.push_update('main')
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()

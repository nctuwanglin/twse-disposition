"""Bounded plain push: never merge, rebase, or force stale generated artifacts."""

import argparse
import re
import subprocess
import sys
import time


class PushError(RuntimeError):
    pass


def push_update(expected_base):
    if not re.fullmatch(r'[0-9a-fA-F]{40}', expected_base):
        raise PushError('Expected checkout base must be a full commit SHA')
    try:
        local = subprocess.run(['git', 'rev-parse', '--verify', 'HEAD'],
                               capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise PushError('Could not determine intended local HEAD: timeout') from exc
    intended_head = local.stdout.strip()
    if local.returncode or not re.fullmatch(r'[0-9a-f]{40}', intended_head):
        raise PushError('Could not determine intended local HEAD')
    transient = ('timed out', 'timeout', 'connection reset', 'connection refused',
                 'could not resolve host', 'remote end hung up',
                 'requested url returned error: 502',
                 'requested url returned error: 503',
                 'requested url returned error: 504')
    for attempt in range(1, 4):
        push_attempted = False
        try:
            remote = subprocess.run(
                ['git', 'ls-remote', '--exit-code', 'origin', 'refs/heads/main'],
                capture_output=True, text=True, timeout=30)
            outcome = remote
            if remote.returncode == 0:
                fields = remote.stdout.split()
                if fields == [intended_head, 'refs/heads/main']:
                    print('Intended local HEAD is already pushed')
                    return
                if fields != [expected_base, 'refs/heads/main']:
                    raise PushError('origin/main changed or is missing; rerun from a fresh checkout')
                push_attempted = True
                outcome = subprocess.run(
                    ['git', 'push', 'origin', f'{intended_head}:refs/heads/main'],
                    capture_output=True, text=True, timeout=60)
                if outcome.returncode == 0:
                    print('Push completed successfully')
                    return
            detail = (outcome.stderr + '\n' + outcome.stdout).strip()
            if not any(marker in detail.lower() for marker in transient):
                raise PushError(f'Push/check failed; rerun from a fresh checkout: {detail}')
        except subprocess.TimeoutExpired:
            detail = 'Git operation timed out'
        if attempt == 3:
            # The final push may have landed even if its acknowledgement was lost.
            # One bounded read confirms it; never issue a fourth push.
            if push_attempted:
                try:
                    confirmation = subprocess.run(
                        ['git', 'ls-remote', '--exit-code', 'origin', 'refs/heads/main'],
                        capture_output=True, text=True, timeout=30)
                except subprocess.TimeoutExpired:
                    confirmation = None
                if confirmation is not None and confirmation.returncode == 0:
                    fields = confirmation.stdout.split()
                    if fields == [intended_head, 'refs/heads/main']:
                        print('Intended local HEAD is already pushed')
                        return
                    if fields != [expected_base, 'refs/heads/main']:
                        raise PushError('origin/main changed or is missing; rerun from a fresh checkout')
            raise PushError(f'Push failed after 3 attempts: {detail}')
        print(f'Transient failure ({attempt}/3); rechecking origin/main before retry',
              file=sys.stderr)
        time.sleep(5 * attempt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-base', required=True)
    args = parser.parse_args()
    try:
        push_update(args.expected_base)
    except PushError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

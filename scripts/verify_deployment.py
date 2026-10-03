"""Verify actual public bytes, including status-only changes; no writes to GitHub."""
import argparse
import sys
import time
import urllib.request
from pathlib import Path

import artifact_bundle


def fetch_bytes(url):
    req = urllib.request.Request(url, headers={'Cache-Control': 'no-cache', 'User-Agent': 'dashboard-verifier'})
    with urllib.request.urlopen(req, timeout=10) as response:
        return response.read()


def check(url, expected, *, fetch=None):
    fetch = fetch or fetch_bytes
    try:
        for rel, content in expected.items():
            actual = fetch(f"{url.rstrip('/')}/{rel}?verify={time.time_ns()}")
            if actual != content:
                print(f'尚未一致：{rel}')
                return False
        return True
    except (OSError, ValueError) as exc:
        print(f'尚未取得線上產物：{type(exc).__name__}')
        return False


def poll(url, expected, *, attempts=12, interval=15):
    deadline = time.monotonic() + 300
    for attempt in range(attempts):
        if check(url, expected):
            return True
        if time.monotonic() >= deadline:
            break
        if attempt + 1 < attempts:
            time.sleep(interval)
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--url', required=True)
    parser.add_argument('--attempts', type=int, default=12)
    args = parser.parse_args()
    root = Path(args.root)
    manifest = artifact_bundle.validate(root)
    rels = set(manifest['artifacts']) | {'build-manifest.json', 'update-status.json'}
    expected = {rel: artifact_bundle.safe_path(root, rel).read_bytes() for rel in sorted(rels)}
    if not poll(args.url, expected, attempts=args.attempts):
        sys.exit('部署驗證失敗：線上內容／健康狀態尚未與本次產物一致')
    print('線上內容與本次產物一致（包含健康狀態）')


if __name__ == '__main__':
    main()

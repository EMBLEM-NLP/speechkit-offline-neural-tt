"""Snapshot or verify native WAV reuse across the public package rename."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def native_wavs(root: Path) -> dict:
    records = {}
    for path in sorted(root.rglob('*.wav')):
        if path.parent.name != 'speech':
            continue
        records[path.relative_to(root).as_posix()] = {
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'bytes': path.stat().st_size, 'modified_ns': path.stat().st_mtime_ns,
        }
    if not records:
        raise ValueError(f'no native segment WAVs under {root}')
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('receipt', type=Path)
    parser.add_argument('--snapshot', action='store_true')
    args = parser.parse_args()
    actual = native_wavs(args.root)
    if args.snapshot:
        args.receipt.write_text(json.dumps(actual, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'status': 'snapshotted', 'native_segments': len(actual)}))
    else:
        before = json.loads(args.receipt.read_text(encoding='utf-8'))
        if before != actual:
            changed = sorted(k for k in before.keys() | actual.keys() if before.get(k) != actual.get(k))
            raise ValueError('native WAVs were changed or replaced: ' + ', '.join(changed))
        print(json.dumps({'status': 'passed', 'native_segments_reused': len(actual), 'wave_bytes_and_mtimes_unchanged': True}))


if __name__ == '__main__':
    main()

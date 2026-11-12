"""Create or verify the source checkout ledger; runtime keeps its own pinned identity."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ('.github', 'docs', 'requests', 'speechkit', 'speechkit_narrate',
               'emblem_rsi', 'powershell', 'tests', 'scripts', 'examples', 'constraints')
SOURCE_FILES = ('.gitattributes', '.gitignore', 'BUNDLE_MANIFEST.json', 'CHANGELOG.md',
                'LICENSE', 'README.md', 'THIRD_PARTY_NOTICES.md', 'bootstrap.py',
                'pyproject.toml', 'requirements.txt', 'requirements-voice.txt', 'constraints-voice.txt', 'MANIFEST.in')


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def source_paths(root: Path) -> list[Path]:
    paths = {root / name for name in SOURCE_FILES if (root / name).is_file()}
    for name in SOURCE_DIRS:
        for path in (root / name).rglob('*'):
            if not path.is_file() or '__pycache__' in path.parts or path.suffix in {'.pyc', '.pyo'}:
                continue
            if path.is_symlink():
                raise ValueError(f'source symlink is not supported: {path}')
            paths.add(path)
    return sorted(paths, key=lambda path: path.relative_to(root).as_posix())


def expected(root: Path) -> dict[str, str]:
    records = {path.relative_to(root).as_posix(): digest(path) for path in source_paths(root)}
    # The separately delivered archive can be absent in a source checkout. Its pin must remain unchanged.
    manifest = json.loads((root / 'BUNDLE_MANIFEST.json').read_text())
    for archive in manifest['archives']:
        records[archive['path']] = archive['sha256']
    return dict(sorted(records.items()))


def verify(root: Path) -> dict:
    wanted = expected(root)
    recorded = {}
    for line in (root / 'SHA256SUMS').read_text().splitlines():
        value, name = line.split('  ', 1)
        if name in recorded or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
            raise ValueError(f'invalid or duplicate ledger entry: {name}')
        recorded[name] = value
    differences = sorted(name for name in wanted.keys() | recorded.keys()
                         if wanted.get(name) != recorded.get(name))
    archives = json.loads((root / 'BUNDLE_MANIFEST.json').read_text())['archives']
    absent = []
    for archive in archives:
        path = root / archive['path']
        if path.exists():
            if digest(path) != archive['sha256']:
                differences.append(archive['path'])
        else:
            absent.append(archive['path'])
    if differences:
        raise ValueError('source ledger mismatch: ' + ', '.join(sorted(set(differences))))
    return {'status': 'passed', 'source_files': len(source_paths(root)),
            'runtime_archives_not_fetched': absent}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args()
    if args.write:
        (ROOT / 'SHA256SUMS').write_text(''.join(f'{value}  {name}\n' for name, value in expected(ROOT).items()))
    print(json.dumps(verify(ROOT), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

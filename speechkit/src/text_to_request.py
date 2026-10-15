# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Convert a plain-text script (one paragraph per segment, blank-line separated) to a request.json."""
import argparse, json, re, sys
from pathlib import Path


def convert(text: str, request_id: str) -> dict:
    paragraphs = [re.sub(r'\s+', ' ', p).strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
    if not paragraphs:
        raise ValueError('The text script contains no paragraphs')
    return {'schemaVersion': '1.0.0', 'id': request_id,
            'segments': [{'id': f'seg-{i:03d}', 'text': p, 'spokenText': p}
                         for i, p in enumerate(paragraphs, 1)]}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--id')
    args = parser.parse_args()
    try:
        request = convert(args.source.read_text(encoding='utf-8'), args.id or args.source.stem)
        args.out.write_text(json.dumps(request, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'status': 'REQUEST_WRITTEN', 'segments': len(request['segments'])}))
    except Exception as error:
        print(json.dumps({'status': 'CONVERSION_FAILED', 'error': str(error)}), file=sys.stderr)
        sys.exit(2)

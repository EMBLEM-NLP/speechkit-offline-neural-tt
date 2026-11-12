"""Run from outside the checkout after installing the wheel to verify packaged resources."""
import importlib.metadata
import importlib.resources
import json
from pathlib import Path
import subprocess
import sys


def main():
    import speechkit_narrate
    import emblem_rsi
    resources = {
        'speechkit_narrate': ['audit/template/report.html', 'asr-models.lock.json'],
        'emblem_rsi': ['rubrics/pronunciation.json', 'js/vendor/acorn.mjs', 'js/vendor/ACORN-LICENSE',
                       'code/js_tokens.mjs', 'code/ps_tokens.ps1'],
    }
    for package, names in resources.items():
        for name in names:
            resource = importlib.resources.files(package).joinpath(name)
            assert resource.is_file() and len(resource.read_bytes()) > 0, (package, name)
    subprocess.run([sys.executable, '-m', 'speechkit_narrate', '--json', 'stages'], check=True)
    print(json.dumps({'status': 'passed', 'version': importlib.metadata.version('speechkit-narrate'),
                      'package': str(Path(speechkit_narrate.__file__).resolve()),
                      'resources': sum(len(names) for names in resources.values())}))


if __name__ == '__main__':
    main()

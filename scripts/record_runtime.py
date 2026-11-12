"""Capture the exact runtime and voice identities used by the Linux narration job."""
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from speechkit_narrate import env
    profile = env.profile('ljspeech-high')
    original = env.voice_model('ljspeech-high')
    aligned = env.aligned_model('ljspeech-high')
    data = {'python': sys.version, 'platform': platform.platform(), 'machine': platform.machine(),
            'libc': platform.libc_ver(), 'git_commit': subprocess.check_output(
                ['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
            'runner_os': os.getenv('RUNNER_OS'), 'runner_arch': os.getenv('RUNNER_ARCH'),
            'image_os': os.getenv('ImageOS'), 'image_version': os.getenv('ImageVersion'),
            'profile': profile, 'profile_sha256': sha(env.profile_path('ljspeech-high')),
            'original_model_sha256': sha(original), 'derived_model_sha256': sha(aligned),
            'voice_config_sha256': sha(env.voice_config('ljspeech-high')),
            'voice_approval': 'PENDING'}
    dest = Path(sys.argv[1])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(data, indent=2) + '\n')


if __name__ == '__main__':
    main()

"""speechkit-offline-neural-tt: verify, fetch and install the hash-pinned offline neural voice runtime."""
from __future__ import annotations  # Keep type annotations independent of declaration order.
import argparse  # Parse explicit verify/install operations.
import hashlib  # Identify every immutable input and generated WAV.
import json  # Record machine-readable execution evidence.
import platform  # Reject incompatible native-wheel targets before extraction.
import re  # Validate SHA-256 records rather than accepting arbitrary text.
import shutil  # Check available disk space before installing large dependencies.
import subprocess  # Invoke the existing verified installer and neural adapter.
import sys  # Use the selected interpreter and return explicit nonzero failures.
import urllib.request  # Used only by the explicit fetch command, never implicitly.
import wave  # Measure actual decoded PCM samples, not estimated speech duration.
import zipfile  # Read and safely extract the original tools archive.
from datetime import datetime, timezone  # Timestamp this run rather than reuse historic results.
from pathlib import Path, PurePosixPath  # Resolve paths relative to this distribution.

ROOT = Path(__file__).resolve().parent  # Allow execution from any working directory.
TOOLS = 'EMBLEM-NLP_Study_Automation_Tools_1_0_0.zip'  # Preserve the original tools identity.
RUNTIME = 'EMBLEM-NLP_Offline_Neural_Runtime_Linux_Py313.zip'  # Include the actual heavy payload.
REPO = 'EMBLEM-NLP/speechkit-offline-neural-tt'  # GitHub home of this distribution.
DIST_VERSION = '1.0.2'  # Repository packaging version; inner component versions are unchanged.
RELEASE_URL = f'https://github.com/{REPO}/releases/download/v{DIST_VERSION}/{RUNTIME}'  # Runtime exceeds git's 100 MB file limit.
PINS = {  # Independent expected identities prevent accepting an accidental replacement.
    TOOLS: 'ed28c91022a586d27ec6b7778889c65efe19f6371a507175f55e4a4c4eeef822',
    RUNTIME: '4aa2d0fa989b127c1c1f6c13d10774d6ad5b2b3f828b52d938d864c34ff64662',
}
SMOKE_TEXT = (  # Use a fresh technical sample, not the historical bundled smoke recording.
    'This is a new neural voice test generated from the bundled model. '
    'The narration plugin and its voice libraries are both present. '
    'Take your time, and listen to each complete sentence.'
)


def sha(path: Path) -> str:
    """Stream a file identity without loading the model into Python memory."""
    with path.open('rb') as handle:  # Always hash actual file bytes.
        return hashlib.file_digest(handle, 'sha256').hexdigest()  # Return lowercase SHA-256.


def safe_name(name: str) -> PurePosixPath:
    """Reject archive traversal and platform-dependent path ambiguity."""
    path = PurePosixPath(name)  # ZIP entries use forward-slash separators.
    if (not name or path.is_absolute() or '..' in path.parts or '\\' in name
            or '\x00' in name or ':' in name or name.startswith('./')):  # Reject unsafe forms.
        raise ValueError(f'Unsafe relative archive path: {name!r}')  # Fail before extraction.
    return path  # Do not normalize a suspicious path into an acceptable one.


def inspect_archive(path: Path, expected: str) -> dict:
    """Verify the outer pin, CRCs, member safety and all internal checksum records."""
    if not path.is_file():  # A filename in documentation is not a present dependency.
        hint = ' Run: python3.13 bootstrap.py fetch' if path.name == RUNTIME else ''
        raise FileNotFoundError(f'Required bundled archive is missing: {path.name}.{hint}')
    actual = sha(path)  # Hash before opening or executing archive contents.
    if actual != expected:  # Preserve the known model/tool identity.
        raise ValueError(f'Archive SHA-256 mismatch: {path.name}')
    with zipfile.ZipFile(path) as archive:  # Read only the verified local archive.
        seen = set()  # Reject duplicate ZIP names before lookup can select the wrong entry.
        expanded = 0  # Bound total extraction size independently of compressed size.
        for member in archive.infolist():  # Validate every archive member.
            safe_name(member.filename)  # Block traversal, drive paths and ambiguity.
            mode = (member.external_attr >> 16) & 0o170000  # Inspect Unix file type flags.
            if member.filename in seen or mode == 0o120000 or member.flag_bits & 1:
                raise ValueError(f'Duplicate, symlink or encrypted member: {member.filename}')
            expanded += member.file_size  # Count all expanded bytes.
            seen.add(member.filename)  # Record the exact original name.
        if expanded > 2_000_000_000:  # Do not allow unexpectedly large extraction.
            raise ValueError('Archive exceeds the two-gigabyte expansion budget')
        bad = archive.testzip()  # Read every member and verify its ZIP CRC.
        if bad is not None:  # Never treat a damaged model as usable.
            raise ValueError(f'Archive CRC failed: {bad}')
        checksum_records = 0  # Report checks actually executed in this run.
        for filename in sorted(seen):  # Inspect every component's existing checksum ledger.
            if PurePosixPath(filename).name != 'SHA256SUMS':
                continue  # Other metadata does not define member identities.
            base = PurePosixPath(filename).parent  # Component paths are relative to its ledger.
            for line in archive.read(filename).decode('utf-8').splitlines():
                if not line.strip():  # Allow harmless empty lines in a ledger.
                    continue
                digest, relative = line.split(None, 1)  # Split only hash and relative filename.
                relative = relative.lstrip('*')  # Support standard binary-mode sha256sum output.
                if not re.fullmatch(r'[a-f0-9]{64}', digest):
                    raise ValueError(f'Malformed checksum in {filename}')
                safe_name(relative)  # The checksum ledger must not refer outside the component.
                entry = (base / relative).as_posix()  # Resolve the member within its component.
                if entry not in seen:  # Fail on a missing file rather than skip its check.
                    raise ValueError(f'Checksum references missing member: {entry}')
                if hashlib.sha256(archive.read(entry)).hexdigest() != digest:
                    raise ValueError(f'Internal checksum failed: {entry}')
                checksum_records += 1  # Count a successful comparison, including nested ledgers.
    return {  # Keep the original component names and measured sizes explicit.
        'name': path.name, 'sha256': actual, 'bytes': path.stat().st_size,
        'members': len(seen), 'expandedBytes': expanded,
        'internalChecksumRecordsPassed': checksum_records, 'crc': 'PASS',
    }


def verify(root: Path = ROOT) -> dict:
    """Fail when either the tools or the model-bearing archive is absent or altered."""
    records = [inspect_archive(root / 'archives' / name, digest)
               for name, digest in PINS.items()]  # Both artifacts are mandatory in this bundle.
    return {  # Verification does not imply installation or neural inference.
        'status': 'BUNDLE_ARCHIVES_VERIFIED', 'neuralRuntimeIncluded': True,
        'archives': records, 'inferenceExecuted': False,
    }


def save(path: Path, value: dict) -> None:
    """Write audit metadata atomically within this installation's own directory."""
    path.parent.mkdir(parents=True, exist_ok=True)  # Create only explicit destination paths.
    temporary = path.with_name(path.name + '.tmp')  # Avoid a partly written success record.
    temporary.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)  # Publish complete JSON as one filesystem operation.


def run(command: list[str], logfile: Path, timeout: int) -> None:
    """Preserve stdout/stderr and fail on timeout or a nonzero child exit."""
    logfile.parent.mkdir(parents=True, exist_ok=True)  # Keep logs with the installed build.
    with logfile.open('w', encoding='utf-8') as handle:  # Never discard error diagnostics.
        subprocess.run(command, check=True, stdout=handle, stderr=subprocess.STDOUT,
                       timeout=timeout)  # The shipped installer uses pip --no-index.


def install(destination: Path, root: Path = ROOT) -> dict:
    """Install to a new directory, then run the real provider and generate fresh speech."""
    verification = verify(root)  # Verify all executable and model inputs before mutation.
    if (sys.implementation.name != 'cpython' or sys.version_info[:2] != (3, 13)
            or platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'AMD64')):
        raise ValueError('Bundled neural wheels require Linux x86_64 / CPython 3.13')
    destination = destination.expanduser().absolute()  # Support relative user output directories.
    if destination.exists() or destination.is_symlink():  # Never overwrite a partial/old runtime.
        raise ValueError('Output already exists; choose a new installation directory')
    ancestor = destination.parent  # Find an existing parent for a meaningful disk-space check.
    while not ancestor.exists():  # A caller may choose a new nested directory.
        ancestor = ancestor.parent
    if shutil.disk_usage(ancestor).free < 1_500_000_000:  # Conservative local install headroom.
        raise ValueError('At least 1.5 GB free disk space is required for installation')
    destination.mkdir(parents=True)  # All dependency mutations remain inside this new directory.
    save(destination / 'BUNDLE_VERIFICATION.json', verification)  # Preserve pre-install evidence.
    try:  # Any failed stage produces a failure report instead of a success claim.
        with zipfile.ZipFile(root / 'archives' / TOOLS) as archive:
            archive.extractall(destination)  # Every path was validated in verify().
        tools = destination / 'EMBLEM-NLP_Study_Automation_Tools_1_0_0'
        plugins = tools / 'EMBLEM-NLP_Reusable_Video_Plugins_1_0_1'
        narration = plugins / 'emblem-nlp-neural-narration'
        runtime = destination / 'runtime'  # Keep heavy dependencies separate from plugin sources.
        logs = destination / 'logs'  # Persist each child process transcript.
        profile = narration / 'profiles' / 'ljspeech-high.json'
        adapter = narration / 'src' / 'voice.py'
        run([sys.executable, str(narration / 'src' / 'install_offline.py'),
             '--archive', str(root / 'archives' / RUNTIME), '--out', str(runtime)],
            logs / '01_install.log', 600)  # Invoke the unchanged hash-checked offline installer.
        python = runtime / '.venv' / 'bin' / 'python'
        run([str(python), str(adapter), 'doctor', '--runtime', str(runtime),
             '--profile', str(profile)], logs / '02_doctor.log', 120)  # Check actual imports/weights.
        script = destination / 'runtime-smoke.request.json'
        save(script, {'schemaVersion': '1.0.0', 'id': 'runtime-packaging-verification',
                      'segments': [{'id': 'runtime-ready', 'text': SMOKE_TEXT,
                                    'spokenText': SMOKE_TEXT}]})  # No copied historical audio.
        speech = destination / 'smoke'
        run([str(python), str(adapter), 'synthesize', '--runtime', str(runtime),
             '--profile', str(profile), '--script', str(script), '--out', str(speech)],
            logs / '03_synthesis.log', 600)  # Run the model, not merely dependency imports.
        manifest = json.loads((speech / 'speech.json').read_text())  # Read actual provider output.
        if manifest.get('status') != 'NEURAL_AUDIO_GENERATED_REVIEW_PENDING':
            raise ValueError('The neural provider did not confirm generated speech')
        if len(manifest.get('segments', [])) != 1:  # This test must contain precisely its one segment.
            raise ValueError('Unexpected smoke-test segment count')
        record = manifest['segments'][0]  # Validate the generated waveform independently.
        audio = speech / 'runtime-ready.wav'
        if record['sha256'] != sha(audio) or record['text'] != SMOKE_TEXT:
            raise ValueError('Generated smoke audio or source text identity does not match')
        with wave.open(str(audio), 'rb') as reader:  # Inspect the PCM container itself.
            measured = {'channels': reader.getnchannels(), 'sampleWidthBytes': reader.getsampwidth(),
                        'sampleRate': reader.getframerate(), 'samples': reader.getnframes()}
        if (measured['channels'] != 1 or measured['sampleWidthBytes'] != 2
                or measured['sampleRate'] != 22050 or measured['samples'] <= 22050
                or measured['samples'] != record['samples']):  # Catch empty/wrong-format output.
            raise ValueError('Generated waveform failed measured format/sample-count checks')
        result = {  # Human acceptance is deliberately not inferred from synthesis.
            'status': 'OFFLINE_INSTALL_AND_NEURAL_SYNTHESIS_PASSED',
            'checkedAt': datetime.now(timezone.utc).isoformat(), 'distributionVersion': DIST_VERSION,
            'toolsRoot': str(tools), 'voiceRuntime': str(runtime), 'python': str(python),
            'inferenceExecuted': True, 'externalModelDownloadRequired': False,
            'smokeAudio': {'path': str(audio), 'sha256': sha(audio), **measured,
                           'durationSeconds': measured['samples'] / measured['sampleRate']},
            'naturalnessScore': None, 'humanApproval': 'PENDING', 'fullVideoRenderExecuted': False,
        }
        save(destination / 'INSTALL_AND_SMOKE_REPORT.json', result)  # Success only after waveform proof.
        return result
    except Exception as error:  # Retain a failed install for inspection; do not label it usable.
        save(destination / 'INSTALL_FAILED.json', {'status': 'INSTALL_OR_INFERENCE_FAILED',
             'error': str(error), 'humanApproval': 'NOT_PERFORMED'})
        raise  # Propagate the failure to the CLI and any calling workflow.


def fetch(root: Path = ROOT, url: str = RELEASE_URL) -> dict:
    """Download the runtime release asset, accepting it only if it matches the pinned SHA-256."""
    target = root / 'archives' / RUNTIME  # Same location verify() and install() read from.
    if target.is_file() and sha(target) == PINS[RUNTIME]:  # Already present and correct.
        return {'status': 'RUNTIME_ALREADY_PRESENT', 'path': str(target), 'sha256': PINS[RUNTIME]}
    if not url.startswith('https://'):  # Never accept plaintext transport.
        raise ValueError('Runtime URL must use https')
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + '.part')  # Never leave a partial file under the real name.
    with urllib.request.urlopen(url, timeout=60) as response, partial.open('wb') as handle:
        shutil.copyfileobj(response, handle, 1 << 20)  # Stream; the archive is ~232 MB.
    actual = sha(partial)
    if actual != PINS[RUNTIME]:  # A wrong download is deleted, not installed.
        partial.unlink()
        raise ValueError(f'Downloaded runtime SHA-256 mismatch: {actual}')
    partial.replace(target)  # Publish only a verified archive.
    return {'status': 'RUNTIME_FETCHED_AND_VERIFIED', 'path': str(target), 'sha256': actual, 'source': url}


def main() -> None:
    """Expose two explicit operations with structured results."""
    parser = argparse.ArgumentParser(description=__doc__)  # Display the local install boundary.
    parser.add_argument('command', choices=['verify', 'fetch', 'install'])  # Network is used only by the explicit fetch command.
    parser.add_argument('--out', type=Path)  # Installation requires a caller-selected directory.
    args = parser.parse_args()
    if args.command == 'install' and args.out is None:  # Avoid an inferred destination.
        parser.error('install requires --out NEW_DIRECTORY')
    result = {'verify': verify, 'fetch': fetch}.get(args.command, lambda: install(args.out))()
    print(json.dumps(result, indent=2))  # Keep stdout machine-readable.


if __name__ == '__main__':  # Do not execute while unit tests import the verifier.
    try:
        main()
    except Exception as error:
        print(json.dumps({'status': 'BOOTSTRAP_BLOCKED', 'error': str(error)}), file=sys.stderr)
        sys.exit(2)  # A missing voice dependency is never a successful narrated setup.

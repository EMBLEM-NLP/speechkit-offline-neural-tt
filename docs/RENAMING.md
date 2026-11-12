# SpeechKit Voice TTS Offline Pipeline migration

The canonical repository, Python distribution, command, plugin and MCP server name is
`speechkit-voice-tts-offline-pipeline`. The pipeline release is 1.3.1. The separately
delivered voice runtime remains component version 1.1.0 with its existing archive name and SHA-256.

## Existing projects and scripts

The `speechkit_narrate` Python package and `python -m speechkit_narrate` remain supported.
The installed `speechkit-narrate` command forwards to the same implementation as the new
`speechkit-voice-tts-offline-pipeline` command. Existing `SPEECHKIT_*` and `EMBLEM_*` settings,
project data directories, schemas and built-in stage source IDs remain compatible.

Source/dependency fingerprints may change during an upgrade. The planner can rerun a stage;
audio reuse still requires the existing synthesis-identity and file-integrity checks to pass.
Automated measurements do not replace listening approval.

## Python installation

Use a fresh virtual environment for the canonical wheel or source package:

```sh
python -m venv .venv
.venv/bin/python -m pip install /path/to/speechkit_voice_tts_offline_pipeline-1.3.1-py3-none-any.whl
.venv/bin/speechkit-voice-tts-offline-pipeline --json stages
.venv/bin/speechkit-narrate --json stages
```

On Windows the interpreter and commands are under `.venv\Scripts` instead of `.venv/bin`.
The Python import namespace stays `speechkit_narrate` even though the installed distribution has a new name.

If reusing an environment that contains the old `speechkit-narrate` distribution, uninstall that
distribution before installing the canonical package. The two distributions provide the same
import package and should not own those files concurrently. A compatibility command is not a
second pip distribution identity. Keep model/cache folders and project files when migrating.

Voice inference requires the provisioned Linux x86_64 runtime (or WSL on Windows).
Core preparation and project tools also run on macOS and Windows. Setup can download dependencies
and recognizers; inference uses local models after provisioning.

## PowerShell

```powershell
Import-Module .\powershell\SpeechKitVoiceTtsOfflinePipeline
Get-NarrationStage
```

`Import-Module .\powershell\SpeechKitNarrate` remains a forwarding entry point. Both use the
canonical implementation and the existing `*-Narration*` cmdlets. Generated objects carry
canonical and legacy type names, and formatting recognizes both. The canonical module keeps
its original module GUID; the forwarding module has a distinct GUID.

## MCP registration and plugins

The canonical MCP server ID is `speechkit-voice-tts-offline-pipeline`. Existing tool names and
`python -m speechkit_narrate mcp` launch commands remain valid.

`Register-NarrationMcpServer` uses the canonical default registration key. When it finds an
identified `speechkit-narrate` Python MCP entry, it migrates that default with a backup. If both
names already exist, or the old entry cannot be identified, it asks the caller to choose with
`-Name`; unrelated entries are preserved. Explicit `-Name speechkit-narrate` remains supported.

Plugins can advertise the `speechkit_voice_tts_offline_pipeline.plugins` entry-point group.
The old `speechkit_narrate.plugins` group remains supported. A module advertised through both
groups loads once.

## Repository URLs and releases

The canonical repository is <https://github.com/EMBLEM-NLP/speechkit-voice-tts-offline-pipeline>.
Update local Git remotes to that URL. The old repository URL redirects while the old name is not reused.

The historical v1.1.0 tag and runtime archive retain their original contents. That release is the
standalone runtime, not the full narration pipeline. New pipeline releases include a Python wheel
and source package; the source package contains the PowerShell modules, bootstrap and documentation.
The runtime archive retains its existing name:
`EMBLEM-NLP_Offline_Neural_Runtime_Linux_Py313.zip`.

Its SHA-256 remains:
`4aa2d0fa989b127c1c1f6c13d10774d6ad5b2b3f828b52d938d864c34ff64662`.

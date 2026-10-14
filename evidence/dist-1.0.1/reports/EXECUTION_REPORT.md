# Executed packaging repair — full bundle with voice 1.0.1

**The missing runtime is now included, verified, installed offline and used for real neural speech in this session.**

The previous Study Automation Tools ZIP was 2,726,666 bytes and contained only the tools/orchestrator. This new distribution includes that exact archive plus the 232,483,719-byte model/dependency archive, not a downloader stub.

## Preserved identities

- Tools: `ed28c91022a586d27ec6b7778889c65efe19f6371a507175f55e4a4c4eeef822`.
- Runtime: `4aa2d0fa989b127c1c1f6c13d10774d6ad5b2b3f828b52d938d864c34ff64662` — matches the narration plugin's runtime lock.
- Component versions: video/narration/RSI plugins 1.0.1; study orchestrator 1.0.0; Piper 1.8.0; ONNX Runtime 1.30.0; voice `en_US-ljspeech-high`.

## Actual checks

1. Both ZIP CRCs passed. The runtime's 33 internal checksum records all matched. The tools' 403 checksum-record comparisons also matched; overlapping parent/component ledgers mean that is not a count of 403 unique files.
2. Fifteen new bootstrap tests passed with no failures. Missing runtime, altered hashes, ledger omission, path traversal, duplicate members and symbolic links fail explicitly.
3. A candidate distribution ZIP was extracted into a new path containing spaces. From `/tmp`, the extracted bootstrap installed the model and seven wheel dependencies into a fresh isolated environment at a second path containing spaces.
4. The actual installer ran `pip --no-index --find-links --require-hashes`. No model download or paid TTS request was used.
5. The provider's real doctor command passed. It then generated a NEW 13.333605-second mono PCM16, 22,050-Hz speech sample: **294,006 samples**. This is not the historical `runner-smoke.wav` inside the original runtime.
6. The fresh waveform hash is `84a1ea0053b220b7739b3f769b46273d5e8da3623ea22857e1b12576196a3e4b`. The actual request, speech manifest, native WAV, install/preflight/synthesis logs and measured report are included.
7. Recursively inspected archived file names, including 12 nested archives: no font binary entries found. Original notices/source archives remain intact.

The final distribution is resealed after adding this run's reports. Bootstrap code and both input archives are unchanged from the executed extracted candidate. Final outer checksum/CRC verification is supplied separately.

## Scope

This is a packaging correction and local engine installation, not a new course or voice-quality release. It changes no teaching content, narration performance parameters, renderer, scoring contract, scheduled task or learner record. No previous 158-test RSI result is presented as a new run here.

The package is self-contained for the supplied voice engine/model and Python wheel dependencies, **not** Node, FFmpeg, Cairo, all graphics Python libraries, fonts, source PDFs or database. It targets compatible Linux x86_64 / CPython 3.13; other host platforms were not tested. Uploading a ZIP does not provision persistent scheduled infrastructure.

Human naturalness, pronunciation and learning acceptance remain unmeasured. The short generated sample verifies inference and packaging only.

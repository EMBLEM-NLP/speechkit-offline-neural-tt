# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Spoken forms for names from source code in narrated prose (project.json spoken.code_names).

speak(word) returns how a display word that names code should be read, or None when the voice can read it as it is:
snake_case, dotted module and file names, paths, command-line options, camelCase and PowerShell cmdlet names are
split into words; file extensions and abbreviations get fixed readings (FIX). spoken(word) is the same reading with
the word's quotes, brackets and trailing punctuation kept around it, which is what the script builder uses.
build_rules(texts) returns the same readings as exact-match spoken.say rules, for a project that wants to review or
edit them by hand.

    python -m speechkit_narrate.text.codenames BOOK.md     prints every word that gets a reading, with the reading
"""
import re
import sys

# exact readings, looked up as written and then in lower case
FIX = {
    "dir": "directory", "dirs": "directories", "onnx": "O N N X", "asr": "A S R", "aac": "A A C", "cli": "C L I", "ugos": "U G O S",
    "pymupdf": "pie M U P D F", "fitz": "fits", "cmdlet": "command-let", "cmdlets": "command-lets", "argv": "arg V",
    "kwargs": "keyword args", "idx": "index", "tmp": "temp", "ai": "A I", "ui": "U I", "id": "I D", "ids": "I Ds",
    "json": "J son", "jsonl": "J son L", "py": "pie", "md": "M D", "toml": "tommel", "yaml": "yammel",
    "yml": "yammel", "html": "H T M L", "css": "C S S", "js": "J S", "mp4": "M P 4", "wav": "wave", "wavs": "waves",
    "m4a": "M 4 A", "mp3": "M P 3", "png": "P N G", "jpg": "J peg", "jpeg": "J peg", "svg": "S V G", "pdf": "P D F",
    "pdfs": "P D Fs", "epub": "E pub", "txt": "text", "xml": "X M L", "ps1": "P S 1", "psm1": "P S M 1",
    "psd1": "P S D 1", "ps1xml": "P S 1 X M L", "sh": "S H", "exe": "E X E", "srt": "S R T", "vtt": "V T T",
    "whl": "wheel", "ini": "I N I", "cfg": "config", "db": "D B", "stdin": "standard in", "stdout": "standard out",
    "stderr": "standard error", "str": "string", "repr": "repper", "ffmpeg": "F F mpeg", "ffprobe": "F F probe",
    "espeak": "E speak", "espeak-ng": "E speak N G", "pwsh": "P W S H", "wsl": "W S L", "npm": "N P M",
    "venv": "V env", "utf": "U T F", "utf-8": "U T F 8", "utf8": "U T F 8", "sha256": "shah 256",
    "sha-256": "shah 256", "ok": "O K", "e.g.": "for example", "i.e.": "that is", "gui": "G U I", "api": "A P I",
    "apis": "A P Is", "url": "U R L", "urls": "U R Ls", "http": "H T T P", "https": "H T T P S", "csv": "C S V",
    "ipa": "I P A", "tts": "T T S", "ocr": "O C R", "mcp": "M C P", "nas": "naz", "rsi": "R S I",
    "arpabet": "arpa bet", "cmu": "C M U", "ctc": "C T C", "wer": "W E R", "dbfs": "D B F S", "db.": "D B",
    "rms": "R M S", "pcm": "P C M", "hz": "hertz", "khz": "kilohertz", "fps": "frames per second",
    "kbit/s": "kilobits per second", "mib": "mebibytes", "kib": "kibibytes", "gib": "gibibytes", "wcag": "W C A G",
    "dom": "dom", "csp": "C S P", "ios": "I O S", "macos": "mac O S", "gpu": "G P U", "cpu": "C P U",
    "cpus": "C P Us", "nvme": "N V M E", "vaapi": "V A A P I", "qsv": "Q S V", "libx264": "lib X 264",
    "h264": "H 264", "h.264": "H 264", "hevc": "H E V C", "psgallery": "P S gallery", "winget": "win get",
    "nuget": "new get", "ugreen": "you green", "dockerfile": "docker file", "oci": "O C I", "ljspeech": "L J speech",
    "nemo": "nemo", "pikepdf": "pike P D F", "pypdf": "pie P D F", "weasyprint": "weasy print", "numpy": "num pie",
    "spacy": "spacey", "matplotlib": "mat plot lib", "onnxruntime": "O N N X runtime", "r2": "R 2", "rgb": "R G B",
    "hsl": "H S L", "oklab": "O K lab", "iife": "I I F E", "async": "a sync", "eof": "E O F", "crlf": "C R L F",
    "lf": "L F", "ascii": "askey", "unicode": "unicode", "nfkd": "N F K D", "nfc": "N F C", "posix": "posix",
    "uuid": "U U I D", "env": "env", "mkdir": "make directory", "chmod": "C H mod", "cwd": "C W D", "pid": "P I D",
    "jsonschema": "J son schema", "github": "git hub", "regex": "regex", "regexes": "regexes", "args": "args",
    "kbps": "kilobits per second", "lufs": "L U F S", "ebu": "E B U", "lra": "L R A", "dpi": "D P I",
    "pt": "points", "px": "pixels", "em": "em", "rem": "rem", "vw": "V W", "vh": "V H", "ms": "milliseconds",
    "kb": "kilobytes", "mb": "megabytes", "gb": "gigabytes", "ffmpeg's": "F F mpeg's", "pdf's": "P D F's",
    "json's": "J son's", "piper's": "piper's", "imagemagick": "image magick", "tesseract": "tesseract",
    "parakeet": "parakeet", "sherpa": "sherpa", "pester": "pester", "isbn": "I S B N", "doi": "D O I",
    "xhtml": "X H T M L", "smil": "smile", "ncx": "N C X", "opf": "O P F", "mathml": "math M L", "aria": "aria",
    "srgb": "S R G B", "icc": "I C C", "cmyk": "C M Y K", "pdf/a": "P D F A", "pdf/x": "P D F X", "tz": "T Z",
    "iso": "I S O", "rfc": "R F C", "toc": "T O C", "lru": "L R U", "kv": "K V", "ipc": "I P C", "tcp": "T C P",
    "ssh": "S S H", "tls": "T L S", "ca": "C A", "cdn": "C D N", "dns": "D N S", "smb": "S M B", "nfs": "N F S",
    "vm": "V M", "vms": "V Ms", "lts": "L T S", "ram": "ram", "ssd": "S S D", "hdd": "H D D", "raid": "raid",
    "speechkit": "speech kit", "speechkit-narrate": "speech kit narrate", "speechkit_narrate": "speech kit narrate", "bookcast": "book cast", "ch": "C H",
    "speechkit-voice-tts-offline-pipeline": "speech kit voice T T S offline pipeline",
    "speechkit_voice_tts_offline_pipeline": "speech kit voice T T S offline pipeline",
    "cmd": "command", "bg": "background", "th": "T H", "impl": "imple", "src": "source", "dst": "destination",
    "og": "O G", "ctx": "context", "getattr": "get atter", "setattr": "set atter", "getitem": "get item",
    "setitem": "set item", "h1": "H 1", "h2": "H 2", "h3": "H 3", "pyproject": "pie project",
    "fontconfig": "font config", "jsonable": "J son able", "wslpath": "W S L path", "docmodel": "doc model",
    "winpaths": "win paths", "emblem-nlp": "Emblem N L P", "emblem-nlp's": "Emblem N L P's", "kbps": "kilobits per second",
    "lstrip": "L strip", "rstrip": "R strip", "isinstance": "is instance", "hasattr": "has atter", "enumerate": "enumerate",
    "len": "len", "elif": "else if", "argparse": "arg parse", "subparser": "sub parser", "subparsers": "sub parsers",
    "dataclass": "data class", "namedtuple": "named tuple", "sys": "sys", "os": "O S", "re": "R E",
    "ocred": "O C R'd", "uint8": "U int 8", "latex": "lay tech", "0xrrggbb": "hex R R G G B B",
    # book.md keywords written in capitals are read as words (exact case only, so ordinary words are untouched)
    "ROW": "row", "NOTE": "note", "LINK": "link", "DESC": "desc", "TABLE": "table", "FIGURE": "figure",
}
# capital pieces of an ALL_CAPS snake name that are spelled; any other piece is read as a word (HOME, PATH, MIN)
ACRONYMS = {"API", "URL", "PDF", "MCP", "NAS", "OCR", "TTS", "ASR", "CPU", "GPU", "UI", "ID", "IPA", "CSS", "HTML",
            "JSON", "WAV", "AAC", "RMS", "DB", "LJ", "CMU", "CTC", "WER", "PS", "TOC", "UTF", "SHA", "CLI", "RSI",
            "DPI", "FPS", "EOF", "MP4", "PCM", "WSL", "NFKD", "IO", "OS", "TZ", "CA", "TLS", "SSH", "MD", "PNG", "SVG",
            "XML", "CSV", "HTTP", "HTTPS", "AI", "LF", "CRLF", "ISO", "RGB", "MIB", "KIB", "DBFS"}
EXT = {"py", "json", "jsonl", "md", "toml", "yaml", "yml", "html", "css", "js", "mp4", "wav", "m4a", "mp3", "png",
       "jpg", "jpeg", "svg", "pdf", "epub", "txt", "xml", "ps1", "psm1", "psd1", "ps1xml", "sh", "exe", "srt", "vtt",
       "whl", "ini", "cfg", "zip", "onnx", "log", "lock", "example", "env", "csv", "tsv", "ttf", "otf", "woff2", "bin",
       "gz", "tar", "db", "sqlite", "pkl", "npy", "dockerignore", "gitignore"}
LEAD = "\"'“‘(["          # opening quotes and brackets kept outside the reading
TRAIL = ")]\"'”’.,;:!?"   # closing quotes, brackets and sentence punctuation kept outside the reading
NUMBER = re.compile(r"^[-+]?\d[\d,]*(\.\d+)?%?$")


def camel(word: str) -> list:
    """Split camelCase and PascalCase, keeping runs of capitals (HTMLParser -> HTML Parser) and digits."""
    return re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", word)


def piece(p: str) -> str:
    """Reading of one simple piece (no separators)."""
    if not p:
        return ""
    for k in (p, p.lower()):
        if k in FIX:
            return FIX[k]
    if p.isdigit():
        return p
    if re.fullmatch(r"[A-Z]{2,5}s", p):  # a plural acronym: PNGs, URLs, CPUs
        return " ".join(p[:-1]) + "s"
    parts = camel(p)
    if len(parts) > 1:
        return " ".join(piece(x) for x in parts)
    if p.isupper() and len(p) <= 4 and p.isalpha():  # a short acronym: spelled letter by letter
        return " ".join(p)
    return p.lower() if not p.isupper() else p.lower()


def speak(word: str) -> str | None:
    """Reading of a display word that names code, or None when it needs no rule."""
    dotted = word.strip(LEAD).rstrip(",;:!?)]\"'”’")  # keeps a final dot, for e.g. and i.e.
    if dotted in FIX or dotted.lower() in FIX and dotted.endswith("."):
        return FIX.get(dotted) or FIX[dotted.lower()]
    core = word.strip(LEAD).rstrip(TRAIL)
    if not core or NUMBER.match(core) or not re.search(r"[A-Za-z0-9]", core):  # bare punctuation: the toolkit's own
        return None  # rules handle it (a free-standing dash is silent)
    if re.fullmatch(r"#[0-9a-fA-F]{3,8}", core):  # a hex colour such as #2a241c
        return "hex " + " ".join(core[1:].upper())
    if re.fullmatch(r"\d+(\.\d+){2,}", core):  # a version such as 2.3.0
        return " point ".join(core.split("."))
    low = core.lower()
    if core in FIX or low in FIX:
        return FIX.get(core) or FIX[low]
    needs = (re.search(r"[_./\\]", core) or core.startswith("-") or re.search(r"[a-z][A-Z]", core)
             or re.search(r"[A-Z]{2,}[a-z]", core) or re.fullmatch(r"[A-Z][a-z]+-[A-Z]\w*", core)
             or re.search(r"[a-zA-Z]\d|\d[a-zA-Z]", core) and not re.fullmatch(r"\d+(st|nd|rd|th|s)", core))
    if not needs:
        return None
    return reading(core)


def reading(core: str) -> str:
    m = re.fullmatch(r"(.+?)['’]s", core)  # a possessive: the name's reading, then 's
    if m and len(m.group(1)) > 1:
        return reading(m.group(1)) + "'s"
    out = []
    if core.startswith("--"):
        out.append("dash dash")
        core = core[2:]
    elif core.startswith("-") and len(core) > 1:
        out.append("dash")
        core = core[1:]
    if core.startswith("./") or core.startswith(".\\"):
        out.append("dot slash" if core[1] == "/" else "dot backslash")
        core = core[2:]
    # paths: slash or backslash between parts
    if "/" in core or "\\" in core:
        parts = re.split(r"([/\\])", core)
        for x in parts:
            if x == "/":
                out.append("slash")
            elif x == "\\":
                out.append("backslash")
            elif x:
                out.append(reading(x))
        return " ".join(s for s in out if s)
    # dotted names: a leading dot (.env), the parts, and a known extension at the end
    if "." in core.strip("."):
        lead = core.startswith(".")
        parts = core.strip(".").split(".")
        if lead:
            out.append("dot")
        said = []
        for i, x in enumerate(parts):
            said.append(FIX.get(x.lower(), x.lower()) if (i == len(parts) - 1 and x.lower() in EXT) else reading(x))
        out.append(" dot ".join(s for s in said if s))
        return " ".join(s for s in out if s)
    if core.startswith(".") and len(core) > 1:
        out.append("dot " + reading(core[1:]))
        return " ".join(out)
    # snake_case and dunder names
    if "_" in core:
        m = re.fullmatch(r"__(\w+?)__", core)
        if m:
            out.append("dunder " + reading(m.group(1)))
            return " ".join(out)
        lead = len(core) - len(core.lstrip("_"))
        if lead:
            out.append("underscore")
        words = [w for w in core.strip("_").split("_") if w]
        if core.isupper():  # an ALL_CAPS name (a constant or an environment variable): its pieces are words
            out.append(" ".join(FIX.get(w.lower()) or (" ".join(w) if w in ACRONYMS and w.isalpha() else w.lower())
                                for w in words))
        else:
            out.append(" ".join(piece(w) for w in words))
        return " ".join(s for s in out if s)
    # PowerShell Verb-Noun, hyphenated names
    if "-" in core:
        out.append(" ".join(piece(w) for w in core.split("-") if w))
        return " ".join(s for s in out if s)
    out.append(piece(core))
    return " ".join(s for s in out if s)


# The reading of one display word with its leading quotes or brackets and its trailing punctuation kept, or None
# when the word needs no reading. Used by text/docmodel.py for every word when spoken.code_names is on.
def spoken(word: str) -> str | None:
    r = speak(word)
    core = word.strip(LEAD).rstrip(TRAIL)
    if r is None or r == core:
        return None
    lead = word[:len(word) - len(word.lstrip(LEAD))]  # the quotes and brackets before the name
    dotted = word.strip(LEAD).rstrip(",;:!?)]\"'”’")  # an abbreviation such as e.g. keeps its own final dot
    if dotted.endswith(".") and (dotted in FIX or dotted.lower() in FIX):
        return lead + r + word[len(lead) + len(dotted):]  # the dot belongs to the abbreviation, not the sentence
    trail = word[len(word.rstrip(TRAIL)):]  # the punctuation after it
    return lead + r + trail


def build_rules(texts: list) -> tuple:
    """One rule per distinct word that needs a reading. Returns (rules, {core: reading})."""
    seen = {}
    for t in texts:
        for w in t.split():
            core = w.strip(LEAD).rstrip(TRAIL)
            if core in seen or not core:
                continue
            r = speak(w)
            if r is not None and r != core:
                seen[core] = r
    rules = []
    for core in sorted(seen, key=lambda c: (-len(c), c)):
        rules.append({"match": "^([\"'“‘(\\[]*)" + re.escape(core) + "([)\\]\"'”’.,;:!?]*)$",
                      "say": "\\g<1>" + seen[core].replace("\\", "\\\\") + "\\g<2>"})
    return rules, seen


if __name__ == "__main__":
    text = open(sys.argv[1], encoding="utf-8").read()
    # only narrated text: drop fenced code blocks
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    rules, seen = build_rules([text])
    for core, r in sorted(seen.items(), key=lambda x: x[0].lower()):
        print(f"{core:40s} {r}")
    print(len(rules), "rules")

"""Build the legal-studies conversations with cached Hebrew speech segments."""
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.parse import quote, unquote

import edge_tts

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent
CACHE = ROOT / ".cache"
OUTPUT = ROOT / "mp3"
VOICES = {"ש": "he-IL-HilaNeural", "א": "he-IL-AvriNeural"}
RATE = "+0%"
AUDIO = ["-c:a", "libmp3lame", "-b:a", "64k", "-ar", "24000", "-ac", "1"]
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
STRIP_NIKUD = re.compile(r"[\u0591-\u05c7]")


def command(args):
    result = subprocess.run(args, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace")[-2000:])
    return result.stdout


def duration(path):
    value = command([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                     "-of", "csv=p=0", str(path)])
    seconds = float(value.strip())
    if seconds <= 0:
        raise ValueError("Empty audio: " + str(path))
    return seconds


def cache_file(kind, key):
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return CACHE / (kind + "_" + digest + ".mp3")


def tts_safe(text):
    # Avoid extra syllables produced by non-BKP dagesh in the Hebrew voices.
    return re.sub(r"([א-ת])([\u05b0-\u05bb\u05c1\u05c2]*)\u05bc",
                  lambda m: m.group(0) if m[1] in "בכפ" else m[1] + m[2], text)


def parse(path):
    parts, title, pending = [], path.stem, 0.0
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line.startswith("# title:"):
            title = line.split(":", 1)[1].strip()
        elif not line:
            pending = max(pending, 0.9)
        elif line.startswith("#"):
            continue
        elif re.fullmatch(r"~\d+(?:\.\d+)?", line):
            pending = max(pending, float(line[1:]))
        else:
            match = re.fullmatch(r"([אש])[\u0591-\u05c7]*\s*:\s*(.+)", line)
            if not match:
                raise ValueError(path.name + ": unrecognized line " + line[:100])
            if parts:
                parts.append(("gap", pending or 0.45))
            parts.append(("say", VOICES[match[1]], tts_safe(match[2])))
            pending = 0.0
    if not parts:
        raise ValueError("No speech in " + str(path))
    return title, parts


async def synth_one(voice, text, target, semaphore):
    async with semaphore:
        for attempt in range(5):
            temporary = target.with_suffix(".mp3.writing")
            try:
                await edge_tts.Communicate(text, voice, rate=RATE).save(str(temporary))
                if temporary.stat().st_size < 500:
                    raise ValueError("Empty speech response")
                temporary.replace(target)
                return
            except Exception as error:
                if attempt == 4:
                    raise RuntimeError("Speech failed: " + text[:80]) from error
                await asyncio.sleep(2 * (attempt + 1))


async def synth_jobs(jobs):
    semaphore = asyncio.Semaphore(6)
    completed = 0
    async def one(job):
        nonlocal completed
        await synth_one(*job, semaphore)
        completed += 1
        if completed % 12 == 0 or completed == len(jobs):
            print("  Speech segments: " + str(completed) + "/" + str(len(jobs)), flush=True)
    await asyncio.gather(*(one(job) for job in jobs))


def silence(seconds):
    target = cache_file("silence", str(seconds))
    if not target.exists():
        command([FFMPEG, "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                 "anullsrc=r=24000:cl=mono", "-t", str(seconds), *AUDIO, str(target)])
    return target


def build(path):
    title, parts = parse(path)
    number = path.name[:2]
    target = OUTPUT / (number + " - " + title + ".mp3")
    signature = hashlib.sha256(json.dumps([title, parts, RATE, AUDIO], ensure_ascii=False).encode("utf-8")).hexdigest()
    stamp = CACHE / ("build_" + path.stem + ".sha256")
    changed = not stamp.exists() or stamp.read_text(encoding="utf-8").strip() != signature
    jobs, pieces = {}, []
    for part in parts:
        if part[0] == "gap":
            pieces.append(silence(part[1]))
        else:
            _, voice, text = part
            piece = cache_file("speech", voice + "|" + RATE + "|" + text)
            pieces.append(piece)
            if not piece.exists():
                jobs[str(piece)] = (voice, text, piece)
    print("Building " + path.name + ": " + str(len(jobs)) + " new speech segments", flush=True)
    if jobs:
        asyncio.run(synth_jobs(list(jobs.values())))
    if changed or not target.exists():
        listing = CACHE / ("concat_" + number + ".txt")
        listing.write_text("".join("file '" + str(p).replace("\\", "/").replace("'", "'\\''") + "'\n"
                                   for p in pieces), encoding="utf-8")
        temporary = OUTPUT / (target.stem + ".writing.mp3")
        command([FFMPEG, "-nostdin", "-v", "error", "-y", "-f", "concat", "-safe", "0",
                 "-i", str(listing), *AUDIO,
                 "-metadata", "title=" + number + " · " + title,
                 "-metadata", "album=היבטים משפטיים בניהול — הכנה למבחן המסכם",
                 "-metadata", "artist=שיחות לימוד", "-metadata", "track=" + str(int(number)),
                 "-metadata", "genre=Education", "-id3v2_version", "3", str(temporary)])
        temporary.replace(target)
        stamp.write_text(signature + "\n", encoding="utf-8")
    plain = ROOT / "scripts" / "plain" / path.name
    spoken = [p[2] for p in parts if p[0] == "say"]
    row = {
        "id": path.stem, "number": int(number), "title": title,
        "duration": round(duration(target), 3),
        "src": "mp3/" + quote(target.name),
        "transcript": "scripts/plain/" + quote(path.name),
        "speech_segments": len(spoken),
        "words": sum(len(STRIP_NIKUD.sub("", s).split()) for s in spoken),
        "bytes": target.stat().st_size,
    }
    if not plain.exists():
        raise ValueError("Plain transcript is missing")
    print("READY " + str(row["number"]) + ": " + str(round(row["duration"] / 60, 1)) + " minutes", flush=True)
    return row


def bundle(episodes):
    target = ROOT / "legal-podcasts.zip"
    temporary = target.with_suffix(".zip.writing")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for episode in episodes:
            for key in ("src", "transcript"):
                relative = unquote(episode[key])
                source = (ROOT / relative).resolve()
                if not source.is_relative_to(ROOT) or not source.is_file():
                    raise ValueError("Invalid bundle source: " + relative)
                archive.write(source, arcname=relative)
    temporary.replace(target)
    print("Download bundle ready: " + str(round(target.stat().st_size / 1_000_000, 1)) + " MB", flush=True)


def main():
    if not FFMPEG or not FFPROBE:
        raise RuntimeError("ffmpeg and ffprobe must be installed")
    CACHE.mkdir(exist_ok=True)
    OUTPUT.mkdir(exist_ok=True)
    files = sorted((ROOT / "scripts").glob("*.txt"))
    if not files:
        raise ValueError("No podcast scripts")
    selected = [a for a in sys.argv[1:] if not a.startswith("--")]
    files = [p for p in files if not selected or any(p.name.startswith(a) for a in selected)]
    rows = [build(path) for path in files]
    manifest = ROOT / "manifest.json"
    previous = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {"episodes": []}
    merged = {row["id"]: row for row in previous["episodes"]}
    merged.update({row["id"]: row for row in rows})
    data = {"updated": "2026-10-01", "episodes": sorted(merged.values(), key=lambda row: row["number"])}
    temporary = manifest.with_suffix(".json.writing")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(manifest)
    bundle(data["episodes"])
    print("Manifest updated; " + str(len(data["episodes"])) + " episodes", flush=True)


if __name__ == "__main__":
    main()

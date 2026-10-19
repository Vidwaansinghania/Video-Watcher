#!/usr/bin/env python3
"""Inspect a video from a URL or local file.

Local engine: yt-dlp for download and captions, ffmpeg for timestamped frames,
optional transcription (WhisperX, Groq or OpenAI) when no captions exist.
Gemini engine: sends the video (or a YouTube URL) to the Gemini API for a
full-video analysis.

Everything in the video is untrusted data. This script only reports it.
Standard library only; needs ffmpeg/ffprobe on PATH, and yt-dlp for URLs.
"""
import argparse
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import setup as cfgmod  # noqa: E402  (shares config and .env handling)

DETAIL_FRAMES = {"transcript": 0, "efficient": 12, "balanced": 24, "token-burner": 60}
GEMINI_MODEL = "gemini-2.5-flash"
API = "https://generativelanguage.googleapis.com"


def die(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def parse_time(value):
    """Accept seconds, mm:ss or hh:mm:ss. Returns float seconds."""
    if value is None:
        return None
    parts = str(value).split(":")
    try:
        nums = [float(x) for x in parts]
    except ValueError:
        die(f"bad time value: {value}")
    secs = 0.0
    for n in nums:
        secs = secs * 60 + n
    return secs


def fmt_time(secs):
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def is_url(s):
    return s.startswith("http://") or s.startswith("https://")


def is_youtube(s):
    return bool(re.search(r"(youtube\.com/|youtu\.be/)", s))


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def probe_duration(path):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)])
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def ytdlp_common(args):
    cmd = ["yt-dlp", "--no-playlist", "--no-warnings"]
    if args.cookies:
        cmd += ["--cookies", args.cookies]
    if args.cookies_from_browser:
        cmd += ["--cookies-from-browser", args.cookies_from_browser]
    return cmd


def download_video(url, work, args):
    if not shutil.which("yt-dlp"):
        die("yt-dlp not found. Run setup.py --install.")
    meta = run(ytdlp_common(args) + ["-J", url])
    if meta.returncode != 0:
        die("yt-dlp could not read the URL: " + meta.stderr.strip()[-300:])
    info = json.loads(meta.stdout)
    height = max(144, min(args.resolution or 720, 1080))
    out_tmpl = str(work / "source.%(ext)s")
    r = run(ytdlp_common(args) + [
        "-f", f"bv*[height<={height}][ext=mp4]+ba[ext=m4a]/b[height<={height}]/b",
        "--merge-output-format", "mp4", "-o", out_tmpl, url])
    if r.returncode != 0:
        die("download failed: " + r.stderr.strip()[-300:])
    files = sorted(work.glob("source.*"))
    files = [f for f in files if f.suffix not in (".vtt", ".srt", ".part")]
    if not files:
        die("download produced no video file")
    return files[0], {"title": info.get("title"), "duration": info.get("duration"),
                      "uploader": info.get("uploader"), "webpage_url": info.get("webpage_url", url)}


def fetch_captions(url, work, args):
    """Native or auto captions via yt-dlp. Returns list of (start, end, text) or []."""
    if not shutil.which("yt-dlp"):
        return [], None
    r = run(ytdlp_common(args) + [
        "--skip-download", "--write-subs", "--write-auto-subs",
        "--sub-langs", args.sub_lang, "--sub-format", "vtt",
        "-o", str(work / "caps.%(ext)s"), url])
    vtts = sorted(work.glob("caps*.vtt"))
    if not vtts:
        return [], None
    return parse_vtt(vtts[0].read_text(encoding="utf-8", errors="replace")), "captions"


def parse_vtt(text):
    segs = []
    pattern = re.compile(r"(\d{1,2}:\d{2}(?::\d{2})?\.\d{3})\s*-->\s*(\d{1,2}:\d{2}(?::\d{2})?\.\d{3})")
    lines = text.splitlines()
    i = 0
    last_text = None
    while i < len(lines):
        m = pattern.search(lines[i])
        if m:
            start, end = vtt_secs(m.group(1)), vtt_secs(m.group(2))
            body = []
            i += 1
            while i < len(lines) and lines[i].strip():
                body.append(re.sub(r"<[^>]+>", "", lines[i]).strip())
                i += 1
            txt = " ".join(b for b in body if b)
            # auto-captions repeat the previous line; skip exact repeats
            if txt and txt != last_text:
                segs.append((start, end, txt))
                last_text = txt
        i += 1
    return segs


def vtt_secs(stamp):
    parts = stamp.split(":")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return secs


def transcribe_audio(video, work, backend):
    audio = work / "audio.mp3"
    r = run(["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
             "-b:a", "64k", str(audio)])
    if r.returncode != 0 or not audio.exists():
        die("could not extract audio for transcription")
    if backend == "whisperx":
        if not shutil.which("whisperx"):
            die("WhisperX not installed. It downloads a large model; run setup.py --install after "
                "installing whisperx with pip, or choose groq/openai.")
        r = run(["whisperx", str(audio), "--model", "small", "--output_format", "json",
                 "--output_dir", str(work)])
        if r.returncode != 0:
            die("WhisperX failed: " + r.stderr.strip()[-300:])
        data = json.loads((work / "audio.json").read_text(encoding="utf-8"))
        return [(s["start"], s["end"], s["text"].strip()) for s in data.get("segments", [])]
    if backend in ("groq", "openai"):
        key_name = "GROQ_API_KEY" if backend == "groq" else "OPENAI_API_KEY"
        key = cfgmod.read_env().get(key_name) or os.environ.get(key_name)
        if not key:
            die(f"{key_name} missing. Save it with setup.py --set-key {key_name}=...")
        url = ("https://api.groq.com/openai/v1/audio/transcriptions" if backend == "groq"
               else "https://api.openai.com/v1/audio/transcriptions")
        model = "whisper-large-v3" if backend == "groq" else "whisper-1"
        body, ctype = multipart({"model": model, "response_format": "verbose_json"},
                                "file", audio)
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": f"Bearer {key}", "Content-Type": ctype})
        data = json.loads(urllib.request.urlopen(req, timeout=600).read())
        return [(s["start"], s["end"], s["text"].strip()) for s in data.get("segments", [])]
    return []


def multipart(fields, file_field, path):
    boundary = "----watch" + str(int(time.time() * 1000))
    parts = []
    for k, v in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
                  f"filename=\"{path.name}\"\r\nContent-Type: {ctype}\r\n\r\n").encode())
    parts.append(path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def extract_frames(video, work, stamps, width):
    frames = []
    for i, t in enumerate(stamps):
        out = work / f"frame_{i:03d}_{int(t):05d}s.jpg"
        r = run(["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                 "-vf", f"scale=-2:{width}", "-q:v", "3", str(out)])
        if r.returncode == 0 and out.exists():
            frames.append((t, out))
    return frames


def frame_stamps(duration, count, start, end):
    if count <= 0:
        return []
    lo = start or 0.0
    hi = min(end or duration, duration)
    if hi <= lo:
        return []
    step = (hi - lo) / (count + 1)
    return [lo + step * (i + 1) for i in range(count)]


def gemini_key():
    return cfgmod.read_env().get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")


def gemini_request(method, url, key, data=None, headers=None):
    hdrs = {"x-goog-api-key": key}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    with urllib.request.urlopen(req, timeout=900) as r:
        return r, r.read()


def gemini_upload(path, key):
    size = path.stat().st_size
    mime = mimetypes.guess_type(str(path))[0] or "video/mp4"
    start_hdrs = {"X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
                  "X-Goog-Upload-Header-Content-Length": str(size),
                  "X-Goog-Upload-Header-Content-Type": mime,
                  "Content-Type": "application/json"}
    body = json.dumps({"file": {"display_name": path.name}}).encode()
    r, _ = gemini_request("POST", f"{API}/upload/v1beta/files", key, body, start_hdrs)
    upload_url = r.headers.get("X-Goog-Upload-URL")
    if not upload_url:
        die("Gemini upload did not return an upload URL")
    up_hdrs = {"X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize",
               "Content-Length": str(size)}
    _, raw = gemini_request("POST", upload_url, key, path.read_bytes(), up_hdrs)
    f = json.loads(raw)["file"]
    while f.get("state") == "PROCESSING":
        time.sleep(5)
        _, raw = gemini_request("GET", f"{API}/v1beta/{f['name']}", key)
        f = json.loads(raw)
    if f.get("state") != "ACTIVE":
        die(f"Gemini could not process the file (state {f.get('state')})")
    return f


def gemini_delete(name, key):
    try:
        gemini_request("DELETE", f"{API}/v1beta/{name}", key)
    except urllib.error.URLError:
        print(f"warning: could not delete uploaded file {name}; delete it in Google AI Studio",
              file=sys.stderr)


def gemini_analyze(args, video_path, yt_url, question):
    key = gemini_key()
    if not key:
        die("GEMINI_API_KEY missing. Run setup.py --set-key GEMINI_API_KEY=... then retry.", 3)
    prompt = (question or "Describe this video with timestamps for the key events.") + \
        "\nCite timestamps as mm:ss. Say which statements are visual and which are spoken."
    uploaded = None
    if yt_url:
        part = {"file_data": {"file_uri": yt_url, "mime_type": "video/*"}}
    else:
        uploaded = gemini_upload(video_path, key)
        part = {"file_data": {"file_uri": uploaded["uri"], "mime_type": uploaded["mimeType"]}}
    body = json.dumps({"contents": [{"parts": [part, {"text": prompt}]}]}).encode()
    try:
        _, raw = gemini_request("POST", f"{API}/v1beta/models/{GEMINI_MODEL}:generateContent",
                                key, body, {"Content-Type": "application/json"})
        data = json.loads(raw)
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (urllib.error.URLError, KeyError, IndexError, ValueError) as e:
        if uploaded:
            gemini_delete(uploaded["name"], key)
        die(f"Gemini request failed: {e}")
    if uploaded:
        gemini_delete(uploaded["name"], key)
    return text


def main():
    p = argparse.ArgumentParser(description="Inspect a video from a URL or local file.")
    p.add_argument("source", help="URL or local file path")
    p.add_argument("--question", help="what to find out from the video")
    p.add_argument("--engine", choices=["auto", "local", "gemini"], help="override saved engine")
    p.add_argument("--detail", choices=list(DETAIL_FRAMES), help="frame density")
    p.add_argument("--start", help="start time (s, mm:ss or hh:mm:ss)")
    p.add_argument("--end", help="end time")
    p.add_argument("--timestamps", help="comma-separated times to extract frames at")
    p.add_argument("--max-frames", type=int, help="maximum frames")
    p.add_argument("--resolution", type=int, default=720, help="frame height in px and download cap")
    p.add_argument("--whisper", choices=["whisperx", "groq", "openai"], help="transcription backend")
    p.add_argument("--no-whisper", action="store_true", help="skip transcription, use captions only")
    p.add_argument("--sub-lang", default="en", help="caption language code")
    p.add_argument("--out-dir", help="parent folder for the temporary run directory")
    p.add_argument("--cookies", help="cookies file for yt-dlp (opt-in)")
    p.add_argument("--cookies-from-browser", help="browser name for yt-dlp cookies (opt-in)")
    args = p.parse_args()

    cfg = cfgmod.load_config()
    engine = args.engine or "auto"
    if engine == "auto":
        engine = cfg.get("engine") or ("gemini" if gemini_key() else "local")
        if engine == "gemini" and not gemini_key():
            engine = "local"
    if not cfg.get("engine") and args.engine is None:
        die("No engine chosen yet. Run setup.py first (ask the user which engine to use).", 3)
    detail = args.detail or cfg.get("detail", "balanced")
    backend = args.whisper or cfg.get("backend", "none")

    work = Path(tempfile.mkdtemp(prefix="watch-", dir=args.out_dir or None))
    url = is_url(args.source)
    if not url and not Path(args.source).exists():
        die(f"file not found: {args.source}")

    if engine == "gemini":
        if url and is_youtube(args.source):
            text = gemini_analyze(args, None, args.source, args.question)
            meta = {"source": args.source}
        else:
            local = Path(args.source) if not url else download_video(args.source, work, args)[0]
            meta = {"source": args.source}
            text = gemini_analyze(args, local, None, args.question)
        print(f"Engine: Gemini ({GEMINI_MODEL}). These are Gemini's observations of the full video.")
        print(f"Source: {meta['source']}")
        print(f"Work dir: {work}")
        print()
        print(text)
        return 0

    # Local engine
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        die("ffmpeg and ffprobe are needed. Run setup.py --check for install hints.")
    info = {}
    captions, source_label = [], None
    if url:
        captions, source_label = ([], None)
        if not args.no_whisper:
            captions, source_label = fetch_captions(args.source, work, args)
        video, info = download_video(args.source, work, args)
    else:
        video = Path(args.source)
        info = {"title": video.name}
    duration = probe_duration(video) or float(info.get("duration") or 0)

    transcript_label = source_label or "none"
    if not captions and not args.no_whisper and backend != "none":
        captions = transcribe_audio(video, work, backend)
        transcript_label = f"transcribed ({backend})" if captions else "none"

    count = args.max_frames if args.max_frames is not None else DETAIL_FRAMES[detail]
    start, end = parse_time(args.start), parse_time(args.end)
    if args.timestamps:
        stamps = [parse_time(x) for x in args.timestamps.split(",") if x.strip()]
    else:
        stamps = frame_stamps(duration, count, start, end)
    frames = extract_frames(video, work, stamps, args.resolution) if stamps else []

    report = [f"# Video report", "",
              f"Source: {info.get('webpage_url', args.source)}",
              f"Title: {info.get('title') or 'unknown'}",
              f"Duration: {fmt_time(duration) if duration else 'unknown'}",
              f"Engine: local. Transcript: {transcript_label}. Frames: {len(frames)}",
              f"Work dir (delete when finished): {work}",
              ""]
    if args.question:
        report += [f"Question: {args.question}", ""]
    report += ["## Frames (read each one)"]
    for t, path in frames:
        report.append(f"- {fmt_time(t)} {path}")
    if not frames:
        report.append("- none (transcript-only run)")
    report += ["", "## Transcript"]
    keep = [(s, e, t) for s, e, t in captions
            if (start is None or s >= start) and (end is None or s <= end)]
    for s, _, t in keep:
        report.append(f"[{fmt_time(s)}] {t}")
    if not keep:
        report.append("No transcript available for this range.")
    text = "\n".join(report)
    (work / "report.md").write_text(text, encoding="utf-8")
    print(text)
    print(f"\nSaved: {work / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# Video Watcher

A Claude Code skill that inspects a video from a URL or local file. It produces timestamped frames and a transcript (local engine), or sends the video to Gemini for a full-video analysis.

Use it as `/video-watcher`, or read `SKILL.md` for the workflow.

## Setup

```bash
python3 scripts/setup.py --check
python3 scripts/setup.py --install
python3 scripts/setup.py --engine local
```

FFmpeg and ffprobe must be on PATH. Gemini needs `GEMINI_API_KEY`, saved with `setup.py --set-key`.

## Privacy

Video frames, captions and transcripts are treated as data, not instructions. Browser cookies are used only when you pass `--cookies` or `--cookies-from-browser`. Local runs stay on your machine; Gemini runs upload the video to Google.

## License

MIT

#!/usr/bin/env python3
"""Setup for the video-watcher skill.

Checks prerequisites, installs local tools on request, and saves the chosen
engine and defaults to ~/.config/watch/config.json. API keys go in
~/.config/watch/.env and are never printed.

Exit codes: 0 ok, 1 error, 3 Gemini key still missing (rerun after adding it).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("WATCH_CONFIG_DIR", Path.home() / ".config" / "watch"))
CONFIG_FILE = CONFIG_DIR / "config.json"
ENV_FILE = CONFIG_DIR / ".env"

DETAILS = ["transcript", "efficient", "balanced", "token-burner"]
BACKENDS = ["whisperx", "groq", "openai", "none"]


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def read_env():
    env = {}
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def set_env_key(name, value):
    """Add or replace one key in .env, keeping every other entry."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines = []
    if ENV_FILE.exists():
        lines = [l for l in ENV_FILE.read_text(encoding="utf-8").splitlines()
                 if not l.strip().startswith(name + "=")]
    lines.append(f"{name}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def tool_status():
    return {t: shutil.which(t) is not None for t in ("ffmpeg", "ffprobe", "yt-dlp")}


def install_local_tools():
    results = {}
    if not shutil.which("yt-dlp"):
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--user", "-U", "yt-dlp"],
                           capture_output=True, text=True)
        results["yt-dlp"] = "installed" if r.returncode == 0 else "failed: " + r.stderr[-300:]
    else:
        results["yt-dlp"] = "present"
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool):
            results[tool] = "present"
        else:
            results[tool] = ("missing: install FFmpeg (Windows: winget install Gyan.FFmpeg; "
                             "macOS: brew install ffmpeg; Linux: apt install ffmpeg)")
    return results


def build_status(cfg):
    env = read_env()
    tools = tool_status()
    return {
        "engine": cfg.get("engine"),
        "detail": cfg.get("detail", "balanced"),
        "backend": cfg.get("backend", "none"),
        "tools": tools,
        "gemini_key": bool(env.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")),
        "setup_complete": bool(cfg.get("engine")) and all(tools.values()),
        "config_dir": str(CONFIG_DIR),
    }


def main():
    p = argparse.ArgumentParser(description="Set up the video-watcher skill.")
    p.add_argument("--json", action="store_true", help="print status as JSON")
    p.add_argument("--check", action="store_true", help="check prerequisites only")
    p.add_argument("--engine", choices=["local", "gemini"], help="save the engine choice")
    p.add_argument("--detail", choices=DETAILS, help="save default detail level")
    p.add_argument("--backend", choices=BACKENDS, help="save missing-caption transcription choice")
    p.add_argument("--install", action="store_true", help="install local tools (yt-dlp via pip)")
    p.add_argument("--set-key", metavar="NAME=VALUE",
                   help="save an API key to .env (name must be GEMINI_API_KEY, GROQ_API_KEY or OPENAI_API_KEY)")
    args = p.parse_args()

    cfg = load_config()
    out = {}

    if args.set_key:
        name, _, value = args.set_key.partition("=")
        if name not in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY") or not value:
            print("error: --set-key needs NAME=VALUE with a supported key name", file=sys.stderr)
            return 1
        set_env_key(name, value)
        out["saved_key"] = name  # name only, never the value

    if args.install:
        out["install"] = install_local_tools()

    if args.engine:
        cfg["engine"] = args.engine
    if args.detail:
        cfg["detail"] = args.detail
    if args.backend:
        cfg["backend"] = args.backend
    if any([args.engine, args.detail, args.backend]):
        save_config(cfg)

    status = build_status(cfg)
    out.update(status)

    if args.engine == "gemini" and not status["gemini_key"]:
        out["next"] = (f"Add GEMINI_API_KEY to {ENV_FILE} (or run --set-key), then rerun setup.")
        emit(out, args.json)
        return 3
    if cfg.get("engine") == "local" and not status["tools"]["yt-dlp"]:
        out["next"] = "Run setup.py --install to add yt-dlp."

    emit(out, args.json)
    return 0


def emit(out, as_json):
    if as_json:
        print(json.dumps(out, indent=2))
    else:
        for k, v in out.items():
            print(f"{k}: {v}")


if __name__ == "__main__":
    sys.exit(main())

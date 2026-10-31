"""Settings, read once from the environment and an optional .env file at the repo root."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Models by role; see README "Model choices" for the numbers behind each.
# The live path (per-sentence check, follow-up verify, perspectives, naming) needs the fastest first
# token: Artificial Analysis measured Sonnet 5.5 at low effort at 1.30 s, Opus 5.5 at low effort at 6.16 s.
LIVE_MODEL = os.getenv("LIVE_MODEL", "claude-sonnet-5-5")
CHECK_EFFORT = os.getenv("CHECK_EFFORT", "low")
# Push-back is user pressure. On MASK (honesty under pressure) Anthropic's system cards put Sonnet 5.5
# ahead of Opus 5.5.
PUSHBACK_MODEL = os.getenv("PUSHBACK_MODEL", "claude-sonnet-5-5")
PUSHBACK_EFFORT = os.getenv("PUSHBACK_EFFORT", "high")

# Fast mode (research preview, Opus 5 / 5.5 / 4.8 only): higher output speed for a premium price.
# Used only when a role runs on one of those models. FAST_MODE=0 turns it off.
FAST_MODE = os.getenv("FAST_MODE", "1") != "0"

# How much reaches the screen: references on a verdict, and voices on a contested claim or a value.
MAX_SOURCES = int(os.getenv("MAX_SOURCES", "2"))
MAX_TAKES = int(os.getenv("MAX_TAKES", "2"))

EXA_API_KEY = os.getenv("EXA_API_KEY", "")
EXA_SEARCH_TYPE = os.getenv("EXA_SEARCH_TYPE", "lite")

ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY", "")
ASSEMBLYAI_MODEL = os.getenv("ASSEMBLYAI_MODEL", "universal-3-6-pro")
# Silence (ms) before AssemblyAI ends a turn. Its documented default with speaker labels is 768 on
# Universal-3.5 Pro; lower ends sentences sooner but may cut people off. Unset = AssemblyAI default.
ASSEMBLYAI_MAX_TURN_SILENCE = os.getenv("ASSEMBLYAI_MAX_TURN_SILENCE", "")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
DEEPGRAM_MODEL = os.getenv("DEEPGRAM_MODEL", "nova-3")
# Server-side speech-to-text: AssemblyAI when its key is set (lower error rate on the Pipecat benchmark, see stt.py), else Deepgram.
STT_PROVIDER = os.getenv("STT_PROVIDER") or ("assemblyai" if ASSEMBLYAI_API_KEY else "deepgram" if DEEPGRAM_API_KEY else "")

WEB_DIR = ROOT / "web"

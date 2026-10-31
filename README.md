# E.V. (Enhanced Virtual Assistant) — v2.0.0

An autonomous, local-first virtual assistant inspired by Iron Man's JARVIS and Spider-Man ("Brand New Day"). Combines deep system control, multi-application workspace orchestration, phone/WhatsApp bridge, persistent long-term memory, and a cinematic dark holographic HUD — engineered specifically to run **100% offline and strictly CPU-bound under a ≤ 4.0 GB RAM budget** on the MSI Bravo 15 (AMD Radeon RX 6500M - 0 VRAM).

---

## 🌌 Key Highlights

- **Cinematic Dark Holographic HUD (PyQt6 / PyQt5 fallback)**:
  - 48px/240px cyan grid, 70 floating ambient particles, CRT scanlines, animated sweep line, and vignette.
  - Central **Status Orb** featuring 5 dynamic states (*Idle*, *Listening*, *Thinking*, *Speaking*, *Acting*), outer 90-tick rotation ring, segmented arc ring, interior hex lattice, and word-by-word streaming caption readout.
  - Floating **Workspace Orchestrator** panel with live launch progress and one-click "Undo workspace" stack reversal.
  - Timed Command **Countdown Dial** with large tabular seconds readout and urgent state shifting.
  - Full-screen Iron-Man **Call Overlay** with rotating holographic rings, audio waveform bars, and VB-Audio voice routing.
  - Floating **System Metrics Drawer** with 28-point sparklines, battery gauge, and `0 MB GPU` enforcement badge.
- **Hardware & RAM Guard**:
  - Automatically isolates AMD Radeon RX 6500M (`n_gpu_layers = 0`, GPU environment flags masked).
  - Background `SystemBridge` polls process tree RSS memory and triggers emergency garbage collection if RAM exceeds 3.8 GB.
- **Fast-Path Intent Router**:
  - Intercepts workspace, call, volume, and timer commands before they touch the local LLM to achieve < 50ms latency and conserve RAM.

---

## 📁 Project Architecture

```
ev/
├── main.py                     # Primary entry point & SystemBridge telemetry loop
├── config.yaml                 # Master configuration (hardware, LLM, voice, GUI)
├── requirements.txt            # Python 3.10+ dependencies
├── core/
│   ├── event_bus.py            # Thread-safe pub/sub between background workers & HUD
│   ├── brain.py                # CPU-only llama.cpp engine (Qwen2.5-3B-Instruct)
│   ├── intent_router.py        # Fast-path keyword & regex command classifier
│   └── scheduler.py            # Second-precision delayed command execution
├── voice/
│   ├── stt.py                  # Offline Vosk speech-to-text
│   ├── tts.py                  # Offline Piper neural text-to-speech
│   ├── wakeword.py             # Offline "Hey E.V." trigger detection
│   └── clap_detector.py        # Double-clap acoustic energy detector
├── automation/
│   ├── system.py               # Process monitoring, app launch, screenshot capture
│   ├── workspace.py            # Multi-app workspace launcher & undo stack
│   ├── media.py                # System media playback & volume control
│   └── web_search.py           # Zero-API DuckDuckGo search queries
├── remote/
│   ├── whatsapp.py             # WhatsApp Web Selenium controller
│   ├── phone_adb.py            # Wireless ADB bridge (call, unlock, SMS, apps)
│   └── audio_routing.py        # VB-Audio Virtual Cable call voice injection
├── memory/
│   ├── sqlite_store.py         # Relational SQLite message & preferences database
│   ├── chroma_store.py         # Persistent ChromaDB vector embedding store
│   └── rag.py                  # Context retrieval for LLM augmentation
├── gui/
│   ├── main_window.py          # Complete PyQt Holographic HUD implementation
│   ├── status_orb.py           # Animated central Status Orb widget
│   ├── overlays.py             # GlassPanel, Countdown, Workspace, Call modals
│   ├── styles.qss              # Dark holographic styling tokens
│   └── assets/                 # Pre-cached icons, audio cues, visuals
├── data/
│   └── workspaces/
│       └── coding.json         # 10-item developer workspace configuration
├── models/                     # GGUF, Vosk, and Piper models (git-ignored)
└── tests/                      # Automated pytest unit test suite
```

---

## 🚀 Getting Started

### 1. Prerequisites
- Python 3.10+ (64-bit)
- Windows 10/11 (or Linux)

### 2. Setup Virtual Environment
```bash
python -m venv .venv
# On Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# On Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. Launching E.V.
```bash
# Standard HUD launch:
python main.py

# Low CPU overhead mode (disables particles and scanlines):
python main.py --minimal-ui

# Fullscreen holographic display:
python main.py --fullscreen
```

---

## 🧪 Running Tests
```bash
pytest
```


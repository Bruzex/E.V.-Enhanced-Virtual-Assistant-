# E.V. — Enhanced Virtual Assistant

**100% Offline • Strict CPU-Only (`n_gpu_layers=0`) • ≤ 4.0 GB Peak RAM • Dark Holographic HUD**

E.V. (Enhanced Virtual Assistant) is a local-first, voice-driven desktop AI assistant inspired by Iron Man's JARVIS and the Jarvis AI Spark project, engineered specifically for constrained hardware (**MSI Bravo 15** with 16 GB RAM and AMD Radeon RX 6500M).

---

## 1. Hardware Constraints & Safety Architecture

| Constraint | Target Specification | Enforcement Mechanism in E.V. |
| :--- | :--- | :--- |
| **Target Laptop** | MSI Bravo 15 (16 GB RAM, AMD RX 6500M) | Windows-first paths, Win32 virtual media/volume keys, wireless ADB |
| **GPU VRAM** | **0 MB VRAM** (`n_gpu_layers = 0`) | Hard-forced in `main.py`, `config.yaml`, and `core/brain.py`; clears `CUDA`/`HIP`/`ROCR`/`GGML_VK` env vars |
| **Peak RAM** | **≤ 4.0 GB RSS** | Fast-path `IntentRouter` bypasses LLM for ~90% of commands; lazy LLM load + 300s idle unload + critical RAM unload at 3.8 GB |
| **Local LLM** | `Qwen2.5-3B-Instruct Q4_K_M` (~2.1 GB) | `llama-cpp-python` CPU-only inference with `use_mmap: true` |
| **Voice I/O** | `Vosk` STT (~50 MB) + `Piper` Neural TTS | Offline streaming recognition with automatic TTS echo-suppression gate |

---

## 2. Project Structure

```text
ev/
├── main.py                      # Entry point, SystemBridge, logs/rss.log telemetry
├── config.yaml                  # Master configuration (strict n_gpu_layers: 0)
├── requirements.txt             # Python 3.10+ dependencies
├── README.md                    # Documentation & setup guide
├── core/
│   ├── brain.py                 # llama-cpp-python wrapper (CPU-only, lazy load/unload)
│   ├── intent_router.py         # Fast regex/keyword router + compound command splitter
│   ├── scheduler.py             # Non-blocking 1 Hz TimedCommandScheduler
│   └── event_bus.py             # Thread-safe pub/sub EventBus & Topics constants
├── voice/
│   ├── stt.py                   # Offline Vosk STT wrapper + TTS echo suppression
│   ├── tts.py                   # Non-blocking Piper neural TTS worker queue
│   ├── wakeword.py              # RMS energy gate + "Hey E.V." wake-word loop
│   └── clap_detector.py         # Percussive double-clap detector on raw PCM stream
├── automation/
│   ├── system.py                # App launch/close, folders/URLs, volume/mute, psutil
│   ├── workspace.py             # JSON Workspace Orchestrator + LIFO Undo stack
│   ├── media.py                 # Media playback keys, Win+D desktop, screenshots
│   └── web_search.py            # Zero-API-key live search via duckduckgo-search
├── remote/
│   ├── phone_adb.py             # Wireless & USB Android ADB bridge (call, SMS, unlock)
│   ├── whatsapp.py              # Lazy-loaded persistent-profile Selenium WhatsApp Web
│   └── audio_routing.py         # VB-Audio Virtual Cable call voice injection
├── memory/
│   ├── sqlite_store.py          # WAL-mode SQLite store for preferences & history
│   ├── chroma_store.py          # Persistent vector memory with zero-RAM hashing embedder
│   └── rag.py                   # Unified RAG context builder & fact recall
├── gui/
│   ├── main_window.py           # Thin HUD orchestrator window
│   ├── status_orb.py            # 5-state animated StatusOrbWidget & theme primitives
│   ├── overlays.py              # Workspace, Countdown, Call, Metrics & Settings panels
│   └── styles.qss               # Dark holographic stylesheet (#0B0F19 / #00D4FF / #7B61FF)
├── data/
│   └── workspaces/
│       └── coding.json          # Default 10-item Coding workspace definition
├── models/                      # Place local GGUF, Vosk, and Piper models here
└── tests/
    ├── test_intent_router.py
    ├── test_workspace.py
    └── test_phone_adb.py
```

---

## 3. Model Placement (`models/`)

Download and place the following offline models inside `ev/models/`:

1. **LLM (`Qwen2.5-3B-Instruct Q4_K_M`)**:
   - Path: `ev/models/qwen2.5-3b-instruct-q4_k_m.gguf`
2. **Vosk STT (`vosk-model-small-en-us`)**:
   - Extract to: `ev/models/vosk-model-small-en-us/`
3. **Piper TTS (`en_US-lessac-medium`)**:
   - Path: `ev/models/piper/en_US-lessac-medium.onnx`
   - Config: `ev/models/piper/en_US-lessac-medium.onnx.json`

*(Note: E.V. degrades gracefully if any model file is not yet present—the HUD, Workspace Orchestrator, Timed Command Scheduler, and deterministic voice/button flows remain 100% functional.)*

---

## 4. Installation & Running E.V.

```bash
# 1. Create and activate virtual environment (Python 3.10+)
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

# 2. Install dependencies (CPU-only llama-cpp-python)
pip install -r requirements.txt

# 3. Launch the E.V. Holographic HUD
python main.py

# Optional flags:
python main.py --minimal-ui   # Disable particles/scanlines for ultra-low CPU usage
python main.py --fullscreen   # Launch HUD in fullscreen mode
```

To watch live RAM and CPU telemetry in a terminal (matching the `RAM monitor` item in `coding.json`):

```bash
tail -f logs/rss.log
```

---

## 5. Key Voice Commands

- **Success Benchmark Scenario (Compound Command)**:
  - *"Hey E.V., open my coding workspace and prepare a call to Mom in 30 seconds."*
- **Workspaces**:
  - *"Hey E.V., launch my coding workspace."*
  - *"Hey E.V., undo my coding workspace."*
- **Timed Commands & Countdown**:
  - *"Hey E.V., in 10 seconds dim desk lights to 30%."*
  - *"Hey E.V., cancel timer."* / *"Hey E.V., run timer now."*
- **Phone Control (Wireless ADB) & WhatsApp**:
  - *"Hey E.V., unlock my phone."*
  - *"Hey E.V., call Mom."*
  - *"Hey E.V., send an SMS to Mom saying I'll be home at 8."*
  - *"Hey E.V., send a WhatsApp voice note to Mom saying I'm wrapping up my sprint."*
- **System, Media & Memory**:
  - *"Hey E.V., system status report."*
  - *"Hey E.V., set volume to 40%."* / *"Hey E.V., mute audio."*
  - *"Hey E.V., remember that my project deadline is November 15."*
  - *"Hey E.V., what do you remember about project deadline?"*
- **Non-Voice Trigger**:
  - **Double-clap** near the microphone to toggle active listening.

---

## 6. Running the Test Suite

```bash
python -m unittest discover -s tests -v
```

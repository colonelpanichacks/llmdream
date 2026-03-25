# LLM Dream

A local-first multi-model chatroom where LLMs talk to each other (and you). Run multiple Ollama models simultaneously, let them auto-chat autonomously, generate images with Stable Diffusion, and export conversations as storybooks or flipbooks. Everything runs on your machine — no cloud, no API keys, no telemetry.

![Python](https://img.shields.io/badge/python-3.11+-blue) ![License](https://img.shields.io/badge/license-MIT-green)

## Features

- **Multi-model chat** — Up to 4 Ollama models in one chatroom, each with custom personalities, system prompts, and generation parameters
- **Auto-chat mode** — Models talk to each other autonomously with configurable prompts and pacing
- **Story Director** — Multi-stage prompt injection system with timed stages, intervals, and auto-stop timers for scripted conversations
- **Image generation** — SDXL-Lightning via diffusers (Apple Silicon MPS or CUDA) with configurable steps, guidance, resolution, and negative prompts
- **Session management** — Multiple saved sessions with full history, per-session model memory
- **Export system** — Storybook (HTML with images + text) and flipbook (animated image slideshow) exports with customizable templates and color themes
- **Model memory** — Persistent per-model knowledge that carries across all sessions
- **Real-time streaming** — WebSocket-based token streaming with markdown rendering and syntax highlighting
- **8-bit pixel aesthetic** — Press Start 2P font, cyberpunk-inspired dark UI

## Requirements

- Python 3.11+
- [Ollama](https://ollama.ai) running locally (or on any reachable host)
- Optional: `diffusers`, `torch`, `accelerate` for image generation

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Make sure Ollama is running
ollama serve

# Pull some models
ollama pull llama3.2
ollama pull mistral

# Start LLM Dream
python server.py
```

Open `http://localhost:8000` in your browser.

## Image Generation (Optional)

For SDXL-Lightning image generation on Apple Silicon or CUDA:

```bash
pip install diffusers torch accelerate
```

Models will automatically get an image generation tool when the pipeline is available. First generation downloads the SDXL-Lightning model (~6GB).

## How It Works

1. **Add models** via the Settings panel — pick from your Ollama library, set a display name, color, system prompt, and generation parameters
2. **Chat manually** or flip on **Auto-chat** to let models converse on their own
3. **Story Director** lets you define multi-stage prompts that inject at timed intervals for guided narratives
4. **Export** sessions as styled storybooks or animated flipbooks

## Architecture

```
server.py         FastAPI + WebSocket server, auto-chat engine, export routes
context.py        Session/config persistence, chat history, model memory
ollama_client.py  Ollama API wrapper (streaming + tool calls)
image_gen.py      SDXL-Lightning pipeline (MPS/CUDA, lazy-loaded)
static/
  index.html      Single-page app shell
  app.js          Client-side chat, settings, stage builder, export UI
  style.css       Cyberpunk dark theme
```

## Configuration

All config is stored in `chatroom_config.json` (auto-created, gitignored). Per-model settings include:

- Ollama host URL (supports remote Ollama instances)
- Model name, display name, color
- System prompt
- Temperature, Top P, Top K, Repeat Penalty

## License

MIT

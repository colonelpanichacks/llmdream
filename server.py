import asyncio
import json
import os
import re
import random
import time
import httpx

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

from ollama_client import list_models, chat_stream, chat_no_stream, unload_model, IMAGE_GEN_TOOL, DEFAULT_OLLAMA_HOST
from context import ChatroomContext
from image_gen import is_available as image_gen_available, generate_image

app = FastAPI()
ctx = ChatroomContext()

# State
connected_clients: list[WebSocket] = []
auto_chat_active = False
kill_event = asyncio.Event()
generation_lock = asyncio.Lock()
current_generation_task: asyncio.Task | None = None
active_tasks: set[asyncio.Task] = set()
turns_since_image = 0  # track turns since last image generation
_last_user_msg_time = 0.0  # for queue vs interrupt detection
_INTERRUPT_WINDOW = 2.0  # seconds — second Enter within this window = interrupt

# Story stages (multi-step prompts)
story_stages: list[str] = []
stage_intervals: list[float] = []  # per-stage intervals in minutes (for sub-event support)
current_stage = 0
stage_started_at = 0.0  # timestamp when current stage began
stage_interval_minutes = 2.0  # minutes between stage injections (configurable via settings)
_auto_kill_after_last_minutes = None  # minutes after last stage before auto-kill (set by stage builder)

# Stable Diffusion settings (mutable at runtime via WebSocket)
sd_settings = {
    "steps": 4,
    "guidance_scale": 0.0,
    "width": 768,
    "height": 768,
    "negative_prompt": "",
}

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
IMAGES_DIR = os.path.join(STATIC_DIR, "images")
EXPORTS_DIR = os.path.join(STATIC_DIR, "exports")
os.makedirs(IMAGES_DIR, exist_ok=True)
os.makedirs(EXPORTS_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
async def startup_cleanup():
    """Unload any Ollama models that aren't in the active chatroom."""
    # Collect unique hosts and their active models
    hosts_models: dict[str, set] = {}
    for mid, cfg in ctx.models.items():
        host = cfg.get("ollama_host", DEFAULT_OLLAMA_HOST)
        name = cfg.get("ollama_model", mid)
        if host not in hosts_models:
            hosts_models[host] = set()
        hosts_models[host].add(name)
        if ":" not in name:
            hosts_models[host].add(f"{name}:latest")
    # If no models configured, still check localhost
    if not hosts_models:
        hosts_models[DEFAULT_OLLAMA_HOST] = set()
    for host, active_ollama in hosts_models.items():
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{host}/api/ps")
                running = resp.json().get("models", [])
            for m in running:
                name = m["name"]
                if name not in active_ollama:
                    print(f"[startup] Unloading unused model on {host}: {name}")
                    await unload_model(name, host=host)
        except Exception as e:
            print(f"[startup] Cleanup skipped for {host}: {e}")

    # Preload SDXL pipeline in background so first image is fast
    if image_gen_available():
        import asyncio
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, lambda: __import__('image_gen')._get_pipe())
        print("[startup] SDXL pipeline preloading in background...")


@app.get("/")
async def index():
    # Serve index.html with no-cache headers so browser always gets fresh version
    import time as _time
    resp = FileResponse(os.path.join(STATIC_DIR, "index.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


def _load_session(session_id: str):
    """Load a session JSON file, return (data, error_response)."""
    session_path = os.path.join(os.path.dirname(__file__), "sessions", f"{session_id}.json")
    if not os.path.exists(session_path):
        return None, HTMLResponse("<h1>Session not found</h1>", status_code=404)
    with open(session_path) as f:
        return json.load(f), None


def _build_story_sections(history, captions=False):
    """Build HTML sections from session history. Returns (sections_html, img_count, first_prompt)."""
    sections = []
    img_count = 0
    first_prompt = ""
    last_user_msg = ""

    for msg in history:
        role = msg.get("role", "")
        name = msg.get("name", "")
        content = msg.get("content", "").strip()
        images = msg.get("images", [])

        if role == "user":
            last_user_msg = content
            if not first_prompt:
                first_prompt = content
            continue
        if name in ("User", "Director"):
            continue
        if not content and not images:
            continue

        clean = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', content).strip()
        clean = re.sub(r'\n{3,}', '\n\n', clean).strip()

        section_html = ""
        if clean:
            paragraphs = clean.split('\n\n')
            for p in paragraphs:
                p = p.strip()
                if p:
                    p = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', p)
                    p = re.sub(r'\*(.+?)\*', r'<em>\1</em>', p)
                    section_html += f'<p>{p}</p>\n'

        for img_url in images:
            section_html += f'<figure><img src="{img_url}" alt="Generated illustration" loading="lazy">'
            if captions:
                # Try to extract image prompt from the message content
                prompt_match = re.search(r'generate_image["\s:]+["\']?([^"\'}\]]+)', content, re.I)
                if prompt_match:
                    cap = prompt_match.group(1).strip()[:200]
                    section_html += f'<figcaption>{cap}</figcaption>'
            section_html += '</figure>\n'
            img_count += 1

        if section_html:
            sections.append(section_html)

    return "\n".join(sections), img_count, first_prompt


# ═══ Storybook templates ═══

STORYBOOK_STYLES = {
    "cyberpunk": """
@import url('https://fonts.googleapis.com/css2?family=Crimson+Pro:ital,wght@0,400;0,600;1,400&family=JetBrains+Mono:wght@400&display=swap');
* { margin:0; padding:0; box-sizing:border-box; }
body { background:#0a0a0f; color:#d4d4d8; font-family:'Crimson Pro',Georgia,serif; font-size:1.2rem; line-height:1.8; min-height:100vh; }
.storybook-header { text-align:center; padding:80px 20px 60px; border-bottom:1px solid rgba(0,255,200,0.1); background:linear-gradient(180deg,rgba(189,0,255,0.05) 0%,transparent 100%); }
.storybook-title { font-size:clamp(2rem,5vw,3.5rem); font-weight:600; background:linear-gradient(135deg,#bd00ff,#00ffc8); -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text; margin-bottom:12px; letter-spacing:1px; }
.storybook-subtitle { font-family:'Crimson Pro',Georgia,serif; font-size:1rem; color:#888; font-style:italic; margin-top:8px; max-width:600px; margin-left:auto; margin-right:auto; }
.storybook-meta { font-family:'JetBrains Mono',monospace; font-size:0.7rem; color:#666; letter-spacing:2px; text-transform:uppercase; margin-top:12px; }
.storybook-content { max-width:720px; margin:0 auto; padding:60px 24px 120px; }
.storybook-content p { margin-bottom:1.4em; text-indent:1.5em; }
.storybook-content p:first-child { text-indent:0; }
.storybook-content p:first-child::first-letter { font-size:3.2em; float:left; line-height:0.8; margin-right:8px; margin-top:6px; background:linear-gradient(135deg,#bd00ff,#00ffc8); -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text; font-weight:600; }
.storybook-content strong { color:#00ffc8; font-weight:600; }
.storybook-content em { color:#c8b4ff; font-style:italic; }
.storybook-content figure { margin:40px -60px; text-align:center; }
.storybook-content figure img { width:100%; max-width:900px; border-radius:8px; box-shadow:0 8px 40px rgba(0,0,0,0.6),0 0 80px rgba(189,0,255,0.08); }
.storybook-content figcaption { font-family:'JetBrains Mono',monospace; font-size:0.7rem; color:#555; margin-top:8px; letter-spacing:1px; font-style:italic; }
.storybook-footer { text-align:center; padding:40px 20px 60px; border-top:1px solid rgba(0,255,200,0.1); font-family:'JetBrains Mono',monospace; font-size:0.65rem; color:#444; letter-spacing:3px; text-transform:uppercase; }
@media (max-width:800px) { .storybook-content figure { margin:30px -12px; } }
@media print { body { background:white; color:#222; } .storybook-title { color:#222; -webkit-text-fill-color:#222; } .storybook-content figure img { box-shadow:none; border:1px solid #ddd; } }
""",
    "bbs": """
@import url('https://fonts.googleapis.com/css2?family=VT323&family=JetBrains+Mono:wght@400&display=swap');
* { margin:0; padding:0; box-sizing:border-box; }
body { background:#0a0a00; color:#33ff33; font-family:'VT323','Courier New',monospace; font-size:1.3rem; line-height:1.6; min-height:100vh; text-shadow:0 0 8px rgba(51,255,51,0.3); }
.storybook-header { text-align:center; padding:60px 20px 40px; border-bottom:1px solid #1a3a1a; }
.storybook-header::before { content:'╔══════════════════════════════════╗'; display:block; color:#1a5a1a; font-size:0.8rem; margin-bottom:16px; }
.storybook-header::after { content:'╚══════════════════════════════════╝'; display:block; color:#1a5a1a; font-size:0.8rem; margin-top:16px; }
.storybook-title { font-size:clamp(1.8rem,4vw,3rem); color:#33ff33; letter-spacing:4px; text-transform:uppercase; text-shadow:0 0 20px rgba(51,255,51,0.5); margin-bottom:8px; }
.storybook-subtitle { font-size:1rem; color:#22aa22; margin-top:8px; }
.storybook-meta { font-size:0.8rem; color:#1a5a1a; letter-spacing:3px; margin-top:12px; }
.storybook-content { max-width:720px; margin:0 auto; padding:40px 24px 120px; }
.storybook-content p { margin-bottom:1.2em; text-indent:0; }
.storybook-content p::before { content:'> '; color:#1a5a1a; }
.storybook-content strong { color:#66ff66; }
.storybook-content em { color:#88cc88; font-style:normal; text-decoration:underline; }
.storybook-content figure { margin:30px 0; text-align:center; border:1px solid #1a5a1a; padding:8px; }
.storybook-content figure img { width:100%; max-width:800px; border:2px solid #33ff33; image-rendering:auto; filter:saturate(0.7) brightness(0.9); }
.storybook-content figcaption { font-size:0.8rem; color:#1a5a1a; margin-top:6px; }
.storybook-footer { text-align:center; padding:40px 20px; border-top:1px solid #1a3a1a; font-size:0.8rem; color:#1a5a1a; letter-spacing:3px; }
@media print { body { background:white; color:#222; text-shadow:none; } }
""",
    "clean": """
@import url('https://fonts.googleapis.com/css2?family=Source+Serif+4:ital,wght@0,400;0,600;1,400&family=Inter:wght@400&display=swap');
* { margin:0; padding:0; box-sizing:border-box; }
body { background:#fafafa; color:#2a2a2a; font-family:'Source Serif 4','Georgia',serif; font-size:1.15rem; line-height:1.9; min-height:100vh; }
.storybook-header { text-align:center; padding:80px 20px 60px; border-bottom:1px solid #e0e0e0; }
.storybook-title { font-size:clamp(2rem,5vw,3.5rem); font-weight:600; color:#1a1a1a; margin-bottom:8px; }
.storybook-subtitle { font-size:1rem; color:#888; font-style:italic; margin-top:8px; max-width:600px; margin-left:auto; margin-right:auto; }
.storybook-meta { font-family:'Inter',sans-serif; font-size:0.7rem; color:#aaa; letter-spacing:2px; text-transform:uppercase; margin-top:12px; }
.storybook-content { max-width:680px; margin:0 auto; padding:60px 24px 120px; }
.storybook-content p { margin-bottom:1.4em; text-indent:1.5em; }
.storybook-content p:first-child { text-indent:0; }
.storybook-content p:first-child::first-letter { font-size:3em; float:left; line-height:0.8; margin-right:6px; margin-top:6px; color:#1a1a1a; font-weight:600; }
.storybook-content strong { color:#1a1a1a; }
.storybook-content em { color:#555; }
.storybook-content figure { margin:40px -40px; text-align:center; }
.storybook-content figure img { width:100%; max-width:800px; border-radius:4px; box-shadow:0 4px 20px rgba(0,0,0,0.08); }
.storybook-content figcaption { font-family:'Inter',sans-serif; font-size:0.75rem; color:#999; margin-top:8px; font-style:italic; }
.storybook-footer { text-align:center; padding:40px 20px 60px; border-top:1px solid #e0e0e0; font-family:'Inter',sans-serif; font-size:0.65rem; color:#bbb; letter-spacing:3px; text-transform:uppercase; }
@media (max-width:800px) { .storybook-content figure { margin:30px -12px; } }
""",
    "neon": """
@import url('https://fonts.googleapis.com/css2?family=Crimson+Pro:ital,wght@0,400;0,600;1,400&family=Orbitron:wght@400;700&family=JetBrains+Mono:wght@400&display=swap');
* { margin:0; padding:0; box-sizing:border-box; }
body { background:#05050a; color:#e0d0ff; font-family:'Crimson Pro',Georgia,serif; font-size:1.2rem; line-height:1.8; min-height:100vh; }
.storybook-header { text-align:center; padding:80px 20px 60px; border-bottom:1px solid rgba(255,0,128,0.15); background:radial-gradient(ellipse at 50% 0%,rgba(255,0,128,0.08) 0%,transparent 70%); }
.storybook-title { font-family:'Orbitron',sans-serif; font-size:clamp(1.8rem,5vw,3rem); font-weight:700; color:#ff0080; text-shadow:0 0 30px rgba(255,0,128,0.5),0 0 60px rgba(255,0,128,0.2); margin-bottom:12px; letter-spacing:3px; text-transform:uppercase; }
.storybook-subtitle { font-size:1rem; color:#8866aa; font-style:italic; margin-top:8px; max-width:600px; margin-left:auto; margin-right:auto; }
.storybook-meta { font-family:'JetBrains Mono',monospace; font-size:0.7rem; color:#553377; letter-spacing:2px; text-transform:uppercase; margin-top:12px; }
.storybook-content { max-width:720px; margin:0 auto; padding:60px 24px 120px; }
.storybook-content p { margin-bottom:1.4em; text-indent:1.5em; }
.storybook-content p:first-child { text-indent:0; }
.storybook-content p:first-child::first-letter { font-size:3.2em; float:left; line-height:0.8; margin-right:8px; margin-top:6px; color:#ff0080; text-shadow:0 0 20px rgba(255,0,128,0.4); font-weight:600; }
.storybook-content strong { color:#00ffff; text-shadow:0 0 8px rgba(0,255,255,0.3); }
.storybook-content em { color:#ff80bf; font-style:italic; }
.storybook-content figure { margin:40px -60px; text-align:center; }
.storybook-content figure img { width:100%; max-width:900px; border-radius:8px; box-shadow:0 0 40px rgba(255,0,128,0.15),0 0 80px rgba(0,255,255,0.08); border:1px solid rgba(255,0,128,0.2); }
.storybook-content figcaption { font-family:'JetBrains Mono',monospace; font-size:0.7rem; color:#553377; margin-top:8px; letter-spacing:1px; }
.storybook-footer { text-align:center; padding:40px 20px 60px; border-top:1px solid rgba(255,0,128,0.15); font-family:'JetBrains Mono',monospace; font-size:0.65rem; color:#332255; letter-spacing:3px; text-transform:uppercase; }
@media (max-width:800px) { .storybook-content figure { margin:30px -12px; } }
@media print { body { background:white; color:#222; } .storybook-title { color:#222; text-shadow:none; -webkit-text-fill-color:#222; } }
""",
}


def _storybook_color_overrides(accent: str, highlight: str, bg: str, text: str) -> str:
    """Generate CSS overrides for custom storybook colors."""
    if not any([accent, highlight, bg, text]):
        return ""
    css = ""
    if bg:
        css += f"body {{ background: {bg}; }}\n"
    if text:
        css += f"body {{ color: {text}; }}\n"
    if highlight and accent:
        css += f".storybook-title {{ background: linear-gradient(135deg, {highlight}, {accent}); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }}\n"
        css += f".storybook-content p:first-child::first-letter {{ background: linear-gradient(135deg, {highlight}, {accent}); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }}\n"
    if accent:
        css += f".storybook-content strong {{ color: {accent}; }}\n"
    if highlight:
        css += f".storybook-content em {{ color: {highlight}; }}\n"
        css += f".storybook-header {{ border-bottom-color: {highlight}33; }}\n"
        css += f".storybook-footer {{ border-top-color: {highlight}33; }}\n"
    return css


@app.get("/storybook/{session_id}")
async def storybook(session_id: str, template: str = Query("cyberpunk"), captions: str = Query(""),
                    title: str = Query(""), subtitle: str = Query(""), prompt_title: str = Query(""),
                    color_accent: str = Query(""), color_highlight: str = Query(""),
                    color_bg: str = Query(""), color_text: str = Query("")):
    """Render a session as a beautiful storybook with template support."""
    session_data, err = _load_session(session_id)
    if err:
        return err
    history = session_data.get("history", [])
    session_title = session_data.get("title", "Untitled Story")
    show_captions = captions == "1"
    use_prompt_title = prompt_title == "1"

    story_body, img_count, first_prompt = _build_story_sections(history, captions=show_captions)
    style = STORYBOOK_STYLES.get(template, STORYBOOK_STYLES["cyberpunk"])
    color_css = _storybook_color_overrides(color_accent, color_highlight, color_bg, color_text)

    # Resolve title: custom > prompt > session (prefer full first_prompt over truncated session title)
    display_title = title if title else (first_prompt if (use_prompt_title or session_title.endswith("…")) and first_prompt else session_title)
    subtitle_html = f'<div class="storybook-subtitle">{subtitle}</div>' if subtitle else ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{display_title} &mdash; LLM Dream Storybook</title>
<style>{style}
{color_css}</style>
</head>
<body>
<header class="storybook-header">
    <h1 class="storybook-title">{display_title}</h1>
    {subtitle_html}
    <div class="storybook-meta">LLM Dream &mdash; {img_count} illustrations</div>
</header>
<article class="storybook-content">
{story_body}
</article>
<footer class="storybook-footer">
    Generated by LLM Dream
</footer>
</body>
</html>"""
    return HTMLResponse(html)


def _build_flipbook_slides(history):
    """Build flipbook slide data from session history."""
    slides = []
    current_text = ""
    for msg in history:
        role = msg.get("role", "")
        name = msg.get("name", "")
        if role == "user" or name in ("User", "Director"):
            continue
        content = msg.get("content", "").strip()
        images = msg.get("images", [])
        clean = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', content).strip()
        clean = re.sub(r'\n{3,}', '\n\n', clean).strip()
        if clean:
            clean = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', clean)
            clean = re.sub(r'\*(.+?)\*', r'<em>\1</em>', clean)
            current_text += clean + " "
        for img_url in images:
            overlay = current_text.strip()
            slides.append({"url": img_url, "text": overlay})
            current_text = ""
    return slides


# ═══ Flipbook templates ═══

FLIPBOOK_STYLES = {
    "cyber": {
        "body": "background:#000; font-family:'Crimson Pro',Georgia,serif; cursor:default;",
        "img": "width:100%; height:100%; object-fit:contain; background:#000; image-rendering:auto;",
        "kenburns": True,
        "overlay_bg": "linear-gradient(transparent, rgba(0,0,0,0.85) 40%)",
        "overlay_color": "#e8e8ea",
        "strong_color": "#00ffc8",
        "em_color": "#c8b4ff",
        "title_bg": "#0a0a0f",
        "title_gradient": "linear-gradient(135deg,#bd00ff,#00ffc8)",
        "progress_gradient": "linear-gradient(90deg,#bd00ff,#00ffc8)",
        "controls_accent": "#00ffc8",
    },
    "comic": {
        "body": "background:#1a1a2e; font-family:'Bangers','Impact',sans-serif; cursor:default;",
        "img": "width:100%; height:100%; object-fit:contain; background:#1a1a2e; image-rendering:auto; border:6px solid #fff; box-sizing:border-box;",
        "kenburns": False,
        "overlay_bg": "linear-gradient(transparent, rgba(255,255,50,0.95) 50%)",
        "overlay_color": "#1a1a2e",
        "overlay_extra": "font-weight:700; font-size:clamp(1rem,2.5vw,1.5rem); text-transform:uppercase; letter-spacing:2px;",
        "strong_color": "#ff3366",
        "em_color": "#3366ff",
        "title_bg": "#1a1a2e",
        "title_gradient": "linear-gradient(135deg,#ff3366,#ffee33)",
        "progress_gradient": "linear-gradient(90deg,#ff3366,#ffee33)",
        "controls_accent": "#ffee33",
    },
    "terminal": {
        "body": "background:#0a0a00; font-family:'VT323','Courier New',monospace; cursor:default;",
        "img": "width:100%; height:100%; object-fit:contain; background:#0a0a00; image-rendering:auto; filter:saturate(0.6) brightness(0.85);",
        "kenburns": False,
        "overlay_bg": "linear-gradient(transparent, rgba(0,0,0,0.9) 40%)",
        "overlay_color": "#33ff33",
        "overlay_extra": "text-shadow:0 0 8px rgba(51,255,51,0.4); font-size:clamp(1rem,2vw,1.4rem);",
        "strong_color": "#66ff66",
        "em_color": "#88cc88",
        "title_bg": "#0a0a00",
        "title_gradient": "none",
        "title_color": "#33ff33",
        "title_shadow": "text-shadow:0 0 30px rgba(51,255,51,0.5);",
        "progress_gradient": "#33ff33",
        "controls_accent": "#33ff33",
        "scanlines": True,
    },
    "clean": {
        "body": "background:#111; font-family:'Inter',-apple-system,sans-serif; cursor:default;",
        "img": "width:100%; height:100%; object-fit:contain; background:#111; image-rendering:auto;",
        "kenburns": False,
        "overlay_bg": "linear-gradient(transparent, rgba(0,0,0,0.7) 50%)",
        "overlay_color": "#e0e0e0",
        "overlay_extra": "font-style:normal; font-size:clamp(0.85rem,1.5vw,1.1rem);",
        "strong_color": "#fff",
        "em_color": "#bbb",
        "title_bg": "#111",
        "title_gradient": "none",
        "title_color": "#fff",
        "progress_gradient": "#fff",
        "controls_accent": "#fff",
    },
}


@app.get("/flipbook/{session_id}")
async def flipbook(session_id: str, template: str = Query("cyber"), speed: str = Query("6"),
                   title: str = Query(""), subtitle: str = Query(""), prompt_title: str = Query(""),
                   color_accent: str = Query(""), color_highlight: str = Query(""),
                   color_bg: str = Query(""), color_text: str = Query(""),
                   hide_text: str = Query("")):
    """Animated flipbook with template support, object-fit:contain, and sharp images."""
    session_data, err = _load_session(session_id)
    if err:
        return err
    history = session_data.get("history", [])
    session_title = session_data.get("title", "Untitled Story")
    try:
        initial_speed = max(0.2, min(20, float(speed)))
    except (ValueError, TypeError):
        initial_speed = 6
    # Resolve first prompt for prompt_title option
    first_prompt = ""
    for msg in history:
        if msg.get("role") == "user" and msg.get("content", "").strip():
            first_prompt = msg["content"].strip()
            break
    display_title = title if title else (first_prompt if (prompt_title == "1" or session_title.endswith("…")) and first_prompt else session_title)

    slides = _build_flipbook_slides(history)
    slides_json = json.dumps(slides)
    img_count = len(slides)
    t = dict(FLIPBOOK_STYLES.get(template, FLIPBOOK_STYLES["cyber"]))  # copy so we can override
    # Apply custom colors if provided
    if color_accent:
        t["strong_color"] = color_accent
        t["controls_accent"] = color_accent
    if color_highlight:
        t["em_color"] = color_highlight
        if t.get("title_gradient") and t["title_gradient"] != "none":
            t["title_gradient"] = f"linear-gradient(135deg, {color_highlight}, {color_accent or '#00ffc8'})"
        t["progress_gradient"] = f"linear-gradient(90deg, {color_highlight}, {color_accent or '#00ffc8'})"
    if color_bg:
        t["title_bg"] = color_bg
        t["body"] = re.sub(r'background:[^;]+', f'background:{color_bg}', t["body"])
        t["img"] = re.sub(r'background:[^;]+', f'background:{color_bg}', t["img"])
    if color_text:
        t["overlay_color"] = color_text

    # Build dynamic CSS
    kenburns_css = """
.slide img { animation: kenburns 12s ease-in-out infinite alternate; }
@keyframes kenburns { 0% { transform:scale(1) translate(0,0); } 100% { transform:scale(1.08) translate(-1%,-1%); } }
""" if t.get("kenburns") else ""

    scanlines_css = """
.flipbook::after { content:''; position:fixed; inset:0; pointer-events:none; z-index:50;
  background:repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,0,0,0.15) 2px, rgba(0,0,0,0.15) 4px); }
""" if t.get("scanlines") else ""

    title_style = ""
    if t.get("title_gradient") and t["title_gradient"] != "none":
        title_style = f"background:{t['title_gradient']}; -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text;"
    else:
        title_style = f"color:{t.get('title_color', '#fff')}; {t.get('title_shadow', '')}"

    overlay_extra = t.get("overlay_extra", "")
    fonts = "https://fonts.googleapis.com/css2?family=Crimson+Pro:ital,wght@0,400;0,600;1,400&family=JetBrains+Mono:wght@400&family=VT323&family=Bangers&family=Inter:wght@400&display=swap"
    # Scale fade with speed: fast = instant, slow = smooth crossfade (max 1s)
    fade_duration = round(min(initial_speed * 0.3, 1.0), 2)
    overlay_fade_delay = round(min(initial_speed * 0.15, 0.5), 2)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{display_title} &mdash; Flip Book</title>
<style>
@import url('{fonts}');
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ {t['body']} overflow:hidden; }}
.flipbook {{ position:fixed; inset:0; }}
.slide {{ position:absolute; inset:0; opacity:0; transition:opacity {fade_duration}s ease; display:flex; align-items:center; justify-content:center; }}
.slide.active {{ opacity:1; }}
.slide img {{ {t['img']} }}
{kenburns_css}
{scanlines_css}
.slide .overlay {{
    position:absolute; bottom:0; left:0; right:0;
    padding:30px 60px 40px;
    background:{t['overlay_bg']};
    color:{t['overlay_color']};
    font-size:clamp(0.85rem,1.5vw,1.1rem);
    line-height:1.7;
    text-align:center;
    max-height:40vh;
    overflow-y:auto;
    opacity:0; transform:translateY(10px);
    transition:opacity {fade_duration}s ease {overlay_fade_delay}s, transform {fade_duration}s ease {overlay_fade_delay}s;
    font-style:italic;
    letter-spacing:0.3px;
    {overlay_extra}
}}
.slide .overlay::-webkit-scrollbar {{ width:4px; }}
.slide .overlay::-webkit-scrollbar-thumb {{ background:rgba(255,255,255,0.2); border-radius:2px; }}
.slide.active .overlay {{ opacity:1; transform:translateY(0); }}
.slide .overlay strong {{ color:{t['strong_color']}; font-style:normal; }}
.slide .overlay em {{ color:{t['em_color']}; }}
.progress {{
    position:fixed; top:0; left:0; height:3px; z-index:100;
    background:{t['progress_gradient']};
    transition:width 0.3s ease;
}}
.counter {{
    position:fixed; top:16px; right:20px; z-index:100;
    font-family:'JetBrains Mono',monospace; font-size:11px;
    color:rgba(255,255,255,0.4); letter-spacing:2px;
    opacity:0; transition:opacity 0.3s;
}}
body:hover .counter {{ opacity:1; }}
.title-card {{
    position:fixed; inset:0; z-index:200;
    display:flex; flex-direction:column; align-items:center; justify-content:center;
    background:{t['title_bg']};
    transition:opacity 1s ease;
}}
.title-card.hidden {{ opacity:0; pointer-events:none; }}
.title-card h1 {{
    font-size:clamp(1.5rem,5vw,4rem); font-weight:600;
    {title_style}
    margin-bottom:16px; text-align:center;
    width:100%; word-wrap:break-word; overflow-wrap:break-word; padding:0 40px;
}}
.title-card .subtitle {{
    font-size:clamp(1rem,2vw,1.4rem); color:#888; font-style:italic;
    text-align:center; padding:0 40px; margin-bottom:16px; max-width:700px;
}}
.title-card .meta {{
    font-family:'JetBrains Mono',monospace; font-size:0.7rem;
    color:#555; letter-spacing:3px; text-transform:uppercase;
}}
</style>
</head>
<body>
<div class="title-card" id="titleCard">
    <h1>{display_title}</h1>
    {"<div class='subtitle'>" + subtitle + "</div>" if subtitle else ""}
    <div class="meta">LLM Dream &mdash; {img_count} frames</div>
</div>
<div class="progress" id="progress"></div>
<div class="counter" id="counter"></div>
<div class="flipbook" id="flipbook"></div>
<script>
const slides = {slides_json};
const showText = {'true' if hide_text != '1' else 'false'};
let current = 0, paused = false, interval = {initial_speed * 1000}, timer = null;
const fb = document.getElementById('flipbook');
const prog = document.getElementById('progress');
const counter = document.getElementById('counter');

slides.forEach((s,i) => {{
    const div = document.createElement('div');
    div.className = 'slide';
    const overlay = showText && s.text ? `<div class="overlay">${{s.text}}</div>` : '';
    div.innerHTML = `<img src="${{s.url}}" alt="">${{overlay}}`;
    fb.appendChild(div);
    const img = new Image();
    img.src = s.url;
}});

function show(idx) {{
    document.querySelectorAll('.slide').forEach((s,i) => s.classList.toggle('active', i===idx));
    prog.style.width = ((idx+1)/slides.length*100)+'%';
    counter.textContent = (idx+1)+' / '+slides.length;
    current = idx;
}}

function next() {{ show((current+1) % slides.length); }}
function prev() {{ show((current-1+slides.length) % slides.length); }}

function togglePause() {{
    paused = !paused;
    if (!paused) startTimer();
    else clearInterval(timer);
}}

function startTimer() {{
    clearInterval(timer);
    timer = setInterval(next, interval);
}}

setTimeout(() => {{
    document.getElementById('titleCard').classList.add('hidden');
    if (slides.length) show(0);
    startTimer();
}}, 3000);

document.addEventListener('keydown', e => {{
    if (e.key === 'ArrowRight' || e.key === ' ') next();
    else if (e.key === 'ArrowLeft') prev();
    else if (e.key === 'p' || e.key === 'Escape') togglePause();
}});

document.getElementById('flipbook').addEventListener('click', next);
</script>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/prompts/{session_id}")
async def prompts_export(session_id: str):
    """Export all prompts/stages from a session as plain text."""
    session_data, err = _load_session(session_id)
    if err:
        return err
    history = session_data.get("history", [])
    title = session_data.get("title", "Untitled Story")

    lines = [f"# {title}", f"# Session: {session_id}", ""]
    for i, msg in enumerate(history):
        role = msg.get("role", "")
        name = msg.get("name", role)
        content = msg.get("content", "").strip()
        images = msg.get("images", [])
        if not content and not images:
            continue
        lines.append(f"[{name}]")
        if content:
            lines.append(content)
        for img_url in images:
            lines.append(f"  [image: {img_url}]")
        lines.append("")

    return PlainTextResponse("\n".join(lines), headers={
        "Content-Disposition": f'inline; filename="{session_id}-prompts.txt"'
    })


@app.get("/storybook/{session_id}/save")
async def storybook_save(session_id: str, template: str = Query("cyberpunk"), captions: str = Query(""),
                         title: str = Query(""), subtitle: str = Query(""), prompt_title: str = Query("")):
    """Save a storybook as an HTML file in static/exports/ and return the URL."""
    session_data, err = _load_session(session_id)
    if err:
        return err
    history = session_data.get("history", [])
    session_title = session_data.get("title", "Untitled Story")
    show_captions = captions == "1"
    use_prompt_title = prompt_title == "1"

    story_body, img_count, first_prompt = _build_story_sections(history, captions=show_captions)
    style = STORYBOOK_STYLES.get(template, STORYBOOK_STYLES["cyberpunk"])
    display_title = title if title else (first_prompt if use_prompt_title and first_prompt else session_title)
    subtitle_html = f'<div class="storybook-subtitle">{subtitle}</div>' if subtitle else ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{display_title} &mdash; LLM Dream Storybook</title>
<style>{style}</style>
</head>
<body>
<header class="storybook-header">
    <h1 class="storybook-title">{display_title}</h1>
    {subtitle_html}
    <div class="storybook-meta">LLM Dream &mdash; {img_count} illustrations</div>
</header>
<article class="storybook-content">
{story_body}
</article>
<footer class="storybook-footer">
    Generated by LLM Dream
</footer>
</body>
</html>"""

    # Save to exports folder
    safe_title = re.sub(r'[^a-zA-Z0-9_-]', '', title.replace(' ', '-'))[:50]
    filename = f"storybook-{safe_title}-{session_id[:8]}.html"
    filepath = os.path.join(EXPORTS_DIR, filename)
    with open(filepath, "w") as f:
        f.write(html)

    return HTMLResponse(html, headers={
        "X-Export-Path": f"/static/exports/{filename}"
    })


async def broadcast(data: dict):
    msg = json.dumps(data)
    for ws in connected_clients[:]:
        try:
            await ws.send_text(msg)
        except Exception:
            try:
                connected_clients.remove(ws)
            except ValueError:
                pass


async def generate_response(model_id: str) -> bool | str:
    """Generate a response from a model and broadcast it token by token.
    Returns True on success, False if killed/interrupted, 'skip' if degenerate."""
    model_cfg = ctx.models.get(model_id, {})
    if not model_cfg:
        return False

    display_name = model_cfg.get("display_name", model_id)
    color = model_cfg.get("color", "#888")
    ollama_model = model_cfg.get("ollama_model", model_id)  # actual Ollama model name
    ollama_host = model_cfg.get("ollama_host", DEFAULT_OLLAMA_HOST)
    system_prompt = ctx.build_system_prompt(model_id)
    messages = ctx.build_ollama_messages(model_id)

    # Build per-model generation options
    options = {"num_ctx": 4096}  # Smaller context = faster inference
    for key in ("temperature", "top_p", "top_k", "repeat_penalty"):
        if key in model_cfg:
            options[key] = model_cfg[key]
    # Cap response length to prevent wall-of-text but not cut off mid-thought
    num_predict = model_cfg.get("num_predict", -1)
    options["num_predict"] = 800 if num_predict == -1 else min(num_predict, 800)

    await broadcast({
        "type": "start",
        "model_id": model_id,
        "name": display_name,
        "color": color,
    })

    # Always offer image gen — check if THIS model's last response had an image
    global turns_since_image
    offer_tool = image_gen_available()
    my_last_had_image = False
    my_turns_without_image = 0
    if offer_tool:
        for msg in reversed(ctx.history):
            if msg.get("role") == model_id:
                if msg.get("images") or msg.get("content", "").startswith("!["):
                    my_last_had_image = True
                    break
                else:
                    my_turns_without_image += 1
                    if my_turns_without_image >= 3:
                        break
    tools = [IMAGE_GEN_TOOL] if offer_tool else None

    # ALWAYS nudge if this model hasn't generated an image in its last response
    if offer_tool and not my_last_had_image:
        nudge_prompts = [
            'You MUST include a {"generate_image": "prompt"} JSON block at the end of your response. Do it NOW.',
            'REMINDER: End your response with {"generate_image": "detailed visual prompt"} — this is mandatory.',
            'You forgot to generate an image last time. Add {"generate_image": "prompt"} at the end of this response.',
        ]
        nudge = random.choice(nudge_prompts)
        messages.append({"role": "user", "content": f"[System]: {nudge}"})

    full_response = ""
    tool_calls = []

    try:
        # ALWAYS stream — text-based tool call extraction handles image prompts
        async def _stream_response():
            nonlocal full_response
            async for chunk in chat_stream(ollama_model, messages, system_prompt, options=options or None, tools=None, host=ollama_host):
                if kill_event.is_set():
                    await broadcast({"type": "end", "model_id": model_id, "name": display_name})
                    return False
                if isinstance(chunk, str):
                    full_response += chunk
                    await broadcast({
                        "type": "token",
                        "model_id": model_id,
                        "token": chunk,
                    })
            return True

        result = await asyncio.wait_for(_stream_response(), timeout=120)
        if result is False:
                return False

    except asyncio.TimeoutError:
        print(f"[generate_response] {display_name} timed out (120s)")
        await broadcast({"type": "system", "message": f"{display_name} timed out, skipping."})
        await broadcast({"type": "end", "model_id": model_id, "name": display_name})
        return "skip"
    except asyncio.CancelledError:
        await broadcast({"type": "end", "model_id": model_id, "name": display_name})
        return False
    except Exception as e:
        print(f"[generate_response] Error from {display_name}: {e}")
        await broadcast({"type": "error", "message": f"Error from {display_name}: {e}"})
        await broadcast({"type": "end", "model_id": model_id, "name": display_name})
        return "skip"  # Don't kill auto-chat on transient errors, just skip turn

    # Store text response
    if full_response:
        raw_response = full_response.strip()

        # === STEP 1: Extract text-based tool calls from RAW response (before stripping) ===
        if not tool_calls and raw_response:
            # Try to parse JSON tool call arrays that models dump as text
            json_match = re.search(r'\[\s*\{[^[]*?"name"\s*:\s*"[^"]*image[^"]*"[^[]*?"prompt"\s*:\s*"([^"]{10,})"', raw_response, re.IGNORECASE | re.DOTALL)
            if json_match:
                extracted = json_match.group(1).strip()
                print(f"[text-tool-extract] Extracted JSON tool call prompt from {display_name}: {extracted[:80]}")
                tool_calls.append({"function": {"name": "generate_image", "arguments": {"prompt": extracted}}})

            # Catch {"generate_image": "prompt text"} format (models use this instead of tool calls)
            if not tool_calls:
                gen_img_match = re.search(r'"generate_image"\s*:\s*"([^"]{10,})"', raw_response, re.IGNORECASE)
                if gen_img_match:
                    extracted = gen_img_match.group(1).strip()
                    print(f"[text-tool-extract] Extracted generate_image value from {display_name}: {extracted[:80]}")
                    tool_calls.append({"function": {"name": "generate_image", "arguments": {"prompt": extracted}}})

            # Catch any key containing "prompt" with a long string value (models invent endless key names)
            if not tool_calls:
                prompt_match = re.search(r'"[^"]*prompt[^"]*"\s*:\s*"([^"]{10,})"', raw_response, re.IGNORECASE)
                if prompt_match:
                    extracted = prompt_match.group(1).strip()
                    print(f"[text-tool-extract] Extracted prompt from JSON in {display_name}: {extracted[:80]}")
                    tool_calls.append({"function": {"name": "generate_image", "arguments": {"prompt": extracted}}})

            # Catch "Generate_image:" without JSON — models write plain text prompts
            if not tool_calls:
                plain_match = re.search(r'[Gg]enerate_?[Ii]mage\s*:\s*(.{10,?)(?:\n|$)', raw_response)
                if plain_match:
                    extracted = plain_match.group(1).strip().rstrip('.')
                    # Don't extract if it's just instructions about how to format
                    if not any(kw in extracted.lower() for kw in ['json block', 'json format', 'please ensure', 'adhere']):
                        print(f"[text-tool-extract] Extracted plain Generate_image from {display_name}: {extracted[:80]}")
                        tool_calls.append({"function": {"name": "generate_image", "arguments": {"prompt": extracted}}})

            from urllib.parse import unquote
            # First, try to extract from URL-encoded generate_image links (models' favorite fake format)
            url_match = re.search(r'generate_image\?prompt=([^\s\)"\*]+)', raw_response)
            if url_match:
                decoded_prompt = unquote(url_match.group(1)).replace('+', ' ').replace(',', ', ').strip()
                # Clean trailing junk
                decoded_prompt = re.sub(r'["\*\)]+$', '', decoded_prompt).strip()
                if len(decoded_prompt) > 10:
                    print(f"[text-tool-extract] Extracted URL-encoded prompt from {display_name}: {decoded_prompt[:80]}")
                    tool_calls.append({"function": {"name": "generate_image", "arguments": {"prompt": decoded_prompt}}})

            # Next, try other text patterns
            if not tool_calls:
                text_tool_patterns = [
                    # [Image generated: prompt description here]
                    r'\[Image generated:\s*(.{10,}?)\]',
                    # [Image: description text here] — wildcard's preferred format
                    r'\[Image:\s*(.{10,}?)\]',
                    # "Image prompt: "quoted text""
                    r'[Ii]mage [Pp]rompt:?\s*\n?\s*"([^"]{10,})"',
                    # *Call tool:* generate_image("prompt") or generate_image("prompt")
                    r'generate_image\s*\(\s*"([^"]{10,})"\s*\)',
                    # "prompt for the generate_image tool:\n\n"quoted text""
                    r'generate_image\s+tool:?\s*\n*\s*"([^"]{10,})"',
                    # Any long quoted string after "prompt:" or "description:"
                    r'(?:prompt|description)\s*(?:for[^:]*)?:\s*\n*\s*"([^"]{10,})"',
                    # JSON-like: "prompt": "..."
                    r'"prompt"\s*:\s*"([^"]{10,})"',
                    # !generate_image prompt text
                    r'!generate_image\s+(.{10,}?)(?:\n|$)',
                    # generate_image: "prompt"
                    r'generate_image\s*:\s*["\']([^"\']{10,})["\']',
                    # *generates_image*: description
                    r'\*generates?_image\*\s*:?\s*(.{10,}?)(?:\n\n|\n\*|$)',
                    # "Generate an image" or "Generate a visual of" followed by quoted text
                    r'[Gg]enerate\s+(?:an?\s+)?(?:image|visual|picture|photo)\s+(?:of\s+)?"([^"]{10,})"',
                    # Catch-all: first long quoted string in the response as last resort
                ]
                raw_lower = raw_response.lower()
                has_image_intent = any(kw in raw_lower for kw in [
                    'generate_image', 'image prompt', '[image generated',
                    '*generates_image', 'call tool', 'generate an image',
                    'generating image', 'let me generate', '[image:',
                    'from pil import', 'image.new(', 'img.save(',
                    'generates image', '(generates image)',
                ])
                if has_image_intent:
                    for pattern in text_tool_patterns:
                        match = re.search(pattern, raw_response, re.IGNORECASE)
                        if match:
                            extracted_prompt = match.group(1).strip()
                            if len(extracted_prompt) > 10:
                                print(f"[text-tool-extract] Extracted image prompt from {display_name}: {extracted_prompt[:80]}")
                                tool_calls.append({"function": {"name": "generate_image", "arguments": {"prompt": extracted_prompt}}})
                                break

            # FALLBACK: If model wrote a response but no image prompt was extracted,
            # use the last sentence/line as an image prompt (for models that ignore the JSON format)
            if not tool_calls and offer_tool and raw_response and len(raw_response) > 30:
                # Grab the last non-empty line that's descriptive enough
                lines = [l.strip() for l in raw_response.strip().split('\n') if l.strip() and len(l.strip()) > 20]
                if lines:
                    last_line = lines[-1]
                    # Strip leading markers
                    last_line = re.sub(r'^[\*\-\>\#]+\s*', '', last_line).strip()
                    # Only use if it looks descriptive (not dialogue or meta)
                    if (len(last_line) > 25
                        and not last_line.startswith('"')
                        and not last_line.startswith("'")
                        and 'would you like' not in last_line.lower()
                        and 'let me know' not in last_line.lower()
                        and 'option' not in last_line.lower()):
                        print(f"[text-tool-extract] FALLBACK last-line prompt from {display_name}: {last_line[:80]}")
                        tool_calls.append({"function": {"name": "generate_image", "arguments": {"prompt": last_line}}})

        # === STEP 2: Clean response for display ===
        clean_response = raw_response
        # Strip think tags leaked by qwen models
        clean_response = re.sub(r'</?\s*think\s*>', '', clean_response, flags=re.IGNORECASE)
        # Strip image markdown refs
        clean_response = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', clean_response)
        clean_response = re.sub(r'\[img[^\]]*\]', '', clean_response, flags=re.IGNORECASE)

        # Strip URL-encoded fake generate_image links: (https://generate_image?prompt=...) and variants
        clean_response = re.sub(r'\(?\s*https?://generate_image\?prompt=[^\s\)]*\s*\)?', '', clean_response, flags=re.IGNORECASE)
        clean_response = re.sub(r'\(?\s*generates?_image\?prompt=[^\s\)]*\s*\)?', '', clean_response, flags=re.IGNORECASE)
        # Strip (generates image) and *generates image* style markers
        clean_response = re.sub(r'\(generates?\s+image\)', '', clean_response, flags=re.IGNORECASE)
        clean_response = re.sub(r'\*generates?\s+image\*', '', clean_response, flags=re.IGNORECASE)
        # Strip "Generate an image/visual/picture of "quoted text"" lines
        clean_response = re.sub(r'^.*[Gg]enerate\s+(?:an?\s+)?(?:image|visual|picture|photo)\s+(?:of\s+)?"[^"]*".*$', '', clean_response, flags=re.MULTILINE)
        # Strip "Generate_image: prompt text" lines (plain text, no JSON)
        clean_response = re.sub(r'^[Gg]enerate_?[Ii]mage\s*:.*$', '', clean_response, flags=re.MULTILINE)
        # Strip *generates_image?prompt=...* style
        clean_response = re.sub(r'\*generates?_image[^*]*\*', '', clean_response, flags=re.IGNORECASE)
        # Strip fake imgur/external image links
        clean_response = re.sub(r'\(https?://i\.imgur\.com/[^\)]+\)', '', clean_response, flags=re.IGNORECASE)

        # Strip all text-based image generation attempts
        clean_response = re.sub(r'\*Call tool:?\*?\s*generate_image\s*\([^)]*\)', '', clean_response, flags=re.IGNORECASE)
        clean_response = re.sub(r'generate_image\s*\([^)]*\)', '', clean_response, flags=re.IGNORECASE)
        clean_response = re.sub(r'\(?\s*generate_image\s*:\s*"[^"]*"\s*\)?', '', clean_response, flags=re.IGNORECASE)
        clean_response = re.sub(r'`generate_image[^`]*`', '', clean_response, flags=re.IGNORECASE)
        clean_response = re.sub(r'\[Image generated[^\]]*\]', '', clean_response, flags=re.IGNORECASE)
        clean_response = re.sub(r'\[generate_image\]', '', clean_response, flags=re.IGNORECASE)
        clean_response = re.sub(r'\[TOOL_CALLS?\]', '', clean_response, flags=re.IGNORECASE)
        clean_response = re.sub(r'!generate_image\s+[^\n]+', '', clean_response, flags=re.IGNORECASE)
        clean_response = re.sub(r'\*generates?_image\*\s*:?\s*[^\n]+', '', clean_response, flags=re.IGNORECASE)
        clean_response = re.sub(r'^[Ii]mage [Pp]rompt:?\s*\n?\s*"[^"]*"', '', clean_response, flags=re.MULTILINE)
        clean_response = re.sub(r'\*Generating Image\.{0,3}\*', '', clean_response, flags=re.IGNORECASE)
        clean_response = re.sub(r'\*Awaiting[^*]*\*', '', clean_response, flags=re.IGNORECASE)
        clean_response = re.sub(r'\*Image generated\*', '', clean_response, flags=re.IGNORECASE)
        # Strip JSON-like tool call blocks (case-insensitive — models output generate_IMAGE etc)
        clean_response = re.sub(r'\{"name":\s*"generate_image".*?\}\s*\}', '', clean_response, flags=re.DOTALL | re.IGNORECASE)
        clean_response = re.sub(r'"name":\s*"generate_image".*?"prompt":\s*"[^"]*".*?\}?\}?', '', clean_response, flags=re.DOTALL | re.IGNORECASE)
        # Strip JSON arrays of tool calls: [{"name": "generate_image", ...}]
        clean_response = re.sub(r'\[\s*\{\s*"name"\s*:\s*"generate[_\s]?image"[\s\S]*?\}\s*\]', '', clean_response, flags=re.IGNORECASE)
        # Strip any remaining {"name": "..._image...", "arguments": {...}} blocks
        clean_response = re.sub(r'\{[^}]*"name"\s*:\s*"[^"]*image[^"]*"[^}]*"arguments"\s*:\s*\{[^}]*\}\s*\}', '', clean_response, flags=re.IGNORECASE)
        # Strip {"generate_image": "..."} blocks
        clean_response = re.sub(r'\{\s*"generate_image"\s*:\s*"[^"]*"\s*\}', '', clean_response, flags=re.IGNORECASE)
        # Nuclear: strip ANY JSON block containing a key with "prompt" or "generate" in it
        clean_response = re.sub(r'\{[^{}]*"[^"]*(?:prompt|generate)[^"]*"\s*:[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', '', clean_response, flags=re.IGNORECASE | re.DOTALL)
        # Strip any code-fenced block containing "prompt" or "generate_image" (catches all variations)
        clean_response = re.sub(r'```[^`]*"[^"]*(?:prompt|generate_image)[^"]*"\s*:[^`]*```', '', clean_response, flags=re.IGNORECASE | re.DOTALL)
        # Strip "Generate image" standalone lines
        clean_response = re.sub(r'^\s*Generate\s+image\.?\s*$', '', clean_response, flags=re.MULTILINE | re.IGNORECASE)
        clean_response = re.sub(r'^In this image,?\s+we\s+(can\s+)?see\b.*$', '', clean_response, flags=re.MULTILINE | re.IGNORECASE)

        # Strip roleplay — models writing as other participants or prefixing with their own name
        all_names = [cfg.get("display_name", mid) for mid, cfg in ctx.models.items()]
        all_names.append("User")
        for name in all_names:
            # **name**: or **name**:  (bold name, colon outside or inside)
            clean_response = re.sub(rf'\*\*{re.escape(name)}\*\*\s*:', '', clean_response, flags=re.IGNORECASE)
            clean_response = re.sub(rf'\*\*{re.escape(name)}:\*\*\s*', '', clean_response, flags=re.IGNORECASE)
            # [name] or [name]: anywhere in text
            clean_response = re.sub(rf'\[{re.escape(name)}\]:?\s*', '', clean_response, flags=re.IGNORECASE)
        # Strip lines where model writes dialogue for OTHER participants (full lines)
        other_names = [cfg.get("display_name", mid) for mid, cfg in ctx.models.items() if mid != model_id]
        other_names.append("User")
        for name in other_names:
            # Remove entire lines that start with another participant's name in bold or brackets
            clean_response = re.sub(rf'^\s*\*\*{re.escape(name)}\*\*\s*:.*$', '', clean_response, flags=re.MULTILINE | re.IGNORECASE)
            clean_response = re.sub(rf'^\s*\[{re.escape(name)}\]:?.*$', '', clean_response, flags=re.MULTILINE | re.IGNORECASE)
            # Also strip "*engineer generates another image*:" style
            clean_response = re.sub(rf'\*{re.escape(name)}\s+generates?[^*]*\*:?\s*', '', clean_response, flags=re.IGNORECASE)

        # Strip common model artifacts
        clean_response = re.sub(r'<\|[^|]*\|>', '', clean_response)
        # Strip PIL/Python code blocks that are fake image generation attempts
        clean_response = re.sub(r'```python\s*\n(?:from PIL|import PIL|from diffusers|img\s*=|draw\s*=|image\s*=)[\s\S]*?```', '', clean_response, flags=re.IGNORECASE)
        # Strip [Image: description] text patterns (already extracted for tool use)
        clean_response = re.sub(r'\[Image:\s*[^\]]+\]', '', clean_response, flags=re.IGNORECASE)
        # Strip empty or near-empty code blocks and orphaned language tags
        clean_response = re.sub(r'```+\s*```+', '', clean_response)
        clean_response = re.sub(r'````*\s*$', '', clean_response)
        clean_response = re.sub(r'^\s*````*', '', clean_response)
        clean_response = re.sub(r'^\s*json\s*$', '', clean_response, flags=re.MULTILINE | re.IGNORECASE)
        # Strip "*A group of ragged men..." style image descriptions in italic
        clean_response = re.sub(r'^\*[A-Z][^*]{20,}\*$', '', clean_response, flags=re.MULTILINE)
        # Collapse excessive whitespace
        clean_response = re.sub(r'\n{3,}', '\n\n', clean_response).strip()

        # Light degenerate check — only catch truly broken output
        clean_response = re.sub(r'<\|[^|]*\|>', '', clean_response).strip()
        # Strip short image reference junk (img-1, img_6, image-3, etc)
        clean_response = re.sub(r'^\s*img[-_]?\d+\.?\s*$', '', clean_response, flags=re.MULTILINE | re.IGNORECASE)
        clean_response = re.sub(r'^\s*image[-_]?\d+\.?\s*$', '', clean_response, flags=re.MULTILINE | re.IGNORECASE)
        clean_response = clean_response.strip()

        is_degenerate = False
        if not clean_response:
            is_degenerate = True
        elif len(clean_response) < 3:
            is_degenerate = True
        else:
            # Only flag exact copy-paste of the model's own last message
            recent = [m["content"] for m in ctx.history[-3:] if m["role"] == model_id]
            if recent and clean_response == recent[-1]:
                is_degenerate = True

        if is_degenerate and not tool_calls:
            print(f"[generate_response] {display_name} degenerate output, skipping")
            await broadcast({"type": "end", "model_id": model_id, "name": display_name})
            return "skip"

        if clean_response:
            ctx.add_message(model_id, display_name, clean_response)
            turns_since_image += 1

    # Handle tool calls (image generation) — appended to the model's streaming message
    for tc in tool_calls:
        func = tc.get("function", {})
        if func.get("name") == "generate_image":
            prompt = func.get("arguments", {}).get("prompt", "")
            if prompt:
                # Show spinner placeholder inline in the stream
                await broadcast({
                    "type": "image_inline_start",
                    "model_id": model_id,
                    "prompt": prompt,
                })
                img_url = await generate_image(
                                    prompt,
                                    steps=sd_settings["steps"],
                                    guidance_scale=sd_settings["guidance_scale"],
                                    width=sd_settings["width"],
                                    height=sd_settings["height"],
                                    negative_prompt=sd_settings["negative_prompt"],
                                    session_id=ctx.current_session_id,
                                )
                if img_url:
                    img_msg = f"![Generated image]({img_url})"
                    # Append image to the model's last text message instead of creating a separate entry
                    appended = False
                    for msg in reversed(ctx.history):
                        if msg.get("role") == model_id and not msg.get("content", "").startswith("!["):
                            msg["content"] += f"\n\n{img_msg}"
                            if "images" not in msg:
                                msg["images"] = []
                            msg["images"].append(img_url)
                            appended = True
                            break
                    if not appended:
                        ctx.add_message(model_id, display_name, img_msg, images=[img_url])
                    ctx.save_session()
                    turns_since_image = 0
                    await broadcast({
                        "type": "image_inline_ready",
                        "model_id": model_id,
                        "url": img_url,
                    })
                else:
                    await broadcast({
                        "type": "image_inline_failed",
                        "model_id": model_id,
                    })

    await broadcast({
        "type": "end",
        "model_id": model_id,
        "name": display_name,
    })
    return True


async def respond_to_user():
    """All active models respond to the user's latest message, then auto-start auto-chat."""
    global auto_chat_active, current_generation_task
    try:
        model_ids = list(ctx.models.keys())

        # Detect multi-stage prompt IMMEDIATELY so schedule pins right away
        if len(ctx.models) >= 2 and not auto_chat_active:
            last_user = None
            for msg in reversed(ctx.history):
                if msg.get("role") == "user":
                    last_user = msg.get("content", "")
                    break
            if last_user:
                stages = parse_story_stages(last_user)
                if stages:
                    story_stages.clear()
                    story_stages.extend(stages)
                    global current_stage, stage_started_at
                    current_stage = 0
                    stage_started_at = time.time()
                    first_stage = stages[0]
                    director_msg = f"[Stage 1/{len(stages)}] {first_stage}"
                    ctx.add_message("user", "Director", director_msg)
                    await broadcast({
                        "type": "message",
                        "role": "user",
                        "name": "Director",
                        "content": director_msg,
                    })
                    await broadcast({
                        "type": "stage_update",
                        "current": 1,
                        "total": len(stages),
                        "text": first_stage,
                    })
                    current_stage = 1
                    await broadcast({
                        "type": "stages_schedule",
                        "stages": stages,
                        "interval_minutes": stage_interval_minutes,
                        "current": 1,
                    })
                    print(f"[story] Parsed {len(stages)}-stage story from chat. Stage 1: {first_stage[:60]}")

        # Show thinking indicators for all models about to respond
        for mid in model_ids:
            cfg = ctx.models.get(mid, {})
            await broadcast({
                "type": "thinking",
                "model_id": mid,
                "name": cfg.get("display_name", mid),
                "color": cfg.get("color", "#888"),
            })
        async with generation_lock:
            for model_id in model_ids:
                if kill_event.is_set():
                    break
                await generate_response(model_id)

        # Auto-start auto-chat if 2+ models and not killed
        if len(ctx.models) >= 2 and not kill_event.is_set() and not auto_chat_active:
            auto_chat_active = True
            kill_event.clear()
            current_generation_task = asyncio.create_task(auto_chat_loop())
            await broadcast({"type": "auto_chat_status", "active": True})
    except asyncio.CancelledError:
        pass


def parse_story_stages(prompt: str) -> list[str]:
    """Parse stages from a prompt. Supports numbered lists, bullets, or plain newlines.
    Each non-empty line becomes a stage. Leading markers (1. / - / Step 1:) are stripped."""
    lines = [l.strip() for l in prompt.strip().split('\n') if l.strip()]
    if len(lines) < 2:
        return []
    stages = []
    for line in lines:
        # Strip optional leading markers: "1. ", "1) ", "Step 1: ", "- ", "* "
        cleaned = re.sub(r'^(?:\d+[.)]\s*|[Ss]tep\s+\d+[:.]\s*|[-*]\s+)', '', line).strip()
        if cleaned:
            stages.append(cleaned)
    return stages if len(stages) >= 2 else []


async def inject_next_stage():
    """Inject the next story stage as a Director message. Returns True if injected, False if no more stages."""
    global current_stage, stage_started_at
    if current_stage >= len(story_stages):
        return False
    stage_text = story_stages[current_stage]
    stage_num = current_stage + 1
    total = len(story_stages)
    director_msg = f"[Stage {stage_num}/{total}] {stage_text}"
    ctx.add_message("user", "Director", director_msg)
    await broadcast({
        "type": "message",
        "role": "user",
        "name": "Director",
        "content": director_msg,
    })
    await broadcast({
        "type": "stage_update",
        "current": stage_num,
        "total": total,
        "text": stage_text,
    })
    current_stage += 1
    stage_started_at = time.time()
    await broadcast({
        "type": "stages_schedule",
        "stages": list(story_stages),
        "interval_minutes": stage_interval_minutes,
        "current": stage_num,
    })
    print(f"[story] Stage {stage_num}/{total}: {stage_text[:60]}")

    # If this was the last stage and auto-kill-after-last is set, schedule delayed kill
    if current_stage >= len(story_stages):
        auto_kill_after = _auto_kill_after_last_minutes
        if auto_kill_after and auto_kill_after > 0:
            async def _delayed_kill():
                await asyncio.sleep(auto_kill_after * 60)
                if auto_chat_active:
                    print(f"[stage-builder] Auto-kill after last: {auto_kill_after}min elapsed. Stopping.")
                    await kill_all()
            asyncio.create_task(_delayed_kill())
            await broadcast({"type": "system", "message": f"All stages sent. Auto-stopping in {auto_kill_after} minutes."})
        else:
            await broadcast({"type": "system", "message": "All stages sent. Auto-chat continues until killed."})

    return True


async def auto_chat_loop():
    """Round-robin auto-chat: each model takes a turn. NEVER stops unless killed or toggled off."""
    global auto_chat_active
    while auto_chat_active and not kill_event.is_set():
        model_ids = list(ctx.models.keys())
        if not model_ids:
            await asyncio.sleep(2)
            continue

        # Check if we should advance to next story stage (time-based)
        if story_stages and current_stage < len(story_stages):
            elapsed = time.time() - stage_started_at
            # Use per-stage interval if available, otherwise global
            cur_interval = stage_intervals[current_stage] if current_stage < len(stage_intervals) else stage_interval_minutes
            if elapsed >= cur_interval * 60:
                await inject_next_stage()

        for model_id in model_ids:
            if not auto_chat_active or kill_event.is_set():
                return
            try:
                async with generation_lock:
                    if not auto_chat_active or kill_event.is_set():
                        return
                    result = await generate_response(model_id)
                    if result == "skip":
                        await asyncio.sleep(2)
                        continue
                    elif not result:
                        return
            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"[auto_chat_loop] Error during {model_id}: {e}")
                await asyncio.sleep(2)
                continue
            if not auto_chat_active or kill_event.is_set():
                return
            await asyncio.sleep(1)


async def kill_all():
    """Stop everything immediately."""
    global auto_chat_active, current_generation_task
    auto_chat_active = False
    kill_event.set()
    # Cancel auto-chat task
    if current_generation_task and not current_generation_task.done():
        current_generation_task.cancel()
        try:
            await current_generation_task
        except (asyncio.CancelledError, Exception):
            pass
        current_generation_task = None
    # Cancel all response tasks
    for task in list(active_tasks):
        if not task.done():
            task.cancel()
    for task in list(active_tasks):
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    active_tasks.clear()
    # Release lock if stuck (force-create new one)
    global generation_lock
    if generation_lock.locked():
        generation_lock = asyncio.Lock()
    await broadcast({"type": "killed", "message": "All generation stopped."})
    await broadcast({"type": "auto_chat_status", "active": False})
    await asyncio.sleep(0.1)
    kill_event.clear()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    global auto_chat_active, current_generation_task, _last_user_msg_time, stage_interval_minutes, _auto_kill_after_last_minutes
    await ws.accept()
    connected_clients.append(ws)

    # Clean junk from history before sending
    ctx.clean_history()

    # Send initial state
    try:
        models = await list_models()
    except Exception:
        models = []

    await ws.send_text(json.dumps({
        "type": "init",
        "available_models": models,
        "active_models": ctx.models,
        "user_profile": ctx.user_profile,
        "history": ctx.history,
        "auto_chat_active": auto_chat_active,
        "sessions": ctx.list_sessions(),
        "current_session_id": ctx.current_session_id,
        "sd_settings": sd_settings,
        "sd_available": image_gen_available(),
        "model_memories": ctx._session_memories,
        "stage_interval_minutes": stage_interval_minutes,
    }))

    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            action = data.get("action")

            if action == "send_message":
                content = data["content"]

                # Handle /image command from user
                img_cmd = re.match(r'^/image\s+(.+)', content, re.IGNORECASE)
                if img_cmd:
                    prompt = img_cmd.group(1).strip()
                    if image_gen_available():
                        await broadcast({"type": "system", "message": f"Generating image: {prompt}..."})
                        img_url = await generate_image(
                                    prompt,
                                    steps=sd_settings["steps"],
                                    guidance_scale=sd_settings["guidance_scale"],
                                    width=sd_settings["width"],
                                    height=sd_settings["height"],
                                    negative_prompt=sd_settings["negative_prompt"],
                                    session_id=ctx.current_session_id,
                                )
                        if img_url:
                            img_msg = f"![Generated image]({img_url})"
                            ctx.add_message("user", "User", img_msg, images=[img_url])
                            await broadcast({
                                "type": "message",
                                "role": "user",
                                "name": "User",
                                "content": img_msg,
                            })
                        else:
                            await broadcast({"type": "system", "message": "Image generation failed."})
                    else:
                        await broadcast({"type": "system", "message": "Image generation not available. Install: pip install diffusers torch accelerate"})
                    continue

                ctx.add_message("user", "User", content)
                await broadcast({
                    "type": "message",
                    "role": "user",
                    "name": "User",
                    "content": content,
                })

                now = time.time()
                double_tap = (now - _last_user_msg_time) < _INTERRUPT_WINDOW
                _last_user_msg_time = now

                if auto_chat_active:
                    if double_tap:
                        # INTERRUPT — stop everything and respond immediately
                        auto_chat_active = False
                        kill_event.set()
                        if current_generation_task and not current_generation_task.done():
                            current_generation_task.cancel()
                            try:
                                await current_generation_task
                            except (asyncio.CancelledError, Exception):
                                pass
                            current_generation_task = None
                        if generation_lock.locked():
                            generation_lock = asyncio.Lock()
                        kill_event.clear()
                        await broadcast({"type": "killed"})
                        # Start fresh response cycle
                        task = asyncio.create_task(respond_to_user())
                        active_tasks.add(task)
                        task.add_done_callback(active_tasks.discard)
                    else:
                        # QUEUE — message is in history, models see it next turn
                        await broadcast({"type": "queued"})
                else:
                    # No auto-chat running, respond normally
                    task = asyncio.create_task(respond_to_user())
                    active_tasks.add(task)
                    task.add_done_callback(active_tasks.discard)

            elif action == "add_model":
                model_id = data["model_id"]
                display_name = data.get("display_name", model_id)
                system_prompt = data.get("system_prompt", "")
                color = data.get("color", "#6c9")
                temperature = float(data.get("temperature", 0.7))
                top_p = float(data.get("top_p", 0.9))
                top_k = int(data.get("top_k", 40))
                repeat_penalty = float(data.get("repeat_penalty", 1.1))
                num_predict = int(data.get("num_predict", -1))
                ollama_host = data.get("ollama_host", DEFAULT_OLLAMA_HOST)
                key = ctx.add_model(model_id, display_name, system_prompt, color,
                                    temperature, top_p, top_k, repeat_penalty, num_predict,
                                    ollama_host=ollama_host)
                if key:
                    await broadcast({
                        "type": "model_added",
                        "model_id": key,
                        "config": ctx.models[key],
                    })

            elif action == "remove_model":
                model_id = data["model_id"]
                model_cfg_remove = ctx.models.get(model_id, {})
                ollama_name = model_cfg_remove.get("ollama_model", model_id)
                ollama_host_remove = model_cfg_remove.get("ollama_host", DEFAULT_OLLAMA_HOST)
                ctx.remove_model(model_id)
                # Only unload from VRAM if no other instance of this model remains on same host
                still_loaded = any(
                    cfg.get("ollama_model", mid) == ollama_name
                    and cfg.get("ollama_host", DEFAULT_OLLAMA_HOST) == ollama_host_remove
                    for mid, cfg in ctx.models.items()
                )
                if not still_loaded:
                    await unload_model(ollama_name, host=ollama_host_remove)
                await broadcast({
                    "type": "model_removed",
                    "model_id": model_id,
                })

            elif action == "update_model":
                model_id = data["model_id"]
                if model_id in ctx.models:
                    for key in ("system_prompt", "display_name", "color",
                                "temperature", "top_p", "top_k",
                                "repeat_penalty", "num_predict", "ollama_host"):
                        if key in data:
                            ctx.models[model_id][key] = data[key]
                    ctx.save_config()
                    await broadcast({
                        "type": "model_updated",
                        "model_id": model_id,
                        "config": ctx.models[model_id],
                    })

            elif action == "update_profile":
                ctx.update_user_profile(data["profile"])

            elif action == "auto_chat_start":
                if len(ctx.models) < 2:
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "message": "Auto-chat requires at least 2 models.",
                    }))
                    continue
                if not auto_chat_active:
                    # Inject initial prompt if provided, or a default seed if history is empty
                    prompt = data.get("prompt", "").strip()
                    if not prompt and not ctx.history:
                        prompt = "Hey everyone! Let's have a conversation. Introduce yourselves and share something interesting."

                    # Parse multi-stage prompts
                    stages = parse_story_stages(prompt) if prompt else []
                    story_stages.clear()
                    global current_stage, stage_started_at
                    current_stage = 0
                    stage_started_at = time.time()

                    if stages:
                        # Multi-stage mode: store stages, inject first one
                        story_stages.extend(stages)
                        first_stage = stages[0]
                        director_msg = f"[Stage 1/{len(stages)}] {first_stage}"
                        ctx.add_message("user", "Director", director_msg)
                        await broadcast({
                            "type": "message",
                            "role": "user",
                            "name": "Director",
                            "content": director_msg,
                        })
                        await broadcast({
                            "type": "stage_update",
                            "current": 1,
                            "total": len(stages),
                            "text": first_stage,
                        })
                        current_stage = 1  # next stage to inject is index 1
                        await broadcast({
                            "type": "stages_schedule",
                            "stages": stages,
                            "interval_minutes": stage_interval_minutes,
                            "current": 1,
                        })
                        print(f"[story] Started {len(stages)}-stage story. Stage 1: {first_stage[:60]}")
                    elif prompt:
                        ctx.add_message("user", "User", prompt)
                        await broadcast({
                            "type": "message",
                            "role": "user",
                            "name": "User",
                            "content": prompt,
                        })

                    auto_chat_active = True
                    kill_event.clear()
                    current_generation_task = asyncio.create_task(auto_chat_loop())
                    await broadcast({"type": "auto_chat_status", "active": True})

            elif action == "send_stages":
                # Stage builder — explicit multi-stage schedule
                stages = data.get("stages", [])
                interval = max(0.5, float(data.get("interval_minutes", 2)))
                auto_kill_after = data.get("auto_kill_after_last")  # minutes or None

                if len(stages) < 2:
                    await ws.send_text(json.dumps({"type": "error", "message": "Need at least 2 stages."}))
                    continue
                if auto_chat_active:
                    await kill_all()

                stage_interval_minutes = interval
                story_stages.clear()
                story_stages.extend(stages)
                stage_intervals.clear()
                per_intervals = data.get("intervals", [])
                if per_intervals and len(per_intervals) == len(stages):
                    stage_intervals.extend([max(0.5, float(x)) for x in per_intervals])
                else:
                    stage_intervals.extend([interval] * len(stages))
                current_stage = 0

                first_stage = stages[0]
                director_msg = f"[Stage 1/{len(stages)}] {first_stage}"
                ctx.add_message("user", "Director", director_msg)
                await broadcast({"type": "message", "role": "user", "name": "Director", "content": director_msg})
                await broadcast({"type": "stage_update", "current": 1, "total": len(stages), "text": first_stage})
                current_stage = 1
                stage_started_at = time.time()  # Start interval timer AFTER Stage 1 is sent
                await broadcast({
                    "type": "stages_schedule",
                    "stages": stages,
                    "interval_minutes": interval,
                    "current": 1,
                    "auto_kill_after_last": auto_kill_after,
                })
                print(f"[stage-builder] Started {len(stages)}-stage schedule. Interval: {interval}min. Auto-kill after last: {auto_kill_after}min")

                # Store auto-kill-after-last for use when last stage fires
                _auto_kill_after_last_minutes = auto_kill_after

                auto_chat_active = True
                kill_event.clear()
                current_generation_task = asyncio.create_task(auto_chat_loop())
                await broadcast({"type": "auto_chat_status", "active": True})

            elif action == "auto_chat_stop":
                auto_chat_active = False
                if current_generation_task and not current_generation_task.done():
                    current_generation_task.cancel()
                    try:
                        await current_generation_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    current_generation_task = None
                await broadcast({"type": "auto_chat_status", "active": False})

            elif action == "kill":
                await kill_all()

            elif action == "clear_history":
                ctx.clear_history()
                await broadcast({"type": "history_cleared"})
                await broadcast({
                    "type": "sessions_list",
                    "sessions": ctx.list_sessions(),
                    "current_session_id": ctx.current_session_id,
                })

            elif action == "refresh_models":
                host = data.get("host", DEFAULT_OLLAMA_HOST)
                try:
                    models = await list_models(host=host)
                except Exception:
                    models = []
                await ws.send_text(json.dumps({
                    "type": "available_models",
                    "models": models,
                }))

            # ── Session management ──────────────────────────────

            elif action == "list_sessions":
                await ws.send_text(json.dumps({
                    "type": "sessions_list",
                    "sessions": ctx.list_sessions(),
                    "current_session_id": ctx.current_session_id,
                }))

            elif action == "create_session":
                # Stop auto-chat before switching sessions
                if auto_chat_active:
                    await kill_all()
                session_id = ctx.create_session()
                await broadcast({"type": "session_loaded", "history": [], "session_id": session_id, "model_memories": {}})
                await broadcast({
                    "type": "sessions_list",
                    "sessions": ctx.list_sessions(),
                    "current_session_id": ctx.current_session_id,
                })

            elif action == "switch_session":
                session_id = data["session_id"]
                if session_id != ctx.current_session_id:
                    # Stop auto-chat before switching
                    if auto_chat_active:
                        await kill_all()
                    ctx.switch_session(session_id)
                    ctx.clean_history()
                    await broadcast({
                        "type": "session_loaded",
                        "history": ctx.history,
                        "session_id": ctx.current_session_id,
                        "model_memories": ctx._session_memories,
                    })
                    await broadcast({
                        "type": "sessions_list",
                        "sessions": ctx.list_sessions(),
                        "current_session_id": ctx.current_session_id,
                    })

            elif action == "delete_session":
                session_id = data["session_id"]
                ctx.delete_session(session_id)
                # If we deleted the current session, a new one was created
                await broadcast({
                    "type": "session_loaded",
                    "history": ctx.history,
                    "session_id": ctx.current_session_id,
                    "model_memories": ctx._session_memories,
                })
                await broadcast({
                    "type": "sessions_list",
                    "sessions": ctx.list_sessions(),
                    "current_session_id": ctx.current_session_id,
                })

            elif action == "rename_session":
                ctx.rename_session(data["session_id"], data["title"])
                await broadcast({
                    "type": "sessions_list",
                    "sessions": ctx.list_sessions(),
                    "current_session_id": ctx.current_session_id,
                })

            elif action == "update_model_memory":
                model_id = data["model_id"]
                memory = data.get("memory", "")
                ctx.update_model_memory(model_id, memory)
                await broadcast({
                    "type": "model_updated",
                    "model_id": model_id,
                    "config": ctx.models[model_id],
                })

            elif action == "update_stage_interval":
                val = float(data.get("minutes", 2.0))
                stage_interval_minutes = max(0.5, min(720.0, val))
                await broadcast({"type": "stage_interval", "minutes": stage_interval_minutes})

            elif action == "update_sd_settings":
                for key in ("steps", "guidance_scale", "width", "height", "negative_prompt"):
                    if key in data:
                        sd_settings[key] = data[key]
                # Clamp values
                sd_settings["steps"] = max(1, min(50, int(sd_settings["steps"])))
                sd_settings["guidance_scale"] = max(0.0, min(20.0, float(sd_settings["guidance_scale"])))
                sd_settings["width"] = max(256, min(2048, int(sd_settings["width"])))
                sd_settings["height"] = max(256, min(2048, int(sd_settings["height"])))
                await broadcast({"type": "sd_settings", "sd_settings": sd_settings})

    except WebSocketDisconnect:
        try:
            connected_clients.remove(ws)
        except ValueError:
            pass


if __name__ == "__main__":
    import uvicorn
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "unknown"
    print(f"\n  Local:   http://localhost:8000")
    print(f"  Network: http://{local_ip}:8000\n")
    uvicorn.run(app, host="0.0.0.0", port=8000)

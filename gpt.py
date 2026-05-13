import chainlit as cl
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from chainlit.data.storage_clients.base import BaseStorageClient
from chainlit.server import app as chainlit_app
from fastapi import Request
from fastapi.responses import JSONResponse
import httpx
import json
import asyncio
import aiofiles
import hashlib
import re
import sqlite3
from functools import partial
from pathlib import Path
from typing import Dict, Any, Optional, Union
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
import os
import uuid

# ── Document parsing ──────────────────────────────────────────────────────────
import docx                        # python-docx  (.docx)
import fitz                        # PyMuPDF      (.pdf)

# ── LangFlow config ──────────────────────────────────────────────────────────
API_KEY    = "sk-lpdj0MppqO_-z0I73FOeo39g44qwc6ddJplTu3abNP8"
BASE_URL   = "http://10.0.0.203:8001"
FLOW_ID    = "9b44d6e7-4592-4df7-85af-95989861d4db"
RUN_URL    = f"{BASE_URL}/api/v1/run/{FLOW_ID}?stream=true"
UPLOAD_URL = f"{BASE_URL}/api/v1/files/upload/{FLOW_ID}"
CHAT_INPUT_ID = "ChatInput-LoD30"

# Set this to the LangFlow LLM component ID whose system_message you want to
# override.  Open the flow in LangFlow's editor, click the LLM node, and copy
# the component ID (e.g. "OpenAIModel-Ab1Cd").  When set, the document body is
# injected into the system message, which keeps it in a fixed prefix position
# and maximises vLLM prefix-cache hit rate.
LLM_COMPONENT_ID = "LanguageModelComponent-Ivzsi"   # ← CONFIGURE ME

SYSTEM_PROMPT = (
    "You are a helpful legal assistant. "
    "Answer questions accurately based on the provided documents. "
    "If the answer cannot be determined from the documents, say so clearly."
)

# ── Paths ─────────────────────────────────────────────────────────────────────
APP_DIR        = Path(os.path.dirname(os.path.abspath(__file__)))
DB_PATH        = APP_DIR / "chainlit_history.db"
DB_URL         = f"sqlite+aiosqlite:///{DB_PATH}"
MATTERS_FILE   = APP_DIR / "matters.json"
UPLOADS_DIR    = APP_DIR / "public" / "uploads"
MATTER_FILES   = APP_DIR / "matter_files"   # persistent per-matter file store

sqlite3.register_adapter(list, lambda v: json.dumps(v))
sqlite3.register_adapter(dict, lambda v: json.dumps(v))

# ── Filename stabilisation (Change 2) ────────────────────────────────────────
# Strips leading datetime prefixes like "2026-05-13_10-04-02_" that LangFlow
# or the upload pipeline may prepend.  A varying prefix before the document
# body breaks the vLLM prefix-cache hash chain.
_TS_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_")

def stable_filename(name: str) -> str:
    """Return the filename with any leading upload-timestamp stripped."""
    return _TS_PREFIX_RE.sub("", name)

# ── Document text extraction ─────────────────────────────────────────────────

def extract_text(file_path: str) -> str:
    """Return the plain-text content of a document file."""
    p = Path(file_path)
    ext = p.suffix.lower()
    if ext == ".docx":
        doc = docx.Document(str(p))
        return "\n".join(para.text for para in doc.paragraphs if para.text.strip())
    if ext == ".pdf":
        with fitz.open(str(p)) as pdf:
            return "\n".join(page.get_text() for page in pdf)
    if ext == ".rtf":
        try:
            from striprtf.striprtf import rtf_to_text
            return rtf_to_text(p.read_text(errors="replace"))
        except ImportError:
            return p.read_text(errors="replace")
    # .txt and everything else
    return p.read_text(errors="replace")

# ── System-message builder (Change 1) ────────────────────────────────────────

def build_system_content(doc_texts: list[dict]) -> str:
    """Compose the full system message: base instructions followed by every
    document body.

    Placing documents in the *system* message (before the user turn) means the
    entire token prefix is identical across different questions on the same
    document set, which maximises the vLLM prefix-cache hit rate.
    """
    parts = [SYSTEM_PROMPT]
    for dt in doc_texts:
        name = stable_filename(dt["name"])
        parts.append(f"\n\n--- Document: {name} ---\n\n{dt['text']}")
    return "".join(parts)

# ── Conversation history formatting ───────────────────────────────────────────

def format_history_input(history: list[dict], current_question: str) -> str:
    """Prepend prior Q&A turns to the current question so the model sees the
    full conversation context.  On the first turn this is just the question."""
    if not history:
        return current_question
    parts = ["[Previous conversation]"]
    for turn in history:
        parts.append(f"User: {turn['q']}")
        parts.append(f"Assistant: {turn['a']}")
    parts.append("")
    parts.append(f"[Current question]")
    parts.append(current_question)
    return "\n".join(parts)

# ── Local storage client ──────────────────────────────────────────────────────

class LocalStorageClient(BaseStorageClient):
    def __init__(self, storage_path: Path):
        self.storage_path = storage_path
        self.storage_path.mkdir(parents=True, exist_ok=True)

    async def upload_file(self, object_key, data, mime="application/octet-stream",
                          overwrite=True, content_disposition=None):
        file_path = self.storage_path / object_key
        file_path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            data = data.encode("utf-8")
        async with aiofiles.open(file_path, "wb") as f:
            await f.write(data)
        return {"url": f"/public/uploads/{object_key}", "object_key": object_key}

    async def delete_file(self, object_key):
        p = self.storage_path / object_key
        if p.exists():
            p.unlink()
        return True

    async def get_read_url(self, object_key):
        return f"/public/uploads/{object_key}"

    async def close(self):
        pass

# ── Per-matter persistent file store ─────────────────────────────────────────

def matter_dir(username: str) -> Path:
    d = MATTER_FILES / username
    d.mkdir(parents=True, exist_ok=True)
    return d

def load_matter_files(username: str) -> list:
    idx = matter_dir(username) / "index.json"
    return json.loads(idx.read_text()) if idx.exists() else []

def save_matter_files(username: str, files: list):
    (matter_dir(username) / "index.json").write_text(json.dumps(files, indent=2))

def persist_file_for_matter(username: str, filename: str, content: bytes) -> str:
    """Copy uploaded file into the matter's persistent store. Returns local path.

    The filename is stabilised (timestamp prefix stripped) so that re-uploads
    of the same document produce an identical on-disk name and, more
    importantly, an identical token sequence in the prompt.
    """
    safe = stable_filename(filename.replace("/", "_").replace("..", "_"))
    dest = matter_dir(username) / safe
    dest.write_bytes(content)
    files = [f for f in load_matter_files(username) if f["name"] != safe]
    files.append({"name": safe, "path": str(dest), "size": len(content)})
    save_matter_files(username, files)
    return str(dest)

def remove_file_from_matter(username: str, filename: str) -> bool:
    """Remove a file from the matter's persistent store. Returns True if found."""
    safe = stable_filename(filename)
    files = load_matter_files(username)
    updated = [f for f in files if f["name"] != safe]
    if len(updated) == len(files):
        return False
    save_matter_files(username, updated)
    # Delete from disk
    target = matter_dir(username) / safe
    if target.exists():
        target.unlink()
    return True

def upload_file_to_langflow(file_path: str) -> str:
    with httpx.Client(timeout=60) as client:
        with open(file_path, "rb") as f:
            r = client.post(UPLOAD_URL, headers={"x-api-key": API_KEY}, files={"file": f})
            r.raise_for_status()
            return r.json()["file_path"]

async def preload_matter_files(username: str) -> list:
    """Upload all persistent matter files to LangFlow and return their server paths."""
    files = load_matter_files(username)
    if not files:
        return []
    loop = asyncio.get_event_loop()
    lf_paths = []
    for f in files:
        try:
            lf_path = await loop.run_in_executor(None, partial(upload_file_to_langflow, f["path"]))
            lf_paths.append(lf_path)
        except Exception:
            pass   # file may have been deleted; skip silently
    return lf_paths

# ── Matter (user) management ─────────────────────────────────────────────────

def load_matters() -> dict:
    return json.loads(MATTERS_FILE.read_text()) if MATTERS_FILE.exists() else {}

def save_matters(matters: dict):
    MATTERS_FILE.write_text(json.dumps(matters, indent=2))

def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

# ── Registration endpoint ─────────────────────────────────────────────────────

@chainlit_app.post("/api/register")
async def register_matter(request: Request):
    data = await request.json()
    name = (data.get("matter") or "").strip()
    pw   = data.get("password") or ""
    if not name or not pw:
        return JSONResponse({"success": False, "error": "Name and password are required."})
    matters = load_matters()
    if name in matters:
        return JSONResponse({"success": False, "error": f'Matter "{name}" already exists.'})
    matters[name] = hash_password(pw)
    save_matters(matters)
    return JSONResponse({"success": True})

# ── DB init ───────────────────────────────────────────────────────────────────

async def init_db():
    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.execute(text("""CREATE TABLE IF NOT EXISTS users (
            "id" TEXT PRIMARY KEY, "identifier" TEXT NOT NULL UNIQUE,
            "metadata" TEXT NOT NULL, "createdAt" TEXT)"""))
        await conn.execute(text("""CREATE TABLE IF NOT EXISTS threads (
            "id" TEXT PRIMARY KEY, "createdAt" TEXT, "name" TEXT,
            "userId" TEXT, "userIdentifier" TEXT, "tags" TEXT, "metadata" TEXT,
            FOREIGN KEY ("userId") REFERENCES users("id") ON DELETE CASCADE)"""))
        await conn.execute(text("""CREATE TABLE IF NOT EXISTS steps (
            "id" TEXT PRIMARY KEY, "name" TEXT NOT NULL, "type" TEXT NOT NULL,
            "threadId" TEXT NOT NULL, "parentId" TEXT, "streaming" INTEGER,
            "waitForAnswer" INTEGER, "isError" INTEGER, "metadata" TEXT,
            "tags" TEXT, "input" TEXT, "output" TEXT, "createdAt" TEXT,
            "start" TEXT, "end" TEXT, "generation" TEXT, "showInput" TEXT,
            "language" TEXT, "indent" INTEGER, "defaultOpen" INTEGER, "autoCollapse" INTEGER)"""))
        await conn.execute(text("""CREATE TABLE IF NOT EXISTS elements (
            "id" TEXT PRIMARY KEY, "threadId" TEXT, "type" TEXT, "url" TEXT,
            "chainlitKey" TEXT, "name" TEXT NOT NULL, "display" TEXT,
            "objectKey" TEXT, "size" TEXT, "page" INTEGER, "language" TEXT,
            "forId" TEXT, "mime" TEXT, "props" TEXT)"""))
        await conn.execute(text("""CREATE TABLE IF NOT EXISTS feedbacks (
            "id" TEXT PRIMARY KEY, "forId" TEXT NOT NULL, "threadId" TEXT NOT NULL,
            "value" INTEGER NOT NULL, "comment" TEXT)"""))
    await engine.dispose()

@cl.on_app_startup
async def startup():
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    MATTER_FILES.mkdir(parents=True, exist_ok=True)
    await init_db()

@cl.data_layer
def get_data_layer():
    return SQLAlchemyDataLayer(conninfo=DB_URL, storage_provider=LocalStorageClient(UPLOADS_DIR))

# ── Auth ──────────────────────────────────────────────────────────────────────

@cl.password_auth_callback
def auth_callback(username: str, password: str):
    if username == "admin" and password == "admin":
        return cl.User(identifier="admin", metadata={"role": "admin"})
    matters = load_matters()
    if username in matters and matters[username] == hash_password(password):
        return cl.User(identifier=username, metadata={"role": "matter"})
    return None

# ── Chat lifecycle ────────────────────────────────────────────────────────────

async def _setup_session():
    """Pre-upload matter files and extract document texts for the system message."""
    user = cl.user_session.get("user")
    username = user.identifier if user else None
    if not username:
        cl.user_session.set("lf_file_paths", [])
        cl.user_session.set("doc_texts", [])
        cl.user_session.set("system_content", SYSTEM_PROMPT)
        cl.user_session.set("conversation_history", [])
        return

    files = load_matter_files(username)
    lf_paths = await preload_matter_files(username)
    cl.user_session.set("lf_file_paths", lf_paths)

    # Extract document text locally so we can build the system message
    # with document content in a fixed prefix position (Change 1).
    doc_texts: list[dict] = []
    for f in files:
        try:
            text_content = extract_text(f["path"])
            doc_texts.append({"name": f["name"], "text": text_content})
        except Exception as e:
            print(f"[WARN] Could not extract text from {f['path']}: {e}")
    cl.user_session.set("doc_texts", doc_texts)

    system_content = build_system_content(doc_texts) if doc_texts else SYSTEM_PROMPT
    cl.user_session.set("system_content", system_content)
    cl.user_session.set("conversation_history", [])

    if files:
        names = "\n".join(f"- {stable_filename(f['name'])}" for f in files)
        await cl.Message(
            content=f"📁 **Matter files loaded ({len(files)}):**\n{names}\n\n"
                    f"These are included automatically with every message."
        ).send()

@cl.on_chat_start
async def on_chat_start():
    await _setup_session()

@cl.on_chat_resume
async def on_chat_resume(thread):
    await _setup_session()

# ── Message handler ───────────────────────────────────────────────────────────

@cl.on_message
async def main(message: cl.Message):
    loop = asyncio.get_event_loop()
    user = cl.user_session.get("user")
    username = user.identifier if user else "unknown"

    # ── Chat commands (/list, /remove) ───────────────────────────────
    text = message.content.strip()

    if text.lower() == "/list":
        files = load_matter_files(username)
        if files:
            names = "\n".join(f"  {i+1}. `{f['name']}`" for i, f in enumerate(files))
            await cl.Message(content=f"📁 **Documents ({len(files)}):**\n{names}\n\n"
                             f"Use `/remove <filename>` to remove one.").send()
        else:
            await cl.Message(content="No documents loaded.").send()
        return

    if text.lower().startswith("/remove "):
        target = text[8:].strip().strip("`")
        target = stable_filename(target)
        if remove_file_from_matter(username, target):
            # Rebuild session caches
            files = load_matter_files(username)
            doc_texts = []
            for f in files:
                try:
                    doc_texts.append({"name": f["name"], "text": extract_text(f["path"])})
                except Exception:
                    pass
            cl.user_session.set("doc_texts", doc_texts)
            system_content = build_system_content(doc_texts) if doc_texts else SYSTEM_PROMPT
            cl.user_session.set("system_content", system_content)
            # Re-upload remaining files to LangFlow
            lf_paths = await preload_matter_files(username)
            cl.user_session.set("lf_file_paths", lf_paths)
            cl.user_session.set("conversation_history", [])
            await cl.Message(content=f"🗑️ Removed `{target}`. "
                             f"{len(files)} document(s) remaining.").send()
        else:
            files = load_matter_files(username)
            names = ", ".join(f"`{f['name']}`" for f in files) if files else "none"
            await cl.Message(content=f"⚠️ `{target}` not found. Current documents: {names}").send()
        return

    # Start with all persistent matter files already uploaded to LangFlow
    file_paths = list(cl.user_session.get("lf_file_paths") or [])

    # Handle any newly attached files
    if message.elements:
        print(f"[DEBUG] Received {len(message.elements)} element(s)")
        for element in message.elements:
            print(f"[DEBUG]   element: name={element.name}, "
                  f"type={type(element).__name__}, "
                  f"path={getattr(element, 'path', 'NO PATH')}")
            if hasattr(element, "path") and element.path:
                try:
                    with open(element.path, "rb") as f:
                        content = f.read()
                    # Persist for this matter (filename is stabilised here)
                    local_path = persist_file_for_matter(username, element.name, content)
                    # Upload to LangFlow
                    lf_path = await loop.run_in_executor(
                        None, partial(upload_file_to_langflow, local_path)
                    )
                    file_paths.append(lf_path)
                    # Cache in session so future messages in this chat include it too
                    cached = list(cl.user_session.get("lf_file_paths") or [])
                    cached.append(lf_path)
                    cl.user_session.set("lf_file_paths", cached)

                    # ── Refresh cached doc texts & system content ─────────
                    doc_texts = list(cl.user_session.get("doc_texts") or [])
                    try:
                        new_text = extract_text(local_path)
                        stable_name = stable_filename(element.name)
                        doc_texts = [d for d in doc_texts if d["name"] != stable_name]
                        doc_texts.append({"name": stable_name, "text": new_text})
                        cl.user_session.set("doc_texts", doc_texts)
                        cl.user_session.set("system_content",
                                            build_system_content(doc_texts))
                    except Exception:
                        pass  # text extraction failed; system content unchanged
                except Exception as e:
                    await cl.Message(
                        content=f"⚠️ Could not process file `{element.name}`: {e}"
                    ).send()
                    return

    response_message = cl.Message(content="")
    await response_message.send()

    thread_id = cl.context.session.thread_id
    system_content = cl.user_session.get("system_content") or SYSTEM_PROMPT

    # Build tweaks ─────────────────────────────────────────────────────
    # When LLM_COMPONENT_ID is set the document body lives in the system
    # message, so do NOT also send files via ChatInput (that would
    # duplicate the content in the user turn with a timestamped name,
    # breaking prefix caching).
    chat_files = [] if LLM_COMPONENT_ID else file_paths
    tweaks: dict[str, Any] = {CHAT_INPUT_ID: {"files": chat_files}}
    if LLM_COMPONENT_ID:
        tweaks[LLM_COMPONENT_ID] = {"system_message": system_content}

    # input_value is the user's question only (not the document body).
    # Combined with the session_id, vLLM's KV cache retains prior turns
    # so the large document prefix is computed only once (Change 3).
    print(f"[DEBUG] Sending to LangFlow: chat_files={chat_files}, "
          f"session_id={thread_id}, "
          f"system_msg_len={len(system_content)}")
    # Prepend conversation history so the model remembers prior turns
    history = cl.user_session.get("conversation_history") or []
    input_with_history = format_history_input(history, message.content)

    payload = {
        "output_type": "chat",
        "input_type": "chat",
        "input_value": input_with_history,
        "session_id": thread_id,
        "tweaks": tweaks,
    }
    headers = {"Content-Type": "application/json", "x-api-key": API_KEY}

    # Stream tokens to the UI as they arrive (async), instead of
    # collecting everything first with list(_stream()).
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("POST", RUN_URL, headers=headers, json=payload) as r:
            buffer = b""
            async for chunk in r.aiter_raw():
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("event") == "token":
                        response_message.content += event["data"].get("chunk", "")
                        await response_message.update()
                    elif event.get("event") == "end":
                        break

    # Save this exchange so subsequent turns include it as context
    history.append({"q": message.content, "a": response_message.content})
    cl.user_session.set("conversation_history", history)

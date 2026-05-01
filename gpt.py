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
import sqlite3
from functools import partial
from pathlib import Path
from typing import Dict, Any, Optional, Union
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
import os
import uuid

API_KEY    = "sk-xCHdUtMM3-OQzwHVis-QAHEtAelBx897GvzNAy7vG6E"
BASE_URL   = "http://10.0.0.203:8001"
FLOW_ID    = "8b58497d-8b8b-416d-9c14-da2b613ec504"
RUN_URL    = f"{BASE_URL}/api/v1/run/{FLOW_ID}?stream=true"
UPLOAD_URL = f"{BASE_URL}/api/v1/files/upload/{FLOW_ID}"

APP_DIR        = Path(os.path.dirname(os.path.abspath(__file__)))
DB_PATH        = APP_DIR / "chainlit_history.db"
DB_URL         = f"sqlite+aiosqlite:///{DB_PATH}"
MATTERS_FILE   = APP_DIR / "matters.json"
UPLOADS_DIR    = APP_DIR / "public" / "uploads"
MATTER_FILES   = APP_DIR / "matter_files"   # persistent per-matter file store

sqlite3.register_adapter(list, lambda v: json.dumps(v))
sqlite3.register_adapter(dict, lambda v: json.dumps(v))

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
    """Copy uploaded file into the matter's persistent store. Returns local path."""
    safe = filename.replace("/", "_").replace("..", "_")
    dest = matter_dir(username) / safe
    dest.write_bytes(content)
    files = [f for f in load_matter_files(username) if f["name"] != filename]
    files.append({"name": filename, "path": str(dest), "size": len(content)})
    save_matter_files(username, files)
    return str(dest)

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
    """Pre-upload matter files and show the user what's available."""
    user = cl.user_session.get("user")
    username = user.identifier if user else None
    if not username:
        cl.user_session.set("lf_file_paths", [])
        return

    files = load_matter_files(username)
    lf_paths = await preload_matter_files(username)
    cl.user_session.set("lf_file_paths", lf_paths)

    if files:
        names = "\n".join(f"- {f['name']}" for f in files)
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

    # Start with all persistent matter files already uploaded to LangFlow
    file_paths = list(cl.user_session.get("lf_file_paths") or [])

    # Handle any newly attached files
    if message.elements:
        for element in message.elements:
            if hasattr(element, "path") and element.path:
                try:
                    with open(element.path, "rb") as f:
                        content = f.read()
                    # Persist for this matter
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
                except Exception as e:
                    await cl.Message(content=f"⚠️ Could not process file `{element.name}`: {e}").send()
                    return

    response_message = cl.Message(content="")
    await response_message.send()

    def _stream():
        headers = {"Content-Type": "application/json", "x-api-key": API_KEY}
        payload = {
            "output_type": "chat",
            "input_type": "chat",
            "input_value": message.content,
            "session_id": str(uuid.uuid4()),
            "tweaks": {"ChatInput-Y4Iyk": {"files": file_paths}},
        }
        buffer = b""
        with httpx.Client(timeout=None) as client:
            with client.stream("POST", RUN_URL, headers=headers, json=payload) as r:
                for chunk in r.iter_raw():
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        line = line.strip()
                        if line:
                            try:
                                yield json.loads(line)
                            except json.JSONDecodeError:
                                continue

    for event in await loop.run_in_executor(None, lambda: list(_stream())):
        if event.get("event") == "token":
            response_message.content += event["data"].get("chunk", "")
            await response_message.update()
        elif event.get("event") == "end":
            break

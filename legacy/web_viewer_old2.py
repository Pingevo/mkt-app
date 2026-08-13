"""Web Viewer — Drag & drop files into agent boxes.

Run:
  python3 web_viewer.py
  → http://localhost:8778
"""

from __future__ import annotations

import asyncio
import json
import os
import queue as _queue
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
import uvicorn

from src.orchestrator import Orchestrator
from src.data_loader import detect_data_files
from src.file_loader import load_file

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"

app = FastAPI(title="MKTApp Viewer")

_orch: Orchestrator | None = None
_session_ts: str = datetime.now().strftime("%Y%m%d_%H%M%S")
_cancel_requested: bool = False
_current_llm: Any = None

AGENT_INFO = {
    "product_spec": {
        "name": "นักวิเคราะห์สินค้า",
        "desc": "สร้างสเปคสินค้าจากข้อมูลดิบ (txt, pdf, xlsx)",
        "icon": "📋",
        "accept": "raw",
    },
    "competitor_analysis": {
        "name": "นักวิเคราะห์คู่แข่ง",
        "desc": "วิเคราะห์เปรียบเทียบคู่แข่ง (ค้นหา web เอง)",
        "icon": "🔍",
        "accept": "spec",
    },
    "campaign_strategy": {
        "name": "นักวางกลยุทธ์แคมเปญ",
        "desc": "วางกลยุทธ์แคมเปญ + ราคาแนะนำ",
        "icon": "📊",
        "accept": "spec",
    },
    "content_creator": {
        "name": "นักสร้างคอนเทนต์",
        "desc": "สร้าง content + prompt รูป + hashtag",
        "icon": "✍️",
        "accept": "spec",
    },
}


def _get_orchestrator() -> Orchestrator:
    global _orch
    if _orch is None:
        _orch = Orchestrator(brand_dir="brand")
    return _orch


def _sse(event_type: str, text: str, **extra) -> str:
    data = {"type": event_type, "text": text, **extra}
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _scan_data_folders() -> list[dict[str, Any]]:
    """List product folders in data/ with summary info."""
    folders = []
    if not DATA_DIR.exists():
        return folders
    for item in sorted(DATA_DIR.iterdir()):
        if not item.is_dir() or item.name.startswith(".") or item.name == "ready":
            continue
        raw_count = 0
        image_count = 0
        ready_count = 0
        for f in item.rglob("*"):
            if not f.is_file() or f.name.startswith(".") or f.name == ".DS_Store":
                continue
            if "ready" in f.parts:
                ready_count += 1
                continue
            ext = f.suffix.lower()
            if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
                image_count += 1
            else:
                raw_count += 1
        folders.append({
            "name": item.name,
            "path": item.name,
            "raw_count": raw_count,
            "image_count": image_count,
            "ready_count": ready_count,
        })
    return folders


def _scan_sessions() -> list[dict[str, Any]]:
    sessions = []
    if not OUTPUT_DIR.exists():
        return sessions
    for item in sorted(OUTPUT_DIR.iterdir(), reverse=True):
        if not item.is_dir() or item.name.startswith(".") or item.name == ".DS_Store":
            continue
        files = []
        for f in sorted(item.iterdir()):
            if f.is_file() and not f.name.startswith(".") and f.name != ".DS_Store":
                files.append({
                    "name": f.name,
                    "size": f.stat().st_size,
                    "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                })
        if files:
            sessions.append({"name": item.name, "files": files})
    return sessions


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return HTML_PAGE


@app.get("/api/data_folders")
def api_data_folders() -> JSONResponse:
    return JSONResponse(_scan_data_folders())


@app.get("/api/sessions")
def api_sessions() -> JSONResponse:
    return JSONResponse(_scan_sessions())


@app.get("/api/file/{session}/{filename:path}")
def api_file(session: str, filename: str) -> JSONResponse:
    filepath = OUTPUT_DIR / session / filename
    if not filepath.exists() or not filepath.is_file():
        return JSONResponse({"content": "ไม่พบไฟล์"})
    return JSONResponse({"content": filepath.read_text(encoding="utf-8")})


@app.post("/api/cancel")
async def api_cancel() -> JSONResponse:
    global _cancel_requested, _current_llm
    _cancel_requested = True
    if _current_llm is not None:
        _current_llm.abort()
        _current_llm = None
    return JSONResponse({"ok": True})


@app.post("/api/run_agent")
async def api_run_agent(request: Request) -> StreamingResponse:
    """Run a single agent with a product folder via SSE."""
    body = await request.json()
    agent_key = body.get("agent", "")
    folder = body.get("folder", "")

    if not agent_key or not folder:
        return JSONResponse({"error": "missing agent or folder"})

    global _cancel_requested, _session_ts
    _cancel_requested = False
    _session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    async def event_stream():
        global _current_llm
        orch = _get_orchestrator()
        q: _queue.Queue[str | None] = _queue.Queue()

        def worker():
            try:
                output_dir = OUTPUT_DIR / _session_ts
                output_dir.mkdir(parents=True, exist_ok=True)

                agent_name = AGENT_INFO.get(agent_key, {}).get("name", agent_key)
                q.put_nowait(_sse("agent_start", f"{agent_name} — {folder}"))

                if _current_llm is None:
                    _current_llm = orch._make_client()
                llm = _current_llm

                # Read all files from the product folder
                product_dir = DATA_DIR / folder
                raw_contents = []
                image_paths = []
                ready_contents = {}

                if product_dir.exists() and product_dir.is_dir():
                    for f in sorted(product_dir.rglob("*")):
                        if not f.is_file() or f.name.startswith(".") or f.name == ".DS_Store":
                            continue
                        ext = f.suffix.lower()
                        if "ready" in f.parts:
                            if ext in {".txt", ".md"}:
                                ready_contents[f.name] = f.read_text(encoding="utf-8")
                        elif ext in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
                            image_paths.append(str(f))
                        elif ext in {".txt", ".md", ".pdf", ".xlsx", ".xls"}:
                            raw_contents.append(load_file(str(f)))

                if agent_key == "product_spec":
                    raw_data = "\n\n".join(raw_contents) if raw_contents else ""
                    if not raw_data:
                        q.put_nowait(_sse("error", f"ไม่พบข้อมูลดิบในโฟลเดอร์ {folder}"))
                        q.put_nowait(None)
                        return
                    orch.product_id = folder
                    orch.product_images = image_paths
                    q.put_nowait(_sse("stream", "กำลังสร้างสเปคสินค้า..."))
                    result = orch.run_product_spec(raw_data, image_paths, llm=llm)
                    orch.results["product_spec"] = result
                    saved = orch.save_result("product_spec", str(output_dir))
                    filepath = saved.get("product_spec", "")
                    q.put_nowait(_sse("agent_done", result[:500], agent=agent_key, file=str(filepath)))

                elif agent_key == "competitor_analysis":
                    # Use product_spec from ready/ folder
                    spec_text = ready_contents.get("product_spec.txt", "")
                    if not spec_text:
                        # Try any file with 'spec' in name
                        for fname, content in ready_contents.items():
                            if "spec" in fname.lower():
                                spec_text = content
                                break
                    if not spec_text:
                        q.put_nowait(_sse("error", f"ไม่พบสเปคสินค้าในโฟลเดอร์ {folder}/ready/"))
                        q.put_nowait(None)
                        return
                    q.put_nowait(_sse("stream", "กำลังวิเคราะห์คู่แข่ง..."))
                    result = orch.run_competitor_analysis(spec_text, None, llm=llm)
                    orch.results["competitor_analysis"] = result
                    saved = orch.save_result("competitor_analysis", str(output_dir))
                    filepath = saved.get("competitor_analysis", "")
                    q.put_nowait(_sse("agent_done", result[:500], agent=agent_key, file=str(filepath)))

                elif agent_key == "campaign_strategy":
                    spec_text = ready_contents.get("product_spec.txt", "")
                    analysis_text = ""
                    for fname, content in ready_contents.items():
                        lower = fname.lower()
                        if "competitor" in lower or "analysis" in lower:
                            analysis_text = content
                        elif "spec" in lower and not spec_text:
                            spec_text = content
                    if not spec_text:
                        q.put_nowait(_sse("error", f"ไม่พบสเปคสินค้าในโฟลเดอร์ {folder}/ready/"))
                        q.put_nowait(None)
                        return
                    q.put_nowait(_sse("stream", "กำลังวางกลยุทธ์แคมเปญ..."))
                    result = orch.run_campaign_strategy(spec_text, analysis_text, llm=llm)
                    orch.results["campaign_strategy"] = result
                    saved = orch.save_result("campaign_strategy", str(output_dir))
                    filepath = saved.get("campaign_strategy", "")
                    q.put_nowait(_sse("agent_done", result[:500], agent=agent_key, file=str(filepath)))

                elif agent_key == "content_creator":
                    spec_text = ready_contents.get("product_spec.txt", "")
                    analysis_text = ""
                    campaign_text = ""
                    for fname, content in ready_contents.items():
                        lower = fname.lower()
                        if "competitor" in lower or "analysis" in lower:
                            analysis_text = content
                        elif "campaign" in lower or "strategy" in lower:
                            campaign_text = content
                        elif "spec" in lower and not spec_text:
                            spec_text = content
                    if not spec_text:
                        q.put_nowait(_sse("error", f"ไม่พบสเปคสินค้าในโฟลเดอร์ {folder}/ready/"))
                        q.put_nowait(None)
                        return
                    q.put_nowait(_sse("stream", "กำลังสร้างคอนเทนต์..."))
                    result = orch.run_content_creator(spec_text, analysis_text, campaign_text, llm=llm)
                    orch.results["content_creator"] = result
                    saved = orch.save_result("content_creator", str(output_dir))
                    filepath = saved.get("content_creator", "")
                    q.put_nowait(_sse("agent_done", result[:500], agent=agent_key, file=str(filepath)))

                else:
                    q.put_nowait(_sse("error", f"ไม่รู้จัก agent: {agent_key}"))

                if _current_llm:
                    _current_llm.close()
                    _current_llm = None
                q.put_nowait(_sse("done", ""))
                q.put_nowait(None)

            except Exception as e:
                if _cancel_requested:
                    q.put_nowait(_sse("status", "หยุดการทำงานแล้ว"))
                    q.put_nowait(_sse("done", ""))
                    q.put_nowait(None)
                    return
                q.put_nowait(_sse("error", str(e)))
                q.put_nowait(_sse("done", ""))
                q.put_nowait(None)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        while True:
            try:
                event = q.get_nowait()
            except _queue.Empty:
                await asyncio.sleep(0.05)
                continue
            if event is None:
                break
            yield event

    return StreamingResponse(event_stream(), media_type="text/event-stream")


HTML_PAGE = """<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MKTApp</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, 'Segoe UI', sans-serif; background: #0f1117; color: #e0e0e0; }
  .header { background: #161821; padding: 16px 24px; border-bottom: 1px solid #2a2d3a; display: flex; align-items: center; justify-content: space-between; }
  .header h1 { font-size: 18px; color: #7c8aff; }
  .header p { font-size: 13px; color: #888; margin-top: 4px; }
  .container { display: flex; height: calc(100vh - 60px); }
  .sidebar { width: 320px; background: #161821; border-right: 1px solid #2a2d3a; display: flex; flex-direction: column; overflow: hidden; }
  .sidebar-tabs { display: flex; border-bottom: 1px solid #2a2d3a; }
  .sidebar-tab { flex: 1; padding: 10px; text-align: center; cursor: pointer; font-size: 13px; color: #888; border-bottom: 2px solid transparent; }
  .sidebar-tab.active { color: #7c8aff; border-bottom-color: #7c8aff; }
  .sidebar-content { flex: 1; overflow-y: auto; padding: 12px; }
  .sidebar-section h2 { font-size: 12px; color: #666; text-transform: uppercase; margin-bottom: 8px; padding: 0 4px; }

  /* File items */
  .file-item { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border-radius: 8px; background: #1c1e2a; margin-bottom: 6px; cursor: grab; transition: all 0.15s; }
  .file-item:hover { background: #252836; transform: translateY(-1px); }
  .file-item:active { cursor: grabbing; }
  .file-item.dragging { opacity: 0.4; }
  .file-icon { font-size: 18px; flex-shrink: 0; }
  .file-info { flex: 1; min-width: 0; }
  .file-name { font-size: 13px; color: #e0e0e0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .file-meta { font-size: 10px; color: #666; margin-top: 2px; }
  .product-group { margin-bottom: 12px; }
  .product-group-header { font-size: 12px; color: #7c8aff; font-weight: 600; padding: 4px 8px; margin-bottom: 4px; cursor: pointer; }
  .product-group-header:hover { color: #6470ff; }

  /* Session items */
  .session-item { padding: 8px 12px; cursor: pointer; border-radius: 6px; font-size: 13px; color: #ccc; }
  .session-item:hover { background: #1e2030; }
  .session-item.active { background: #2a2d4a; color: #7c8aff; }
  .sub-file-item { padding: 6px 12px 6px 24px; cursor: pointer; border-radius: 6px; font-size: 12px; color: #aaa; }
  .sub-file-item:hover { background: #1e2030; color: #fff; }
  .sub-file-item.active { color: #7c8aff; }

  /* Main area */
  .main { flex: 1; overflow-y: auto; padding: 24px 32px; }
  .main h2 { font-size: 16px; color: #7c8aff; margin-bottom: 16px; }
  .content-box { background: #161821; border: 1px solid #2a2d3a; border-radius: 8px; padding: 24px; }
  .content-box pre { white-space: pre-wrap; word-wrap: break-word; font-size: 14px; line-height: 1.7; font-family: 'SF Mono', 'Consolas', monospace; }
  .empty { text-align: center; padding: 60px; color: #555; }
  .file-info-display { font-size: 12px; color: #666; margin-bottom: 12px; }

  /* Agent boxes */
  .agents-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 20px; }
  .agent-box { background: #161821; border: 2px dashed #2a2d3a; border-radius: 16px; padding: 24px; min-height: 300px; display: flex; flex-direction: column; transition: all 0.2s; }
  .agent-box.drag-over { border-color: #7c8aff; background: #1a1d2e; }
  .agent-box.running { border-style: solid; border-color: #7c8aff; }
  .agent-box.done { border-style: solid; border-color: #4ade80; }
  .agent-box.error { border-style: solid; border-color: #ff6b6b; }
  .agent-header { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
  .agent-icon { font-size: 32px; }
  .agent-title { font-size: 16px; font-weight: 600; color: #e0e0e0; }
  .agent-desc { font-size: 12px; color: #888; margin-bottom: 16px; line-height: 1.5; }

  .drop-zone { flex: 1; border-radius: 10px; padding: 12px; min-height: 80px; margin-bottom: 12px; }
  .drop-zone-empty { text-align: center; color: #444; font-size: 13px; padding: 20px; border: 1px dashed #333; border-radius: 10px; }

  .dropped-file { display: flex; align-items: center; gap: 8px; padding: 8px 10px; background: #1c1e2a; border-radius: 8px; margin-bottom: 6px; font-size: 13px; }
  .dropped-file .remove-btn { margin-left: auto; color: #ff6b6b; cursor: pointer; font-size: 16px; padding: 0 4px; }
  .dropped-file .remove-btn:hover { color: #ff8888; }

  .agent-status { font-size: 13px; min-height: 20px; margin-bottom: 12px; }
  .agent-status.running { color: #7c8aff; }
  .agent-status.done { color: #4ade80; }
  .agent-status.error { color: #ff6b6b; }

  .agent-output { font-size: 12px; color: #aaa; background: #0f1117; border-radius: 8px; padding: 12px; max-height: 200px; overflow-y: auto; white-space: pre-wrap; word-wrap: break-word; margin-bottom: 12px; display: none; }
  .agent-output.visible { display: block; }

  .agent-file-link { font-size: 12px; color: #7c8aff; cursor: pointer; margin-bottom: 12px; display: none; }
  .agent-file-link.visible { display: block; }
  .agent-file-link:hover { text-decoration: underline; }

  .agent-actions { display: flex; gap: 8px; }
  .confirm-btn { background: #4ade80; color: #0f1117; border: none; border-radius: 8px; padding: 10px 16px; font-size: 14px; font-weight: 600; cursor: pointer; flex: 1; }
  .confirm-btn:hover { background: #45c97c; }
  .confirm-btn:disabled { background: #3a3d5a; color: #888; cursor: not-allowed; }
  .stop-btn { background: #e04848; color: #fff; border: none; border-radius: 8px; padding: 10px 16px; font-size: 14px; cursor: pointer; flex: 1; display: none; }
  .stop-btn:disabled { background: #3a3d5a; cursor: not-allowed; }
  .clear-btn { background: none; border: 1px solid #2a2d3a; color: #888; border-radius: 8px; padding: 10px 12px; font-size: 13px; cursor: pointer; }
  .clear-btn:hover { border-color: #555; color: #ccc; }

  .typing { display: inline-block; animation: blink 1s infinite; }
  @keyframes blink { 0%,100% { opacity: 0.3; } 50% { opacity: 1; } }

  .hint-bar { background: #161821; border: 1px solid #2a2d3a; border-radius: 8px; padding: 12px 16px; margin-bottom: 20px; font-size: 13px; color: #888; }
  .hint-bar b { color: #7c8aff; }
</style>
</head>
<body>
<div class="header">
  <div>
    <h1>MKTApp</h1>
    <p>โยนไฟล์เข้ากล่อง agent → กดยืนยัน → ได้ผลลัพธ์</p>
  </div>
</div>
<div class="container">
  <div class="sidebar">
    <div class="sidebar-tabs">
      <div class="sidebar-tab active" onclick="switchSidebarTab('files', event)">ไฟล์</div>
      <div class="sidebar-tab" onclick="switchSidebarTab('sessions', event)">ผลลัพธ์</div>
    </div>
    <div class="sidebar-content" id="sidebar-content"></div>
  </div>
  <div id="main-area" class="main">
    <div class="hint-bar">
      <b>วิธีใช้:</b> ลากไฟล์จากแถบซ้าย → โยนลงกล่อง agent → กด <b>ยืนยัน</b> เพื่อเริ่มทำงาน
    </div>
    <div class="agents-grid" id="agents-grid"></div>
  </div>
</div>
<script>
let currentSidebarTab = 'files';
let runningAgents = {};
let abortController = null;
let agentFiles = {}; // { agent_key: [file_path, ...] }

const AGENT_ORDER = ['product_spec', 'competitor_analysis', 'campaign_strategy', 'content_creator'];
const AGENT_INFO = {
  product_spec: { name: 'นักวิเคราะห์สินค้า', desc: 'สร้างสเปคสินค้าจากข้อมูลดิบ (txt, pdf, xlsx)', icon: '📋' },
  competitor_analysis: { name: 'นักวิเคราะห์คู่แข่ง', desc: 'วิเคราะห์เปรียบเทียบคู่แข่ง (ค้นหา web เอง)', icon: '🔍' },
  campaign_strategy: { name: 'นักวางกลยุทธ์แคมเปญ', desc: 'วางกลยุทธ์แคมเปญ + ราคาแนะนำ', icon: '📊' },
  content_creator: { name: 'นักสร้างคอนเทนต์', desc: 'สร้าง content + prompt รูป + hashtag', icon: '✍️' },
};

function switchSidebarTab(tab, ev) {
  currentSidebarTab = tab;
  document.querySelectorAll('.sidebar-tab').forEach(t => t.classList.remove('active'));
  if (ev) ev.target.classList.add('active');
  if (tab === 'files') loadFileList();
  else if (tab === 'sessions') loadSessions();
}

function loadFileList() {
  fetch('/api/data_files').then(r => r.json()).then(files => {
    const el = document.getElementById('sidebar-content');
    if (!files.length) {
      el.innerHTML = '<div style="color:#555;font-size:12px;padding:12px">ยังไม่มีไฟล์ใน data/</div>';
      return;
    }
    // Group by product
    const groups = {};
    for (const f of files) {
      if (!groups[f.product]) groups[f.product] = [];
      groups[f.product].push(f);
    }
    let html = '';
    for (const [product, fileList] of Object.entries(groups)) {
      html += '<div class="product-group">';
      html += '<div class="product-group-header">' + escapeHtml(product) + '</div>';
      for (const f of fileList) {
        const icon = f.type === 'image' ? '🖼️' : (f.ext === '.pdf' ? '📄' : '📝');
        const typeLabel = f.type === 'ready' ? 'พร้อม' : (f.type === 'image' ? 'รูป' : 'ดิบ');
        html += '<div class="file-item" draggable="true" ondragstart="onDragStart(event,\\'' + f.path + '\\')" ondragend="onDragEnd(event)">';
        html += '<span class="file-icon">' + icon + '</span>';
        html += '<div class="file-info">';
        html += '<div class="file-name">' + escapeHtml(f.name) + '</div>';
        html += '<div class="file-meta">' + typeLabel + ' · ' + formatSize(f.size) + '</div>';
        html += '</div></div>';
      }
      html += '</div>';
    }
    el.innerHTML = html;
  });
}

function formatSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return Math.round(bytes / 1024) + ' KB';
  return Math.round(bytes / 1048576) + ' MB';
}

function onDragStart(ev, filePath) {
  ev.dataTransfer.setData('text/plain', filePath);
  ev.target.classList.add('dragging');
}

function onDragEnd(ev) {
  ev.target.classList.remove('dragging');
}

function renderAgentBoxes() {
  const grid = document.getElementById('agents-grid');
  let html = '';
  for (const key of AGENT_ORDER) {
    const info = AGENT_INFO[key];
    html += '<div class="agent-box" id="box-' + key + '"';
    html += ' ondragover="onDragOver(event,\\'' + key + '\\')" ondragleave="onDragLeave(event,\\'' + key + '\\')" ondrop="onDrop(event,\\'' + key + '\\')">';
    html += '<div class="agent-header"><span class="agent-icon">' + info.icon + '</span><span class="agent-title">' + info.name + '</span></div>';
    html += '<div class="agent-desc">' + info.desc + '</div>';
    html += '<div class="drop-zone" id="dropzone-' + key + '"><div class="drop-zone-empty">ลากไฟล์มาวางที่นี่</div></div>';
    html += '<div class="agent-status" id="status-' + key + '"></div>';
    html += '<div class="agent-output" id="output-' + key + '"></div>';
    html += '<div class="agent-file-link" id="file-' + key + '"></div>';
    html += '<div class="agent-actions">';
    html += '<button class="confirm-btn" id="confirm-' + key + '" onclick="runAgent(\\'' + key + '\\')" disabled>ยืนยัน</button>';
    html += '<button class="stop-btn" id="stop-' + key + '" onclick="stopAgent(\\'' + key + '\\')">หยุด</button>';
    html += '<button class="clear-btn" onclick="clearAgent(\\'' + key + '\\')">ล้าง</button>';
    html += '</div>';
    html += '</div>';
  }
  grid.innerHTML = html;
}

function onDragOver(ev, agentKey) {
  ev.preventDefault();
  document.getElementById('box-' + agentKey).classList.add('drag-over');
}

function onDragLeave(ev, agentKey) {
  document.getElementById('box-' + agentKey).classList.remove('drag-over');
}

function onDrop(ev, agentKey) {
  ev.preventDefault();
  document.getElementById('box-' + agentKey).classList.remove('drag-over');
  const filePath = ev.dataTransfer.getData('text/plain');
  if (!filePath) return;
  if (!agentFiles[agentKey]) agentFiles[agentKey] = [];
  if (agentFiles[agentKey].includes(filePath)) return;
  agentFiles[agentKey].push(filePath);
  updateDropZone(agentKey);
}

function updateDropZone(agentKey) {
  const dz = document.getElementById('dropzone-' + agentKey);
  const confirmBtn = document.getElementById('confirm-' + agentKey);
  const files = agentFiles[agentKey] || [];
  if (!files.length) {
    dz.innerHTML = '<div class="drop-zone-empty">ลากไฟล์มาวางที่นี่</div>';
    confirmBtn.disabled = true;
    return;
  }
  let html = '';
  for (const fpath of files) {
    const name = fpath.split('/').pop();
    html += '<div class="dropped-file">';
    html += '<span>📄</span><span>' + escapeHtml(name) + '</span>';
    html += '<span class="remove-btn" onclick="removeFile(\\'' + agentKey + '\\',\\'' + fpath.replace(/'/g,"\\\\'") + '\\')">×</span>';
    html += '</div>';
  }
  dz.innerHTML = html;
  confirmBtn.disabled = false;
}

function removeFile(agentKey, filePath) {
  if (!agentFiles[agentKey]) return;
  agentFiles[agentKey] = agentFiles[agentKey].filter(f => f !== filePath);
  updateDropZone(agentKey);
}

function clearAgent(agentKey) {
  agentFiles[agentKey] = [];
  updateDropZone(agentKey);
  const box = document.getElementById('box-' + agentKey);
  const status = document.getElementById('status-' + agentKey);
  const output = document.getElementById('output-' + agentKey);
  const fileLink = document.getElementById('file-' + agentKey);
  box.className = 'agent-box';
  status.className = 'agent-status';
  status.textContent = '';
  output.className = 'agent-output';
  output.textContent = '';
  fileLink.className = 'agent-file-link';
  fileLink.textContent = '';
}

async function runAgent(agentKey) {
  const files = agentFiles[agentKey] || [];
  if (!files.length) return;
  if (runningAgents[agentKey]) return;

  runningAgents[agentKey] = true;
  const box = document.getElementById('box-' + agentKey);
  const status = document.getElementById('status-' + agentKey);
  const output = document.getElementById('output-' + agentKey);
  const confirmBtn = document.getElementById('confirm-' + agentKey);
  const stopBtn = document.getElementById('stop-' + agentKey);

  box.className = 'agent-box running';
  status.className = 'agent-status running';
  status.innerHTML = '<span class="typing">●</span> กำลังทำงาน...';
  output.className = 'agent-output visible';
  output.textContent = '';
  confirmBtn.style.display = 'none';
  stopBtn.style.display = 'block';
  stopBtn.disabled = false;

  // Determine product from first file path
  const product = files[0].split('/')[0];

  try {
    abortController = new AbortController();
    const res = await fetch('/api/run_agent', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ agent: agentKey, files: files, product: product }),
      signal: abortController.signal,
    });

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const data = JSON.parse(line.slice(6));
          handleAgentSSE(data, agentKey);
        } catch (e) {}
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') {
      status.className = 'agent-status error';
      status.textContent = 'เกิดข้อผิดพลาด: ' + e.message;
      box.className = 'agent-box error';
    }
  }

  delete runningAgents[agentKey];
  confirmBtn.style.display = 'block';
  stopBtn.style.display = 'none';
}

function handleAgentSSE(data, agentKey) {
  const box = document.getElementById('box-' + agentKey);
  const status = document.getElementById('status-' + agentKey);
  const output = document.getElementById('output-' + agentKey);
  const fileLink = document.getElementById('file-' + agentKey);

  if (data.type === 'agent_start') {
    status.innerHTML = '<span class="typing">●</span> ' + escapeHtml(data.text) + ' กำลังทำงาน...';
  } else if (data.type === 'stream') {
    output.textContent = data.text;
  } else if (data.type === 'agent_done') {
    status.className = 'agent-status done';
    status.textContent = 'เสร็จเรียบร้อย ✓';
    box.className = 'agent-box done';
    output.textContent = data.text + '\\n\\n... (ดูผลลัพธ์เต็มที่แท็บผลลัพธ์)';
    if (data.file) {
      fileLink.className = 'agent-file-link visible';
      fileLink.textContent = '📄 ' + data.file.split('/').pop();
      fileLink.onclick = () => switchToSessions();
    }
  } else if (data.type === 'error') {
    status.className = 'agent-status error';
    status.textContent = data.text;
    box.className = 'agent-box error';
  } else if (data.type === 'status') {
    status.textContent = data.text;
  }
}

function stopAgent(agentKey) {
  const stopBtn = document.getElementById('stop-' + agentKey);
  if (stopBtn) stopBtn.disabled = true;
  if (abortController) {
    abortController.abort();
    abortController = null;
  }
  fetch('/api/cancel', { method: 'POST' });
  delete runningAgents[agentKey];
  const box = document.getElementById('box-' + agentKey);
  const status = document.getElementById('status-' + agentKey);
  if (box) box.className = 'agent-box';
  if (status) {
    status.className = 'agent-status';
    status.textContent = 'หยุดการทำงาน';
  }
  const confirmBtn = document.getElementById('confirm-' + agentKey);
  if (confirmBtn) confirmBtn.style.display = 'block';
  if (stopBtn) stopBtn.style.display = 'none';
}

function switchToSessions() {
  switchSidebarTab('sessions', null);
  document.querySelectorAll('.sidebar-tab')[1].classList.add('active');
}

function loadSessions() {
  fetch('/api/sessions').then(r => r.json()).then(sessions => {
    const el = document.getElementById('sidebar-content');
    if (!sessions.length) {
      el.innerHTML = '<div style="color:#555;font-size:12px;padding:12px">ยังไม่มี session</div>';
      return;
    }
    let html = '';
    for (const s of sessions) {
      html += '<div class="session-item" onclick="expandSession(\\'' + s.name + '\\',this)">' + s.name + '</div>';
      html += '<div id="files-' + s.name + '" style="display:none">';
      for (const f of s.files) {
        html += '<div class="sub-file-item" onclick="loadSessionFile(\\'' + s.name + '\\',\\'' + f.name + '\\',this)">📄 ' + f.name + '<br><span style="color:#555">' + f.size + ' B · ' + f.modified + '</span></div>';
      }
      html += '</div>';
    }
    el.innerHTML = html;
  });
}

function expandSession(name, el) {
  const filesEl = document.getElementById('files-' + name);
  const isOpen = filesEl.style.display !== 'none';
  document.querySelectorAll('[id^="files-"]').forEach(e => e.style.display = 'none');
  document.querySelectorAll('.session-item').forEach(e => e.classList.remove('active'));
  if (!isOpen) {
    filesEl.style.display = 'block';
    el.classList.add('active');
  }
}

function loadSessionFile(session, filename, el) {
  document.querySelectorAll('.sub-file-item').forEach(e => e.classList.remove('active'));
  el.classList.add('active');
  fetch('/api/file/' + session + '/' + encodeURIComponent(filename)).then(r => r.json()).then(data => {
    const main = document.getElementById('main-area');
    main.innerHTML = '<h2>' + escapeHtml(filename) + '</h2><div class="file-info-display">Session: ' + escapeHtml(session) + '</div><div class="content-box"><pre>' + escapeHtml(data.content) + '</pre></div>';
  });
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

// Init
renderAgentBoxes();
loadFileList();
</script>
</body>
</html>"""


if __name__ == "__main__":
    port = int(os.environ.get("VIEWER_PORT", "8778"))
    print(f"\\n  MKTApp Viewer → http://localhost:{port}\\n")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")

"""Web Viewer — ดูผลลัพธ์ สถานะสินค้า และสั่งงานผ่านเว็บ.

Run:
  python3 web_viewer.py
  → เปิด http://localhost:8778
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

from rapidfuzz import fuzz

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

# Shared state
_orch: Orchestrator | None = None
_conversation: list[dict[str, str]] = []
_session_ts: str = datetime.now().strftime("%Y%m%d_%H%M%S")
_cancel_requested: bool = False
_current_llm: Any = None


def _get_orchestrator() -> Orchestrator:
    global _orch
    if _orch is None:
        _orch = Orchestrator(brand_dir="brand")
    return _orch


def _sse(event_type: str, text: str, **extra) -> str:
    """Format an SSE data line."""
    data = {"type": event_type, "text": text, **extra}
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _scan_products() -> list[dict[str, Any]]:
    """List all products and their ready/ status."""
    products = []
    if not DATA_DIR.exists():
        return products
    for item in sorted(DATA_DIR.iterdir()):
        if not item.is_dir() or item.name.startswith(".") or item.name == "ready":
            continue
        raw_files = []
        image_files = []
        for f in item.rglob("*"):
            if f.is_file() and not f.name.startswith(".") and f.name != ".DS_Store":
                if "ready" in f.parts:
                    continue
                ext = f.suffix.lower()
                if ext in {".txt", ".md", ".pdf", ".xlsx", ".xls"}:
                    raw_files.append(f.name)
                elif ext in {".jpg", ".jpeg", ".png"}:
                    image_files.append(f.name)
        ready_dir = item / "ready"
        ready_files = []
        if ready_dir.exists():
            for f in sorted(ready_dir.iterdir()):
                if f.is_file() and not f.name.startswith(".") and f.name != ".DS_Store":
                    ready_files.append(f.name)
        products.append({
            "name": item.name,
            "raw_files": raw_files,
            "image_files": image_files,
            "ready_files": ready_files,
        })
    return products


def _scan_sessions() -> list[dict[str, Any]]:
    """List all output sessions."""
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
            sessions.append({
                "name": item.name,
                "files": files,
            })
    return sessions


def _read_file(session: str, filename: str) -> str:
    """Read a file from output session."""
    filepath = OUTPUT_DIR / session / filename
    if not filepath.exists() or not filepath.is_file():
        return "ไม่พบไฟล์"
    return filepath.read_text(encoding="utf-8")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return HTML_PAGE


@app.get("/api/products")
def api_products() -> JSONResponse:
    return JSONResponse(_scan_products())


@app.get("/api/sessions")
def api_sessions() -> JSONResponse:
    return JSONResponse(_scan_sessions())


@app.get("/api/file/{session}/{filename:path}")
def api_file(session: str, filename: str) -> JSONResponse:
    return JSONResponse({"content": _read_file(session, filename)})


@app.post("/api/chat")
async def api_chat(request: Request) -> StreamingResponse:
    """SSE endpoint — stream manager response + agent execution."""
    body = await request.json()
    user_message = body.get("message", "")

    if not user_message.strip():
        return JSONResponse({"error": "empty message"})

    _conversation.append({"role": "user", "content": user_message})

    global _cancel_requested
    _cancel_requested = False

    async def event_stream():
        global _current_llm, _session_ts
        orch = _get_orchestrator()

        queue: _queue.Queue[str | None] = _queue.Queue()

        def worker():
            """Run blocking agent work in a thread, push SSE events to queue."""
            try:
                queue.put_nowait(_sse('status', 'Manager กำลังวิเคราะห์...'))

                llm = orch._make_client()
                _current_llm = llm
                try:
                    plan = orch.run_manager(user_message, _conversation, llm=llm)
                except Exception as e:
                    if _cancel_requested:
                        queue.put_nowait(_sse('status', 'หยุดการทำงานแล้ว'))
                        queue.put_nowait(_sse('done', ''))
                        queue.put_nowait(None)
                        return
                    queue.put_nowait(_sse('error', str(e)))
                    queue.put_nowait(None)
                    return

                reply = plan.get("reply", "")
                action = plan.get("action", "none")
                agents_to_run = plan.get("agents", [])
                product_id = plan.get("product_id")
                missing = plan.get("missing", [])

                # Safety: check if user actually mentioned a product name
                msg_lower = user_message.lower()
                products = orch.get_products_state()
                user_mentioned_product = False
                for p in products:
                    full_name = p["name"].lower()
                    # Check full name or last part (e.g. "k9" from "Lagenio K9")
                    short_name = full_name.split()[-1] if " " in full_name else full_name
                    if full_name in msg_lower or short_name in msg_lower:
                        user_mentioned_product = True
                        break

                if not user_mentioned_product and action == "run":
                    # LLM guessed a product — override to ask
                    action = "ask"
                    agents_to_run = []
                    product_id = None

                _conversation.append({"role": "manager", "content": reply})
                queue.put_nowait(_sse('manager', reply))

                if missing:
                    queue.put_nowait(_sse('missing', ', '.join(missing)))

                if action != "run" or not agents_to_run or not product_id:
                    queue.put_nowait(_sse('done', ''))
                    queue.put_nowait(None)
                    return

                # Normalize product_id to list
                if isinstance(product_id, list):
                    product_list = [str(p).strip() for p in product_id if p]
                elif isinstance(product_id, str) and "," in product_id:
                    product_list = [p.strip() for p in product_id.split(",") if p.strip()]
                else:
                    product_list = [str(product_id).strip()]

                if not product_list:
                    queue.put_nowait(_sse('done', ''))
                    queue.put_nowait(None)
                    return

                agent_names = {
                    "product_spec": "นักวิเคราะห์สินค้า",
                    "competitor_analysis": "นักวิเคราะห์คู่แข่ง",
                    "campaign_strategy": "นักวางกลยุทธ์แคมเปญ",
                    "content_creator": "นักสร้างคอนเทนต์",
                }

                # Phase 2: Run agents
                output_dir = OUTPUT_DIR / _session_ts
                output_dir.mkdir(parents=True, exist_ok=True)

                results_log: dict[str, dict[str, bool]] = {}

                for pid in product_list:
                    results_log[pid] = {}
                    orch.results = {}
                    detected = detect_data_files(product_id=pid)

                    for agent_key in agents_to_run:
                        if _cancel_requested:
                            queue.put_nowait(_sse('status', 'หยุดการทำงานแล้ว'))
                            queue.put_nowait(_sse('done', ''))
                            queue.put_nowait(None)
                            return

                        display = f"{agent_names.get(agent_key, agent_key)} — {pid}"
                        queue.put_nowait(_sse('agent_start', display))

                        if _current_llm is None:
                            _current_llm = orch._make_client()
                            llm = _current_llm

                        try:
                            if agent_key == "product_spec":
                                if not detected["raw"]:
                                    queue.put_nowait(_sse('error', f'ไม่พบข้อมูลดิบสำหรับ {pid}'))
                                    results_log[pid][agent_key] = False
                                    continue
                                raw_data = load_file(detected["raw"])
                                product_images = detected["images"] or []
                                orch.product_id = pid
                                orch.product_images = product_images

                                queue.put_nowait(_sse('stream', f'กำลังสร้างสเปคสินค้า {pid}...'))
                                result = orch.run_product_spec(raw_data, product_images, llm=llm)
                                orch.results["product_spec"] = result
                                saved = orch.save_result("product_spec", str(output_dir))
                                filepath = saved.get("product_spec", "")
                                queue.put_nowait(_sse('agent_done', result[:500], agent='product_spec', file=str(filepath)))
                                results_log[pid][agent_key] = True
                                detected = detect_data_files(product_id=pid)

                            elif agent_key == "competitor_analysis":
                                if not detected["product_spec"]:
                                    queue.put_nowait(_sse('error', f'{pid}: ต้องรัน product_spec ก่อน'))
                                    results_log[pid][agent_key] = False
                                    continue
                                product_spec = load_file(detected["product_spec"])
                                queue.put_nowait(_sse('stream', f'กำลังวิเคราะห์คู่แข่ง {pid}...'))
                                result = orch.run_competitor_analysis(product_spec, None, llm=llm)
                                orch.results["competitor_analysis"] = result
                                saved = orch.save_result("competitor_analysis", str(output_dir))
                                filepath = saved.get("competitor_analysis", "")
                                queue.put_nowait(_sse('agent_done', result[:500], agent='competitor_analysis', file=str(filepath)))
                                results_log[pid][agent_key] = True
                                detected = detect_data_files(product_id=pid)

                            elif agent_key == "campaign_strategy":
                                if not detected["product_spec"]:
                                    queue.put_nowait(_sse('error', f'{pid}: ต้องรัน product_spec ก่อน'))
                                    results_log[pid][agent_key] = False
                                    continue
                                product_spec = load_file(detected["product_spec"])
                                comp_files = list(output_dir.glob(f"02_competitor_analysis_{pid}_*.md"))
                                if detected["competitor"]:
                                    analysis = load_file(detected["competitor"])
                                elif comp_files:
                                    analysis = comp_files[0].read_text(encoding="utf-8")
                                else:
                                    queue.put_nowait(_sse('error', f'{pid}: ต้องรัน competitor_analysis ก่อน'))
                                    results_log[pid][agent_key] = False
                                    continue
                                queue.put_nowait(_sse('stream', f'กำลังวางกลยุทธ์แคมเปญ {pid}...'))
                                result = orch.run_campaign_strategy(product_spec, analysis, llm=llm)
                                orch.results["campaign_strategy"] = result
                                saved = orch.save_result("campaign_strategy", str(output_dir))
                                filepath = saved.get("campaign_strategy", "")
                                queue.put_nowait(_sse('agent_done', result[:500], agent='campaign_strategy', file=str(filepath)))
                                results_log[pid][agent_key] = True

                            elif agent_key == "content_creator":
                                if not detected["product_spec"]:
                                    queue.put_nowait(_sse('error', f'{pid}: ต้องรัน product_spec ก่อน'))
                                    results_log[pid][agent_key] = False
                                    continue
                                product_spec = load_file(detected["product_spec"])
                                comp_files = list(output_dir.glob(f"02_competitor_analysis_{pid}_*.md"))
                                camp_files = list(output_dir.glob(f"03_campaign_strategy_{pid}_*.md"))
                                if detected["competitor"]:
                                    analysis = load_file(detected["competitor"])
                                elif comp_files:
                                    analysis = comp_files[0].read_text(encoding="utf-8")
                                else:
                                    queue.put_nowait(_sse('error', f'{pid}: ต้องรัน competitor_analysis ก่อน'))
                                    results_log[pid][agent_key] = False
                                    continue
                                if not camp_files:
                                    queue.put_nowait(_sse('error', f'{pid}: ต้องรัน campaign_strategy ก่อน'))
                                    results_log[pid][agent_key] = False
                                    continue
                                campaign = camp_files[0].read_text(encoding="utf-8")
                                queue.put_nowait(_sse('stream', f'กำลังสร้างคอนเทนต์ {pid}...'))
                                result = orch.run_content_creator(product_spec, analysis, campaign, llm=llm)
                                orch.results["content_creator"] = result
                                saved = orch.save_result("content_creator", str(output_dir))
                                filepath = saved.get("content_creator", "")
                                queue.put_nowait(_sse('agent_done', result[:500], agent='content_creator', file=str(filepath)))
                                results_log[pid][agent_key] = True

                            else:
                                queue.put_nowait(_sse('error', f'ไม่รู้จัก agent: {agent_key}'))
                                results_log[pid][agent_key] = False

                        except Exception as e:
                            if _cancel_requested:
                                queue.put_nowait(_sse('status', 'หยุดการทำงานแล้ว'))
                                queue.put_nowait(_sse('done', ''))
                                queue.put_nowait(None)
                                return
                            queue.put_nowait(_sse('error', f'{pid}: {e}'))
                            results_log[pid][agent_key] = False
                            _current_llm = None

                # Phase 3: Simple summary (no LLM)
                success_parts = []
                fail_parts = []
                for pid, agent_results in results_log.items():
                    for agent_key, ok in agent_results.items():
                        if ok:
                            success_parts.append(f"{pid}/{agent_key}")
                        else:
                            fail_parts.append(f"{pid}/{agent_key}")

                summary_lines = []
                if success_parts:
                    summary_lines.append(f"สำเร็จ: {', '.join(success_parts)}")
                if fail_parts:
                    summary_lines.append(f"ล้มเหลว: {', '.join(fail_parts)}")
                if summary_lines:
                    summary = "ผลการทำงาน:\n" + "\n".join(summary_lines)
                    _conversation.append({"role": "manager", "content": summary})
                    queue.put_nowait(_sse('manager', summary))

                if _current_llm:
                    _current_llm.close()
                    _current_llm = None
                queue.put_nowait(_sse('done', ''))
                queue.put_nowait(None)

            except Exception as e:
                queue.put_nowait(_sse('error', str(e)))
                queue.put_nowait(_sse('done', ''))
                queue.put_nowait(None)

        # Start worker thread
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        # Yield events from thread-safe queue
        while True:
            try:
                event = queue.get_nowait()
            except _queue.Empty:
                await asyncio.sleep(0.05)
                continue
            if event is None:
                break
            yield event

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/reset")
async def api_reset() -> JSONResponse:
    global _conversation, _session_ts
    _conversation = []
    _session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return JSONResponse({"ok": True})


@app.post("/api/cancel")
async def api_cancel() -> JSONResponse:
    global _cancel_requested, _current_llm
    _cancel_requested = True
    if _current_llm is not None:
        _current_llm.abort()
        _current_llm = None
    return JSONResponse({"ok": True})


HTML_PAGE = """<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MKTApp</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, 'Segoe UI', sans-serif; background: #0f1117; color: #e0e0e0; }
  .header { background: #161821; padding: 16px 24px; border-bottom: 1px solid #2a2d3a; }
  .header h1 { font-size: 18px; color: #7c8aff; }
  .header p { font-size: 13px; color: #888; margin-top: 4px; }
  .container { display: flex; height: calc(100vh - 60px); }
  .sidebar { width: 280px; background: #161821; border-right: 1px solid #2a2d3a; overflow-y: auto; }
  .sidebar-section { padding: 12px 16px; border-bottom: 1px solid #2a2d3a; }
  .sidebar-section h2 { font-size: 13px; color: #888; text-transform: uppercase; margin-bottom: 8px; }
  .tab-btn { padding: 8px 12px; border: none; background: none; color: #ccc; cursor: pointer; font-size: 13px; text-align: left; width: 100%; border-radius: 6px; }
  .tab-btn:hover { background: #1e2030; }
  .tab-btn.active { background: #2a2d4a; color: #7c8aff; }
  .session-item { padding: 8px 12px; cursor: pointer; border-radius: 6px; font-size: 13px; color: #ccc; }
  .session-item:hover { background: #1e2030; }
  .session-item.active { background: #2a2d4a; color: #7c8aff; }
  .file-item { padding: 6px 12px 6px 24px; cursor: pointer; border-radius: 6px; font-size: 12px; color: #aaa; }
  .file-item:hover { background: #1e2030; color: #fff; }
  .file-item.active { color: #7c8aff; }
  .product-card { padding: 10px 12px; border-radius: 8px; background: #1c1e2a; margin-bottom: 8px; }
  .product-card .name { font-size: 14px; font-weight: 600; color: #e0e0e0; }
  .product-card .detail { font-size: 11px; color: #888; margin-top: 4px; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 10px; margin-right: 4px; }
  .badge-green { background: #1a3a2a; color: #4ade80; }
  .badge-gray { background: #2a2d3a; color: #888; }
  .main { flex: 1; overflow-y: auto; padding: 24px 32px; }
  .main h2 { font-size: 16px; color: #7c8aff; margin-bottom: 16px; }
  .content-box { background: #161821; border: 1px solid #2a2d3a; border-radius: 8px; padding: 24px; }
  .content-box pre { white-space: pre-wrap; word-wrap: break-word; font-size: 14px; line-height: 1.7; font-family: 'SF Mono', 'Consolas', monospace; }
  .empty { text-align: center; padding: 60px; color: #555; }
  .file-info { font-size: 12px; color: #666; margin-bottom: 12px; }
  .chat-main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
  .chat-messages { flex: 1; overflow-y: auto; padding: 24px 32px; }
  .chat-input-area { padding: 12px 24px 20px; border-top: 1px solid #2a2d3a; background: #161821; }
  .chat-input-row { display: flex; gap: 8px; }
  .chat-input { flex: 1; background: #1c1e2a; border: 1px solid #2a2d3a; border-radius: 8px; padding: 12px 16px; color: #e0e0e0; font-size: 14px; outline: none; }
  .chat-input:focus { border-color: #7c8aff; }
  .chat-input::placeholder { color: #555; }
  .chat-btn { background: #7c8aff; color: #fff; border: none; border-radius: 8px; padding: 12px 20px; font-size: 14px; cursor: pointer; }
  .chat-btn:hover { background: #6470ff; }
  .chat-btn:disabled { background: #3a3d5a; cursor: not-allowed; }
  .chat-reset { background: none; border: 1px solid #2a2d3a; color: #888; border-radius: 6px; padding: 6px 12px; font-size: 12px; cursor: pointer; }
  .chat-reset:hover { border-color: #555; color: #ccc; }
  .msg { margin-bottom: 16px; }
  .msg-user { text-align: right; }
  .msg-user .bubble { background: #2a2d4a; color: #e0e0e0; }
  .msg-manager .bubble { background: #1a2a3a; color: #b0d0ff; }
  .msg-agent .bubble { background: #1c1e2a; color: #ccc; border-left: 3px solid #4ade80; }
  .msg-error .bubble { background: #3a1a1a; color: #ff8888; }
  .msg-status .bubble { background: none; color: #666; font-style: italic; }
  .bubble { display: inline-block; max-width: 80%; padding: 12px 16px; border-radius: 12px; font-size: 14px; line-height: 1.6; text-align: left; white-space: pre-wrap; word-wrap: break-word; }
  .msg-user .bubble { border-bottom-right-radius: 4px; }
  .msg-manager .bubble, .msg-agent .bubble, .msg-error .bubble, .msg-status .bubble { border-bottom-left-radius: 4px; }
  .agent-label { font-size: 11px; color: #4ade80; margin-bottom: 4px; font-weight: 600; }
  .agent-file { font-size: 11px; color: #7c8aff; margin-top: 8px; }
  .typing { display: inline-block; animation: blink 1s infinite; }
  @keyframes blink { 0%,100% { opacity: 0.3; } 50% { opacity: 1; } }
</style>
</head>
<body>
<div class="header">
  <h1>MKTApp</h1>
  <p>สั่งงาน ดูผลลัพธ์ ดูสถานะสินค้า</p>
</div>
<div class="container">
  <div class="sidebar">
    <div class="sidebar-section">
      <button class="tab-btn active" onclick="switchTab('chat', event)">สั่งงาน</button>
      <button class="tab-btn" onclick="switchTab('sessions', event)">ผลลัพธ์</button>
      <button class="tab-btn" onclick="switchTab('products', event)">สินค้า</button>
    </div>
    <div id="sidebar-content"></div>
  </div>
  <div id="main-area" class="main" style="display:none">
    <div class="empty">เลือกจากแถบซ้าย</div>
  </div>
  <div id="chat-area" class="chat-main">
    <div class="chat-messages" id="chat-messages">
      <div class="empty" style="text-align:left;padding:24px;max-width:600px;margin:0 auto">
        <p style="font-size:15px;color:#7c8aff;margin-bottom:12px">ทีมของคุณมี 4 ตำแหน่ง:</p>
        <p style="font-size:13px;color:#ccc;margin-bottom:4px">1. นักวิเคราะห์สินค้า — สร้างสเปคสินค้าจากข้อมูลดิบ</p>
        <p style="font-size:13px;color:#ccc;margin-bottom:4px">2. นักวิเคราะห์คู่แข่ง — วิเคราะห์เปรียบเทียบคู่แข่ง (ค้นหา web เอง)</p>
        <p style="font-size:13px;color:#ccc;margin-bottom:4px">3. นักวางกลยุทธ์แคมเปญ — คิดแคมเปญ + ราคาแนะนำ</p>
        <p style="font-size:13px;color:#ccc;margin-bottom:12px">4. นักสร้างคอนเทนต์ — สร้าง content + prompt + hashtag</p>
        <p style="font-size:13px;color:#888">พิมพ์เป็นภาษาไทยได้เลย สั่งงานอะไรก็ได้ที่ทีมทำได้</p>
      </div>
    </div>
    <div class="chat-input-area">
      <div class="chat-input-row">
        <input class="chat-input" id="chat-input" placeholder="พิมพ์คำสั่ง..." onkeydown="if(event.key==='Enter')sendChat()" autocomplete="off">
        <button class="chat-btn" id="chat-send" onclick="sendChat()">ส่ง</button>
        <button class="chat-btn" id="chat-stop" onclick="cancelChat()" style="display:none;background:#e04848">หยุด</button>
      </div>
      <div style="margin-top:8px;text-align:right">
        <button class="chat-reset" onclick="resetChat()">เริ่มใหม่</button>
      </div>
    </div>
  </div>
</div>
<script>
let currentTab = 'chat';
let isRunning = false;
let abortController = null;

function switchTab(tab, ev) {
  currentTab = tab;
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  if (ev) ev.target.classList.add('active');
  const mainArea = document.getElementById('main-area');
  const chatArea = document.getElementById('chat-area');
  if (tab === 'chat') {
    mainArea.style.display = 'none';
    chatArea.style.display = 'flex';
  } else {
    mainArea.style.display = 'block';
    chatArea.style.display = 'none';
    if (tab === 'sessions') loadSessions();
    else if (tab === 'products') loadProducts();
    mainArea.innerHTML = '<div class="empty">เลือกจากแถบซ้าย</div>';
  }
  document.getElementById('sidebar-content').innerHTML = '';
}

async function sendChat() {
  const input = document.getElementById('chat-input');
  const msg = input.value.trim();
  if (!msg || isRunning) return;
  isRunning = true;
  document.getElementById('chat-send').style.display = 'none';
  document.getElementById('chat-stop').style.display = 'inline-block';
  document.getElementById('chat-stop').disabled = false;
  input.value = '';

  const msgs = document.getElementById('chat-messages');
  if (msgs.querySelector('.empty')) msgs.innerHTML = '';

  const userDiv = document.createElement('div');
  userDiv.className = 'msg msg-user';
  userDiv.innerHTML = '<div class="bubble">' + escapeHtml(msg) + '</div>';
  msgs.appendChild(userDiv);
  msgs.scrollTop = msgs.scrollHeight;

  const statusDiv = document.createElement('div');
  statusDiv.className = 'msg msg-status';
  statusDiv.innerHTML = '<div class="bubble"><span class="typing">●</span> Manager กำลังวิเคราะห์...</div>';
  msgs.appendChild(statusDiv);
  msgs.scrollTop = msgs.scrollHeight;

  try {
    abortController = new AbortController();
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: msg}),
      signal: abortController.signal,
    });

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream: true});
      const lines = buffer.split('\\n');
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const data = JSON.parse(line.slice(6));
          handleSSE(data, msgs, statusDiv);
        } catch(e) {}
      }
    }
  } catch(e) {
    statusDiv.querySelector('.bubble').textContent = 'เกิดข้อผิดพลาด: ' + e.message;
    statusDiv.className = 'msg msg-error';
  }

  isRunning = false;
  document.getElementById('chat-send').style.display = 'inline-block';
  document.getElementById('chat-stop').style.display = 'none';
  document.getElementById('chat-input').focus();
}

function handleSSE(data, msgs, statusDiv) {
  if (data.type === 'status') {
    statusDiv.querySelector('.bubble').innerHTML = '<span class="typing">●</span> ' + escapeHtml(data.text);
  } else if (data.type === 'manager') {
    statusDiv.remove();
    const div = document.createElement('div');
    div.className = 'msg msg-manager';
    div.innerHTML = '<div class="bubble">' + escapeHtml(data.text) + '</div>';
    msgs.appendChild(div);
  } else if (data.type === 'missing') {
    const div = document.createElement('div');
    div.className = 'msg msg-error';
    div.innerHTML = '<div class="bubble">ขาด: ' + escapeHtml(data.text) + '</div>';
    msgs.appendChild(div);
  } else if (data.type === 'agent_start') {
    const div = document.createElement('div');
    div.className = 'msg msg-agent';
    div.innerHTML = '<div class="agent-label">⚙ ' + escapeHtml(data.text) + '</div><div class="bubble"><span class="typing">●</span> กำลังทำงาน...</div>';
    msgs.appendChild(div);
    msgs.scrollTop = msgs.scrollHeight;
  } else if (data.type === 'stream') {
    const lastAgent = msgs.querySelector('.msg-agent:last-child .bubble');
    if (lastAgent) {
      lastAgent.textContent = data.text;
      msgs.scrollTop = msgs.scrollHeight;
    }
  } else if (data.type === 'agent_done') {
    const lastAgent = msgs.querySelector('.msg-agent:last-child .bubble');
    if (lastAgent) {
      lastAgent.textContent = data.text + '\\n\\n... (ดูผลลัพธ์เต็มที่แท็บผลลัพธ์)';
    }
    if (data.file) {
      const fileDiv = document.createElement('div');
      fileDiv.className = 'agent-file';
      fileDiv.textContent = '📄 ' + data.file;
      msgs.appendChild(fileDiv);
    }
    msgs.scrollTop = msgs.scrollHeight;
  } else if (data.type === 'error') {
    const div = document.createElement('div');
    div.className = 'msg msg-error';
    div.innerHTML = '<div class="bubble">' + escapeHtml(data.text) + '</div>';
    msgs.appendChild(div);
  } else if (data.type === 'done') {
    statusDiv.remove();
  }
  msgs.scrollTop = msgs.scrollHeight;
}

async function resetChat() {
  await fetch('/api/reset', {method: 'POST'});
  document.getElementById('chat-messages').innerHTML = '<div class="empty" style="text-align:left;padding:24px;max-width:600px;margin:0 auto"><p style="font-size:13px;color:#888">เริ่มใหม่แล้ว พิมพ์คำสั่งด้านล่าง</p></div>';
}

async function cancelChat() {
  document.getElementById('chat-stop').disabled = true;
  if (abortController) {
    abortController.abort();
    abortController = null;
  }
  await fetch('/api/cancel', {method: 'POST'});
  isRunning = false;
  document.getElementById('chat-send').style.display = 'inline-block';
  document.getElementById('chat-stop').style.display = 'none';
  document.getElementById('chat-input').focus();
}

async function loadSessions() {
  const res = await fetch('/api/sessions');
  const sessions = await res.json();
  const el = document.getElementById('sidebar-content');
  if (!sessions.length) {
    el.innerHTML = '<div class="sidebar-section"><p style="color:#555;font-size:12px">ยังไม่มี session</p></div>';
    return;
  }
  let html = '<div class="sidebar-section">';
  for (const s of sessions) {
    html += `<div class="session-item" onclick="expandSession('${s.name}', this)">${s.name}</div>`;
    html += `<div id="files-${s.name}" style="display:none">`;
    for (const f of s.files) {
      html += `<div class="file-item" onclick="loadFile('${s.name}','${f.name}',this)">📄 ${f.name}<br><span style="color:#555">${f.size} bytes · ${f.modified}</span></div>`;
    }
    html += '</div>';
  }
  html += '</div>';
  el.innerHTML = html;
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

async function loadFile(session, filename, el) {
  document.querySelectorAll('.file-item').forEach(e => e.classList.remove('active'));
  el.classList.add('active');
  const res = await fetch(`/api/file/${session}/${encodeURIComponent(filename)}`);
  const data = await res.json();
  document.getElementById('main-area').innerHTML = `<h2>${filename}</h2><div class="file-info">Session: ${session}</div><div class="content-box"><pre>${escapeHtml(data.content)}</pre></div>`;
}

async function loadProducts() {
  const res = await fetch('/api/products');
  const products = await res.json();
  const el = document.getElementById('sidebar-content');
  if (!products.length) {
    el.innerHTML = '<div class="sidebar-section"><p style="color:#555;font-size:12px">ยังไม่มีสินค้า</p></div>';
    return;
  }
  let html = '<div class="sidebar-section">';
  for (const p of products) {
    const rawBadge = p.raw_files.length ? `<span class="badge badge-green">ดิบ ${p.raw_files.length}</span>` : '<span class="badge badge-gray">ดิบ 0</span>';
    const imgBadge = p.image_files.length ? `<span class="badge badge-green">รูป ${p.image_files.length}</span>` : '';
    const readyBadge = p.ready_files.length ? `<span class="badge badge-green">พร้อม ${p.ready_files.length}</span>` : '<span class="badge badge-gray">พร้อม 0</span>';
    html += `<div class="product-card" onclick="showProduct('${p.name}')">
      <div class="name">${p.name}</div>
      <div class="detail">${rawBadge} ${imgBadge} ${readyBadge}</div>
    </div>`;
  }
  html += '</div>';
  el.innerHTML = html;
}

function showProduct(name) {
  // re-fetch products to find detail
  fetch('/api/products').then(r => r.json()).then(products => {
    const p = products.find(x => x.name === name);
    if (!p) return;
    const main = document.getElementById('main');
    let html = `<h2>${p.name}</h2>`;
    html += '<div class="content-box">';
    html += `<p style="margin-bottom:12px"><b>ไฟล์ข้อมูลดิบ:</b> ${p.raw_files.length ? p.raw_files.join(', ') : 'ไม่มี'}</p>`;
    html += `<p style="margin-bottom:12px"><b>รูปภาพ:</b> ${p.image_files.length ? p.image_files.join(', ') : 'ไม่มี'}</p>`;
    html += `<p><b>ข้อมูลพร้อมใช้ (ready/):</b></p>`;
    if (p.ready_files.length) {
      html += '<ul style="margin:8px 0;padding-left:20px">';
      for (const f of p.ready_files) html += `<li style="font-size:13px;color:#ccc">${f}</li>`;
      html += '</ul>';
    } else {
      html += '<p style="color:#555;font-size:13px">ยังไม่มีข้อมูลพร้อมใช้</p>';
    }
    html += '</div>';
    document.getElementById('main-area').innerHTML = html;
  });
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

document.getElementById('chat-input').focus();
</script>
</body>
</html>"""


if __name__ == "__main__":
    port = int(os.environ.get("VIEWER_PORT", "8778"))
    print(f"\n  MKTApp Viewer → http://localhost:{port}\n")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")

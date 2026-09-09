# MKTApp Full Beta Gap Audit — Engineering Decision Document

> **HISTORICAL / SUPERSEDED — preserved as evidence only.**
> This document is preserved as historical planning/evidence. It must NOT be used to determine current next actions, model configuration, paid-call authorization, or readiness status. Current execution status lives in `AI_EMPLOYEE_BETA_EXECUTION_PLAN.md`; readiness criteria live in `AGENT_PRODUCTION_READINESS_SPEC.md`.

- **HEAD (source of truth):** `09e3ba8bdb078ac6d31d9e9a060f5ce9f05c777d`
- **Worktree status:** `?? FULL_BETA_GAP_AUDIT.md` (untracked, not committed)
- **Mode:** read-only, no production code changes, no paid API calls, no tests run, no commit
- **Date:** 2026-09-01
- **Goal:** ทุก Marketing Capability ที่ product/UI ประกาศรองรับสำหรับ Full Beta (หนึ่ง Agent ต่อ flow) ต้องทำงาน end-to-end ได้จริง ในระดับคุณภาพใกล้เคียงหรือทัดเทียม Frontier AI สำหรับงานนั้น โดย user ไม่ต้องป้อน context เดิมซ้ำ

> **หมายเหตุสำคัญ:** รายงานนี้คำนวณจาก atomic capabilities ทีมีหลักฐานใน repo คะแนนยังขึ้นกับ weight policy ทีกำหนดข้างล่าง หาก weights เปลี่ยนตัวเลขจะเปลี่ยนตาม แต่ rule `PROVEN=100%, PARTIAL=50%, UNPROVEN/BROKEN=0%` ตายตัว

---

## H. ข้อจำกัดการทำงานครั้งนี้

- ไม่แก้ production code
- ไม่เพิ่ม validator / regex
- ไม่ยิง paid LLM / API qualification
- ไม่สร้าง scenario / qualification artifact ใหม่
- ไม่ commit
- ไม่ถือ test ผ่าน = quality ผ่าน

---

## A. Execution Architecture ปัจจุบัน (จริง)

```
User / Browser
  → web_viewer.py (FastAPI, SSE)
      → POST /api/run_agents หรือ /api/run_flows
          → Orchestrator.run_* หรือ run_pipeline
              → Product DB / Asset Library / Brand Rules
              → BaseAgent.run()
                  1. _build_system_prompt()
                  2. build_prompt()
                  3. _build_multimodal_content()
                  4. llm.chat()  (generate)
                  5. _output_is_blank()
                  6. _review_and_refine()  (ถ้าเปิด)
                  7. validate_output()  (markdown / JSON / quality)
                  8. _repair_output()
                  9. คืน final text
              → save_result() → output/{session}/
```

หมายเหตุ: ปัญหาไม่ได้อยู่ที่ runtime ขาด component ใหญ่ แต่ runtime ทำงานอยู่แล้ว แล้วถูก **config และ validator บังคับรูปแบบ output** มากเกินไป

---

## B. ตำแหน่งทีกำลังบังคับ LLM โดยไม่จำเป็น

| ตำแหน่ง | อะไรถูกบังคับ | ผลกระทบ | หลักฐาน |
|---|---|---|---|
| `config/agents.yaml:103-113` | 10 หัวข้อ `required_output_sections` สำหรับ `product_spec` | User ขอ brief สั้นก็ยังถูกดึงให้ครบ 10 หัวข้อ | `config/agents.yaml` |
| `config/agents.yaml:348-355` | 7 หัวข้อ `required_output_sections` สำหรับ `campaign_strategy` | ขอ executive one-page ยาก | `config/agents.yaml` |
| `src/content_schema.py:25-135` | Strict JSON `posts[]` บังคับทุก field | TikTok / multi-post / media ต้อง fit schema เดิม | `src/content_schema.py` |
| `src/output_validators.py:131-137` | Regex ตรวจ heading section | บังคับให้ LLM ออกรูปแบบเดิม | `src/output_validators.py` |
| `src/campaign_validator.py:45-500` | Markers/regex ตรวจคำ target/guarantee/benchmark | False positive บล็อกแผนที่ใช้ได้ | `src/campaign_validator.py` |
| `web_viewer.py:3167` | `flow_agents = flow_agents[:1]` | UI เปิด multi-agent แต่ backend รันแค่ตัวแรก | `web_viewer.py:3164-3167` |

---

## E. Atomic Full Beta Score

### Score Rule (ตายตัว)

| Status | Score |
|---|---|
| PROVEN | 100% |
| PARTIAL | 50% |
| UNPROVEN | 0% |
| BROKEN | 0% |

### Atomic Capability Matrix

| ID | Capability | Acceptance Criterion | Status | Evidence | Weight | Weighted Score |
|---|---|---|---|---|---|---|
| **Infrastructure / Shared** |||||| **20%** |
| I1 | Product DB + ingestion | สินค้าเข้า DB ได้ มี raw text/image/metadata | PROVEN | `src/product_db.py`, `src/ingestion.py:586-913` | 4% | 4.0 |
| I2 | Single product selection | เลือกสินค้า 1 รุ่นและส่งไป agent ได้ | PROVEN | `src/wizard_ui.js:347-415` | 3% | 3.0 |
| I3 | Quick Brief per run | ส่งคำสั่งเฉพาะรอบไป agent ได้และมีผล | PROVEN | `src/wizard_ui.js:149,688-694`, `tests/test_product_spec_quick_brief.py` | 3% | 3.0 |
| I4 | Agent Settings UI | ตั้งค่า preset/custom แล้วถูกส่งไป prompt | PARTIAL | `config/agent_instructions.json:28-167`, ผล Agent 1 ผ่าน แต่ 2-4 ยังไม่ผ่าน real-model | 2% | 1.0 |
| I5 | Multi-product selection | เลือกหลายสินค้าและ agent สร้าง spec แยกถูกต้อง | PARTIAL | `src/agents/product_spec.py:42-51`, ยังไม่ผ่าน real-model แบบรวม | 2% | 1.0 |
| I6 | Multi-agent per flow | UI เลือกหลาย agent แล้วรันทั้งหมดตามลำดับ | UNPROVEN (Post-Beta) | `web_viewer.py:3167` ตัด `flow_agents[:1]` — ไม่นับเป็น Full Beta blocker | 3% | 0.0 |
| I7 | Scheduling | ตั้งเวลา save/run/rerun ใช้งานได้ | UNPROVEN | `src/wizard_ui.js:152` มีปุ่ม แต่ไม่มี qualification | 1% | 0.0 |
| I8 | Brand context injection | Brand rules/reference ถูกใส่ prompt ทุก agent | PROVEN | `src/brand_priority.py:217`, `src/agents/base_agent.py:216-272` | 1% | 1.0 |
| I9 | Asset library references | Asset IDs ถูกเลือกและอ้างอิงใน content | PARTIAL | `src/asset_library.py`, `tests/test_phase4_media_wiring.py` ผ่าน mock | 1% | 0.5 |
| **Agent 1 — Product Analyst** |||||| **12%** |
| A1.1 | Text product spec จากสินค้าที่เลือก | สร้าง spec ถูกต้องจาก raw text | PROVEN | `AGENT_PRODUCTION_READINESS_SPEC.md:186-188`, `src/agents/product_spec.py:15-54` | 5% | 5.0 |
| A1.2 | Quick Brief steering | เปลี่ยนรูปแบบ/ละเอียดตาม Quick Brief ได้ | PROVEN | `tests/test_product_spec_quick_brief.py` | 2% | 2.0 |
| A1.3 | Image-derived product claims | อ่านภาพสินค้าแล้วบอกข้อเท็จจริงที่เห็นจริง | UNPROVEN | `src/agents/product_spec.py:26-40` ส่งรูปแล้ว แต่ยังไม่ผ่าน real-model | 2% | 0.0 |
| A1.4 | Multi-product combined spec | หลายสินค้าไม่ปนกันและครบ | PARTIAL | `src/agents/product_spec.py:42-51` รองรับ แต่ยังไม่ qualify real-model | 1% | 0.5 |
| A1.5 | Flexible output (ไม่บังคับ 10 หัวข้อ) | User ขอ brief สั้น/แบบอื่น ได้ตามขอ | UNPROVEN | `config/agents.yaml:103-113` บังคับ 10 หัวข้อ | 2% | 0.0 |
| **Agent 2 — Competitor Analyst** |||||| **12%** |
| A2.1 | Evidence JSON → Markdown | คืน JSON evidence แล้ว render เป็น report ได้ | PROVEN | `src/agents/competitor_evidence.py`, `tests/test_competitor_evidence_mode.py` | 3% | 3.0 |
| A2.2 | URL provenance validation | ทุก URL มีหลักฐานจาก web search จริง | PROVEN | `src/agents/competitor_evidence.py:258-309` | 2% | 2.0 |
| A2.3 | Default web discovery | ไม่ระบุคู่แข่งก็ค้นพบและวิเคราะห์ได้ | PARTIAL | `AGENT_PRODUCTION_READINESS_SPEC.md:216-218` Limited Beta | 2% | 1.0 |
| A2.4 | Quick Brief scope steering | Quick Brief เปลี่ยน scope/geography/field ได้ | PARTIAL | `AGENT_PRODUCTION_READINESS_SPEC.md:217-218` | 1% | 0.5 |
| A2.5 | Agent Settings effect | analysis depth / competitor types มีผลกับ output | UNPROVEN | `config/agent_instructions.json:49-134` มี UI แต่ไม่มี real-model proof | 1% | 0.0 |
| A2.6 | Flexible competitor deliverable | ไม่ใช่แค่ตาราง รองรับ brief อื่น ๆ | UNPROVEN | `src/agents/competitor_evidence.py:350-448` บังคับตารางแบบเดิม | 3% | 0.0 |
| **Agent 3 — Campaign Strategist** |||||| **14%** |
| A3.1 | Product-only campaign | วางแผนจาก product context ได้ถูกต้อง | PROVEN | `data/agent3_beta_status.md`, `AGENT_PRODUCTION_READINESS_SPEC.md:249-251` | 4% | 4.0 |
| A3.2 | Financial guardrails | ไม่เสนอราคา/งบ/KPI โดยไม่มี baseline | PROVEN | `src/campaign_validator.py`, `tests/test_campaign_validator.py` | 2% | 2.0 |
| A3.3 | Competitor context | รับและใช้ผล Agent 2 ในบริบทแคมเปญ | PARTIAL | `src/agents/campaign_strategy.py:31-78` มี code แต่ยังไม่ผ่าน real-model ชัด | 2% | 1.0 |
| A3.4 | Agent Settings effect | budget/discount/forbid tactics มีผล | UNPROVEN | `config/agent_instructions.json:136-148` ไม่มี real-model proof | 2% | 0.0 |
| A3.5 | Mandatory live web research | User สั่งหาข้อมูลปัจจุบัน ระบบบังคับค้นและยืนยัน source | UNPROVEN | `web_search: true` แต่ไม่บังคับ (`src/agents/campaign_strategy.py:278-286`) | 2% | 0.0 |
| A3.6 | Flexible campaign output | ไม่บังคับ 7 หัวข้อ สามารถตอบตาม Quick Brief | UNPROVEN | `config/agents.yaml:348-355` บังคับ sections | 2% | 0.0 |
| **Agent 4 — Content Creator** |||||| **18%** |
| A4.1 | Facebook single post + image prompt | สร้าง Facebook post พร้อม caption/hashtags/image prompt | PROVEN | `AGENT_PRODUCTION_READINESS_SPEC.md:285-286`, `src/agents/content_creator.py` | 5% | 5.0 |
| A4.2 | TikTok post / 9:16 / script | สร้าง TikTok post พร้อม vertical script | UNPROVEN | `src/content_schema.py:37,71-74,84-110` รองรับ schema แต่ยังไม่ qualify | 2% | 0.0 |
| A4.3 | Multi-post count 1-20 | สร้างหลายโพสต์ตาม count ที่เลือก โดยไม่ซ้ำ | UNPROVEN | `src/wizard_ui.js:595-613` UI รองรับ แต่ qualify 1 โพสต์ | 2% | 0.0 |
| A4.4 | Actual image generation | สร้างภาพจริงจาก image prompt | UNPROVEN | `src/orchestrator.py:444-459`, cache image ว่าง | 3% | 0.0 |
| A4.5 | Actual video generation | สร้างวิดีโอจริงจาก video prompt | UNPROVEN | cache video มีข้อมูล แต่ไม่มี real-model qualification | 3% | 0.0 |
| A4.6 | Ask-before media mode | User เลือก media_when=ask แล้วไม่สร้างสื่อทันที | PROVEN | `tests/test_bug3_ask_mode_no_auto_media.py` | 1% | 1.0 |
| A4.7 | Asset / product image references | ระบุ asset_ids / อ้างอิงรูปสินค้าใน post | PARTIAL | `tests/test_phase4_media_wiring.py` ผ่าน mock | 1% | 0.5 |
| A4.8 | Content history / dedup | ตรวจว่า content ซ้ำกับเก่า | PARTIAL | `config/content_policy.yaml:1-13`, ผ่าน offline | 1% | 0.5 |
| **Frontier-Parity / Runtime** |||||| **24%** |
| F1 | Frontier model access | ใช้ OpenRouter model ได้ | PROVEN | `src/llm_client.py`, `config/agents.yaml` | 2% | 2.0 |
| F2 | Multimodal image input | ส่งรูปจริงเป็น input ให้ LLM | PARTIAL | `src/run_context.py:88` ส่งรูปได้ แต่ quality ยังไม่ผ่าน | 2% | 1.0 |
| F3 | Web search tool | เปิดใช้ web_search ได้ | PROVEN | `src/agents/base_agent.py:335-362` | 2% | 2.0 |
| F4 | Mandatory research mode | ระบบรู้ว่าต้องค้นเมื่อไหร่และบังคับให้ค้น | UNPROVEN | ยังเป็น optional | 3% | 0.0 |
| F5 | Structured artifact แยกจาก user-facing output | มี internal artifact ไว้ส่งต่อ และ user output แยกสำหรับอ่าน | UNPROVEN | ยังเป็น output เดียวกัน | 4% | 0.0 |
| F6 | Flexible deliverable by user intent | Output เปลี่ยนตาม Quick Brief ไม่บังคับ template | UNPROVEN | `required_output_sections` / strict JSON ครอบงำ | 3% | 0.0 |
| F7 | Multi-agent workflow | หลาย agent ต่อ flow ทำงานร่วมกันได้ | UNPROVEN (Post-Beta) | `web_viewer.py:3167` ตัดเหลือ 1 — ไม่นับเป็น Full Beta blocker | 3% | 0.0 |
| F8 | Planning / tool-use decision | LLM วางแผนว่าต้องค้น/เลือก tool อะไร | UNPROVEN | ยังไม่มี planning stage หรือ mode | 3% | 0.0 |
| F9 | Review/repair quality | แก้ไขผลงานได้โดยไม่ทำให้แย่ลง | PARTIAL | `_review_and_refine` มี แต่ใช้ model เดียวกัน | 2% | 1.0 |

### Calculation

```
Infrastructure  =  4.0 + 3.0 + 3.0 + 1.0 + 1.0 + 0.0 + 0.0 + 1.0 + 0.5 = 13.5
Agent 1         =  5.0 + 2.0 + 0.0 + 0.5 + 0.0 = 7.5
Agent 2         =  3.0 + 2.0 + 1.0 + 0.5 + 0.0 + 0.0 = 6.5
Agent 3         =  4.0 + 2.0 + 1.0 + 0.0 + 0.0 + 0.0 = 7.0
Agent 4         =  5.0 + 0.0 + 0.0 + 0.0 + 0.0 + 1.0 + 0.5 + 0.5 = 7.0
Frontier        =  2.0 + 1.0 + 2.0 + 0.0 + 0.0 + 0.0 + 0.0 + 0.0 + 1.0 = 6.0
--------------------------------------------------------------------------------
Total weighted score = 13.5 + 7.5 + 6.5 + 7.0 + 7.0 + 6.0 = 47.5
Full Beta Progress = 47.5 / 100 = 47.5%
```

**Atomic Full Beta Progress = 47.5%**

หมายเหตุ: ถ้าปรับ weights ตัวเลขจะเปลี่ยน แต่ rule ตัดสิน PROVEN/PARTIAL/UNPROVEN/BROKEN ตายตัว

---

## D. Architecture Proposals — Required vs Optional

| Proposed Component | ปัญหาจริงที่พบ | User-Visible Failure ถ้าไม่ทำ | ทำไมแก้เล็กกว่าไม่ได้ | Required / Optional |
|---|---|---|---|---|
| **Remove `flow_agents[:1]` lock (preparatory)** | UI เปิด multi-agent แต่ backend ตัดเหลือตัวแรก | User ลาก agent หลายตัวแต่รันตัวเดียว | ไม่มี component ใหม่ แค่เอา lock ออก แล้วใช้ `run_flow_steps` ที่มีอยู่ (`web_viewer.py:3274`) แต่นี่เป็นเฉพาะ compatibility prep ไม่ใช่ user-facing feature | **PREPARATORY / FUTURE** (ไม่นับเป็น Full Beta deliverable) |
| **Remove `required_output_sections` หรือทำ optional** | `product_spec` บังคับ 10 sections, `campaign_strategy` บังคับ 7 | User ขอ executive brief/one-page ไม่ได้ | เปลี่ยนแค่ `config/agents.yaml` และ `output_validators.py` ไม่ต้องแก้ BaseAgent | **REQUIRED** |
| **Mandatory research mode** | `web_search: true` คือ "อนุญาต" ไม่ใช่ "บังคับ" | User ขอข้อมูลปัจจุบัน แต่ Agent 3 ไม่ค้น | ต้องใส่ mode ใน `BaseAgent`/`Orchestrator` เพื่อ inject tool หรือ prompt บังคับ | **REQUIRED** |
| **Soften `CONTENT_RESPONSE_SCHEMA` สำหรับ TikTok/multi-post** | Schema `posts[]` บังคับทุก field | TikTok / multi-post ผิดรูปหรือ fail | อาจเพิ่ม `oneOf` หรือลด required บาง field ตาม platform | **REQUIRED** สำหรับ Agent 4 |
| **Review model แยก** | Reviewer ใช้ model เดียวกับ generate | คุณภาพ review ต่ำ | แก้แค่ `review_model` config หาก model ดีกว่า | **OPTIONAL** (cost สูง) |
| **Artifact Store (`src/artifact_store.py`)** | ต้องการ artifact_id + input_refs | Multi-agent ไม่ทำงาน | `run_flow_steps` มี context handoff แล้ว (`web_viewer.py:3274`) สามารถเอา lock ออกก่อน | **FUTURE** (ไม่จำเป็นตอนนี้) |
| **Context Adapter** | ต้อง map artifact → prompt | Multi-step ผิด context | ปัจจุบัน `build_prompt` รับ positional args จาก orchestrator พอใช้ | **FUTURE** |
| **Planning stage แยก** | LLM ต้องวางแผนก่อนทำ | Tool ถูกใช้ผิดเวลา | สามารถเริ่มจาก prompt/system prompt บอกให้ค้นเมื่อต้องการ current facts | **OPTIONAL** |
| **Separate structured vs user-facing output** | User เห็นรูปแบบเดิม | ไม่ flexible | เอา `required_output_sections` ออกแล้วให้ LLM ตอบตาม intent ก็แก้ส่วนใหญ่ | **PARTIAL — ทำผ่าน constraint relief** |

**สรุป architecture changes ที่ REQUIRED สำหรับ Full Beta (smallest set):**

1. ลบ/ทำ optional `required_output_sections` สำหรับ `product_spec` และ `campaign_strategy` (config + validator)
2. ลบ `flow_agents[:1]` lock ใน `web_viewer.py` (กับ `api_run_agents` ถ้ามี) และ test multi-agent flow ที่มีอยู่
3. เพิ่ม `research_required` mode ใน `BaseAgent`/`Orchestrator` สำหรับ Agent 3
4. Soften `CONTENT_RESPONSE_SCHEMA` / required fields สำหรับ TikTok/multi-post

**ไม่ต้องสร้าง:** `artifact_store.py`, `context_adapter.py`, planning engine, separate review model

---

## C. Root-Cause → Minimal Change Mapping

| Gap | Root Cause | Smallest Viable Change | Shared / Per-Agent | Code / Qualification | Files | Regression Risk |
|---|---|---|---|---|---|---|
| A1.3 Image-derived claims ไม่พิสูจน์ | ยังไม่เคยรัน real-model กับรูป | รัน real-model 1 ครั้ง กับสินค้าที่มีรูป + Quick Brief | Per-Agent | Qualification | `src/agents/product_spec.py` | ต่ำ |
| A1.4 Multi-product ไม่ผ่าน real-model | ยังไม่ qualify แบบรวม | รัน real-model กับ 2 สินค้า ตรวจว่าไม่ปนกัน | Per-Agent | Qualification | `src/agents/product_spec.py` | ต่ำ |
| A1.5 Product spec ไม่ flexible | `required_output_sections` บังคับ 10 หัวข้อ | ลบ/comment หรือทำ optional ใน `config/agents.yaml` + ปรับ `output_validators.py` | Per-Agent (config) | Code (small) | `config/agents.yaml:103-113`, `src/output_validators.py` | ต่ำถึงกลาง (อาจทำให้ test บางตัวพัง) |
| A2.3 Default discovery แค่ Limited Beta | ยังไม่ qualify ลึก | รัน real-model default discovery อีกชุด | Per-Agent | Qualification | `src/agents/competitor_analysis.py` | ต่ำ |
| A2.5 Agent 2 settings ไม่พิสูจน์ | ยังไม่เคย test กับ real model | รัน real-model กับ analysis depth / competitor types | Per-Agent | Qualification | `config/agent_instructions.json`, `src/agents/competitor_analysis.py` | ต่ำ |
| A2.6 ไม่ flexible | `CompetitorReportRenderer` บังคับตาราง | ลองลดบังคับตาราง หรือให้ LLM สร้าง format ตาม Quick Brief แต่ยังส่ง JSON evidence | Per-Agent | Code (small) | `src/agents/competitor_evidence.py:350-448` | กลาง |
| A3.4 Agent 3 settings ไม่พิสูจน์ | Settings ไม่เข้า prompt ชัด / ยังไม่ qualify | ตรวจ `_format_instructions` แล้วรัน real-model กับ budget/discount | Shared (BaseAgent `_format_instructions`) + Per-Agent | Qualification | `src/agents/base_agent.py:62-214`, `config/agent_instructions.json:136-148` | ต่ำ |
| A3.5 Mandatory live web ไม่มี | `web_search: true` คือ optional | เพิ่ม `research_required: true` ใน config + force `web_search` tools | Shared (BaseAgent) | Code (small) | `src/agents/base_agent.py:333-363`, `src/orchestrator.py:319-415`, `config/agents.yaml` | กลาง |
| A3.6 Campaign output ไม่ flexible | `required_output_sections` 7 หัวข้อ | ทำ optional/ลบ sections | Per-Agent (config) | Code (small) | `config/agents.yaml:348-355`, `src/output_validators.py` | กลาง |
| A4.2 TikTok ไม่พิสูจน์ | ยังไม่ qualify | รัน real-model กับ TikTok + Quick Brief | Per-Agent | Qualification | `src/agents/content_creator.py`, `src/content_schema.py` | ต่ำ |
| A4.3 Multi-post ไม่พิสูจน์ | ยังไม่ qualify มากกว่า 1 | รัน real-model กับ count=2 ต่อ platform | Per-Agent | Qualification | `src/wizard_ui.js`, `src/orchestrator.py`, `src/content_schema.py` | ต่ำ |
| A4.4/A4.5 Image/Video gen ไม่พิสูจน์ | Media gen cache ว่าง/ไม่เคย end-to-end | รัน real-model กับ image/video gen | Per-Agent (Tool) | Qualification | `src/media_gen.py`, `src/orchestrator.py:421-532` | กลางถึงสูง (ค่าใช้จ่าย) |
| I6 Multi-agent lock (preparatory) | `flow_agents = flow_agents[:1]` | ลบ lock เพื่อ compatibility สำหรับ team flow ในอนาคต โดยไม่นับเป็น Full Beta acceptance | Shared | Code (1 line) | `web_viewer.py:3167` | ต่ำ (ไม่เปลี่ยน user-facing scope) |
| F5 Structured artifact แยก | ยังไม่มี | สำหรับ `content_creator` มีอยู่แล้ว สำหรับ agent อื่นยังไม่ต้องสร้างใหม่ ถ้าเอา `required_output_sections` ออกและให้ `output/` เป็น user-facing ก็พอ | Shared/Per-Agent | Code (small) | `src/output_validators.py`, `src/orchestrator.py` | ต่ำ |

**สรุป:** ส่วนใหญ่ของ gap เป็น **Qualification** (ประมาณ 10 รายการ) และ **Small code changes** ไม่ใช่ **Large shared runtime refactor**

---

## G. Minimal-Dependency Roadmap

### M0 — Scope Freeze (ไม่แก้โค้ด)

- ตกลงว่า UI capabilities ไหนอยู่ใน Full Beta scope
- ตกลง weight policy ของ atomic score
- Gate: PO อนุมัติ

### M1 — Constraint Relief (shared, small)

- **Why now:** ทำให้ qualification ต่อจากนี้มีความหมาย ไม่ใช่รันแล้วติด template
- **Scope:**
  1. ลบ/ทำ optional `required_output_sections` ใน `product_spec` และ `campaign_strategy`
  2. ลบ `flow_agents[:1]` lock ใน `web_viewer.py` เฉพาะเพื่อ compatibility/preparatory (ไม่เปิด user-facing multi-agent flow หรือใช้เป็น Full Beta acceptance gate)
  3. เพิ่ม `research_required` flag สำหรับ Agent 3
  4. Soften `CONTENT_RESPONSE_SCHEMA` สำหรับ TikTok/multi-post
- **Files:** `config/agents.yaml`, `src/output_validators.py`, `web_viewer.py:3167`, `src/agents/base_agent.py`, `src/content_schema.py`
- **User-Visible Result:**
  - สามารถขอ brief/one-page ได้
  - backend ไม่ตัด agent ตัวถัดไปใน flow (preparatory สำหรับอนาคต; ยังไม่เปิด user-facing multi-agent flow หรือนับเป็น Full Beta gate)
  - Agent 3 ค้นสดเมื่อบังคับ

> หมายเหตุ: ห้ามใช้ผล backend runner หลาย agent เป็นหลักฐานว่า user-facing team flow พร้อมใช้ใน Full Beta
- **Offline Gate:** 776 tests ผ่าน
- **Real-Model Gate:** ยังไม่ต้อง
- **Regression Risk:** กลาง (ต้อง update test ที่ assert sections)

### M2 — Agent 1 Qualification

- **Why now:** M1 เอา constraint ออกแล้ว รันจริงได้
- **Scope:** image + multi-product + flexible brief
- **Files:** `src/agents/product_spec.py`
- **Real-Model Gate:** 1-2 calls
- **Frontier Gate:** ไม่ต้อง

### M3 — Agent 2 Qualification

- **Why now:** ไม่ขึ้น Agent 1
- **Scope:** settings + custom deliverable
- **Files:** `src/agents/competitor_analysis.py`, `src/agents/competitor_evidence.py`
- **Real-Model Gate:** 1-2 calls

### M4 — Agent 3 Qualification

- **Why now:** ต้องมี `research_required` จาก M1 ก่อน
- **Scope:** live web mandatory + settings + flexible output
- **Files:** `src/agents/campaign_strategy.py`
- **Real-Model Gate:** 1-2 calls

### M5 — Agent 4 Qualification

- **Why now:** ขึ้น M1 schema soften + media gen tool
- **Scope:** TikTok + multi-post + actual image (และ video ถ้าเปิด)
- **Files:** `src/agents/content_creator.py`, `src/media_gen.py`
- **Real-Model Gate:** 3-5 calls

### M6 — Frontier-Parity UAT

- **Why now:** หลัง capability ครบ
- **Scope:** Side-by-side blind review 3-5 งานหลัก
- **Files:** ไม่แก้โค้ด
- **Gate:** คะแนนไม่ด้อยกว่า Frontier

### M7 — Full Beta Release

- PO อนุมัติ หลัง M6

---

## Final 4 Answers

1. **Full Beta Progress (atomic capability score) = 47.5%** คำนวณจาก matrix ด้านบน
2. **Shared architecture changes ที่ REQUIRED จริง = 3 รายการ + 1 รายการ PREPARATORY:**
   - ลบ/ทำ optional `required_output_sections` (product_spec + campaign_strategy)
   - เพิ่ม `research_required` mode สำหรับ Agent 3
   - Soften `CONTENT_RESPONSE_SCHEMA` สำหรับ TikTok/multi-post
   - (Preparatory) ลบ `flow_agents[:1]` lock เพื่อ compatibility ของ future team flow ไม่นับเป็น Full Beta deliverable
3. **Gaps ที่เป็นเฉพาะ qualification = 10 รายการหลัก:** A1.3 image, A1.4 multi-product, A2.3 default discovery, A2.5 settings, A2.6 flexible deliverable, A3.4 settings, A3.5 mandatory web, A3.6 flexible output, A4.2 TikTok, A4.3 multi-post, A4.4/A4.5 image/video gen
4. **Milestone แรกที่ควร implement จริง = M1 — Constraint Relief**
   - เหตุผล: เป็น shared changes ที่เล็กทีสุด ปลด lock ที่ขวาง LLM ให้ตอบตาม user intent ทำให้ qualification ต่อไปไม่เสียไปกับ template แบบเดิม เป็น prerequisite ของทุก Agent qualification

---

**สรุปสำหรับตัดสินใจ:**

- ไม่ต้องสร้าง `artifact_store.py`, `context_adapter.py`, planning engine หรือ general-purpose agent framework
- ปัญหาใหญ่คือ **over-constraint จาก config/validator** ไม่ใช่ architecture หลักขาด
- ทำ M1 (4 อย่างเล็ก ๆ) ก่อน แล้วค่อย qualify Agent 1-4 ตามลำดับ dependency

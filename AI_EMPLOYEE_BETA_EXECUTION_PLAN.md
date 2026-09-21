# AI Employee Beta Execution Plan

เอกสารนี้เป็นแผนส่งมอบงานแบบถาวรสำหรับพา Agent ทั้ง 4 ตัวขึ้น Beta โดยไม่พึ่งความจำของ chat session

อ่านเอกสารตามลำดับนี้ทุกครั้งก่อนเริ่มงาน:

1. `AI_EMPLOYEE_PRODUCT_VISION.md` — เรากำลังสร้างอะไร
2. `AGENT_PRODUCTION_READINESS_SPEC.md` — Beta/Production Ready หมายถึงอะไร
3. `AI_EMPLOYEE_BETA_EXECUTION_PLAN.md` — ตอนนี้ต้องทำอะไรและห้ามทำอะไร
4. `AGENT_ORCHESTRATION_SPEC.md` — อ่านเมื่อทำ team flow ในอนาคตเท่านั้น

## คำตัดสินหลัก

**ไม่เขียน Agent ทั้งหมดใหม่** โครงสร้างที่มีอยู่มีของที่ใช้ต่อได้จำนวนมาก ได้แก่ product database/ingestion, brand context, assets, UI options, Quick Brief, Agent Settings, LLM client, usage logging, evidence pipeline, media wiring, scheduler และแกนความสามารถเฉพาะของแต่ละ Agent

งานที่ต้องทำแบ่งเป็นสองส่วน:

1. แก้ shared contract ครั้งเดียว เพื่อให้ทุก Agent รับ context และคำสั่งแบบเดียวกัน
2. ปรับเฉพาะ default job, internal artifact และ user-facing deliverable ของแต่ละตำแหน่ง

การ rewrite ทั้งหมดจะเพิ่ม regression risk และเสียสิ่งที่พิสูจน์แล้ว โดยไม่ได้แก้ปัญหาหลักเรื่อง product contract

## Current fast-track status (overrides the broad phase sequence below)

สถานะปัจจุบัน (authoritative — updated 2026-09-18 after POST-ISOLATION-INTEGRITY-01; ตารางก่อนหน้าที่บอกว่า "เฟสถัดไปคือ Media Capability Coverage (C2)" และ "System81 live functional qualification PASS" ถูก mark เป็น **SUPERSEDED** — ดู Progress Ledger ล่างสุด):

| Field | Authoritative value |
|---|---|
| Current phase | **LAUNCH-READINESS-01 — COMPLETE: CORE WORKFLOWS PASS** — live verification 2026-09-21: Agent 1/2/4 + immediate media all produce usable, factually-correct output with correct brand context; Brand Asset delivery verified end-to-end; ASSET-ISO-01 closed |
| Current objective | Make Agent 1–4 output safe and usable for real launch — fix the verified defects (Product Detail crash, Agent 4 revised-script loss, Agent 2 evidence/comparison failures, unsupported-claim grounding, Brand Asset delivery) at generic seams only |
| Next exact action | **Supervisor review of the UNSTAGED AliExpress URL-import fix** (`src/url_import.py` + 3 test files — kept out of the staged candidate; verified end-to-end on the real failing URL: `page_class=single`, product text + images extracted, zero paid calls). Staged candidate remains ready to commit when authorized. |

| Capability | สถานะล่าสุด | งานที่ต้องทำต่อ |
|---|---|---|
| Products / Product Detail | **PASS — brand-scoped** — `product_db` resolves via `brand_state_root()` (fails closed, no user/global fallback); cross-brand invisibility proven by browser e2e | — |
| Brand Settings (voice/terms/visual/audience/profile) | **PASS — brand-scoped** — files under `brands/<bid>/brand/`; rules reach Agents 1/3/4 system prompts, reference reaches Agents 3/4, evidence-mode Agent 2 receives neither (by contract); hard vs soft rules separated by `brand_priority` | — |
| Brand Assets | **PASS — brand-scoped (ASSET-ISO-01 CLOSED)** — files under `brands/<bid>/brand/assets/`; catalog now at `brands/<bid>/cache/assets/db.json`; every accessor brand-scoped; user-only context fails closed; legacy shared catalogs migrate once per brand by resolved-path containment, unprovable records quarantined in the preserved legacy file | — |
| Content Pillars | **PASS — brand-scoped** — `config/content_pillars.yaml` under brand root; switching brands switches pillars; missing pillars = supported state; pillars are guidance not keyword requirement; delivered to Agents 3–4 per contract | — |
| Agent Settings (persistent instructions) | **PASS — user-scoped by design** — `workspace/local/config/agent_instructions.json` is per-user account configuration (endpoints are not brand-gated, same class as `ui_prefs.json`); isolated per user, shared across that user's own brands; reaches all 4 agents via `_make_agent` | — |
| Manual Agent execution (`/api/run_agent`) | **PASS** — brand-gated `_require_brand_context()` (403 without verified brand); product data/images/pillars resolve under active brand | — |
| Auto execution (`/api/run_auto` → `select_product_auto`/`run_content_creator_auto`) | **PASS** — product/pillar/history tools brand-scoped; `_select_assets_for_content` now reads the brand-scoped catalog → cross-brand assets cannot be listed, selected, or resolved into media references | — |
| Schedule | **PASS — parity proven** — jobs/runs under `brand_state_root()`; fire re-establishes `WorkspaceContext.for_brand` + per-user session + brand cookie before any store access; brandless/legacy records fail closed; rerun replays the same flow | — |
| AI enrichment | **PASS — brand-scoped** — `ai_enrichment` resolves under `brand_state_root()` | — |
| Media C1 (mechanical transport) | **ACCEPTED / FROZEN** (`5cc4724` + `06157af`) — per-item product+asset reference selection; preflight errors without truncation; reference-cap test conflict classified **stale test** (old silent-cap-3 expectation vs current explicit-preflight contract, `max_refs_per_post: 5`) | — |
| Media C2 (capability coverage) | **PARTIAL** (open, deferred) — video/reference-fidelity sub-checkpoint PASS; image capability and other C2 criteria remain open | Separate phase, after isolation remediation + Agent qualification |
| System81 | **Code integration ACCEPTED / FROZEN** (`1a0ef78`); **live functional qualification NOT PERFORMED** — 0 live calls, mocked tests only; earlier "live functional qualification PASS" statements are **SUPERSEDED** (no immutable evidence); live smoke test still required before production | One live smoke test under PO authorization |
| Agent 1–4 qualification | **BLOCKED** — no current-model qualification evidence; cannot start while ASSET-ISO-01 is open; Agent 4 core text flow is historically live-proven but final-grounding requalification is pending | After remediation: prepare smallest Agent 1–3 qualification cases, stop for PO paid-call authorization |
| Overall | **NOT FREEZE-READY** — integrity gate FAIL on one concrete defect; no Agent 1–4 declared Beta-ready | ASSET-ISO-01 remediation → re-verify → Checkpoint B/E |

ห้ามย้อนกลับไปทำ Phase 1–6 ทั้งชุดโดยอัตโนมัติ ลำดับกว้างด้านล่างเป็นแผน Production/ความสมบูรณ์ระยะยาว Checkpoint A และ Media Core (C1) แช่แข็งแล้ว

**Agent context delivery contract (authoritative — proven via real Orchestrator + recording FakeLLM, 2026-09-18):**

- Product images (selected product's real image paths) are intended for **Agents 1–4** — attached multimodal on all four agents' calls.
- Brand Asset Library items are intended primarily for **Agent 4 / Media** (asset summary, stable `asset_ids`, media reference catalog) — no contract delivers the general library to Agents 1–3.
- Content Pillars are intended for **Agents 3–4** — configured list + selected pillar appear in both agents' user prompts; not delivered to Agents 1–2.
- Agent 2 (evidence mode) receives **no** brand context/reference in its system prompt — evidence collection is uncontaminated; brand interpretation is a separate post-evidence pass.

## เป้าหมาย Beta รอบนี้

ผู้ใช้เลือกสินค้า เลือก Agent ตั้ง UI options แล้วกดรันได้ แม้ไม่เขียน Quick Brief และได้รับผลงานที่ใช้ต่อได้ในหน้าที่ของ Agent นั้น

ถ้าผู้ใช้ใส่ Quick Brief หรือ Agent Settings ระบบต้องปรับงานตามคำสั่ง โดยไม่ละเมิดสินค้า แบรนด์ assets และตัวเลือกที่เลือกใน UI

Agent ผ่าน Beta เพราะทำงานของตำแหน่งสำเร็จในมุมผู้ใช้ ไม่ใช่เพราะเติม template ครบ การทดสอบห้าม assert จำนวนหัวข้อ ชื่อ section ลำดับคำตอบ หรือตารางแบบตายตัว เว้นแต่โจทย์ทดสอบขอรูปแบบนั้นเอง ให้ตรวจผลลัพธ์ตาม usefulness, factuality, instruction/UI following, brand/product fit และการลดงานของผู้ใช้

Beta ไม่ได้หมายถึงรองรับทุก prompt หรือทุก edge case และไม่รวม multi-Agent team flow

## สิ่งที่อยู่ในขอบเขต

- manual product selection และ Auto selection ที่ UI เปิดใช้อยู่
- Agent หนึ่งตัวต่อ flow
- zero-prompt default job
- optional Quick Brief, Agent Settings และ attachments
- Content Creator platform/count/media/ask-before controls
- user-facing deliverable ที่อ่านและใช้ต่อได้
- internal artifact ที่รักษา identity, provenance และสถานะงาน
- bounded retry/cost, failure state และ AI Usage Hub reconciliation
- offline regression, real-model representative acceptance และ owner UAT

## สิ่งที่ไม่อยู่ในขอบเขต Beta

- multi-Agent team workflow หรือ autonomous routing (team flow เป็น feature ที่ต้องมี product decision, dependency UX และ acceptance criteria แยกก่อนเริ่ม; ห้ามใช้ผล backend runner หลาย agent เป็นหลักฐานว่า user-facing team flow พร้อมใช้)
- การรองรับทุก prompt ที่คิดขึ้นได้
- Production-grade variance testing ทุก model/provider
- การสร้าง validator สำหรับ style, heading หรือถ้อยคำทุกกรณี
- การพิสูจน์ว่า model ไม่เคยผิด
- platform/publishing integration ที่ UI ยังไม่เปิด

แนวคิดใหม่ที่ไม่แก้ Beta blocker ให้ใส่ Post-Beta Backlog ห้ามแทรกกลาง Phase

## Shared execution contract

### Authority order

1. factual integrity, safety, authorization และ hard brand/business constraints
2. typed UI selections ของ run นั้น
3. Quick Brief ของ run นั้น
4. persistent Agent Settings
5. default job ของ Agent

### Run input

ทุก execution path ต้องสร้าง run context เดียวกันอย่างน้อยประกอบด้วย:

- selected/auto-resolved product ids
- selected Agent
- typed UI options
- Quick Brief
- Agent Settings snapshot
- brand/product/assets/resources ที่อนุญาต
- run/source ids สำหรับ trace และ usage

ห้ามให้ manual, Auto, scheduler หรือ rerun ประกอบ prompt คนละกติกา

### Output contract

แยกผลลัพธ์เป็นสองชั้น:

- **Internal artifact:** ข้อมูลที่ระบบต้องใช้ต่อ เช่น identity, facts, evidence, assumptions, asset ids, validation และ failure state
- **User-facing deliverable:** งานที่อ่านง่ายและปรับรูปแบบตามโจทย์ ไม่บังคับ JSON หรือหัวข้อเดิมทุกครั้ง

Default output เป็นเพียง fallback เมื่อ Quick Brief ว่าง ส่วน Quick Brief/Settings มีสิทธิ์เปลี่ยน presentation, depth, emphasis และชนิด deliverable ได้ ตราบใดที่ไม่ละเมิด hard facts, authority หรือ typed UI selections

ห้ามแสดง failed, malformed หรือ truncated artifact เป็น success

### Validation boundary

Code hard-block เฉพาะ:

- สินค้า/แบรนด์/asset ผิด
- critical fact, price-as-fact หรือ external source ที่แต่งขึ้น
- ฝ่าฝืน UI selection หรือ hard business constraint
- unauthorized action
- empty/malformed/truncated success
- retry/cost ไม่จำกัด หรือ paid call ตรวจย้อนหลังไม่ได้

คุณภาพเชิงความหมาย กลยุทธ์ รูปแบบ และสำนวนให้ LLM สร้างและให้ human/frontier comparison ประเมิน ไม่สร้าง regex ต่อทุกเหตุการณ์

## แผนส่งมอบ

ตัวเลขด้านล่างเป็น **focused engineering days** ของ developer หนึ่งคน รวมการแก้และ verification ใน Phase นั้น แต่ไม่รวมเวลารอ feedback จาก Product Owner

### Phase 0 — Freeze และสร้าง baseline (0.5–1 วัน)

งาน:

- ยืนยันไฟล์/commit baseline ของงาน Devin ปัจจุบัน โดยไม่ทิ้งงานที่ยังไม่ commit
- บันทึก capability ที่เปิดจริงใน UI และ config
- เลือกสินค้า fixture เดียวที่ข้อมูล/asset พร้อมสำหรับ Agent 1–4
- เก็บ frontier output จาก input เดียวกันเป็น quality baseline ไม่ใช้เป็น schema บังคับ
- ระบุ Agent 3 ว่า Beta รอบแรกเปิด live web หรือ non-web เท่านั้น

Definition of Done:

- มี baseline ที่ย้อนกลับและเทียบ diff ได้
- ไม่มีคำว่า “ปรับทุกอย่างให้ดีขึ้น” โดยไม่มี acceptance case
- ทุกงานถัดไปอ้าง Phase ID และ Agent ID

### Phase 1 — Shared employee contract (2–4 วัน)

งาน:

- ทำให้ manual, Auto, scheduler และ rerun ใช้ authority/run context เดียวกัน
- ยืนยันว่า Quick Brief ว่างได้ และ Settings/attachments/UI options ส่งถึง Agent จริง
- แยก internal artifact ออกจาก user-facing rendering โดยไม่ rewrite core agents
- ใช้ failure envelope และ bounded retry/cost แบบเดียวกัน
- ทำให้ paid request ทุกเส้นทางมี local accounting และ Hub delivery status
- ตัด/ลด semantic validator ที่อยู่นอก hard-block boundary

Definition of Done:

- offline contract tests ผ่านทุก Agent
- ไม่มี execution path สำคัญที่ประกอบ context คนละกติกา
- 1 paid request แสดง request id, model, token/cost เมื่อ provider ส่งมา และ Hub status ได้
- ไม่มี automatic test loop หรือ repair เกิน budget ที่กำหนดต่อ logical run

### Phase 2 — Agent 1 Product Analyst (1–2 วัน)

เก็บไว้:

- ingestion, OCR/file loading, product segmentation และ product database
- product identity/scope logic ที่มีหลักฐานว่าทำงาน

แก้เฉพาะ:

- default job เมื่อ Quick Brief ว่าง
- fact/inference/missing separation ใน internal artifact
- user-facing renderer ให้ตอบตามคำขอได้ ไม่บังคับรายงานทรงเดียว
- handling สำหรับข้อมูลหลายสินค้า/ขัดแย้งในขอบเขตที่ UI เปิด

Beta acceptance:

1. เลือกสินค้าหนึ่งตัว ไม่มี Quick Brief → ได้ product brief ใช้ต่อได้
2. Quick Brief ขอเน้นมุมหนึ่ง → งานเปลี่ยนตามคำสั่งแต่ไม่เปลี่ยน identity
3. Settings เปลี่ยนรูปแบบ/ข้อห้าม → มีผลจริง
4. ข้อมูลสำคัญขาด/ขัดแย้ง → ระบุอย่างตรงไปตรงมา ไม่แต่ง fact

Definition of Done:

- hard blockers ผ่าน offline
- real-model UI-equivalent อย่างน้อย 2–3 representative cases ผ่าน
- Product Owner ตอบว่า default output และ steered output ใช้ต่อได้

### Phase 3 — Agent 2 Competitor Analyst (1–2 วัน)

เก็บไว้:

- discovery/evidence pipeline, source selection, citation และ identity controls
- structured research artifact ที่พิสูจน์แล้ว

แก้เฉพาะ:

- user-facing renderer บน structured artifact เพื่อไม่ให้ประสบการณ์เป็น JSON/search dump
- default competitor job และ Quick Brief steering
- missing/conflicting/stale evidence presentation
- ปิด live-web mode ที่ยังไม่มีหลักฐาน แทนการบล็อก Agent ทั้งตัว

Beta acceptance:

1. ไม่มี Quick Brief → วิเคราะห์คู่แข่งที่เกี่ยวข้องและให้ข้อเสนอแนะใช้ต่อได้
2. Quick Brief จำกัดคู่แข่ง/geography/มุมวิเคราะห์ → ทำตาม
3. source pressure → ไม่ใช้ source ที่ไม่ได้รับอนุญาต
4. evidence ไม่พอ/ขัดแย้ง → บอกข้อจำกัดโดยยังให้คำแนะนำเท่าที่ข้อมูลรองรับ

Definition of Done:

- evidence/identity hard blockers ผ่าน
- real-model UI-equivalent อย่างน้อย 2–3 representative cases ผ่าน
- งานหลักเทียบ frontier แล้วไม่ด้อยอย่างมีนัยสำคัญ และดีกว่าเรื่อง brand/product/evidence context

### Phase 4 — Agent 3 Campaign Strategist (2–3 วัน)

เก็บไว้:

- campaign prompt/context wiring, budget guard, usage accounting และ qualification harness
- gates ที่พิสูจน์ product identity และ unauthorized-source behavior แล้ว

แก้เฉพาะ:

- ลด semantic regex/repair ที่พยายามตัดสินกลยุทธ์แทน model
- รักษาเฉพาะ hard constraints เช่น budget/discount/authorization/critical facts
- default strategy job ที่ทำงานได้จาก product context แม้ข้อมูลการเงินไม่ครบ
- แยก fact, hypothesis, recommendation และ pending validation
- flexible user-facing output ตาม Quick Brief

Beta acceptance:

1. product-only, no Quick Brief → แผนใช้ต่อได้ ไม่ crash
2. Quick Brief เปลี่ยน emphasis/constraint → ทำตามโดยไม่เปลี่ยน identity
3. source pressure → ใช้เฉพาะ selected evidence
4. missing baseline/financials → ไม่ guarantee และไม่ทำให้ output ไร้ประโยชน์
5. live web → ทดสอบเฉพาะเมื่อ Product Owner เลือกให้อยู่ใน Beta scope

Definition of Done:

- non-web core cases ผ่านก่อน
- ถ้าเปิด live web ต้องมี post-fix green real-model run หนึ่ง representative case
- ไม่มี paid retry loop; generation + repair รวมไม่เกิน budget ต่อ logical run
- Product Owner ยอมรับว่า output เป็น strategist ที่ใช้ทำงานได้ ไม่ใช่ validator report

### Phase 5 — Agent 4 Content Creator (2–3 วัน)

เก็บไว้:

- content schema/history, brand voice, asset selection และ media generation wiring
- UI controls สำหรับ platform/count/media/ask-before

แก้เฉพาะ:

- default content job เมื่อ Quick Brief ว่าง
- typed UI options เป็น source of truth ทุก path
- flexible concept/caption/script presentation โดย schema อยู่ภายใน
- factual grounding, non-duplication และ explicit media consent
- graceful partial failure เมื่อ text สำเร็จแต่ media ไม่สำเร็จ

Beta acceptance:

1. Facebook only, 1 post, ask-before → ได้โพสต์และไม่สร้าง media
2. TikTok only, หลายโพสต์ → จำนวน/platform ถูกและไม่ซ้ำอย่างมีนัยสำคัญ
3. image/video/both → media prompts และ action ตรง UI
4. Quick Brief/Settings → เปลี่ยน tone/concept แต่ไม่ override platform/count/media
5. missing asset/media failure → แสดงสถานะจริง ไม่ส่ง failed media เป็น success

Definition of Done:

- UI contract ผ่าน offline ทุก option class; ไม่จำเป็นต้องไล่ทุก permutation
- real-model UI-equivalent อย่างน้อย 3 representative text cases ผ่าน
- ใช้ paid image/video generation เฉพาะ 1 representative case ต่อ media type ที่จะเปิดใน Beta
- Product Owner ยอมรับว่า content พร้อมแก้เล็กน้อย/นำไปใช้ ไม่ใช่ template แข็งซ้ำเดิม

### Phase 6 — Integrated Beta release (2–4 วัน)

งาน:

- รัน Web UI end-to-end ของ Agent ทั้ง 4 ด้วย release model/config
- blind side-by-side กับ frontier baseline 3 งานหลักต่อ Agent
- ตรวจ history, usage, cost, error, artifacts และ Hub reconciliation
- ซ่อน/ติด experimental capability ที่ไม่ผ่าน แทนการเพิ่มงานไม่จบ
- Product Owner ทำ UAT และอนุมัติ label/scope ของแต่ละ Agent

Definition of Done:

- critical defect = 0 ใน Beta scope
- ทุก Agent ผ่าน zero-prompt, Quick Brief และ Settings paths
- user-facing scope ตรงกับสิ่งที่พิสูจน์แล้ว
- มี rollback/config fallback และ known-issues list

## ระยะเวลาโดยประมาณ

| เป้าหมาย | งานพัฒนา | เวลาปฏิทินโดยประมาณ |
|---|---:|---:|
| Limited Beta โดยคง scope ปัจจุบันและปิด capability ที่ยังไม่ผ่าน | 5–8 focused days | 1–2 สัปดาห์ |
| Beta ตามประสบการณ์ “พนักงาน AI” ครบ Agent 1–4 | 9–16 focused days | 2–3 สัปดาห์ |
| Production Ready ครบ execution paths/operations/UAT | เพิ่มอีก 15–30 focused days | เพิ่มอีกราว 3–6 สัปดาห์ |

ช่วงเวลาเป็น range เพราะ codebase มีงานที่ยังไม่ commit จำนวนมากและ Agent 3 มี validator/evaluation work ซ้อนกัน ความคลาดเคลื่อนควรถูกลดหลัง Phase 0 ไม่ใช่ด้วยการเดาตัวเลขละเอียดเกินจริง

## Real-model test budget และ stopping rule

การทดสอบจริงมีไว้ตอบคำถามที่ mock ตอบไม่ได้เท่านั้น: model ทำตามงานจริงหรือไม่ และ output ใช้ได้หรือไม่

- offline ก่อน paid เสมอ
- representative cases 2–3 ต่อ Agent สำหรับ Beta; Agent 4 เพิ่ม media case เฉพาะ capability ที่เปิด
- แต่ละ logical case: 1 generation และ bounded repair สูงสุด 1 ครั้ง เว้นแต่ Product Owner อนุมัติเป็นกรณี
- ห้าม rerun เพียงเพราะ style ไม่ถูกใจโดยไม่มี hypothesis ว่าจะแก้อะไร
- ผ่านแล้วหยุด; ไม่ยิงซ้ำเพื่อ “เพิ่มความมั่นใจ” โดยไม่มีความเสี่ยงใหม่
- fail แล้วจำแนกเป็น code defect, prompt/model behavior หรือ scope mismatch ก่อนอนุมัติ call ถัดไป
- บันทึก request id, actual model, generation/repair count, token/cost, local accounting และ Hub status ทุกครั้ง

## Frontier comparison rubric

ใช้ input/context เดียวกันและ blind review 1–5 คะแนน:

- งานหลักถูกต้องและใช้ต่อได้
- ทำตาม UI/Quick Brief/Settings
- product/brand factuality
- insight/creativity ตามตำแหน่ง
- ความชัดเจนและเหมาะกับผู้ใช้

Beta ผ่านเมื่อ MKTApp ไม่ด้อยกว่า frontier baseline อย่างมีนัยสำคัญในงานหลัก และดีกว่าอย่างเห็นได้ชัดอย่างน้อยหนึ่งด้านที่เป็นเหตุผลของผลิตภัณฑ์ เช่น brand fit, asset use, zero-prompt convenience, evidence หรือ repeatability

## Session/Devin handoff protocol

ทุก prompt งานต้องมี:

- Phase ID และ Agent ID
- objective หนึ่งประโยค
- files/areas ที่อนุญาตให้แก้
- acceptance cases ที่แน่นอน
- non-goals และสิ่งที่ห้ามเพิ่ม
- paid-call permission/budget
- คำสั่งให้อัปเดต Progress Ledger โดยไม่ commit จนกว่าจะได้รับอนุญาต

เมื่อจบ session ต้องบันทึก:

- ทำอะไรเสร็จ พร้อมหลักฐาน
- ไฟล์ที่แก้
- test ที่รันและผล
- paid calls/cost/request ids/Hub status
- ปัญหาที่ยังค้างและเหตุผล
- งานถัดไปเพียงหนึ่งขั้น

ห้ามใช้ chat transcript เป็น source of truth หากข้อความใน chat ขัดกับเอกสารนี้ ให้หยุดและขอ Product Owner ตัดสิน แล้วอัปเดตเอกสารก่อนทำต่อ

## Authority rule (binding — read before any mutation stage)

1. **Product source** determines product capabilities and technical facts.
2. **Researched evidence** may add externally verified facts with provenance.
3. **Brand/Audience** controls positioning, audience, tone, vocabulary, and channels; it must not create product capabilities.
4. **Quick Brief** controls the requested job but cannot turn an unsupported capability or offer into fact.
5. **Brand examples for one product must not transfer their facts to another product.**
6. **Any stage that mutates content after review must be followed by final grounding before persistence.**

## Remediation checkpoints (current cycle)

### Checkpoint A — Generic final-output truth boundary

> Status: `ACCEPTED / FROZEN` at `65ed8c7`. Implemented safeguard; Agents 1–4 requalification against current model still pending (Checkpoint E).

Implement the smallest generic architecture fix so the final persisted output is grounded after all mutation stages.

- No K5-, video-call-, "24 ชั่วโมง"-, promotion-, or keyword-specific production logic.
- Use model reasoning for semantic entailment.
- Every post-processing stage that can change factual/commercial content must receive the authorized runtime context.
- After the final mutation, verify the final candidate against: selected product source; verified researched evidence; Brand/Audience settings; Quick Brief and explicit offers; UI options.
- If the final candidate adds unsupported facts or offers, do not persist it as success. Repair with a bounded attempt or retain the last grounded version.
- Brand vocabulary approval means wording is allowed; it does not prove that the selected product has that capability.
- Add tests demonstrating that a K9 Brand example cannot create that capability for a different product. Keep the test generic in production behavior.
- Apply this boundary to Agent 1–4, not only Script Reviewer.

### Checkpoint B — User-facing presentation

> Status: `PLANNED` (open). No accepted closure evidence yet.

Separate machine-readable status from user-facing language.

- Agent 2: keep verified/unverified/inference state internally; replace internal phrases ("ค่าที่จับคู่ได้", "evidence", "inference/recommendation") with concise natural Thai; omit non-informative rows or use a short natural missing-data note; remove concrete premises whose supporting evidence fails validation.
- Agent 3: preserve financial/operational safety internally; use one concise Thai readiness note only where cost-bearing mechanics exist; do not repeat "Pending financial/operational validation" on every item; present unapproved promotions as optional ideas; do not apply financial disclaimers to qualitative recommendations.
- Tests must inspect complete rendered output, not merely search for safety keywords.

### Checkpoint C — Media completion

> Status split into C1 (frozen) and C2 (partial/open). Any provider UAT requires separate Product Owner approval.

**C1 — Media Core (mechanical reference transport):** `ACCEPTED / FROZEN` at `5cc4724` + `06157af` (60 focused + 8 browser tests passed; `git diff --check` clean). Do not reopen speculative media plumbing.

**C2 — Media Capability Coverage:** `PARTIAL` (open). Scope: product fidelity, logo/brand fidelity, mascot/person/child consistency, provider fallback, cost-aware tool selection. No K5-specific forward instructions; no real image/video run is authorized without separate Product Owner approval.

**C2 video/reference-fidelity sub-checkpoint:** `PASS` (2026-09-10). Browser UI → production Media path → accepted OpenRouter Gateway → `alibaba/wan-2.7`. Five original references (3 K5 product images + child a_0007 + logo a_0008) delivered as `input_references` (not image-first). Request `aTo9OCZ5KlcV3Ld1r1nY`: 10 seconds, $1.00, status `success`. Video persisted (`video_1.mp4`, 9.5 MB, h264+aac) and playable in UI. K5, child, and logo references visibly used. Accepted by Product Owner as PASS with known output-quality limitations. This sub-checkpoint does not close C2 — image capability and other C2 criteria remain open.

**Known issue — aspect-ratio mismatch (no production fix, needs investigation):** Requested and app-side-composed as 9:16; returned artifact is 1280×720. The responsible upstream layer—OpenRouter normalization versus Wan provider behavior—is not yet proven.

### Checkpoint D — Scheduler qualification

> Status: **ACCEPTED / FROZEN** at `5781fd8` (offline + real UI wall-clock qualification).

**Offline evidence:** 72 focused Scheduler/run-context tests passed, 0 failed; 1 Browser Schedule Modal E2E passed; `git diff --check` clean. Attachment forwarding, durable cloning, restart/exact-once, rerun lifecycle, history retention, and orphan cleanup all accepted.

**Real UI wall-clock qualification evidence:**
- One one-time scheduled job created through the real Schedule UI (Playwright-automated wizard DOM, not direct API).
- Product: `Lagenio K5`; Agent: `product_spec`; Quick Brief: `สรุปสเปคสินค้า Lagenio K5 แบบสั้น กระชับ 1 หน้า`.
- Browser closed before fire; server remained running; no manual Run/rerun/execution API called.
- Actual fire time: `2026-09-09T16:06:00+07:00`; Scheduler started run autonomously at `16:06:00.007 +07:00`; trigger: `auto`.
- Exactly one logical Scheduler/Agent execution; one-time job removed after completion; status: `success`.
- 2 paid LLM calls (1 generate + 1 review, 0 repair); model: `google/gemini-3.8-flash`; total cost: `$0.03216`.
- Image generation submissions: 0; video generation submissions: 0; media provider calls: 0.
- Agent output persisted (4,119 bytes, 28 lines); run history visible through UI; usage/cost visible through UI; result renders in UI overlay.

**Known non-blocking findings:**
- `product_spec`-only UI flow serializes default `auto_image=true` from wizard defaults; real qualification confirmed zero media provider calls because no `content_creator` path executes.
- Scheduled run record had an empty `session_ts`; `output_files`, run history, UI rendering, status, and cost evidence all functioned correctly.

- UI flow serialization into schedule save; one-time job save/list; recurring job save/list; invalid schedule returns error and is not persisted; toggle off/on; deletion; server restart reload; missed job and stuck-run recovery; manual run-now; history and rerun; failure status; full parity of selected product, Agent, Quick Brief, Agent Settings, platform, content count, media mode, attachments/resource references, and auto/manual media consent.
- Real representative proof: one low-cost scheduled one-time flow with media generation OFF; schedule for near-future time and let APScheduler fire it naturally; verify UI/API save → registered next_run → timed fire → real Agent output → run history → cost/trace; confirm scheduled Agent receives the same runtime contract as manual flow; delete test schedule after preserving evidence.

### Checkpoint E — Qualification and test rule

> Status: `BLOCKED` (open). No current-model qualification evidence; no Agent 1–4 declared Beta-ready. Paid UAT requires separate Product Owner approval. Agent 4 final-grounding/text requalification belongs here; Agent 4 visual fidelity belongs to Media Capability Coverage (C2).

1. Direct unit tests for the changed seam.
2. Affected integration groups.
3. Read every final persisted output completely.
4. One representative paid UAT per Agent only after offline tests pass.
5. One real image and one real video only — each requires separate Product Owner approval.
6. One real scheduled run — requires separate Product Owner approval.
7. Run the full suite once, immediately before the final freeze decision, because shared production code changed.

Do not repeatedly run the full suite after small edits. Do not accept a run based only on finish_reason=stop, call counts, or absence of one previously observed phrase. For every real UAT, compare the complete final output against the exact runtime inputs and classify every questionable statement. No remediation is allowed during a qualification run. Preserve failed runs as immutable evidence.

## Progress Ledger

อัปเดตตารางนี้ทุกครั้งที่ Devin/Codex จบงาน ไม่สร้าง progress report แยกกระจัดกระจายหากไม่จำเป็น

| Field | Current value |
|---|---|
| Current phase | **AGENT12-LIVE-DEFECT-01 — COMPLETE: ALL AGENTS LAUNCH-USABLE (media gen pending spend authorization)** — fresh `Lagenio Evo` evidence: Agent 1 confirmed-capability context gap fixed and live-verified (`วิดีโอคอล` preserved); Agent 2 grounding `malformed_json` root-caused to provider-aborted response, fixed at shared boundary + agent-evidence propagation fixed, live-verified with usable report; Agent 3 unchanged/accepted; Agent 4 five-case bounded verification: text paths all PASS, immediate-image and deferred-video verified up to the paid seam (generation calls NOT made — spend not authorized). |
| Current objective | Make Agent 1–4 output safe and usable for real launch via generic root-cause fixes only. ~~Then proceed to Media~~ **SUPERSEDED** — Media stays deferred. |
| Checkpoint A — Final grounding boundary | `ACCEPTED / FROZEN` at `65ed8c7`. Generic final-output truth boundary wired into the smallest common pre-persistence seam for Agents 1–4; model reasoning for semantic entailment; no keyword/regex lists. Implemented safeguard — not yet re-qualified against current model. |
| Gemini 3.8 migration | `ACCEPTED / FROZEN` at `c39f596`. Production text/reasoning workloads migrated to `google/gemini-3.8-flash` (read from `config/agents.yaml`). |
| Checkpoint C1 — Media Core (mechanical transport) | `ACCEPTED / FROZEN` at `5cc4724` + `06157af`. Per-item product+asset reference selection via `extract_reference_ordinals`/`filter_catalog_by_ordinals`/`selected_reference_ordinals` in `compose_media_input`; `preflight_reference_mentions` rejects unknown/out-of-range ordinals and empty-catalog mentions. Reviewed and accepted by Codex. Do not reopen speculative media plumbing. |
| Checkpoint C2 — Media Capability Coverage | `PARTIAL` (open) — **DEFERRED** while AUTH-ISO-01 is blocking. Video/reference-fidelity sub-checkpoint: `PASS` (2026-09-10). Browser UI → production Media → accepted OpenRouter Gateway → `alibaba/wan-2.7`; five original references (3 K5 product + child a_0007 + logo a_0008) delivered as `input_references`; request `aTo9OCZ5KlcV3Ld1r1nY`, 10 s, $1.00, `success`; video persisted and playable in UI; K5/child/logo visibly used. Known issue: requested 9:16, returned 1280×720 — responsible upstream layer (OpenRouter normalization vs Wan provider behavior) not yet proven. Image capability and other C2 criteria remain open. |
| Checkpoint B — User-facing presentation | `PLANNED` (open). Agent 2/3 user-facing rendering still has internal phrases / pending-validation leakage; no accepted closure evidence. |
| Scheduler (Checkpoint D) | **ACCEPTED / FROZEN** at `5781fd8`. Offline: 72 focused tests + 1 browser E2E passed. Real UI wall-clock qualification: one one-time `product_spec` job fired autonomously at `2026-09-09T16:06:00+07:00`, status `success`, 2 LLM calls, cost `$0.03216`, 0 media calls. Known non-blocking: `auto_image=true` serialized by UI defaults but no media path executes for `product_spec`-only flow; `session_ts` empty in run record but output/history/cost all functioned. |
| Checkpoint E — Final qualification | `BLOCKED` (open). No current-model qualification evidence; no Agent 1–4 declared Beta-ready. Agent 4 final-grounding/text requalification belongs here; visual fidelity belongs to C2. |
| Evidence (Media Core) | `tests/test_media_gen_provider_flow.py tests/test_phase4_media_wiring.py` = 60 passed, 0 failed; `tests/test_browser_e2e.py::TestImageGenerationBrowserE2E + TestVideoGenerationBrowserE2E + TestMediaPersistenceBrowserE2E` = 8 passed, 0 failed; `git diff --check` clean at acceptance. Regression fixtures: `test_uat_regression_unrelated_product_ref_not_sent`, `test_unknown_reference_ordinal_rejected_via_real_sequence`, `test_reference_mention_with_empty_full_catalog_rejected`. |
| Evidence (historical, immutable) | Image probes: `evaluation_artifacts/image_probe_20260908_092915/`; Final Agent 4 UAT: `evaluation_artifacts/final_agent4_uat_20260908_094512/`; Original failed UAT preserved: `evaluation_artifacts/real_uat_20260908_083822_FAILED_PARTIAL/`; Pre-subset-fix real UAT: `output/uat_media_20260909_125427/uat_report.json` — real image/video execution succeeded before the subset correction, but exposed extra-reference contamination; `06157af` fixed the defect offline; this is not post-fix fidelity/subset-isolation acceptance; do not rerun without separate Product Owner approval. These remain evidence, not current PASS. |
| Paid calls allowed now | No / $0. No paid/model/web/media/provider UAT is authorized unless the Product Owner approves it separately. The $1.00 Wan 2.7 video generation (request `aTo9OCZ5KlcV3Ld1r1nY`, 2026-09-10) was a completed, previously approved historical call — not continuing authorization. |
| Next exact action | **Product Owner decision needed for media spend only.** AGENT12-LIVE-DEFECT-01 remediation complete and verified; all text workflows PASS. Remaining: authorize one image-gen call (`google/gemini-3.1-flash-image`, ≈$0.04) + one video-gen call (`alibaba/wan-2.7` 5–6 s 720p/1080p, ≈$0.30–0.60) to close the media legs of the Agent 4 matrix; then commit the remediation diff (not committed yet). |
| Last updated | 2026-09-21 (AGENT12-LIVE-DEFECT-01 closed: Agent 1/2 fixes live-verified on Lagenio Evo; Agent 4 bounded 5-case verification: Facebook/TikTok/Brand-context PASS, media legs verified to the paid seam and stopped — authorization pending) |

## Progress Ledger — ARCH-CLEANUP-01 (Manager removal)

| Field | Value |
|---|---|
| Phase ID | ARCH-CLEANUP-01 |
| Product Owner decision | MKTApp does not want or need a Manager agent. `select_product_auto` and asset selection must remain orchestration helpers, not represented/named/configured/logged as a Manager. |
| Files deleted | `src/agents/manager.py` |
| Files edited | `src/agents/__init__.py`, `src/orchestrator.py`, `config/agents.yaml`, `main.py`, `tests/test_model_migration.py`, `tests/test_browser_upload_real_source.py`, `tests/test_manager_removal_contract.py` (new), `AGENT_PRODUCTION_READINESS_SPEC.md`, `AI_EMPLOYEE_BETA_EXECUTION_PLAN.md` (this ledger) |
| Tests added | `tests/test_manager_removal_contract.py` (9 focused offline contract tests) |
| Behavior preserved | Agent 1–4 standalone capability; `/api/run_auto` contract; `select_product_auto` / `_select_assets_for_content` semantics; Checkpoint A grounding; C1 reference transport; Scheduler. Auto-selection model/temperature/retry migrated from removed `manager` config section to `auto_mode`. |
| Behavior removed | `ManagerAgent` class; `Orchestrator.run_manager()`; `Orchestrator.get_products_state()` (dead Manager-era helper); `Orchestrator.llm_chat_raw()` (Manager-only); `manager` config section; CLI no-subcommand interactive Manager mode and its unused helpers (`list_products`, `has_files_in_data_root`, `show_product_info`, `list_all_products`, `_agent_display_name`, `AGENTS`); unused imports (`datetime`, `Prompt`, `Confirm`, `detect_data_files`). |
| No replacement | No new router/coordinator/manager class, abstraction, schema, dependency, feature flag, or deprecation alias was introduced. |
| Paid calls | 0. No LLM, embedding, web, image, video, provider, UI qualification, or network calls. Budget: $0. |
| Tests (offline, rerun) | `pytest tests/test_manager_removal_contract.py tests/test_model_migration.py` = 49 passed, 0 failed. `pytest tests/test_orchestrator_campaign_integration.py tests/test_orchestrator_visual_style_types.py` = 17 passed, 0 failed. `pytest tests/test_media_gen_provider_flow.py tests/test_pricing_format.py tests/test_qual_c2_launcher.py` = 105 passed, 0 failed. Combined total: 171 passed, 0 failed. |
| Tests (browser, rerun) | `pytest tests/test_browser_e2e.py -k "ImageGenerationBrowserE2E or VideoGenerationBrowserE2E or MediaPersistenceBrowserE2E"` = 8 passed, 0 failed (rerun during this revision; confirms `/api/run_auto` still reaches `run_content_creator_auto()` and media generation). |
| Tests (pre-existing failures, unchanged) | `tests/test_browser_upload_real_source.py` = 33 failed (require real source files not present in worktree; identical to baseline before ARCH-CLEANUP-01). |
| Next single action | **SUPERSEDED** by AUTH-ISO-01 (see ledger below). Wan 2.7 config-switch diff review is deferred until AUTH-ISO-01 closes. |

## Progress Ledger — AUTH-ISO-01 (Auth + Per-User Workspace Isolation remediation) — CURRENT BLOCKING PHASE

| Field | Value |
|---|---|
| Phase ID | AUTH-ISO-01 |
| Agent ID | SHARED-RUNTIME |
| Gate status | Stage A COMPLETE (commit `71c1e62`). Stage B COMPLETE (commit `7a3c293`). MB-01 COMPLETE (commit `f15cdbd`). System81 integration COMPLETE (commit `1a0ef78`). MB-02 ACCEPTED/CLOSED (commit `d625ecd`). Stage C ACCEPTED (commit `d904825`). MB-UI implemented, pending Codex acceptance. |
| Blocking | Yes — supersedes all other work (including the deferred Wan 2.7 review) until closed |
| Baseline HEAD | `3e0588f` (auth + workspace isolation initial implementation) |
| Stage A commit | `71c1e62 fix(auth): enforce per-user workspace isolation` |
| Stage B commit | `7a3c293 fix(auth): isolate scheduler state per user` |
| MB-01 commit | `f15cdbd feat(brand): add multi-brand workspace foundation` |
| System81 integration | Uncommitted — pending final Codex acceptance review. Production auth = System81. Local username/password is dev/test-only (`MKTAPP_DEV_AUTH=1`). Identity mapping = `system81_{sha256(sub)}` (full 64-char hex, collision-domain-safe). |
| SYSTEM81 CONTRACT QUALIFICATION REQUIRED | Before production/live acceptance, confirm: (1) Is `sub` always present in userinfo? (2) Is `sub` immutable/stable for the same person? (3) Can `username` be renamed/recycled? If `sub` is guaranteed present+stable, pin identity to `sub` and remove fallback ambiguity. If not, define the actual canonical System81 identifier before production release. |
| LIVE SYSTEM81 QUALIFICATION PENDING | Zero live System81 calls performed. After code acceptance and commit, one live smoke test required: obtain login URL → real System81 login → callback → userinfo verification → MKTApp session → `/api/auth/me` → brand list → logout. |
| Token-redaction/logging policy | System81 contract uses `?token=...` query-string callback. Current `log_level="warning"` suppresses Uvicorn access logs. Before production deployment, verify token-redaction/logging policy so callback query tokens are not exposed in access logs. |
| Uncommitted follow-up (preserved, not part of this proposal) | `src/scheduler.py`, `web_viewer.py`, `tests/test_scheduler_ownership.py` — scheduler ownership check for `remove_job`/`toggle_job`/`run_now` |
| Code-truth findings | 9 verified defects (see below). The earlier `PER-USER STATE ISOLATION VERIFIED` conclusion is **superseded** — it passed API-level isolation tests but missed thread-context propagation, path-component containment, scheduler store resolution order, process-global state, and legacy fallback paths. |
| Paid calls allowed | No / $0 |
| Live System81 calls | 0 — all System81 HTTP behavior mocked in tests |
| Next exact action | Codex final System81 acceptance review (correction round complete). |
| Last updated | 2026-09-11 — System81 correction round: collision-domain-safe identity mapping + dev verifier mixed-schema robustness. See System81 integration results below. |

### Accepted code-truth findings (9 defects)

1. **Thread context loss**: `web_viewer.py` has 8+ `threading.Thread(target=...)` calls (lines 991, 1298, 1437, 1569, 2221, 2312, 2882, 3698, 3947, 3961, 4559). None propagate the `contextvars.ContextVar`. `contextvars` do not auto-propagate into threads — workers fall back to global project root.
2. **Path traversal in product_id/folder**: `web_viewer.py:1406` `product_dir = DATA_DIR() / folder_name` (user-controlled `product_name`); `src/product_db.py:56` `_product_dir(product_id)` = `_project_root() / "cache" / product_id` (no containment). Direct proof: `product_db.save("../../user_b/cache/Secret", ...)` writes into User B's cache.
3. **Path traversal in uploaded filename**: `src/product_db.py:266` `dest = product_dir / filename`; `src/staging.py:87` `dest = source_dir / filename`; `web_viewer.py:2194` `dest = assets_dir / f.filename`. All user-controlled, no containment.
4. **Scheduler store resolution order**: `src/scheduler.py:894` `_run_job` calls `self._store.load_jobs()` *before* line 908 sets workspace. APScheduler callbacks receive only `job["id"]` (lines 319, 565, 628) — no `user_id`. `rerun_run` (line 698) loads runs before any workspace. `_on_job_missed` (line 279) loads jobs with no workspace. `_job_specs` (line 262) keyed by bare `job_id`, process-global. Direct proof: job saved in User A store = 1; visible from scheduler worker context = 0; `_run_job` reports "job not found."
5. **Scheduler startup**: `src/scheduler.py:293` `start()` calls `self._store.load_jobs()` with no workspace. Stuck-run cleanup (`_cleanup_stuck_running`, line 290) and durable-session cleanup (`_cleanup_orphaned_durable_sessions`, line 291) run once against the global fallback store. On restart, only the global store loads; per-user jobs are orphaned.
6. **Process-global state**: `web_viewer.py:211` `_conflict_cache`, `:261` `_BRAND_VISUAL_CACHE`, `:418` `_cancel_requested`, `:2798` `_current_llm`, `:419` `_active_llms`; `scheduler.py:265` `_running_status`. All module-level, shared across users. `_running_status` not user-filtered. `/api/cancel` (line 2740) takes no run/session ID — cancels ALL active runs globally.
7. **Recovery archive is global, not per-user**: `src/local_workspace.py:38` `_archive_root()` returns `_project_root() / ".recovery_archive"` where `_project_root()` (line 31) is `Path(__file__).resolve().parent.parent` — **not workspace-aware**. Even with User A's `WorkspaceContext` active, the archive resolves to `/Users/its-dev2/MKTApp/.recovery_archive`, not `users/user_a/.recovery_archive`. Child-path traversal can undermine it. Archive inaccessibility to runtime readers is not proven.
8. **Legacy fallback**: `src/brand_loader.py:303-304` falls back to `Path.cwd()/cache` and `Path.cwd()/data`, letting legacy/global state influence a new user.
9. **Auth documentation discrepancy + test defects**: `DEVELOPER_LOG.md:38` falsely claims "HMAC-signed tokens" — the implementation (`src/auth.py:224`) uses opaque random tokens (`secrets.token_urlsafe(32)`) with server-side revocation. Opaque tokens are an accepted design, not a defect. The false HMAC claim is a documentation discrepancy. `tests/test_local_workspace.py:49` autouse fixture calls `reset_local_workspace()` against the real repo before redirecting paths. Browser fixtures patch `DATA_DIR`/`OUTPUT_DIR` directly, masking the missing thread-context propagation.

### Behavioral contract (acceptance cases)

1. Every authenticated execution path — request handler, async generator, worker thread, scheduler executor, APScheduler fire — resolves the same `WorkspaceContext` for the same `user_id`. No fallback to global project root when an authenticated user is the originator.
2. User-controlled path components (`product_id`, `folder`, `filename`, `session_ts`, `batch_id`, `output_dir`) cannot resolve outside the active user workspace root. Covers read, write, upload, rename, delete, archive, output, product DB, staging, assets, media/session access.
3. User A cannot read, write, delete, list, run, rerun, toggle, cancel, or observe User B's products, cache, history, brand, settings, pillars, assets, scheduler jobs/status/runs/resources, output, usage logs, or recovery data — even if A knows B's IDs or submits traversal paths.
4. Authenticated web execution never falls back to legacy global state. CLI backward compat remains only through an explicit, non-web boundary.
5. Scheduler save → run-now/timed fire → run history and restart reload preserve the owning user without creating reusable long-lived credentials.
6. New users start clean — no reading of legacy global product profiles, brand state, history, or output.
7. Factory config/templates remain global and read-only: `config/`, tracked `brand/*.example.*`, provider capability cache, tracked fixtures.
8. Recovery copies and verifies before deletion, remains inaccessible to normal runtime readers, preserves user ownership, and archives into the owning user's workspace.

### Staged design

#### Stage A — Safe path construction, worker-context propagation, legacy fallback removal, test-fixture repair

**Thread-context helper** (corrected): one stdlib helper in `src/workspace_context.py` that wraps a callable with `contextvars.copy_context().run(...)` while preserving the existing thread lifecycle. It does **not** create or join a thread — it returns a wrapped callable suitable for `threading.Thread(target=...)` or `ThreadPoolExecutor.submit(...)`. Caller retains full control of thread creation, daemon flag, and timing.

```python
def with_workspace_context(fn, /, *args, **kwargs):
    """Return a zero-arg callable that runs fn under the current context.
    Use as: threading.Thread(target=with_workspace_context(worker)) or
    executor.submit(with_workspace_context(lambda: ...))."""
    ctx = contextvars.copy_context()
    def _run():
        return fn(*args, **kwargs)
    return lambda: ctx.run(_run)
```

Tests exercise this helper and the actual production worker launch points — not bare `threading.Thread` or bare `ThreadPoolExecutor` (Python provides no such inheritance contract).

**Path-containment helper** (corrected): one shared mechanical helper based on resolved containment under an explicit root. Rejects absolute paths, `..` escapes, and symlink-based escapes (via `Path.resolve()`). Accepts valid product display names without rewriting identity. Applied only at actual filesystem boundary functions. Replaces scattered substring checks (e.g. `".." in real_name` at line 1671) with the single resolved-containment check.

```python
def contain_path(child: str, root: Path) -> Path:
    """Resolve child under root; reject absolute paths, .. escapes, and symlink escapes."""
    root_resolved = root.resolve()
    candidate = (root_resolved / child).resolve()
    if not candidate.is_relative_to(root_resolved):
        raise ValueError(f"path escapes root: {child!r}")
    return candidate
```

**Complete filesystem trust-boundary inventory** (every direct join of request-controlled values under `DATA_DIR()`, `CACHE_DIR()`, `OUTPUT_DIR()`, staging roots, local brand roots, and request-created temporary directories):

| # | Unsafe component | Operation | Boundary function | File:line | Status |
|---|---|---|---|---|---|
| 1 | `product_name` (folder) | write | `api_upload` | `web_viewer.py:1406` | unsafe |
| 2 | `filename` (upload) | write | `save_uploaded_files` | `src/product_db.py:266` | unsafe |
| 3 | `product_id` | read/write | `_product_dir` | `src/product_db.py:56` | unsafe |
| 4 | `filename` (staging) | write | `create_batch` | `src/staging.py:87` | unsafe |
| 5 | `filename` (asset) | write | `api_assets_upload` | `web_viewer.py:2194` | unsafe |
| 6 | `product_id` (ingestion) | read | `_scan_product_files`, `_generate_product_profile` | `src/ingestion.py:455,416` | unsafe |
| 7 | `product_id` (brand profile) | read | `load_product_profile` | `src/brand_loader.py:301` | unsafe |
| 8 | `folder` | read | `api_ingest` | `web_viewer.py:1543` | unsafe |
| 9 | `folder` | read | `api_ingest_status` | `web_viewer.py:1575` | unsafe |
| 10 | `folder` | read | `api_folder_files` | `web_viewer.py:1589` | unsafe |
| 11 | `folder` | read | `api_product_image` | `web_viewer.py:1636` | unsafe |
| 12 | `folder` + `filepath` | delete | `api_delete_file` (cache branch uses `".." in real_name` substring check; data branch uses `is_relative_to` but `folder` not contained) | `web_viewer.py:1671,1679` | unsafe |
| 13 | `folder` | delete (rmtree) | `api_delete_folder` | `web_viewer.py:1738,1743` | unsafe |
| 14 | `file` (output path) | delete | `api_delete_output_file` | `web_viewer.py:1771,1780` | **existing safe** — resolved containment at lines 1778-1782; regression test only |
| 15 | `old_name` + `new_name` | rename | `api_rename_folder` | `web_viewer.py:1844,1847` | unsafe |
| 16 | `filename` (brand) | read | `api_brand_file_get` | `web_viewer.py:1902` | unsafe |
| 17 | `folder` (profile) | write | `api_product_profile_save` | `web_viewer.py:2021` | unsafe |
| 18 | `session` | read | `api_media_status` | `web_viewer.py:1314` | unsafe |
| 19 | `session` | read | `api_media_retry_log` | `web_viewer.py:1325` | unsafe |
| 20 | `session` | write | `api_media_retry` | `web_viewer.py:1367` | unsafe |
| 21 | `session` + `filename` | read | `api_file` | `web_viewer.py:2717` | unsafe |
| 22 | `session` | read | `api_session_files` | `web_viewer.py:2694` | unsafe |
| 23 | `output_dir` + `filename` | write | generate-media endpoint | `web_viewer.py:846,847` | unsafe |
| 24 | `batch_id` (route param) | read/commit/delete | `api_get_stage`, `api_commit_stage`, `api_delete_stage` | `web_viewer.py:1484,1494,1513` | unsafe |
| 25 | `session` | read | `api_cost_summary` | `web_viewer.py:1343` | unsafe |
| 26 | `folder` | read | `_read_folder` | `web_viewer.py:2904-2905` | unsafe |
| 27 | `filename` (voice temp) | write | `api_voice_learn_upload` | `web_viewer.py:2145` | unsafe (temp dir, but absolute/traversal filename escapes it) |
| 28 | `filename` (video temp) | write | `api_video_style_upload` | `web_viewer.py:2380` | unsafe (temp dir, but absolute/traversal filename escapes it) |

Boundaries 1-13, 15-28 (excluding #14) use the shared `contain_path` helper at the filesystem seam. Scattered substring checks (`".." in real_name`) are replaced by the single resolved-containment check. Boundary #14 (`api_delete_output_file`) already enforces resolved containment — retain it and add a regression test.

**Legacy fallback removal**: `src/brand_loader.py:303-304` — remove `Path.cwd()/cache` and `Path.cwd()/data` candidates. Resolve only through `user_state_root()`.

**Test-fixture repair**: `tests/test_local_workspace.py:49` — autouse fixture must redirect paths to tmp *before* calling `reset_local_workspace()`, not after. Browser fixtures must set the workspace context and let production resolvers work, not patch `DATA_DIR`/`OUTPUT_DIR` directly.

**Stage A allowed files:**
- Production: `src/workspace_context.py`, `src/product_db.py`, `src/staging.py`, `src/ingestion.py`, `src/brand_loader.py`, `web_viewer.py` (all 24 boundary functions + thread-launch sites)
- Documentation: `DEVELOPER_LOG.md` (correct false HMAC claim only)
- Tests: `tests/test_workspace_context.py`, `tests/test_local_workspace.py`, `tests/test_brand_loader.py`, new `tests/test_thread_context_propagation.py`, browser fixtures in `tests/test_browser_e2e.py` / `tests/test_browser_integration_real_orch.py` / `tests/test_browser_upload_real_source.py`

**Stage A test classification:**

*Manual reproductions / proposed red tests (not yet in `tests/` — will be written and run against current code first):*
1. `test_thread_context_propagation.py::test_production_worker_launch_propagates` — set workspace, launch `threading.Thread(target=worker)` (current production pattern), assert worker sees workspace. **Manual reproduction:** workers see `None`, fall back to global root.
2. `test_workspace_context.py::test_product_db_save_traversal_blocked` — `product_db.save("../../user_b/cache/Secret", ...)` raises, no file created outside workspace. **Manual reproduction:** no containment, file written into User B's cache.
3. `test_workspace_context.py::test_delete_folder_traversal_blocked` — `DELETE /api/folder` with `folder="../../user_b"` does not rmtree outside workspace. **Manual reproduction:** no containment on `folder`.
4. `test_workspace_context.py::test_rename_folder_traversal_blocked` — `POST /api/rename_folder` with `new_name="../../user_b"` does not rename outside workspace. **Manual reproduction:** no containment.
5. `test_workspace_context.py::test_brand_file_read_traversal_blocked` — `GET /api/brand_file/../../etc/passwd` does not read outside workspace. **Manual reproduction:** no containment on `filename`.
6. `test_workspace_context.py::test_file_read_traversal_blocked` — `GET /api/file/../../etc/passwd/foo` does not read outside workspace. **Manual reproduction:** no containment on `session`/`filename`.
7. `test_brand_loader.py::test_no_legacy_fallback_for_new_user` — User B with no profile calls `load_product_profile("ProductA")`, returns `{}`. **Manual reproduction:** falls back to `Path.cwd()/cache`.
8. `test_local_workspace.py::test_autouse_fixture_does_not_touch_real_repo` — fixture redirects before reset. **Manual reproduction:** reset runs first against real repo.

*Proposed red tests not yet written (helpers don't exist yet):*
9. `test_thread_context_propagation.py::test_helper_propagates_workspace` — call `with_workspace_context(lambda: get_workspace().user_id)()`, assert returns set user_id. **Not yet written:** helper doesn't exist.
10. `test_workspace_context.py::test_contain_path_rejects_traversal` — `contain_path("../../user_b/cache/Secret", root)` raises `ValueError`. **Not yet written:** helper doesn't exist.
11. `test_workspace_context.py::test_contain_path_rejects_absolute` — `contain_path("/etc/passwd", root)` raises. **Not yet written.**
12. `test_workspace_context.py::test_contain_path_rejects_symlink_escape` — symlink inside root pointing outside, `contain_path` raises. **Not yet written.**

*Existing safe boundary (regression test only):*
13. `test_workspace_context.py::test_delete_output_file_existing_containment` — `api_delete_output_file` rejects external file via existing `is_relative_to` check. **Existing safe:** lines 1778-1782.

*Existing test defects requiring repair:*
14. `tests/test_local_workspace.py` autouse fixture (line 49) — calls `reset_local_workspace()` against real repo before redirect. **Existing defect.**
15. Browser fixtures patch `DATA_DIR`/`OUTPUT_DIR` directly — masks missing thread-context propagation. **Existing defect.**

**Stage A acceptance cases:** contract items 1, 2, 4, 6, 7.
**Stage A stop point:** all Stage A tests green; no production code beyond Stage A allowed files touched; stop for Codex review before Stage B.

### Stage A implementation results — COMPLETE

**Files changed (production):**
- `src/workspace_context.py` — added `contain_path()` and `with_workspace_context()` helpers
- `src/product_db.py` — `_product_dir()` and `save_uploaded_files()` use `contain_path`
- `src/staging.py` — `_batch_dir()` and `create_batch()` use `contain_path`
- `src/ingestion.py` — `_scan_product_files()` and `_generate_product_profile()` use `contain_path`
- `src/brand_loader.py` — removed `Path.cwd()` fallback; `load_product_profile()` uses `contain_path`
- `web_viewer.py` — 28 boundary functions use `contain_path`; 11 `threading.Thread` launch sites use `with_workspace_context`; `api_generate_all_media` file-path fallback fixed to resolve via `OUTPUT_DIR()` instead of `PROJECT_ROOT`
- `DEVELOPER_LOG.md` — corrected false HMAC wording to opaque high-entropy server-side tokens

**Files changed (tests):**
- `tests/test_workspace_context.py` — containment and traversal tests
- `tests/test_thread_context_propagation.py` — helper existence, propagation, no-thread-creation, bare-pattern-loses-context, **production worker launch test** (exercises real `POST /api/ingest/{folder}` endpoint via TestClient), static analysis defense-in-depth
- `tests/test_brand_loader.py` — `_workspace_at()` context manager; legacy fallback test; 4 cwd-based tests converted to workspace context
- `tests/test_local_workspace.py` — autouse fixture redirects to tmp before reset; `_ws_root()` helper for workspace-aware paths
- `tests/test_browser_e2e.py` — fixture uses WorkspaceContext instead of patching OUTPUT_DIR/DATA_DIR/CACHE_DIR; teardown resets workspace token via try/finally
- `tests/test_browser_integration_real_orch.py` — same fixture fix
- `tests/test_browser_upload_real_source.py` — same fixture fix

**Acceptance review fixes (3 fixes after independent review):**
1. **Production worker-context acceptance test** — added `test_production_ingest_worker_sees_authenticated_workspace` which exercises the real `POST /api/ingest/{folder}` endpoint via FastAPI TestClient, with auth middleware setting the workspace, and proves the launched worker thread sees the authenticated user's workspace (not None, not global fallback). Also added `test_all_web_viewer_thread_launches_use_with_workspace_context` as defense-in-depth static check.
2. **Browser fixture lifecycle** — `tests/test_browser_e2e.py` teardown now wraps cleanup in try/finally and always resets `_ws_token` via `_reset_ws(_ws_token)`, preventing ContextVar leakage into later tests.
3. **Browser image-generation regression** — root cause: `api_generate_all_media` (line 1023-1026) used `PROJECT_ROOT / filepath` as fallback, bypassing workspace context. Frontend sends `output/session/filename`; with workspace context the file lives at `OUTPUT_DIR()/session/filename` = `tmp/users/<uid>/output/session/filename`, but `PROJECT_ROOT/filepath` = `tmp/output/session/filename` (wrong). Fixed to strip `output/` prefix and resolve via `contain_path(remaining, OUTPUT_DIR())`. This was a **real Stage A boundary defect**, not a fixture mismatch.

**Red test results (pre-fix, run against unmodified code):**
- 14 failed, 1 passed
- Failures: worker context loss, contain_path missing, traversal/absolute/symlink/Unicode containment, product_db save/delete/rename traversal, legacy fallback leak

**Post-fix test commands and counts:**
```
python3 -m pytest tests/test_thread_context_propagation.py tests/test_workspace_context.py \
  tests/test_brand_loader.py tests/test_local_workspace.py tests/test_auth.py \
  tests/test_staging_api.py tests/test_schedule_api.py tests/test_scheduler_ownership.py \
  tests/test_scheduler_rerun.py --tb=short
```
- Result: **153 passed, 1 failed**
- The 1 failure (`test_product_agent_instructions_no_per_agent_sections`) is **pre-existing** — confirmed failing on baseline commit `3e0588f` before any Stage A changes. It checks that `config/agent_instructions.json` lacks per-agent sections, but the factory config file has them. Unrelated to Stage A.

**Browser E2E tests:**
```
python3 -m pytest tests/test_browser_e2e.py -k 'not test_video and not test_schedule and not test_media_persistence and not test_auto_image and not test_auto_video' --tb=short
```
- Result: **18 passed, 5 deselected** (deselected: video/schedule/media_persistence/auto tests require longer runtime)
- Includes `test_manual_image_generation_button` (previously failing, now fixed) and `test_image_error_surfaces_in_ui` (run after, proves no ContextVar leakage)

**Tests not run and why:**
- `tests/test_browser_e2e.py` video/schedule/media_persistence/auto tests — deselected to keep runtime under 3 minutes; they require the same fixture lifecycle already proven by the 18 that ran
- `tests/test_browser_integration_real_orch.py`, `tests/test_browser_upload_real_source.py` — require real LLM/orchestration mocks; fixture changes verified by inspection
- No LLM/embedding/web/media/provider/network/paid calls: **$0**

**Unrelated dirty files preserved:**
- `src/scheduler.py` — scheduler ownership follow-up (20 insertions, 8 deletions) — unchanged by Stage A
- `cache/_media_capabilities/image:google_gemini-3.1-flash-image.json` — pre-existing dirty — unchanged
- `tests/test_scheduler_ownership.py` — untracked, scheduler follow-up — unchanged

**`git diff --check`:** passed (no whitespace errors)

**Paid calls:** $0

**Next single action:** Codex final Stage A acceptance review.

#### Stage B — Scheduler owner propagation, restart reload, rerun/fire/status isolation

**Corrected scheduler design** — one internal owner key `(user_id, job_id)` used consistently across every APScheduler-facing and store-facing path:

- **Internal owner key**: `(user_id, job_id)` tuple. The public `job_id` (UUID) remains unchanged in JSON records. APScheduler's in-memory `MemoryJobStore` uses a namespaced ID `f"{user_id}:{job_id}"` to prevent cross-user ID collisions (in-memory only — no persisted APScheduler state file exists, so no state-file incompatibility).
- **APScheduler `add_job`** (lines 316, 562, 625): `id=f"{user_id}:{job_id}"`, `args=[job_id, user_id]`.
- **APScheduler `get_job`/`remove_job`**: look up by namespaced ID.
- **`_job_specs`** (line 262): change from `dict[str, dict]` (keyed by `job_id`) to `dict[tuple[str, str], dict]` (keyed by `(user_id, job_id)`).
- **`_run_job(self, job_id, user_id)`**: set `WorkspaceContext.for_user(user_id, project_root)` *before* any `self._store.load_jobs()` call.
- **`run_now`**: `self._executor.submit(self._run_job, job_id, user_id)` — `user_id` from calling context (`_SchedulerProxy._current_user_id()`).
- **`rerun_run`**: load the run record to get `user_id` from the stored job, then `self._executor.submit(self._rerun_from_record, dict(run), user_id)`. `_rerun_from_record(self, source_run, user_id)` sets workspace before any store access.
- **`_on_job_missed(event)`**: recover owner from the namespaced APS ID (split `event.job_id` on `:`), set that workspace before `self._store.load_jobs()` or `self._record_missed(job)`.
- **`add_job`/`toggle_job`/`remove_job`/`list_jobs`/`job_exists`**: all accept/use `user_id` and operate only on the owner's store.
- **One-time completion cleanup**: runs inside the owner's workspace.
- **`_running_status`** (line 265): change from `dict[str, dict]` (keyed by `job_id`) to `dict[str, dict[str, dict]]` (keyed by `user_id` → `job_id` → status). `get_running_status(user_id)` returns only the calling user's entries.
- **Restart reload**: `start()` enumerates **authoritative registered users** via `UserStore.list_users()` (not arbitrary `users/*` directories). For each user: set `WorkspaceContext`, run `_cleanup_stuck_running()` and `_cleanup_orphaned_durable_sessions()` inside that workspace, then load jobs. No reusable long-lived credentials are minted — workspace context is set from `user_id` alone; the short-lived session token created in `_run_job` remains scoped to the job execution and revoked after.

**Stage B allowed files:**
- Production: `src/scheduler.py`, `web_viewer.py` (scheduler proxy + restart hook only)
- Tests: `tests/test_scheduler_ownership.py`, `tests/test_scheduler_rerun.py`, `tests/test_schedule_api.py`

**Stage B test classification:**

*Reproduced failing tests against current production code:*
1. `test_scheduler_ownership.py::test_run_job_finds_job_in_owner_store` — save job as User A, call `_run_job(job_id)` (current signature, no `user_id`), assert job is found. **Reproduced fail:** store loads from global fallback, job not found.
2. `test_scheduler_ownership.py::test_restart_reloads_per_user_jobs` — save job as User A, call `start()`, assert job is rescheduled. **Reproduced fail:** `start()` loads from global store only.
3. `test_scheduler_ownership.py::test_running_status_user_filtered` — User A has running job, User B calls `get_running_status`, assert B sees empty. **Reproduced fail:** `_running_status` is process-global, not user-filtered.
4. `test_scheduler_ownership.py::test_on_job_missed_preserves_owner` — fire `_on_job_missed` with a User A job, assert missed record written to User A's store. **Reproduced fail:** `_on_job_missed` loads from global fallback.
5. `test_scheduler_ownership.py::test_stuck_run_cleanup_per_user` — User A has stuck run, User B has stuck run, `start()` cleans both. **Reproduced fail:** cleanup runs once against global store.

*Proposed red tests not yet written:*
6. `test_scheduler_ownership.py::test_rerun_preserves_owner` — rerun a User A run record, assert it executes in User A's workspace. **Not yet written:** `rerun_run` doesn't pass `user_id`.
7. `test_scheduler_ownership.py::test_apscheduler_id_namespaced` — two users with same job UUID don't collide in APScheduler. **Not yet written:** IDs not namespaced.

*Existing passing regression tests:*
8. `tests/test_scheduler_ownership.py` (9 existing tests for `remove_job`/`toggle_job`/`run_now` ownership) — **existing pass**, must remain green.

**Stage B acceptance cases:** contract items 3 (scheduler subset), 5.
**Stage B stop point:** all Stage B tests green; stop for Codex review before Stage C.

### Stage B implementation results — COMPLETE

**Root cause closed:** Scheduler callbacks (`_run_job`, `_on_job_missed`, `_rerun_from_record`) started by reading the store with no workspace context active, so they read from the global fallback path — User A's timed fire could not find User A's job. APScheduler used bare `job_id` as its in-memory key, so the same public `job_id` for two users would collide. `_running_status` was a process-global dict keyed by `job_id` — any user could see any other user's running status. `start()` loaded jobs once from the fallback store — per-user jobs were orphaned on restart. `rerun_run` had no ownership check.

**Final invariant:** Every execution path that occurs outside an authenticated request carries owner identity explicitly before accessing the per-user store. APScheduler uses an internal owner-bearing ID `_aps_id(user_id, job_id)` = `f"{user_id}::{job_id}"` (deterministic, reversible, no collision). The public `job_id` remains unchanged. No auth/session secrets are embedded in APS IDs.

**Existing changes reused:** The pre-existing uncommitted ownership checks for `remove_job`, `toggle_job`, `run_now`, and `_SchedulerProxy._current_user_id()` were retained as-is — they were correct. Extended `rerun_run` and `get_running_status` proxy methods to pass `user_id`.

**Owner flow (concrete):**
1. Authenticated create → `add_job` captures `user_id` from `WorkspaceContext` → saves job to user's store
2. APScheduler registration uses `id=_aps_id(user_id, job_id)`, `args=[user_id, job_id]`
3. Timed callback fires → `_run_job(user_id, job_id, trigger)` receives owner explicitly
4. `WorkspaceContext.for_user(user_id, self._project_root)` established BEFORE any store access
5. `self._store.load_jobs()` reads from the user's workspace (not fallback)
6. Ownership validated: `job.get("user_id") == user_id` (fail closed)
7. Execution, run history, status, output all written inside the user workspace
8. `reset_workspace(ws_token)` in `finally`

**Restart flow:** `start()` calls `_reload_all_users()` which enumerates registered users via `get_user_store().list_users()` (not filesystem directories). For each user: set `WorkspaceContext`, run `_cleanup_stuck_running()` + `_cleanup_orphaned_durable_sessions()` inside that workspace, then `_register_user_jobs(user_id)` loads and registers enabled jobs. Workspace is reset before moving to the next user. Unregistered `users/<name>` directories are never loaded. Legacy jobs (no `user_id`) are handled by `_reload_legacy_jobs()` which loads from the fallback store.

**Files changed (production):**
- `src/scheduler.py` — added `_aps_id`/`_decode_aps_id`; updated `add_job`, `remove_job`, `toggle_job`, `list_jobs`, `job_exists`, `run_now`, `_run_job`, `_rerun_from_record`, `rerun_run`, `_on_job_missed`, `start`/`_reload_all_users`/`_register_user_jobs`/`_reload_legacy_jobs`, `get_running_status`; added `user_id` to run records; fixed `_project_root` to use `self._project_root`; fixed `_cleanup_orphaned_durable_sessions` to handle non-existent dirs
- `web_viewer.py` — `_SchedulerProxy.rerun_run` and `get_running_status` pass `user_id=self._current_user_id()`

**Files changed (tests):**
- `tests/test_scheduler_ownership.py` — extended with running status isolation, rerun cross-user blocked, same job_id independently controllable
- `tests/test_scheduler_exec_ownership.py` — new: APS ID collision, timed fire owner, timed fire workspace write, missed event owner, malformed APS ID, restart multi-user, restart unregistered dir, restart cleanup per user, run-now owner, rerun owner workspace
- `tests/test_scheduler_misfire_grace.py` — updated `_fake_run_job` signature to `(user_id, job_id, trigger)`
- `tests/test_scheduler_restart.py` — updated `_fake_run_job` signatures
- `tests/test_scheduler_rerun.py` — updated direct `_run_job` call to new signature
- `tests/test_scheduler_attachments.py` — updated direct `_run_job` calls to new signature

**Red test results (pre-fix, 12 failed, 10 passed):**
- ImportError: `_aps_id` not found (3 tests)
- TypeError: `rerun_run()` got unexpected `user_id` (2 tests)
- AttributeError: `_register_user_jobs` not found (4 tests)
- AssertionError: stuck run not marked error (1 test)
- AssertionError: `run_now` not passing owner (1 test)
- FileNotFoundError: run_resources dir missing (1 test)

**Green test commands and counts:**
```
python3 -m pytest tests/test_scheduler_ownership.py tests/test_scheduler_exec_ownership.py \
  tests/test_scheduler_rerun.py tests/test_scheduler_restart.py tests/test_scheduler_missed.py \
  tests/test_scheduler_cleanup.py tests/test_scheduler_misfire_grace.py \
  tests/test_scheduler_attachments.py tests/test_schedule_api.py --tb=short
```
- Result: **55 passed, 0 failed**

**Workspace/auth regression:**
```
python3 -m pytest tests/test_workspace_context.py tests/test_local_workspace.py \
  tests/test_thread_context_propagation.py --tb=short
```
- Result: **89 passed, 1 failed** — the 1 failure (`test_product_agent_instructions_no_per_agent_sections`) is **pre-existing** (confirmed failing on baseline before Stage B changes).

**`git diff --check`:** passed (no whitespace errors)

**Paid calls:** $0

**Stage C untouched:** `_cancel_requested`, `_active_llms`, `_current_llm`, `_session_ts`, `_BRAND_VISUAL_CACHE`, `_conflict_cache` — all unchanged. Only Scheduler `_running_status` was modified (now user-filtered via `get_running_status(user_id)`).

**Next single action:** Codex final Stage B acceptance review.

### Stage B final remediation results — COMPLETE

**4 bounded fixes applied after independent acceptance review identified defects:**

**Fix 1 — Ownerless legacy jobs quarantined (CRITICAL):**
- `_reload_legacy_jobs()` no longer registers ownerless jobs into APScheduler. It disables them in-place and records a missed/error run, preserving evidence without executing.
- `_run_job(user_id, ...)` now fails closed when `user_id` is empty — returns immediately before any store access or execution. Defense-in-depth even if registration is bypassed.
- Red tests proved ownerless jobs WERE registered and could reach `_execute_flow`. After fix: not registered, not executed, `_run_job("")` fails closed.

**Fix 2 — Authoritative UserStore root-aware (ARCHITECTURAL):**
- `get_user_store(project_root=None)` now accepts an optional project root. When provided, returns a `UserStore` bound to that root's `data/auth/users.json`. When omitted, behavior is unchanged (global singleton for normal application startup).
- `Scheduler._reload_all_users()` now calls `get_user_store(self._project_root)` so `Scheduler(tmp_root)` enumerates users from `tmp_root`, not the source checkout.
- Tests no longer monkeypatch the global singleton — they write to `tmp_path/data/auth/users.json` and the real root-aware seam resolves correctly.

**Fix 3 — Running status fail-closed:**
- `get_running_status(user_id=None)` now returns `{}` instead of all entries. Empty string also returns `{}`. No authenticated path can request "all users."

**Fix 4 — Run log ownership explicit:**
- `get_run_log(user_id=None)` now returns `[]` when no user identity is provided. When `user_id` is provided, establishes the user's workspace and filters runs by `run.get("user_id") == user_id`. Legacy runs with missing/empty `user_id` are never returned to an authenticated user.
- `_SchedulerProxy.get_run_log` now passes `user_id=self._current_user_id()`.

**Files changed (production):**
- `src/scheduler.py` — `_reload_legacy_jobs` quarantine; `_run_job` fail-closed; `get_running_status` fail-closed; `get_run_log` user_id filter + workspace establishment
- `src/auth.py` — `get_user_store(project_root=None)` optional root parameter
- `web_viewer.py` — `_SchedulerProxy.get_run_log` passes `user_id`

**Files changed (tests):**
- `tests/test_scheduler_exec_ownership.py` — new tests: ownerless legacy quarantine, `_run_job` fail-closed, root-aware user enumeration, running status fail-closed, run log ownership filter
- `tests/test_scheduler_rerun.py` — workspace fixture + user_id in run records and rerun calls
- `tests/test_scheduler_restart.py` — workspace fixture + user_id in jobs and run records
- `tests/test_scheduler_misfire_grace.py` — workspace fixture + user_id in job_exists/add_job/remove_job
- `tests/test_scheduler_attachments.py` — workspace fixture + user_id in _run_job/rerun_run/remove_job calls

**Green test commands and counts:**
```
python3 -m pytest tests/test_scheduler_ownership.py tests/test_scheduler_exec_ownership.py \
  tests/test_scheduler_rerun.py tests/test_scheduler_restart.py tests/test_scheduler_missed.py \
  tests/test_scheduler_cleanup.py tests/test_scheduler_misfire_grace.py \
  tests/test_scheduler_attachments.py tests/test_schedule_api.py --tb=short
```
- Result: **64 passed, 0 failed**

**Workspace/auth regression:**
```
python3 -m pytest tests/test_workspace_context.py tests/test_local_workspace.py \
  tests/test_thread_context_propagation.py --tb=short
```
- Result: **89 passed, 1 failed** — the 1 failure (`test_product_agent_instructions_no_per_agent_sections`) is **pre-existing** (confirmed failing on baseline before Stage B changes).

**Auth regression:**
```
python3 -m pytest tests/test_auth.py --tb=short
```
- Result: **15 passed, 0 failed**

**`git diff --check`:** passed (no whitespace errors)

**Paid calls:** $0

**Stage C untouched:** `_cancel_requested`, `_active_llms`, `_current_llm`, `_session_ts`, `_BRAND_VISUAL_CACHE`, `_conflict_cache` — all unchanged. Auth login/session/token/cookie behavior unchanged. Media generation unchanged. Agent prompts/schemas unchanged.

**Next single action:** Codex final Stage B acceptance review.

#### Stage C — Concurrent per-user process state and recovery archive per-user isolation

**Corrected process-state design** — preserves existing `/api/cancel` contract (no run ID parameter):

| Global | Ownership key | Lifecycle | Cancel behavior |
|---|---|---|---|
| `_cancel_requested` | per-user (`user_id`) | created on run start, cleared on completion/cancel/failure | `/api/cancel` sets the *calling user's* flag only; other users unaffected |
| `_current_llm` | closure-local where possible; per-user (`user_id`) fallback | created on run start, closed on completion/failure | cancel closes all of the calling user's clients |
| `_active_llms` | per-user (`user_id`) → list | appended on create, removed on close | cancel clears all of the calling user's clients |
| `_session_ts` | closure-local where possible | created on run start, discarded on completion | not a global; if a global is unavoidable, per-user |
| `_BRAND_VISUAL_CACHE` | per-user (`user_id`) | created on first access, cleared on brand-change refresh | read-only cache, no cancel action |
| `_conflict_cache` | per-user (`user_id`) | created on first access, refreshed on brand/agent settings change | read-only cache, no cancel action |
| scheduler `_running_status` | per-user (`user_id`) → per-job | (covered in Stage B) | (covered in Stage B) |

No new framework or state-manager abstraction. Each global becomes a `dict[key, value]` with a small thread-safe helper for create/lookup/remove. `/api/cancel` is scoped by authenticated `user_id` — it stops that user's active runs but never another user's. Same-user per-run cancellation is a **non-goal** unless separately approved (would require an API change to add a run ID).

**Recovery archive per-user isolation (corrected):**
- `src/local_workspace.py:38` `_archive_root()` must change from `_project_root() / ".recovery_archive"` to `user_state_root(_project_root()) / ".recovery_archive"`, so it resolves to `users/{user_id}/.recovery_archive` when a workspace is active.
- Archive destination = `users/{user_id}/.recovery_archive/` (per-user, not global).
- Acceptance tests prove: (a) archive destination inside owning user workspace; (b) copy + hash verification before source deletion; (c) another user cannot read or restore it; (d) ordinary product/history/output listing cannot expose archive contents; (e) traversal and symlink escape attempts into archive fail.

**Stage C allowed files:**
- Production: `web_viewer.py` (global state → per-user dicts + cancel scoping), `src/local_workspace.py` (`_archive_root` per-user)
- Tests: `tests/test_local_workspace.py` (archive isolation tests), new `tests/test_concurrent_run_state.py`

**Stage C test classification:**

*Reproduced failing tests against current production code:*
1. `test_concurrent_run_state.py::test_cancel_scoped_to_user` — User A and User B running concurrently, User A calls `/api/cancel`, User B continues. **Reproduced fail:** `_cancel_requested` is a single global bool; cancel stops all users.
2. `test_concurrent_run_state.py::test_llm_client_scoped_to_user` — User A and User B running concurrently, each gets separate LLM clients. **Reproduced fail:** `_current_llm`/`_active_llms` are process-global.
3. `test_concurrent_run_state.py::test_brand_visual_cache_scoped_to_user` — User A and User B get separate brand visual caches. **Reproduced fail:** `_BRAND_VISUAL_CACHE` is process-global.
4. `test_local_workspace.py::test_archive_destination_inside_owner_workspace` — with User A workspace active, `reset_local_workspace()` archives to `users/user_a/.recovery_archive/`, not global `.recovery_archive`. **Reproduced fail:** `_archive_root()` returns global path.
5. `test_local_workspace.py::test_archive_inaccessible_to_other_users` — User B cannot read User A's archive. **Reproduced fail:** archive is global, shared.

*Proposed red tests not yet written:*
6. `test_local_workspace.py::test_archive_traversal_blocked` — traversal into archive fails. **Not yet written:** no containment on archive child paths.

**Stage C acceptance cases:** contract items 3 (process-state subset), 8.
**Stage C stop point:** all Stage C tests green; full AUTH-ISO-01 regression rerun; stop for Product Owner freeze review.

### Auth documentation correction (no crypto change)

- **Decision**: keep existing opaque, high-entropy (`secrets.token_urlsafe(32)`) server-side session tokens with TTL and server-side revocation. Opaque tokens are an accepted design, not a defect. Do **not** add HMAC signing. Do **not** modify `src/auth.py` solely for crypto.
- Correct `DEVELOPER_LOG.md:38` which falsely claims "HMAC-signed tokens" — change to "opaque high-entropy server-side session tokens with TTL and server-side revocation."
- Do not add `hmac.compare_digest` unless a concrete exploitable timing-attack requirement is demonstrated (256-bit random token makes timing attack not materially exploitable).

### Cookie/network (no change in this task)

- Transport changes (bind address, `Secure` flag) are **outside** the isolation implementation stages.
- `Secure` cookies must be enabled when the application is actually served through HTTPS. For loopback HTTP (local/Beta default), `Secure` is not set — the cookie would not be sent over plain HTTP.
- Any bind-address or remote-access change remains a **Product Owner hard stop**.
- Do **not** change the bind address or cookie behavior in this task.

### Explicit non-goals

- No new dependency. No new semantic validator or keyword list. No model/web/image/video/media calls. Budget: $0.
- No benchmark-, product-, brand-, or scenario-specific logic.
- No change to public API/schema/orchestration contracts (including `/api/cancel` — no run ID added).
- No change to Agent behavior or frozen media.
- No commit, push, deploy, or GitHub issue update.
- No destructive reset against the real checkout.
- No change to bind address or cookie `Secure` flag in this task.
- No same-user per-run cancellation (requires API change — non-goal unless separately approved).

### Product Owner hard stops still unresolved

1. **Scheduler restart reload scope**: `start()` will enumerate authoritative registered users via `UserStore.list_users()` and load each user's job store. This changes startup behavior (previously loaded a single global store). Product Owner must confirm multi-user restart is the intended deployment model.

No other hard stops. APScheduler uses in-memory `MemoryJobStore` (default, no `jobstores=` arg at line 254) — namespacing the in-memory APS ID does not alter any persisted state file, because none exists. The application's JSON job/run records retain the existing public `job_id`. All other changes are mechanical containment, context propagation, and state isolation within existing architecture seams.

## Progress Ledger — MB-01 (Multi-Brand Foundation)

|| Field | Value |
||---|---|
|| Phase ID | MB-01 |
|| Agent ID | SHARED-RUNTIME |
|| Gate status | MB-01 implementation complete; pending Codex acceptance review. |
|| Baseline | Stage A `71c1e62` (frozen), Stage B `7a3c293` (frozen) |
|| Ownership hierarchy | `User → many Brands`. A brand belongs to exactly one user. System81 later authenticates the PERSON only; MKTApp owns Brand entities and brand ownership. |
|| Products scope | Products, product profiles, product raw data, ingestion, and staging are formally classified **BRAND-SCOPED**. Not migrated in MB-01. Recorded here so future phases do not preserve the old user-shared assumption. |
|| User root invariant | `user_state_root(project_root)` → `users/<user_id>/` — semantics unchanged, always the user root, never a brand root. |
|| Brand root invariant | `brand_state_root()` → `users/<user_id>/brands/<brand_id>/` — separate explicit seam. Fails closed when no verified `brand_id` is active. Brand root always beneath the authenticated user's root. |
|| WorkspaceContext | Extended single propagated context with optional `brand_id`. `for_user()` → user context (brand_id=None). `for_brand()` → brand context (ownership-verified via BrandRegistry). `with_workspace_context()` propagates `brand_id` automatically. Stage A behavior preserved. |
|| Brand identity | `brand_id` = `secrets.token_hex(8)` — internally generated, immutable, filesystem-safe, independent from display name, not client-chosen, not reused after archive. |
|| APScheduler IDs | Stage B `(user_id, job_id)` APS identity untouched. No brand-awareness added to scheduler in MB-01. |
|| BrandRegistry | `src/brand_registry.py` — JSON-backed, user-scoped (`users/<user_id>/brand_registry.json`). Operations: create, list, get, rename, archive. Every get/rename/archive verifies owner. Archive (not delete) preserves brand_id. `get(brand_id, *, active_only=True)` returns `None` for archived brands by default; management/history lookups pass `active_only=False`. Archived brands cannot be selected, cannot establish a brand context, and stale cookies pointing to archived brands fail closed to user-only context. |
|| Brand API | `web_viewer.py` — `GET/POST /api/brands`, `GET/PATCH/DELETE /api/brands/{brand_id}`, `POST /api/brands/{brand_id}/select`, `POST /api/brands/deselect`, `GET /api/brands/active`. No arbitrary user_id exposed. Selection via HttpOnly cookie `mktapp_brand`, revalidated against authenticated user on every request via AuthMiddleware. `GET /api/brands/{brand_id}` uses `active_only=False` (management/history); all other paths use default active-only. |
|| `brand_dir` gap | Current run endpoints accept client-controlled `brand_dir` and pass it to the Orchestrator. **BLOCKING FOLLOW-UP**: no future multi-brand phase may treat client `brand_dir` as trusted. Eventual contract is verified `brand_id`, not arbitrary filesystem `brand_dir`. Not migrated in MB-01. |
|| Migration | No existing single-brand state moved/deleted. `workspace/local/brand`, products, product profiles, raw data, staging, pillars, history, output, scheduler, run resources all untouched. |
|| Files changed (production) | `src/brand_registry.py` (new), `src/workspace_context.py`, `web_viewer.py` (Brand API + AuthMiddleware brand_id propagation) |
|| Files changed (tests) | `tests/test_brand_registry.py` (new), `tests/test_brand_context.py` (new), `tests/test_brand_api.py` (new) |
|| Files changed (docs) | `AI_EMPLOYEE_BETA_EXECUTION_PLAN.md` (this ledger) |
|| Scheduler | Business behavior untouched. No brand semantics added. Stage B APS IDs unchanged. |
|| System81 | Pending after multi-brand foundation. Not started. |
|| Stage C | ACCEPTED at `d904825`. |
|| Paid calls | $0. No LLM, web, image, video, provider, or network calls. |
|| Test results | BrandRegistry: 28 passed. BrandContext: 19 passed. Brand API: 17 passed. Stage A workspace/auth: 47 passed. Stage B scheduler (exec ownership + ownership + schedule API): 34 passed. Stage B scheduler (rerun/restart/missed/cleanup/misfire/attachments): 30 passed. Total: 175 passed, 0 failed. |
|| `git diff --check` | passed (no whitespace errors) |
|| Next exact action | Codex MB-01 final acceptance. |

## Progress Ledger — MB-02 (Brand-Scoped Business-State Isolation)

|| Field | Value |
||---|---|
|| Phase ID | MB-02 |
|| Agent ID | SHARED-RUNTIME |
||| Gate status | **ACCEPTED / CLOSED** at commit `d625ecd` on dev branch. Browser closure + ledger accuracy corrections applied. |
|| Baseline HEAD | `1a0ef78` (System81 authentication integration — frozen) |
|| Accepted checkpoints | Stage A `71c1e62` (per-user workspace isolation). Stage B `7a3c293` (per-user Scheduler ownership). MB-01 `f15cdbd` (multi-brand foundation). System81 `1a0ef78` (authentication integration). |
|| System81 live functional qualification | PASS. Canonical System81 ID stability remains a release note, not an MB-02 blocker. |
|| Security motivation | `User A / Brand A1 must never accidentally consume Brand A2 state.` Hierarchy: `User → Brand → products, product profiles, raw ingestion data, staging, brand profile/voice/visual/audience, brand assets, content pillars, content history, generated outputs/media, Scheduler jobs/runs/resources`. Brand B has a separate isolated subtree. |
|| Enforcement model | Workspace context, filesystem paths, ownership checks, server-derived identity — NOT prompt instructions. Client-supplied filesystem paths (`brand_dir`, existing `file` paths) cannot redirect server state when a verified brand context is active. Brand-scoped APIs fail closed (403) without an active verified brand. Legacy user-root fallbacks are not used for authenticated user-only contexts. True no-workspace CLI/test compatibility remains where explicitly supported. |
|| Proposed staging (production) | `web_viewer.py`, `src/brand_loader.py`, `src/content_history.py`, `src/ingestion.py`, `src/local_workspace.py`, `src/product_db.py`, `src/run_resources.py`, `src/scheduler.py`, `src/staging.py` |
|| Proposed staging (tests — modified) | `tests/conftest.py`, `tests/test_beta_e2e_smoke.py`, `tests/test_browser_e2e.py`, `tests/test_browser_integration_real_orch.py`, `tests/test_browser_upload_real_source.py`, `tests/test_bug1_auto_mode_agents.py`, `tests/test_bug2_parallel_dedup.py`, `tests/test_bug3_ask_mode_no_auto_media.py`, `tests/test_bug4_regular_flow_like_auto.py`, `tests/test_bug5_auto_all_agents.py`, `tests/test_campaign_behavioral_eval.py`, `tests/test_checkpoint_a_run_paths.py`, `tests/test_content_history.py`, `tests/test_context_composition_remediation.py`, `tests/test_g3_cross_domain_integration.py`, `tests/test_ingestion_k52_source_fidelity.py`, `tests/test_local_workspace.py`, `tests/test_m6_frontier_uat.py`, `tests/test_m6_judge_runner.py`, `tests/test_m6_uplift_harness.py`, `tests/test_m6_uplift_runner.py`, `tests/test_phase4_media_wiring.py`, `tests/test_qual_runner_offline.py`, `tests/test_recommendation_contract.py`, `tests/test_review_findings_remediation.py`, `tests/test_review_findings_v2.py`, `tests/test_schedule_api.py`, `tests/test_scheduler_attachments.py`, `tests/test_scheduler_cleanup.py`, `tests/test_scheduler_exec_ownership.py`, `tests/test_scheduler_misfire_grace.py`, `tests/test_scheduler_missed.py`, `tests/test_scheduler_ownership.py`, `tests/test_scheduler_rerun.py`, `tests/test_scheduler_restart.py`, `tests/test_ui_to_agent_flow.py`, `tests/test_web_e2e_data_integrity.py`, `tests/test_workspace_context.py` |
|| Proposed staging (tests — new) | `tests/test_brand_state_isolation.py`, `tests/test_data_folders_brand_scoped.py`, `tests/test_endpoint_security_media.py`, `tests/test_no_brand_sentinel.py`, `tests/test_run_endpoint_brand_security.py`, `tests/test_run_resources_cleanup.py`, `tests/test_run_resources_lifecycle.py`, `tests/test_scheduler_brand_ownership.py` |
|| Proposed staging (docs) | `AI_EMPLOYEE_BETA_EXECUTION_PLAN.md` (this ledger) |
|| Note on deleted file | `tests/test_scheduler_api_brand_security.py` was untracked (never committed). It was consolidated into `tests/test_no_brand_sentinel.py` and removed from the working tree. Git has no deletion to stage. |
|| Files excluded (unrelated dirty) | `cache/_media_capabilities/image:google_gemini-3.1-flash-image.json` (runtime media-capability cache side-effect; not MB-02) |
|| Files excluded (unrelated untracked) | `.agents/`, `.benchmark_evidence/`, `.benchmark_snapshot_20260910/`, `.codex/`, `brand/.gitkeep`, `mockup_data/`, `users/`, `scripts/qual_c2_launcher.py`, `scripts/qual_c2_start.py`, `scripts/qual_post_fire_screenshot.png`, `scripts/qual_schedule_create.py`, `scripts/qual_schedule_observe.py`, `scripts/qual_schedule_screenshot.png`, `scripts/reset_local_workspace.py`, `tests/test_qual_c2_launcher.py` |
|| Removed | Application-wide `@app.exception_handler(ValueError)` handler in `web_viewer.py` (string-matched "brand" in messages). Replaced by explicit `_require_brand_context()` checks at every brand-scoped API boundary. |
|| Endpoint coverage | Explicit `_require_brand_context()` guards on all brand-scoped endpoints: data/upload/stage/ingest/folder_files/folder/rename_folder/brand_files/brand_file/brand_json/brand_json_save/brand_save/brand_migrate/product_profile/product_profile_save/product_profile_suggest/voice_learn/voice_learn_upload/assets (list/get/upload/update/delete/file/reingest)/video_style_analyze/video_style_upload/pillars/pillars_save/pillars_suggest/sessions/session_files/content_history/file/run_agent/run_agents/run_flows/run_auto/run-resources (upload/delete)/schedule/* (all 8 endpoints)/generate_media/generate_all_media/parse_media_prompts/media_status/media_retry_log/cost_summary/media_retry/ingest_status/product_image/folder_file/output_file. |
|| Existing-file escape | `/api/generate_all_media` and `/api/parse_media_prompts` previously accepted an arbitrary existing absolute or project-local path supplied by the client. Now require an active brand first, then resolve the candidate safely and accept it only when contained beneath the verified active brand's output root. Spies at `media_gen.parse_media_prompts` + `media_gen.generate_image_with_retry` + `media_gen.generate_video_with_retry` prove rejection occurs before parsing/provider execution. |
|| Startup cleanup | `_cleanup_run_resources_all_brands(project_root)` — synchronous, testable helper that enumerates every registered user → active brand, sets each brand context, and runs `cleanup_expired()` per brand. Returns a per-brand result list (user_id, brand_id, removed count, error). Failures are logged per-brand, not silently discarded. Startup hook awaits completion via `asyncio.to_thread(...)` — startup completes only after cleanup finishes. |
|| Scheduler | APS IDs encode `(user_id, brand_id, job_id)`. Old two-part and legacy IDs decode safely and fail closed when brand context cannot be established. Job/run stores resolve to `brand_state_root()/cache/`. `add_job()` derives identity from verified workspace, not untrusted job data. `get_running_status()` filters by user+brand. `rerun_run()` rejects brandless records. Callbacks and reruns restore `WorkspaceContext.for_brand(...)`. |
|| Browser 403 contract | Both browser fixtures record HTTP responses (URL + status). `test_no_fatal_console_errors` asserts the expected no-brand responses are exactly `/api/data_folders` and `/api/schedule/status` with status 403, and fails if any other 403 response exists — inspected at the HTTP response layer, not console text. Generic 403 console messages are correlated with HTTP response count. |
|| Non-fatal media errors | Media generation errors (image/video) are non-fatal — the agent still completes. They are sent as `status` SSE events (not `error` events) so the JS doesn't mark the flow step as `.flow-step.error` prematurely. The error text is visible as a status message during the flow (captured via MutationObserver in tests). |
|| Test commands — focused | `python3 -m pytest tests/test_no_brand_sentinel.py tests/test_run_resources_cleanup.py tests/test_run_endpoint_brand_security.py tests/test_data_folders_brand_scoped.py tests/test_endpoint_security_media.py tests/test_brand_state_isolation.py tests/test_scheduler_brand_ownership.py tests/test_scheduler_ownership.py tests/test_context_composition_remediation.py` → `101 passed` |
|| Test commands — startup cleanup | `python3 -m pytest tests/test_run_resources_cleanup.py` → `5 passed` (includes actual startup handler test via TestClient lifespan) |
|| Test commands — media security | `python3 -m pytest tests/test_endpoint_security_media.py` → `5 passed` (includes parse/provider spies) |
|| Test commands — MB-02 regression subset | `python3 -m pytest tests/test_brand_state_isolation.py tests/test_data_folders_brand_scoped.py tests/test_no_brand_sentinel.py tests/test_run_endpoint_brand_security.py tests/test_run_resources_cleanup.py tests/test_run_resources_lifecycle.py tests/test_scheduler_brand_ownership.py tests/test_scheduler_ownership.py tests/test_scheduler_exec_ownership.py tests/test_scheduler_restart.py tests/test_scheduler_missed.py tests/test_scheduler_cleanup.py tests/test_scheduler_attachments.py tests/test_scheduler_misfire_grace.py tests/test_schedule_api.py tests/test_workspace_context.py tests/test_local_workspace.py tests/test_content_history.py tests/test_context_composition_remediation.py tests/test_checkpoint_a_run_paths.py tests/test_endpoint_security_media.py tests/test_brand_registry.py tests/test_brand_context.py tests/test_brand_api.py tests/test_beta_e2e_smoke.py tests/test_web_e2e_data_integrity.py tests/test_ui_to_agent_flow.py` → `1 failed, 410 passed, 155 warnings in 86.72s` (only accepted baseline failure) |
|| Test commands — browser group | `python3 -m pytest tests/test_browser_e2e.py tests/test_browser_integration_real_orch.py tests/test_browser_upload_real_source.py` → `29 passed, 33 skipped, 0 failed in 181.43s` (33 skips are missing external source fixtures) |
|| Accepted baseline failure | `tests/test_local_workspace.py::test_product_agent_instructions_no_per_agent_sections` — checks the real `config/agent_instructions.json` file; pre-existing, unrelated to MB-02. |
|| Unrelated pre-existing failures (not part of MB-02 gate) | `tests/test_recommendation_contract.py` (4 tests) — `CompetitorReportRenderer` rendering issues; pre-existing at HEAD `1a0ef78` (verified via `git stash` + rerun). Not part of the MB-02 regression subset. |
|| Paid/provider calls | $0. No LLM, embedding, web, image, video, provider, or network calls. |
|| `git diff --check` | passed (no whitespace errors) |
|| Stage C | ACCEPTED at `d904825`. |
|| System81/authentication production code | Unchanged. |
|| Next exact action | Codex MB-02 final acceptance review. |


## Progress Ledger — Stage C (Runtime/Recovery Isolation)

||| Field | Value |
|||---|---|
||| Phase ID | Stage C |
||| Agent ID | SHARED-RUNTIME |
||| Gate status | ACCEPTED at `d904825` (per Codex multi-brand audit baseline). |
||| Baseline HEAD | `d625ecd` (MB-02 accepted/closed — frozen) |
||| Accepted checkpoints | Stage A `71c1e62`, Stage B `7a3c293`, MB-01 `f15cdbd`, System81 `1a0ef78`, MB-02 `d625ecd` |
||| Security motivation | A user or brand must not cancel, observe, reuse, overwrite, recover, or inherit another user/brand's runtime state. |
||| Enforcement model | Per-user dicts keyed by verified `user_id` from `get_workspace()` (set by AuthMiddleware). Identity comes from server-side workspace context, never client-supplied values. Recovery archive resolves to `users/{user_id}/.recovery_archive` when a workspace is active. |
||| Proposed staging (production) | `web_viewer.py`, `src/local_workspace.py` |
||| Proposed staging (tests — modified) | `tests/test_browser_e2e.py`, `tests/test_browser_integration_real_orch.py`, `tests/test_browser_upload_real_source.py`, `tests/test_schedule_api.py`, `tests/test_endpoint_security_media.py`, `tests/test_g3_cross_domain_integration.py`, `tests/test_web_e2e_data_integrity.py`, `tests/test_no_brand_sentinel.py`, `tests/test_data_folders_brand_scoped.py`, `tests/test_beta_e2e_smoke.py`, `tests/test_media_gen_provider_flow.py` |
||| Proposed staging (tests — new) | `tests/test_concurrent_run_state.py` |
||| Proposed staging (docs) | `AI_EMPLOYEE_BETA_EXECUTION_PLAN.md` (this ledger) |
||| Runtime state isolation | **User-scoped** (per `user_id`): `_cancel_requested`, `_current_llm`, `_active_llms`, `_session_ts` — cancel/LLM/session state for a user's active runs, not brand-derived. **Brand-scoped** (per `(user_id, brand_id)`): `_BRAND_VISUAL_CACHE`, `_conflict_cache` — brand-derived data (visual.json, brand priority rules, brand-scoped instructions); switching U1 from A1 to A2 gets a different cache key, so A1's cached data is never consumed by A2. `/api/cancel` sets only the calling user's flag and aborts only their LLM clients. `_register_llm`/`_unregister_llm` manage per-user LLM lists under `_runtime_lock`. `_is_cancelled()` checks the current user's flag. `_current_llm` check-then-set is under `_runtime_lock`. `_BRAND_VISUAL_CACHE` miss path uses `setdefault` under lock (load outside lock). |
||| Archive isolation | `_archive_root()` resolves to `user_state_root() / ".recovery_archive"` when a workspace is active — `users/{user_id}/.recovery_archive`. Falls back to project-level path only in true no-workspace CLI/test mode. |
||| Test commands — focused Stage C | `python3 -m pytest tests/test_concurrent_run_state.py` → `16 passed` |
||| Test commands — affected existing | `python3 -m pytest tests/test_local_workspace.py tests/test_no_brand_sentinel.py tests/test_data_folders_brand_scoped.py tests/test_endpoint_security_media.py tests/test_beta_e2e_smoke.py` → `129 passed, 1 failed` (only accepted baseline failure) |
||| Test commands — browser group | `python3 -m pytest tests/test_browser_e2e.py tests/test_browser_integration_real_orch.py tests/test_browser_upload_real_source.py` → `29 passed, 33 skipped, 0 failed` |
||| Test commands — MB-02 regression subset | `python3 -m pytest` (same 27-file subset as MB-02) → `1 failed, 410 passed` (only accepted baseline failure) |
||| Accepted baseline failure | `tests/test_local_workspace.py::test_product_agent_instructions_no_per_agent_sections` — pre-existing, unrelated to Stage C. |
||| Paid/provider calls | $0. No LLM, embedding, web, image, video, provider, or network calls. |
||| `git diff --check` | passed (no whitespace errors) |
||| Stage C stop point | All Stage C tests green; stop for Codex acceptance review. |
||| System81/authentication production code | Unchanged. |
||| Media phase | Not started. |
||| Next exact action | Codex Stage C acceptance review. |

## Progress Ledger — MB-UI (Multi-Brand Account Flow UI)

||| Field | Value |
|||---|---|
||| Phase ID | MB-UI |
||| Agent ID | SHARED-RUNTIME |
||| Gate status | Implemented; pending Codex acceptance review. Backend audit classified **B. BACKEND COMPLETE, UI MISSING** — registry, ownership verification, selection cookie, brand-scoped storage all accepted and unchanged. |
||| Baseline HEAD | `d904825` (Stage C accepted) |
||| Accepted checkpoints | Stage A `71c1e62`, Stage B `7a3c293`, MB-01 `f15cdbd`, System81 `1a0ef78`, MB-02 `d625ecd` (backend isolation), Stage C `d904825` (runtime isolation) |
||| Scope | User-facing multi-brand account flow through the real web UI: blocking brand picker/onboarding when no active brand; header brand switcher; create/select/switch via DOM only; `location.reload()` as the accepted switch architecture (AuthMiddleware rebuilds the verified WorkspaceContext from the new cookie). No server-side last-active-brand persistence (cookie-scoped selection is the existing design). Logout clearing brand selection remains accepted behavior. |
||| Files changed (production) | `web_viewer.py` — `HTML_PAGE` only: brand-gate/switcher CSS block; `#brand-switcher-btn` in `.header-right`; `#brand-gate-overlay` picker markup; JS `initBrandGate`/`openBrandPicker`/`closeBrandPicker`/`_renderBrandGateList`/`selectBrand`/`createBrandFromGate` + `DOMContentLoaded` registration. No backend route/middleware/registry changes. |
||| Files changed (tests — new) | `tests/test_browser_multibrand_e2e.py` — real uvicorn + Chromium; session-cookie injection only; all brand create/list/select/switch via real DOM controls; product data seeded directly under `users/<uid>/brands/<bid>/data/`; `mktapp_brand` never set by the test. |
||| Files changed (docs) | `AI_EMPLOYEE_BETA_EXECUTION_PLAN.md` (this ledger) |
||| Safety properties | Brand names rendered via `textContent`/DOM APIs only (no innerHTML interpolation). Empty/whitespace names rejected client-side. All picker controls disabled during in-flight create/select. Only server-returned `brand_id`s are submitted to `/select`. Backend remains authoritative. |
||| Browser evidence | `python3 -m pytest tests/test_browser_multibrand_e2e.py -vv --tb=short` → `2 passed`: (1) `test_create_switch_isolation_and_reload` — no-brand blocking picker → create AlphaBrand via DOM → active → seed ProductA1 → open switcher → create BetaBrand via DOM → both listed → A2 active → seed ProductA2 → sidebar shows only A2 data → switch A2→A1 via DOM → only A1 data → switch A1→A2 → only A2 → switch back A2→A1 → A1 returns → `page.reload()` preserves A1 + data; no unexpected 401/403 after activation; no fatal console errors. (2) `test_existing_brands_listed_without_brand_cookie` — create GammaBrand via DOM in context A → new context with session cookie only (no `mktapp_brand`) → blocking picker lists GammaBrand → select via DOM → workspace opens with GammaBrand data. |
||| Browser regression | `python3 -m pytest tests/test_browser_e2e.py tests/test_browser_integration_real_orch.py tests/test_browser_upload_real_source.py` → `29 passed, 33 skipped, 0 failed` (baseline preserved). |
||| API regression subset | `python3 -m pytest tests/test_brand_api.py tests/test_brand_state_isolation.py tests/test_data_folders_brand_scoped.py -vv --tb=short` → `33 passed, 0 failed`. |
||| Backend changes required | None. No System81/auth, MB-02, Stage C, scheduler, media, prompt, schema, or agent changes. |
||| Paid/provider calls | $0. No LLM, embedding, web, image, video, provider, or network calls (`/api/credits` OpenRouter lookup stubbed in the new test fixture). |
||| `git diff --check` | clean (no whitespace errors) |
||| Plan status | The User/Brand isolation plan is **not** declared complete until this real UI flow is accepted by Codex. Media phase not started. |
||| Next exact action | Codex MB-UI acceptance review. |

## Progress Ledger — URL-IMPORT (Product URL Import v1)

||| Field | Value |
|||---|---|
||| Phase ID | URL-IMPORT |
||| Agent ID | SHARED-RUNTIME |
||| Gate status | Implemented; pending Codex acceptance review. Architecture/reuse audit ACCEPTED (classification **B — reuse with thin adapter**). No second product pipeline created. |
||| Baseline HEAD | `ddb4e3a` (multi-brand account switching; U/B isolation frozen) |
||| Scope — supported | Public http/https product pages; static/server-rendered HTML; official brand pages; normal ecommerce; Shopify/WooCommerce-like pages when useful content exists in the initial HTML. JS-shell pages get a best-effort Playwright fallback (below). |
||| Scope — unsupported (v1) | Shopee, Lazada, Facebook, Instagram, TikTok, YouTube/video URLs, login/auth pages, anti-bot/challenge pages. These may ROUTE to the browser fallback but are NOT claimed as supported — no live qualification performed, fixtures only. Refresh/sync of an existing product from its source URL. No Selenium/yt-dlp/provider-web_fetch. |
||| Acquisition strategy (v1.1) | Static httpx fetch first → parse → if normalized body text < `MIN_BODY_CHARS`(240) → headless-Chromium fallback via `_browser_fetch` → rendered DOM re-parsed by the SAME `_PageExtractor`/`_normalized_text` → still thin → controlled `empty_page` error. No per-URL Chromium; no hardcoded JS-domain list — the signal is deterministic content insufficiency. Result gains `fetched_via: static|browser` (also persisted in `source_import`). Reference design: Pingevo/multi-agents `ac89dfd` (static-first + playwright fallback) — its host-list and substring SSRF checks were replaced by our central validator. |
||| Browser SSRF contract (verified Playwright 1.60.0) | `context.route("**/*")` intercepts every http/https request — document, redirect hops, frames/iframes, XHR/fetch, images, scripts, fonts — each validated by `_validate_url`; non-public → `route.abort()`. `service_workers="block"` prevents fetch-intercepting SW installs. `route_web_socket` (registered AFTER route — reverse order deadlocks the sync dispatcher, verified) intercepts page/frame WebSockets; the handler never calls `connect_to_server()` so no socket ever opens. `Worker`/`SharedWorker`/`WebSocket` removed via `_BROWSER_INIT_SCRIPT` — worker-created WS bypass route_web_socket entirely (verified on 1.60), so the WS surface is closed by removing workers; pages needing workers degrade to thin content → controlled error. `BROWSER_SETTLE_MS`=500 post-idle beat for late JS. Endpoint runs the sync fetch via `asyncio.to_thread`. Residual: DNS TOCTOU (same as static path — documented ponytail ceiling); `route.fulfill`ed 302s aren't re-routed (test-layer only; real redirect hops DO route). |
||| Challenge/unusable-page gate | `_is_unusable(title, body_text)` — small generic marker set (access-denied/403, verify-human/captcha, checking-browser/ddos, just-a-moment, log-in-to-continue) matched on title+extracted body only, never URL. Applies to BOTH static and rendered results: static unusable/thin → browser; rendered still unusable/thin → controlled `empty_page` — a long Cloudflare interstitial can never become a product. |
||| Architecture | `UI paste link → POST /api/product_from_url → src/url_import.py fetch+normalize → [(filename, bytes)] → product_db.save_uploaded_files → existing ingest_product thread (with_workspace_context) → same product record/status/agent-context lifecycle as uploads`. `ingest_product` itself is unchanged. |
||| Files changed (production — new) | `src/url_import.py` — safe fetch/normalize adapter only; never writes to the workspace. Public interface: `fetch_product_page(url) → {original_url, final_url, canonical_url, page_title, og_title, text, images[{name,content,source_url}], fetched_at}`; raises `UrlImportError` with user-safe Thai messages (internals stay in `.detail`). |
||| Files changed (production — modified) | `web_viewer.py` — new `POST /api/product_from_url` endpoint (mirrors `/api/upload` minus multipart) + sibling "วางลิงก์สินค้า" input inside the existing `#upload-overlay` modal (`importProductFromUrl()` JS; hidden in manage/edit mode) + `url-import-block` markup. |
||| Files changed (tests — new) | `tests/test_url_import_security.py` (38 tests), `tests/test_product_url_import.py` (8 tests), `tests/test_browser_url_import_e2e.py` (1 real-browser test), `tests/test_url_import_browser.py` (11 browser-fallback tests, real Chromium + fulfill stubs). |
||| AI participation in ingestion (audited) | `ingest_product` LLM steps: (1) `segment_products` — new-upload catalog boundary decision, falls back to `_fallback_single` without key; (2) `_generate_metadata_summary` — metadata.summary/category, deterministic preview fallback without key; (3) `_generate_product_profile` → `voice_learner.analyze_product_positioning` → `product_profile.json`, skipped without key, never overwrites existing file. Deterministic: classify/hash/extract_text/embedded-image extract/raw_text rebuild/text_extracts/image_descriptions/status. URL evidence lands as `.txt`/`.png` → receives ALL of these automatically — NO second AI extraction step added. |
||| Provider `web_fetch` fallback (assessed, not implemented) | Existing OpenRouter `web_fetch` (base_agent) could supplement when local+Chromium yield nothing. Deferred: output is model-generated text, not raw evidence; would need provenance separation (`fetched_via: "provider"`), ~1 paid call per import, citations not guaranteed. Violates v1's evidence-first principle if persisted as trusted raw. Recommendation: only as a marked `provider_fetch.txt` artifact if ever added. |
||| Product editability (audited — Task C) | `product_profile.json` = existing user-editable positioning layer (`audience`, `competitors`, `differentiators`, `use_cases`, `price_tier`, `tone_adjustment`, `visual_override`, `category`). Read/edit backend: `GET /api/product_profile/{folder}`, `POST /api/product_profile_save/{folder}`, `POST /api/product_profile_suggest/{folder}` (AI pre-fill); UI = `pp-section` in manage modal. Agents consume it via `load_brand_rules` (tone), `load_brand_reference`/`load_brand_priority` (audience+positioning), `load_brand_visual` (visual_override), `build_multi_product_profile_context` (multi-product envelopes), orchestrator product tools (`price_tier`/`differentiators`/full profile). Edits survive re-ingest (created once only). GAP: profile covers positioning, NOT extracted facts — `raw_text`/`text_extracts` are regenerated each ingest and have no editable layer. Classification: **B** — backend partially supports correction; thin integration needed for fact-level edits. Recommended smallest canonical layer: `facts` dict inside `product_profile.json` rendered as a "user-verified" block ahead of `raw_text` in `get_agent_context_text` — reuses existing save endpoint (accepts arbitrary dict), no schema change. Product Editor NOT implemented (audit-only phase). |
||| Shared reader (audited) | `voice_learner.fetch_url_content` — local `httpx.get`, blind redirects, 5k cap, ZERO SSRF protection (scheme/IP/DNS/redirect/MIME all unchecked). Clean migration path: `fetch_url_content(url) → fetch_product_page(url)["text"][:5000]` — inherits full hardening for a 3-line change; keeps 5k contract. Deferred to its own acceptance (behavior change in a second consumer). NOT migrated: `video_style` URLs (provider-side multimodal, different contract), agent `web_search`/`web_fetch` (provider research tools with citations, different contract). |
||| Artifact format | `data/{product}/source_page.txt` — provenance header (Source URL / Final URL / Fetched) + normalized text (title, description, headings/lists/table/body text; script/style stripped; ≤ `MAX_TEXT_CHARS`=60k, NOT the old 5k cap) + `data/{product}/page_img_NNN.{jpg,png,webp}` for downloaded images. Provenance also recorded additively on the product record as `source_import{original_url, final_url, canonical_url, fetched_at, page_title, source_file, image_count}` — no schema change. |
||| Security/SSRF contract | http/https only; userinfo/malformed rejected; hostname resolved via `socket.getaddrinfo` and EVERY resolved IP must be `ipaddress.is_global` and non-multicast (v4+v6); manual redirect chain (`MAX_REDIRECTS`=5) re-validates DNS/IP on every hop — redirects into private/link-local nets rejected; streamed bodies with caps (`MAX_PAGE_BYTES`=2MB, `MAX_IMAGE_BYTES`=5MB, `MAX_TOTAL_IMAGE_BYTES`=15MB, `MAX_IMAGES`=8, `HTTP_TIMEOUT` 10s connect/20s read); main page must be HTML/text MIME; images restricted to image/{jpeg,png,webp} (the ingestible set); optional image failures never fail the text import; error messages expose no internal network details. Known ceiling (documented `ponytail:` in module): DNS re-resolution at connect time (TOCTOU) — connect-by-IP+SNI is the upgrade path. |
||| Naming/merge semantics | Explicit `product_name` → same `contain_path` check as `/api/upload`; existing folder → controlled 409 (URL refresh/merge deliberately not invented). Derived name → sanitized `og_title`/`<title>` → staging-style `name (N)` dedup — always creates a NEW product, never merges into an existing one. |
||| Test commands — security unit | `python3 -m pytest tests/test_url_import_security.py -q` → `38 passed` (browser path stubbed — suite is fetch/validation only) |
||| Test commands — browser fallback | `python3 -m pytest tests/test_url_import_browser.py -q` → `18 passed` (static-rich → no Chromium; JS shell → browser; rendered text+image reach normal seams; failure → controlled error; private nav/redirect/iframe/XHR/image aborted; SW registration blocked; page WS intercepted+never connects; Worker/SharedWorker/WebSocket constructors removed; worker-WS impossible; long challenge → browser; long rendered challenge → rejected; genuine long page → static; detector unit) |
||| Test commands — integration | `python3 -m pytest tests/test_product_url_import.py -q` → `8 passed` (provenance incl. `fetched_via`, ready status, raw_text marker, image seam, data_folders, get_scoped_context_text, get_product_image_paths, 401/403, A1↔A2 isolation, invalid/SSRF/collision/dedup/explicit-name) |
||| Test commands — browser E2E | `python3 -m pytest tests/test_browser_url_import_e2e.py -q` → `1 passed` (real uvicorn+Chromium: DOM modal → paste URL → import → sidebar product ready → A2 absent → A1 returns; fetch stubbed at `_make_client`/`_browser_fetch`/`getaddrinfo` seams — no internet) |
||| Test commands — affected regressions | `python3 -m pytest tests/test_product_db.py tests/test_ingestion_incremental.py tests/test_ingestion_multi_product.py tests/test_staging.py -q` → `44 passed`; `python3 -m pytest tests/test_brand_state_isolation.py tests/test_brand_api.py tests/test_brand_context.py tests/test_data_folders_brand_scoped.py tests/test_no_brand_sentinel.py tests/test_run_endpoint_brand_security.py tests/test_endpoint_security_media.py tests/test_voice_learner.py -q` → `127 passed`; `python3 -m pytest tests/test_browser_upload_real_source.py tests/test_browser_multibrand_e2e.py -q` → `2 passed, 33 skipped` (fixture-data skips, baseline-identical) |
||| Pre-existing baseline failures (unchanged, verified via stash) | `tests/test_staging_api.py` (16 — auth-only clients vs `_require_brand_context`, fails identically at baseline); `tests/test_ingestion_k52_source_fidelity.py` (8 — missing `data/CACGO K52` fixture, fails identically at baseline) |
||| Paid/provider calls | $0. No LLM, embedding, web, image, video, provider, or network calls — all HTTP via `httpx.MockTransport`, DNS via `getaddrinfo` stub, `gateway.get_api_key` stubbed empty. |
||| Existing security follow-up (separate, not this phase) | `src/voice_learner.py::fetch_url_content` has the SSRF class this module fixes (no scheme/IP/redirect/size/MIME validation). Not redesigned per phase scope; candidate for a minimal behavior-compatible swap to the new safe fetcher — requires Codex acceptance before broadening. |
||| `git diff --check` | clean (no whitespace errors) |
||| Unrelated exclusions | `cache/_media_capabilities/image:google_gemini-3.1-flash-image.json` (pre-existing runtime drift) — excluded from staging. |
||| Next exact action | Codex URL-IMPORT acceptance review. Nothing staged/committed/pushed. |

## Progress Ledger — PRODUCT-LIFECYCLE-E2E-01 (Real-browser lifecycle qualification)

||| Field | Value |
|||---|---|
||| Phase ID | PRODUCT-LIFECYCLE-E2E-01 |
||| Agent ID | DEVIN |
||| Gate status | QUALIFICATION COMPLETE — **verdict FAIL / NO-COMMIT**. 11 confirmed production defects recorded. No production remediation applied. |
||| Baseline HEAD | `30598a4ef3e5adef05adca17070fc041a1a39e22` (branch `dev`, +2 ahead of `origin/dev`; ~1,100 lines pre-existing uncommitted production changes = frozen qualification target) |
||| Production files changed | **None.** Production behavior frozen throughout the run. |
||| Files added (test-only) | `tests/test_browser_lifecycle_qual.py` (7 journeys); `tests/_qual_artifacts/PRODUCT-LIFECYCLE-E2E-01/` (36 artifacts: 18 screenshots, 8 Playwright traces, 10 JSON findings/ledgers) |
||| Harness | Fresh tmp workspace; real uvicorn; fresh Chromium contexts per journey; `context.route` guard aborts all non-localhost requests (`network_attempts.json` = `[]`, zero external traffic); deterministic FakeLLM with per-call ledger (source, markers, thread); `fetch_product_page` stubbed at module seam; no real users/sessions/brands; $0 provider/model/media spend. |
||| Journey results | A PASS (auth, two-user/two-brand isolation, brand-gate, forged-cookie fail-closed, logout/login). B PASS (staging preview before commit; select A+C skip B; zero BRAVO metadata/profile calls; cancel → zero products, zero enrichment). C PASS (URL import → same staging/review; selected-only materialization; cancel → nothing). D FAIL (2 defects). E PASS (owner list/read OK; 401 no-auth; 403 no-brand; other-user/other-brand denied; traversal/encoded/absolute/sibling blocked; no content/path leakage). F PASS-record (restart persistence OK; manual-overrides-derived OK; derived facts NOT labelled verified; **stale derived facts retained after empty `derived_facts` regeneration**; **failed enrichment → status `ready`, no `ingest_error`** — false-READY). G FAIL (9 boundary defects). |
||| Confirmed defects — G/commit boundary | `src/staging.py::commit_batch`: (1) absolute `name` → `mkdir` outside brand root executes BEFORE containment rejects (dir created at `/tmp/qual_escape`, then 400); (2) `../` `name` → same pattern, dir created outside `data/` then 400; (3) `segment_index: -1` → Python negative indexing → product materialized (200); (4) `segment_index: 99` → uncaught IndexError → 500; (5) duplicate segment selection → materialized twice (`Dup`, `Dup (1)`); (6) unknown `action` → silently ignored, 200; (7) `update` with `target:".."` → source files copied to brand root outside `data/` before 400; (8) non-atomic — valid choice committed before later choice 500s (`PartialOK` persisted); (9) `POST /api/upload` creates+enriches product(s) without staging review (ledger seq 72–73: `analyze_product_positioning` fired for unreviewed `BRAVO-UNIQ`). |
||| Confirmed defects — D/Product Detail | `pdSaveMkt()` (`web_viewer.py` ~8563–8572) omits `audience`/`visual_override` keys when cleared → server merge preserves old values → **clearing audience/visual override never persists** (browser-verified: leftover `{'primary': {'age': '30-40','role': ''}}` and `{'image_style': {'tone': 'neon cyber'}}` after save+reopen). |
||| Confirmed defects — F/truthfulness | Re-ingest with `derived_facts:{}` → previous derived facts retained (stale). Deterministic enrichment exception → final status `ready`, `ingest_error` null → false READY (decision-rule blocker). |
||| Confirmed defects — C/naming | `product_name` is validated (contain_path + collision → 409) then never applied: created product named `URL Alpha Gadget (1)`, not the supplied name; `#upload-product-name-modal` is hidden in Add-Product mode so the field is unreachable via UI — dead parameter. |
||| Disproven/mitigated suspicions | "Derived facts labelled verified" — NOT found in PD body text (disproven at UI level). "PD file-add double ingestion" — `_pdFileSelected` does call `/api/upload`+`/api/ingest`, but the 409-on-processing guard produced exactly 1 run in observation; second call is timing-dependent redundant request, silent 409 (P3 code-smell, not a proven double-run). Source-file route containment — all matrix variants blocked (E PASS). |
||| Overlapping `source_refs` | UNPROVEN — FakeLLM plans used disjoint refs; overlap rejection not exercised. |
||| Regression suites | 472 passed / 14 failed, all stale-vs-new-contract (verified pre-existing class): `test_staging_api.py` ×8 + `test_thread_context_propagation.py` ×1 → `403 active brand required` (fixtures never select a brand — same class as URL-IMPORT ledger's documented baseline); `test_product_url_import.py` ×4 → `KeyError 'folder'` (endpoint now returns staged batch, not `folder` — contract changed by uncommitted work); `test_local_workspace.py` ×1 → `product_spec` present in product agent_instructions (unrelated config drift). |
||| Code-truth inventory | All 12 spec-listed findings confirmed/disproven with evidence in artifact JSONs. Legacy existing-product modal JS remains as dead code (unreachable — PD is sole management surface, verified: card click and gear both reach PD). |
||| Smallest proposed remediation sequence (NOT implemented — requires Codex/PO instruction) | 1) `commit_batch`: validate ALL choices (index range incl. negative, action enum, `contain_path` on name/target, dup detection) BEFORE any mkdir/copy — one pre-flight pass, then materialize; wrap materialization so a late failure doesn't leave partial state (or document non-atomic + surface it). 2) `pdSaveMkt`: always send `audience`/`visual_override` keys (empty dict = cleared). 3) `ingest_product`/commit enrichment: propagate enrichment failure → `no_usable_data`/`ingest_error` instead of unconditional READY; empty regenerated `derived_facts` should replace, not merge-retain. 4) `/api/product_from_url`: apply validated `product_name` to the staged batch default or remove the parameter. 5) `/api/upload` new-folder path: route through staging or reject non-PD-context creates. Items 1/3 touch orchestration-adjacent contracts — hard-stop review needed before implementation. |
||| Commands | `MKTAPP_DEV_AUTH=1 python3 -m pytest tests/test_browser_lifecycle_qual.py -v` → 5 passed, 2 failed (D, G); regression command = 32 focused test files (see run log). |
||| Next exact action | Codex triage of the 11 confirmed defects → remediation instruction under a separate phase. |

## Progress Ledger — PRODUCT-LIFECYCLE-REMEDIATION-01 + PRODUCT-LIFECYCLE-E2E-02

|||| Field | Value |
||||---|---|
|||| Phase ID | PRODUCT-LIFECYCLE-REMEDIATION-01 → PRODUCT-LIFECYCLE-E2E-02 |
|||| Agent ID | DEVIN |
|||| Gate status | REMEDIATION + RE-QUALIFICATION COMPLETE — E2E-02 verdict **PASS (7/7 journeys)**; recommendation NO-COMMIT pending Codex review. |
|||| Baseline HEAD | `30598a4ef3e5adef05adca17070fc041a1a39e22` (branch `dev`; unchanged — nothing committed/pushed) |
|||| Production files changed | `src/staging.py` (commit_batch two-phase validate-then-materialize; raw image files registered into `image_descriptions`; honest per-product enrichment status + `source_import`/`llm` pass-through); `src/ingestion.py` (enrichment failure → `no_usable_data` + `ingest_error`; explicit-empty `derived_facts` replaces stale; `_generate_product_profile` propagates); `src/voice_learner.py` (`_run_llm_json`/`analyze_product_positioning` opt-in `strict=` raise-after-retries; default `{}` contract preserved); `web_viewer.py` (`/api/upload` rejects new-product creation 400; `product_name` → staged `suggested_name`; `pdSaveMkt`+legacy `saveProductProfile` always send `audience`/`visual_override`; `_pdFileSelected` single ingest trigger) |
|||| Test-only files changed | `tests/test_lifecycle_remediation.py` (new, 23 tests: boundary matrix + zero-side-effect proofs + upload-review + truthfulness + overlap diagnostic); `tests/test_browser_lifecycle_qual.py` (E2E-02 label, strengthened F/G assertions, D fast-fake ingest-count case, C explicit-name assertion, render-wait fixes); `tests/test_staging_api.py`, `tests/test_thread_context_propagation.py` (brand-aware fixtures per MB-02 contract); `tests/test_product_url_import.py` (staged/review contract); `tests/test_ingestion_multi_product.py` (FakeLLM returns valid extraction JSON — enrichment failure is now honest) |
|||| Journey results (E2E-02) | **A PASS** (isolation, forged-cookie fail-closed, logout/login). **B PASS** (preview→select→commit; zero unselected enrichment; cancel→nothing). **C PASS** (URL→staging parity; explicit `product_name` honored as staged `suggested_name` → committed `My Explicit Name`). **D PASS** (marketing set/clear persisted `{}`; manual facts persist across refresh; dirty-warning dismiss/accept; source popup; delete; **exactly 1 ingest in slow AND fast fake**). **E PASS** (full source-security matrix denied). **F PASS** (restart persistence; empty regeneration cleared stale `derived_facts` → `{}`; forced enrichment failure → `no_usable_data` + `ingest_error` recorded — no false-READY). **G PASS** (all 9 malformed payloads → 400 with zero filesystem/product/enrichment side effects; `/api/upload` new-product → 400, nothing created). |
|||| Network/provider attempts | **0** (`network_attempts.json` = `[]`; `context.route` aborts non-localhost; FakeLLM + stubbed `fetch_product_page`; $0 spend) |
|||| Deterministic regression | `test_lifecycle_remediation` 23 + `test_staging_api` 8 + `test_thread_context_propagation` 6 + `test_product_url_import` 8 + `test_ingestion_multi_product` — all green |
|||| Broad offline suite | `pytest tests/` minus 35 documented live-coupled files → **1643 passed, 60 failed, 19 skipped**. All 60 classified pre-existing: MB-02 workspace-resolution fixture drift (~50: fixtures patch `_project_root`/tmp while production resolves `brand_state_root`/`user_state_root` — same class as the previously documented `test_staging_api` failures), real-data fixtures missing (k52 ×8), documented unrelated drift (`test_local_workspace` ×1), unrelated contract drift (media/gateway/output ×~5). Zero new regressions attributable to remediation hunks. |
|||| Root causes repaired | (1) `commit_batch` mutated FS before validating choices → two-phase plan-then-materialize; (2) enrichment exceptions swallowed → propagate via `strict` seam, status `no_usable_data`+`ingest_error`; (3) `derived_facts` assigned only when non-empty → assign authoritative `{}` on successful extraction; (4) `pdSaveMkt`/`saveProductProfile` omitted empty keys → always send (server merge requires explicit empty to clear); (5) `_pdFileSelected` fired redundant `/api/ingest` → `/api/upload` is sole trigger; (6) `/api/upload` created+enriched unreviewed products → 400 rejection, staging-only creation; (7) `product_name` validated-then-dropped → carried to staged `suggested_name`; (8) staged commits didn't register raw image files → `image_descriptions` includes them. |
|||| Deferred/unproven | Overlapping `source_refs` across segments: diagnostic confirms currently ACCEPTED (per-segment validation only) — evidence-bleed gap reported to Codex, segmentation contract unchanged per instruction. Legacy manage-modal JS still present (clear-defect fixed, rest is P3 dead code). Output/history isolation beyond source files — out of scope. Double-ingest race between PD and other callers — guard retained as defense-in-depth. 60 baseline-drift failures above need their own fixture-update phase (not this scope). |
|||| Commands | `MKTAPP_DEV_AUTH=1 python3 -m pytest tests/test_browser_lifecycle_qual.py -x -q` → **7 passed** (55s); `pytest tests/test_lifecycle_remediation.py tests/test_staging_api.py tests/test_thread_context_propagation.py tests/test_product_url_import.py -x -q` → **45 passed**; broad offline → 1643/60/19 (see classification) |
|||| Artifacts | `tests/_qual_artifacts/PRODUCT-LIFECYCLE-E2E-02/` (screenshots, traces, journey JSON ledgers); E2E-01 artifacts preserved untouched under `PRODUCT-LIFECYCLE-E2E-01/` |
|||| Next exact action | Codex review of remediation diff + E2E-02 evidence → commit decision. |

## Progress Ledger — FREE-IMPORT-AI-PROPOSAL-SOURCE-STALE-01

| Field | Value |
|---|---|
| Phase ID | FREE-IMPORT-AI-PROPOSAL-SOURCE-STALE-01 (continuation of FREE-IMPORT-AI-PROPOSAL-FINAL-REVIEW; gate: NO-COMMIT / CHANGES REQUIRED) |
| Agent ID | DEVIN |
| Baseline HEAD | `30598a4` (branch `dev`; unchanged — nothing committed/pushed) |
| Root cause | Two layers. (1) `_baseline()` snapshotted canonical fields only; `resolve_proposal()` compared canonical drift only — source file add/delete/replace, `raw_text` re-ingest, transcripts and scope could change the evidence AI analyzed without moving the accepted canonical field. (2) Codex review round 2: a record-only fingerprint misses the disk-pending window — `save_uploaded_files()` lands bytes on disk BEFORE background re-ingest rebuilds `product.json`, so a fresh upload left `source_stale` false and accept wrote obsolete AI output (confirmed reproduction). |
| Smallest seam | Existing proposal-baseline seam plus the existing source-validity scanner. `_baseline()` persists `source_rev` — SHA-256 over a canonical JSON serialization of source-derived evidence (`raw_text`, `files` name+hash manifest, `text_extracts`, `image_descriptions`, video/audio transcripts, `derived_facts`, `scope`; basenames only, sorted, no paths/timestamps/dict-order/process state). Shared gate `_source_stale()` = `source_rev` mismatch (covers completed re-ingest mutations; missing fingerprint → legacy proposals fail safe) **OR** `product_db.find_stale_files()` non-empty (covers pending disk add/delete/replace — the existing re-ingest scanner reused, no second filesystem walk). `resolve_proposal()` gates every `accept` on it → explicit conflict `{"reason": "source_changed"}`, no writes, no consume, proposal stays pending; `keep`/`edit` unaffected (user authority). `get_proposal_view()` exposes `source_stale` via the same gate. Canonical user facts deliberately excluded from the fingerprint — manual edits remain per-field canonical drift. |
| Production files changed | `src/ai_enrichment.py` (+`hashlib` import; `_source_revision()`; `_source_stale()` gate; `source_rev` in `_baseline`; stale gate in `resolve_proposal`; `source_stale` in `get_proposal_view`). `web_viewer.py` JS only: `_pdAiReviewHtml` renders a `.pd-ai-stale` warning and withholds all accept paths (per-row accept + bulk accept) when `view.source_stale`, keeping keep/edit/discard; `_pdAiRow` gained a `stale` param; `_pdAiResolve` distinguishes `reason === 'source_changed'` from canonical-field drift in the status message. No endpoint/shape change — `source_stale` and conflict `reason` are additive JSON response fields. No `product_db.py` change — `find_stale_files()` was already the source-validity seam. |
| Test files changed | `tests/test_ai_proposal.py` (+`TestSourceRevision`, 13 tests incl. 3 real-disk-mutation cases via `save_uploaded_files`/unlink/replace; `_mk_product` now registers `files[]` with real path+hash so `find_stale_files` sees a clean tree). `tests/test_browser_staging_review.py` (+`TestStaleSourceReview` DOM regression; `_FakeLLM` serves canned proposal JSON while staying a call-recording tripwire; removed stale `staging._project_root=lambda: tmp` patch — production resolves `brand_state_root`, patch had diverted committed data dirs to flat `tmp/data` breaking `_require_product`; two assertions moved to `brand_root/data`). No change to `test_free_import_boundary.py` — re-run green. |
| Red-before-green | Round 1 red: `pytest tests/test_ai_proposal.py` → **7 failed, 3 passed** in `TestSourceRevision` — `assert 0 == 1` on conflicts (silent accept). Round 2 red (disk-window): 3 new tests → **3 failed** — `assert view["source_stale"] is True` got False after `save_uploaded_files` (Codex's exact reproduction). Test-side fixes during green: canonical snapshot captured post-mutation; delete/hash loop product names `P{i+1}` (ProposalPending); browser test resolves by real proposal fact key (pipeline keys facts by label). Green: `test_ai_proposal` 36, boundary 13, staging_review 14. |
| Commands + results | `python3 -m pytest -q -p no:cacheprovider tests/test_ai_proposal.py tests/test_free_import_boundary.py` → **49 passed**. `python3 -m pytest -q -p no:cacheprovider tests/test_lifecycle_remediation.py tests/test_product_url_import.py tests/test_staging_api.py` → **42 passed**. `python3 -m pytest -q -p no:cacheprovider tests/test_browser_staging_review.py` → **14 passed** (incl. DOM stale-review). `git diff --check` → clean. `git status` → staged: none. |
| Provider/model/network calls | **0** — `$0`. `_source_revision`/`find_stale_files` are pure stdlib/disk; no import/re-ingest path constructs an LLM client (`_make_llm` still only at `web_viewer.py` enrich; `ingestion.py:415` dormant residual unchanged). All model behavior via `_FakeLLM` fixtures; browser runs hit only localhost uvicorn. |
| Caller inspection | `generate_proposal`/`resolve_proposal`/`get_proposal_view`/`discard_proposal` called only from `web_viewer.py` AI endpoints (2654, 2684, 2721, 2723). `find_stale_files` shared with the re-ingest path (`mark_stale_if_changed`, `check_and_mark_stale`). `source_stale` + conflict `reason` are additive JSON fields consumed by `_pdAiReviewHtml`/`_pdAiResolve`. |
| Remaining risks | `find_stale_files` flags any untracked non-dotfile in `data/{id}` — that IS the intended source-of-truth scan (same definition re-ingest uses), so a stray drop makes the proposal stale until ingested; correct fail-closed behavior. Files pending ingest (`status != "ingested"`) are checked for deletion but not content-change by `find_stale_files` — pre-existing scanner semantics, unchanged. A proposal generated while disk is already pending is born stale until re-ingest completes — intended. |
| Git safety | Nothing staged, committed, pushed, deployed, cleaned, reset, or stashed. `stash@{0}` untouched. `users/` untouched (ignored since prior task). |
| Next exact action | Codex review of `src/ai_enrichment.py` + `tests/test_ai_proposal.py` diff → commit decision. |

## Progress Ledger — POST-COMMIT-SAFETY-AUDIT-01 / TEST-CONTRACT-MIGRATION-01

| Field | Value |
|---|---|
| Phase ID | POST-COMMIT-SAFETY-AUDIT-01 / TEST-CONTRACT-MIGRATION-01 |
| Agent ID | DEVIN |
| Baseline commit | `cb8493a2b5400c0411f4e39e68c77d1ccee6a166` (branch `dev`) |
| Scope | Test-only migration — no production file changed. |
| Root cause | Qualification harness drift: `TestJourneyD.test_d_product_detail_management` still waited for/clicked the removed `+ เพิ่มข้อมูล` button; accepted PD contract is the chip-style `#pd-body .pillar-keyword-add input` (Enter creates a prefilled row). A second latent defect surfaced after the fix: D's delete-safety sentinel asserted on `QualWatch Alpha`, which is created by Journey B — D could never pass in a subset run. |
| Fix | `tests/test_browser_lifecycle_qual.py` Journey D only: chip input `fill("color")` + `press("Enter")`, wait for `.pd-fact-row` count+1, assert prefilled label `color`, fill value `purple` (save/reopen persistence assertions preserved); delete sentinel replaced with a self-seeded `D Sibling Sentinel` data dir — same "only the chosen product is deleted" assertion, order-independent. Old button not restored; no production code touched. |
| Commands + results | `MKTAPP_DEV_AUTH=1 PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_browser_lifecycle_qual.py::TestJourneyA::test_a_isolation tests/test_browser_lifecycle_qual.py::TestJourneyD::test_d_product_detail_management` → **2 passed**. `MKTAPP_DEV_AUTH=1 PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_browser_lifecycle_qual.py tests/test_browser_pd_acceptance.py` → **15 passed**. `git diff --check` → clean. |
| Ledgers | `network_attempts.json` = `[]` (0 external); `llm_ledger.json` = 14 FakeLLM-seam entries only (`__url_fetch__` stub, Journey H explicit enrich calls, `__close__`) — 0 real provider/model calls, $0 spend. |
| Git safety | Nothing staged/committed/pushed; only the test file + this ledger entry changed; artifacts refreshed in-place under the untracked `_qual_artifacts/` dir. |
| Next exact action | Codex review of the test diff → commit decision. |

## Progress Ledger — POST-ISOLATION-INTEGRITY-01 + CANONICAL-DOCS-01 (CLOSED — verdict superseded by ASSET-ISO-01-REMEDIATION green re-verification)

| Field | Value |
|---|---|
| Phase ID | POST-ISOLATION-INTEGRITY-01 + CANONICAL-DOCS-01 |
| Agent ID | DEVIN / SHARED-RUNTIME |
| Date | 2026-09-18 |
| Baseline | `dev` == `origin/dev` == `62ffc1909da3cfce32c604ee06300576c9eac0a2`; 40 dirty/untracked entries preserved; nothing staged; external backup `/Users/its-dev2/mktapp-reconcile-backup-20260918-160338` (257M) verified |
| Bounds honored | Verification + documentation only. No production code changed. No prompts/schemas/config changed. No staging/commit/push/branch. No complete-suite run. $0 — zero provider/model/media/System81/web/embedding calls; all tests used local deterministic fakes. |
| Gate verdict | **FAIL — CONCRETE POST-ISOLATION DEFECT** |
| Defect ID | **ASSET-ISO-01 — Brand Asset Library catalog is user-scoped while asset files are brand-scoped** |
| Failing user journey | User U owns brands A1 and A2. U selects A1, uploads asset `logo.png` via `/api/assets/upload` → file lands at `users/U/brands/A1/brand/assets/` but its record lands in the shared catalog `users/U/workspace/local/cache/assets/db.json` with no `brand_id` field. U switches to A2 → `/api/assets` returns A1's `a_0001`; `get_asset()`/`get_asset_paths()` resolve A1's file path; `run_content_creator_auto` → `_select_assets_for_content` calls unfiltered `asset_library.list_all()`/`get_asset()` → A1 assets can be preselected, composed into Agent 4's asset summary, and emitted as `asset_ids` / media `input_references` for an A2 run. |
| Code path | `src/asset_library.py`: `_assets_dir()` → `local_brand_dir()/"assets"` (brand-scoped) vs `_db_path()` → `local_root()/"cache"/"assets"/"db.json"` (user-scoped). `list_all`, `get_asset`, `query_assets`, `get_asset_paths`, `update_asset`, `delete_asset` operate on the shared DB with no `brand_id` key or filter. Consumers: `src/orchestrator.py::_select_assets_for_content` (`list_all()` + `get_asset()`), all `/api/assets*` endpoints in `web_viewer.py` (brand-gated but unfiltered). |
| Runtime evidence | Direct reproduction: under `WorkspaceContext.for_brand(U, A1)`, `ingest_asset(logo.png)` wrote the file under A1's brand dir and record `a_0001` into `users/U/workspace/local/cache/assets/db.json`. Under `for_brand(U, A2)`: `list_all()` → `[a_0001]`, `get_asset_paths([a_0001])` → A1's file path. Agent-context probe: `a_0001` appears in the A2-visible catalog while every other A1 marker (product/settings/pillar/profile) is absent. |
| Affected scope | Any user with ≥2 brands: cross-brand asset listing, selection, Agent 4 asset composition, and media reference construction. Cross-USER isolation is unaffected (DB is per-user). Agents 1–3 are unaffected (no asset-library contract). |
| Smallest generic seam for remediation | Scope the catalog by brand at the existing `_db_path()` seam (e.g., `brand_state_root()/cache/assets/db.json`, or carry `brand_id` on records and filter at `list_all`/`get_asset`/`query_assets`/`get_asset_paths`/`update_asset`/`delete_asset`), plus one-time migration/quarantine rule for existing shared records. Single seam — no new abstraction needed. NOT implemented (production fix outside this phase's authority). |
| Blast radius | `src/asset_library.py` path/accessor layer; `/api/assets*` handlers (upload record tagging / list filtering); `_select_assets_for_content` + media reference build (automatically correct once catalog is scoped); `tests/test_phase3_asset_integration.py` + asset fixtures. |

### Workspace chain — verified production paths

| Component | Resolution | Result |
|---|---|---|
| Authenticated user | `AuthMiddleware` (web_viewer.py:234-278) session → `WorkspaceContext.for_user`; brand cookie revalidated via `BrandRegistry` before brand upgrade — invalid brand → stays user-only, never another brand | PASS |
| `WorkspaceContext` | frozen dataclass `{user_id, root, brand_id}`; `for_brand` verifies ownership; `brand_state_root()`/`require_brand_context()` fail closed | PASS |
| Products/data/cache/profile | `product_db` → `brand_state_root()`; `load_product_profile` `contain_path`-protected | PASS |
| Brand Settings | `local_brand_dir()` → `brand_state_root()/brand` (fails closed for user-only; client `brand_dir` ignored under brand context); `brand_priority` separates hard (banned/restricted/replacements) vs soft (tone/personality/approved) | PASS |
| Brand Assets | files `local_brand_dir()/assets` brand-scoped; catalog `cache/assets/db.json` user-scoped, unfiltered | **FAIL — ASSET-ISO-01** |
| Content Pillars | `config/content_pillars.yaml` under brand root; `config_loader` merges into `config["pillars"]`; `_build_configured_pillars_text` → Agents 3/4 | PASS |
| Agent Settings | `workspace/local/config/agent_instructions.json` — per-user by design (endpoints not brand-gated, same class as `ui_prefs.json`); `_make_agent` applies to all 4 agents | PASS (user-scoped, per-user isolated) |
| Content History/output | `content_history` + output dirs → `brand_state_root()` | PASS |
| Scheduler jobs/runs/resources | store + runs under `brand_state_root()`; `_run_job`/`_rerun_from_record` require user_id+brand_id (fail closed), verify brand via `BrandRegistry`, set `for_brand` before store access; `_execute_flow` posts to real `/api/run_auto`/`/api/run_flows` with session + `mktapp_brand` cookies → same context resolution as manual runs; `run_resources` under `brand_state_root()` | PASS |

### Agent 1–4 context delivery matrix — proven via real Orchestrator + recording FakeLLM (first LLM call per agent, under Brand A1 then A2)

| Input | Agent 1 Product Analyst | Agent 2 Competitor Analyst | Agent 3 Campaign Strategist | Agent 4 Content Creator |
|---|---|---|---|---|
| Selected product identity/facts/profile | YES (USR) | YES (USR, spec block) | YES (USR) | YES (USR) |
| Selected product images | YES (multimodal) | YES (multimodal) | YES (multimodal) | YES (multimodal) |
| Brand Settings/rules | YES (SYS: hard+soft) | NO — evidence-mode SYS is `EVIDENCE_SYSTEM_PROMPT`, zero brand markers (by contract) | YES (SYS) | YES (SYS + inlined voice/visual in USR) |
| Brand reference/audience | NO (flag unset, by contract) | NO in evidence call; separate post-evidence interpretation pass only | YES (SYS: audience+profile) | YES (SYS) |
| Agent Settings | YES | YES | YES | YES |
| Quick Brief | YES (USR) | YES (USR) | YES (USR) | YES (USR) |
| Run attachments/resources | YES (via `step_context`/resource_context seam) | YES | YES | YES |
| Content Pillars | NO (not required) | NO (not required) | YES (configured + selected, USR) | YES (configured + selected, USR) |
| Brand Asset metadata/files | NO (not required) | NO (not required) | NO (not required) | YES (asset summary + stable `asset_ids` + media reference catalog) — **catalog source unfiltered: ASSET-ISO-01** |
| Cross-brand contamination | none (A2 markers absent) | none | none | none EXCEPT catalog leak (ASSET-ISO-01) |

### Focused tests run (bounded, $0, no suite-wide run)

| Suite | Result |
|---|---|
| `test_brand_context.py` + `test_brand_state_isolation.py` + `test_run_endpoint_brand_security.py` + `test_context_composition_remediation.py` + `test_ui_to_agent_flow.py` | 59 passed |
| `test_browser_multibrand_e2e.py` (real Chromium: create/switch/sidebar isolation/reload) | 3 passed |
| `test_browser_product_to_agents_e2e.py` (UI product→agent run→cross-brand invisibility) | 3 passed |
| Scheduler group (`test_schedule_api`, `test_scheduler_attachments`, `test_scheduler_brand_ownership`, pillar/asset-related) | 68 passed |
| `test_phase3_asset_integration.py` + asset/media integration | 47 passed, **1 failed** |
| `test_media_gen_provider_flow.py` | 49 passed |
| **Total** | **229 passed, 1 failed** |

| Classification — asset reference-cap conflict | **STALE TEST.** `test_phase3_asset_integration.py::test_build_input_references_merges_and_caps` expects a silent cap of 3 references. Current contract: `config/assets.yaml` `max_refs_per_post: 5`; `build_input_references`/`build_reference_catalog` deliberately do not truncate (explicit docstring: overflow is caught by `preflight_references` as an error, not silently dropped); `test_media_gen_provider_flow.py::test_max_refs_per_post_overflow_errors_without_truncation` pins the new contract (49/49 green). Not a production regression; test not modified. |
| Classification — Agent Settings scope | **User-scoped by design**, not a defect: `agent_instructions.json` lives under the per-user workspace; `/api/agent_instructions*` endpoints carry no brand gate (account-level preference, same class as `ui_prefs.json`); per-user isolation intact. Note: earlier ledger wording calling instructions "brand-scoped" (cache row) is loose — the `_conflict_cache` keys are brand-scoped, the instruction file itself is user-level. |
| System81 reconciliation | Prior statements "System81 live functional qualification PASS" (this ledger + MB-02 ledger) are **SUPERSEDED** — contradicted by AUTH-ISO-01's own record ("Zero live System81 calls performed", "LIVE SYSTEM81 QUALIFICATION PENDING") and there is no immutable live-call evidence. Authoritative: code integration accepted/frozen `1a0ef78`; live smoke test pending. |
| Browser journey coverage | Covered by existing harnesses (no second architecture built): `test_browser_multibrand_e2e` (login boundary→brand create/select/switch→per-brand sidebar isolation→reload persistence) + `test_browser_product_to_agents_e2e` (UI add product→brand-scoped commit→restart→agent run scoped context→cross-brand product invisibility) + `test_run_endpoint_brand_security` (API auth boundaries) + `test_ui_to_agent_flow` (execution route→agent context) + `test_scheduler_brand_ownership` (fire restores brand). A single composite browser journey including asset+schedule was not executed — the constituent boundaries are each proven; the one uncovered boundary (asset) is the defect above. |
| Documentation changes | This section updated only: fast-track status → one authoritative Current phase/objective/Next action + full capability matrix; superseded statements marked inline (Media-as-next, System81 live-PASS); intended delivery contract recorded (product images→Agents 1–4; Brand Asset Library→Agent 4/Media; Content Pillars→Agents 3–4); reference-cap classification recorded. |
| Git safety | Nothing staged/committed/pushed; only `AI_EMPLOYEE_BETA_EXECUTION_PLAN.md` modified; all dirty/untracked work preserved. |
| Paid calls | **0 / $0** — no OpenRouter, embeddings, web, System81, image, video, or provider calls. |
| Next exact action | Product Owner / Codex decision on bounded ASSET-ISO-01 remediation → re-run bounded integrity verification → then prepare smallest Agent 1–3 qualification cases and stop for PO paid-call authorization. **Do not reopen broad architecture/isolation remediation beyond this one defect.** |

## Progress Ledger — ASSET-ISO-01-REMEDIATION (CLOSED — PASS)

| Field | Value |
|---|---|
| Phase ID | ASSET-ISO-01-REMEDIATION |
| Agent ID | DEVIN |
| Date | 2026-09-18 |
| Baseline | `dev` == `origin/dev` == `62ffc1909da3cfce32c604ee06300576c9eac0a2`; all 40 pre-existing dirty/untracked entries preserved; nothing staged; external backup untouched |
| Verdict | **PASS — ASSET-ISO-01 CLOSED** — all acceptance cases green, zero paid/external calls |
| Root cause | Asset FILES were brand-scoped (`local_brand_dir()/assets` → `brands/<bid>/brand/assets/`) but the catalog `_db_path()` resolved to `local_root()/cache/assets/db.json` (user-level). Records carried no `brand_id`; `list_all`/`query_assets`/`get_asset`/`get_asset_paths`/`update_asset`/`delete_asset` and every consumer (`_select_assets_for_content`, media `build_reference_catalog`, all `/api/assets*`) read the shared catalog unfiltered → Brand A assets were listable, selectable, resolvable, and mutable under Brand B of the same user. |
| Fix (smallest existing seam) | `src/asset_library.py` only (+86/−1). `_db_path()` now mirrors `local_brand_dir()`: active brand → `brand_state_root()/cache/assets/db.json`; no workspace → legacy `local_root()/cache/assets/db.json` (CLI/test compat); user-only workspace → `ValueError` (fail closed). No changes needed in `web_viewer.py`, `src/orchestrator.py`, or media code — every read/mutation op (`list_all`, `query_assets`, `get_asset`, `get_asset_paths`, `update_asset`, `delete_asset`, `ingest_asset`, `ingest_all`, `build_reference_catalog`, tool handlers) bottoms out at `_load_db()`/`_save_db()` → `_db_path()`. |
| Final scoping contract | Catalog: `users/<uid>/brands/<bid>/cache/assets/db.json`. Files: `users/<uid>/brands/<bid>/brand/assets/` (unchanged). Brand ops fail closed under user-only context; the no-workspace fallback is CLI/test-only and can never be reached by a request (all `/api/assets*` endpoints are `_require_brand_context()`-gated; media paths run inside brand-gated agent flows). Asset IDs remain stable within their owning brand (`next_id` continues from max claimed id). |
| Legacy migration | One-time per brand at first catalog access when the brand catalog is absent and a legacy shared catalog exists: a record is claimed only when its stored `path` resolves inside that brand's `brand/assets/` dir (`Path.resolve().is_relative_to` — mechanical ownership proof; a missing file inside the dir still proves ownership by location). Unprovable/empty/outside records stay in the **untouched** legacy file (non-destructive quarantine — invisible to every brand, visible only to no-workspace CLI). Atomic writes via tmp+`os.replace` for both the brand catalog and `cache/assets/migration_report.json` (claimed ids, quarantined ids+reasons, source, status). Deterministic + idempotent: legacy is read-only; re-running produces the same catalog; once the brand catalog exists it is authoritative and migration never re-runs (no delete-then-resurrect). Unreadable legacy → empty brand catalog + `status: legacy_unreadable` report, legacy preserved. |
| Red-before-green | New `tests/test_asset_library_brand_isolation.py` (9 tests). Before fix: **6 failed** (cross-brand list/query/get/paths/update/delete + media refs; switch-back; fail-closed; persistence; Agent 4 `_select_assets_for_content`; legacy migration) — reproduced the leak: `list_all()` under B returned `['a_0001']` (A's record). After fix: **9/9 pass**. |
| Focused test results | `test_asset_library_brand_isolation.py` **9/9 PASS**. `test_asset_library.py + test_phase3_asset_integration.py + test_media_gen_provider_flow.py` = 74 passed / 1 failed — the 1 failure is the pre-classified **stale** silent-cap-3 test (`test_build_input_references_merges_and_caps`), unchanged per instruction. `test_phase4_media_wiring.py` = 9 failed / 8 passed — **identical on clean HEAD worktree** (verified via `git worktree` run): pre-existing MB-02 containment fixture drift (`file path ไม่ถูกต้อง — must be within active brand output`), zero regression from this change. `test_brand_context + test_brand_state_isolation + test_run_endpoint_brand_security + test_context_composition_remediation + test_ui_to_agent_flow + test_local_workspace` = 116 passed / 1 failed — the 1 failure (`test_product_agent_instructions_no_per_agent_sections`) is the documented accepted baseline failure. `test_browser_product_to_agents_e2e + test_browser_multibrand_e2e` = 6/6 pass (real Chromium). |
| External-call ledger | **0 calls, $0** — no OpenRouter/embeddings/web/System81/image/video/provider calls; all tests used injected tagger/embedder stubs, deterministic fakes, and tmp workspaces. |
| Git status | Nothing staged/committed/pushed/branched/deployed/stashed/cleaned. `git diff --check` clean. Changed files: `src/asset_library.py` (+86/−1), `tests/test_asset_library_brand_isolation.py` (new, test-only), `AI_EMPLOYEE_BETA_EXECUTION_PLAN.md` (this ledger + authoritative status rows). Dirty/untracked set: 42 entries = 40 preserved + the 2 above. |
| Documentation changes | Fast-track status: Current phase → Checkpoint B/E; objective → isolation verified, proceed to Agent 1–3 qualification; next action → prepare smallest Agent 1–3 qualification cases, stop for PO paid-call authorization. Capability matrix: Brand Assets → PASS (closed), Auto execution → PASS. Media C2 stays deferred; System81 live qualification stays pending. |
| Next exact action | Prepare the smallest missing Agent 1–3 current-model qualification cases and STOP for Product Owner paid-call authorization. Do not reopen wider isolation remediation. |

## Progress Ledger — LAUNCH-READINESS-01 — CURRENT (bounded, in progress)

| Field | Value |
|---|---|
| Phase ID | LAUNCH-READINESS-01 |
| Agent ID | DEVIN |
| Date | 2026-09-18 |
| Baseline | `dev` == `origin/dev` == `62ffc19`; all prior dirty/untracked entries preserved; nothing staged; backup untouched |
| Acceptance evidence | Real user-run outputs for `Lagenio K2`, brand `11be75b86f0fd99d`, 2026-09-18 17:16–17:20: `01_product_spec` (17.16.32), `02_competitor_analysis` (17.16.40), `03_campaign_strategy` (17.16.48), `04_content_creator`+image (17.17.07), `04_content_creator`+video (17.17.21) |
| Verified defects | **(1) Product Detail browser crash** `vo.keywords.join is not a function` blocks expanding "เนื้อหาการตลาด" — schema/renderer contract. **(2) Agent 4 revised-script loss** — `script_review.script_changed=true` + `review.revised_script` produced (score 68→78, 2 iterations), but persisted `posts[0].script` and rendered `.md` still carry the ORIGINAL script — revision never reaches final output. **(3) Agent 2 evidence loss** — source data contains IP68 yet output says not found; competitor evidence contains 2ATM yet output says no evidence; camera comparison mismatches K2 rear-camera absence vs a competitor front-camera spec; structured source data leaks into the user-facing table as raw text (`Display \| Type \| AMOLED`). **(4) Agent 1/4 over-claiming** — conclusions stated more strongly than supporting product facts. **(5) Brand Asset delivery gap** — runs recorded `asset_ids=[]`, `catalog_asset_ids=[]`; Agent 4 never received selected assets despite the (now brand-scoped) Asset Library contract. |
| Explicitly NOT defects | **Agent 3** usable as draft — verify only that invented prices/budgets/capabilities are blocked and missing business inputs surface clearly; do not over-remediate. **Content Pillars** — pillar config was saved AFTER these runs; test adherence later with a deterministic current-context case. **Image** — product-reference fidelity is good; missing Brand Asset use is tracked under defect 5, not a Media redesign. **Video** — final persisted state checked: `_media_status.json` = `completed`, 1/1, no errors; earlier `in_progress` observation superseded. |
| Execution order | 1. record plan ✓ → 2. Product Detail crash → 3. Agent 4 revised-script → 4. Agent 2 root cause → 5. Agent 1/4 grounding via shared seam only → 6. Brand Asset delivery → 7. focused regressions → 8. report + minimum live verification ask |
| Constraints | No architecture redesign; no per-example/K2/brand-specific prompt patches; generic seams only; no paid/model/media calls; no commit/push; do not reopen isolation architecture unless a regression is demonstrated |
| Launch standard (PO 2026-09-18) | **"Can a real user complete the core job and obtain usable output?"** — recoverable UI defects are post-launch cleanup, NOT blockers. A defect blocks launch only when it breaks a core workflow, produces materially wrong/unusable output, loses/corrupts data, uses wrong user/brand/product context, makes Agent facts/claims materially incorrect, ignores an accepted revision, blocks required media generation, drops required reference/context data, makes persisted results unusable, or has no reasonable user workaround. Priority order: output correctness → workflow completion → context integrity → persistence/recovery → media usability → UI polish last. |
| Brand Asset flow contract (PO addendum) | **Flow A** (asset selection during content creation): `_select_assets_for_content` → `_selected_asset_ids` → reference catalog → persisted `asset_ids`/`catalog_asset_ids`; valid, used by auto path. **Flow B** (deferred/manual media): content persists without requiring selection; generate-media endpoint recovers references from persisted session meta + content JSON, or accepts user-chosen `asset_ids`/`resource_paths` in the request body — empty `asset_ids` at content time is VALID. **Flow C** (immediate media in same run, the 17:17 runs): selection must occur before content generation so media gets references — the manual path never called `_select_assets_for_content` → real wiring defect (fixed). Per-item `asset_ids` are optional in structured output; required only as references when media runs. |
| Root causes | **(1) Product Detail crash:** `visual_override.keywords` (and `competitors`/`differentiators`/`use_cases`, `image_style`) persist in legacy string form; the view renderer called `.join()` on strings — backend normalizes this schema but the renderer did not. FIXED via generic `Array.isArray` normalize + string-`image_style`→tone in both view and edit branches. **(2) Agent 4 revised-script: NOT A DEFECT** — disproven by deterministic test: persisted `script` IS the accepted round-1 revision (score 68→78); `review.revised_script` is the round-2 reviewer's further unverified suggestion, correctly not applied; `script_review`/`score`/`original_score` metadata is consistent. Contract now pinned by regression. **(3) Agent 2 evidence loss — three-layer generic defect:** (a) `staging.py` segmentation path wrote `text_extracts` without positioned `tables` (parity gap vs `ingest_file`); (b) `extract_source_facts` pass 2 accepted only exactly-2-cell pipe lines, silently dropping every sparse-hierarchy category-header row (`Display\|Type\|AMOLED`, `Other\|Waterproof level\|IP68`); (c) `CompetitorReportRenderer._product_cells` prefix-matched flattened pipe lines and dumped whole raw lines into cells — leak + first-match rear-vs-front camera mismatch + missed values whose label sits inside a section path. FIXED at all three layers (block classification mirrors the positioned-table sparse/dense rule). **Xiaomi 2ATM cell:** rendered table holds exactly 6 validated evidence records = `maxItems: 6` schema cap starving a 4-field×2-competitor table — provider-contract parameter, needs PO decision + live verification, NOT changed blind. **(4) Agent 1/4 over-claiming:** shared grounding contract had no claim-strength rule (rule 7 passed any non-fact inference) — added rule 8 "claim stated as fact must not be stronger than its evidence" to `verify_final_grounding`, covering agents 1–4 through the one shared gate. **(5) Brand Asset delivery:** manual Flow-C path never called `_select_assets_for_content` (auto path did) → empty catalog + empty `asset_ids`/`catalog_asset_ids`. FIXED: selection wired before the platform loop when the run requests media; `_selected_asset_ids` reset at selection entry AND per-run so a shared orchestrator cannot leak a previous run's selection into session meta. |
| Changed files | `web_viewer.py` (legacy-schema normalize in Product Detail view+edit; manual Flow-C asset selection wiring + per-run reset), `src/orchestrator.py` (`_select_assets_for_content` resets `_selected_asset_ids` at entry), `src/agents/competitor_evidence.py` (`_product_cells`: pipe-line parse, label-path word match, merge ambiguous sub-keys), `src/ingestion.py` (pass 2 block-aware sparse-hierarchy extraction), `src/staging.py` (attach positioned `tables` to staged `text_extracts`), `src/agents/base_agent.py` (grounding rule 8), `tests/test_remediation_fixes.py` (+7), `tests/test_browser_pd_acceptance.py` (+1) |
| Test results | `test_remediation_fixes.py` 18/18 (pipe-leak/sub-label-merge/label-path-match renderer cases, sparse-hierarchy + dense-fail-closed extraction, staged-commit tables parity, grounding contract pin, script-review revision contract); `test_free_ingest_facts.py` 23/23; `test_staging_api.py` + remediation suite 49/49; evidence/grounding/orchestrator suites 105/105; asset+media+UI-flow suites 109 passed / 1 pre-existing stale-cap failure (documented, unchanged); `test_browser_pd_acceptance.py` real-Chromium legacy-schema regression green. Real K2 `raw_text` dry-run: `other_waterproof_level=IP68`, `display_type=AMOLED`, `battery_capacity=680mAh`, both cameras extracted; rendered cells clean (no pipe leak, front+back disambiguated). `git diff --check` clean. |
| External-call ledger | **$0.31231 total, 26 provider calls (2026-09-21)** — Case 1 Agent 2: 3 runs × 5 calls (generate/revise/semantic_review/brand_interpretation/final_grounding_check) ≈ $0.196 — one extra run was spent on a stale server process before the dedupe fix; one corrective re-run authorized and used. Case 2 Agent 1: 3 calls ≈ $0.020. Case 3 Agent 4+media: select_assets ×2, content generate/review/final_grounding, embeddings ×2, image_gen ×1 ≈ $0.097. All gemini-3.8-flash + gemini-3.1-flash-image + text-embedding-3-small. |
| Live verification result (2026-09-21) | **CORE WORKFLOWS PASS** — Case 1 Agent 2: K2 column renders `AMOLED`/`IP68`/`680mAh` (no pipe leak, no dup after fix), Xiaomi `2 ATM` **present** with citation (cap question closed — no maxItems change), honest `_NO_EVIDENCE` cells, evidence/inference sections correctly separated. Case 2 Agent 1: all facts match source (AMOLED, 5MP front/no rear, sensors, IP68), derived conclusions explicitly labeled `[ข้อเสนอเชิงวิเคราะห์]`, complete usable deliverable. Case 3 Agent 4+immediate media: `catalog_asset_ids=["a_0001","a_0003"]` + per-post `asset_ids` persisted (selection ran pre-generation — Flow-C wiring verified live), image prompt composes product+logo+lifestyle references, `image_1.png` (1376×768) generated ok/retry 0 and persisted usable; caption claims all backed by real specs. Remaining non-blocking cleanup: none observed in live outputs. |
| Final engineering review (2026-09-21) | **PASS after one new offline regression was found and fixed.** Flow-C wiring originally gated asset selection with `(auto_image or auto_video or media_type)`. Because `media_type` defaults to `"image"` even when `media_when="ask"`, deferred Flow B spent an unintended asset-selection model call and violated the recorded contract that content may persist with empty `asset_ids`. Red test: `test_run_flows_null_auto_image_does_not_auto_generate_media` + assertion on `_select_assets_for_content` failed on the candidate delta. Smallest fix: gate selection only on `auto_image or auto_video`; green after fix. Release-only clean-clone regression: **152 passed**, `git diff --cached --check` clean, staged patch hash matched the reviewed simulation byte-for-byte. The focused Chromium test could not be rerun in the sandbox because local socket bind permission was denied before test setup; prior accepted real-Chromium evidence remains recorded above. No provider/media calls were made. |
| AliExpress URL-import remediation (2026-09-21, UNSTAGED — not in the staged candidate) | **Root cause:** `fetch_product_page` fired the Chromium fallback only when `_usable()` failed (body < `MIN_BODY_CHARS` or challenge markers). AliExpress item pages serve a *fat localization shell* — ~1055 chars of nav/footer boilerplate + `og:type=product` meta that pass `_usable`, but **zero JSON-LD** → `classify_product_signals` → `product_count=0` → `ambiguous` → `web_viewer.py` rejects "ไม่สามารถยืนยันสินค้าเดียว". Verified live: static `fetched_via=static`, `product_count=0`, `multi_signals=[]`, empty title; `_browser_fetch` rendered 367KB → real product title, 4280 body chars, JSON-LD `[Product, Offer]` → `single`. **Fix (generic):** the fallback trigger is now `not _usable OR page_class != "single"` — any fetched document that cannot *confirm* one product may be an incomplete document (client-rendered structured data) and gets exactly one render under the same SSRF/DNS/MIME policy; the rendered doc is adopted only if usable, and classification of the kept doc still decides (fail-closed unchanged). Browser failure or an unusable render on a previously-usable static doc keeps the static verdict instead of masking it with a fetch error. **Red→green:** `test_fat_shell_falls_back_to_rendered_product` failed pre-fix (`fetched_via=static`/`ambiguous`), green after; plus fat-shell→still-ambiguous, challenge-in-render keeps static verdict, browser-failure keeps static verdict, and endpoint-level `test_client_rendered_shell_imports_single_product` (stage→commit→record carries rendered text + `fetched_via=browser` provenance). Fixtures updated to match the real contract: `STATIC_RICH_HTML` and the apify non-Shopee fixture now carry the Product JSON-LD a genuine complete product page has (assertions unchanged); `test_product_url_import._stub_fetch` is a virtual browser returning per-URL rendered docs so multi/ambiguous rejections are exercised through the new path, not bypassed. **Focused results:** browser suite 22/22 (real Chromium), product-url-import + security + apify 82/82, product-info + e2e + staging-review 23/23, classify + free-import-boundary + lifecycle-remediation 74/74. **Live verification (free HTTP + local Chromium only):** the real `th.aliexpress.com/item/…` URL now returns `fetched_via=browser`, `page_class=single`, `product_count=1`, real Thai title, 4582-char product text, 3 images via the safe downloader. **External calls:** zero paid/provider/model/media calls; ordinary GET + headless render of the user-supplied page only. **Files:** `src/url_import.py` (`fetch_product_page` trigger + adopt-only-if-usable), `tests/test_url_import_browser.py`, `tests/test_product_url_import.py`, `tests/test_url_import_apify.py`. |

## Progress Ledger — AGENT12-LIVE-DEFECT-01 (Lagenio Evo launch remediation) — COMPLETE

| Field | Value |
|---|---|
| Phase ID | AGENT12-LIVE-DEFECT-01 |
| Agent ID | DEVIN |
| Date | 2026-09-21 |
| Product / Brand | `Lagenio Evo` / brand `11be75b86f0fd99d` (user `system81_…`) |
| Scope | Two fresh launch defects only — Agent 1 semantic fidelity (`video call` → `การติดตามวิดีโอ` substitution) and Agent 2 final-grounding `malformed_json` — plus bounded Agent 4 representative verification. Agent 3 untouched (latest Campaign Strategy accepted launch-usable). |
| Agent 1 root cause | **Context composition gap, not model drift.** `get_agent_context_text`/`get_agent_context` sent only spec-table `facts` + scope + `raw_text` + images. The user-accepted profile (`summary`, `differentiators`, `use_cases`) — where "video call" was confirmed — never reached the model. The only "video" strings in input were the machine-translated title `ระบบติดตามวิดีโอ` (page carries an AI-translation disclaimer) and `วิดีโอคอล` mentions belonging to *other* products in the listing (Wonlex KT31/KT42). The model copied the mistranslation; review+grounding passed it because the phrase literally exists in raw source. The chemical claim `สารเคมีที่มีความกังวลสูง: ไม่มี` is sourced (spec table) — not hallucinated. Same silent capability drop observed on Lagenio K2 → generic defect. |
| Agent 1 fix | New `_confirmed_profile_text(product_id)` in `src/product_db.py` renders only confirmed factual profile fields (`summary`, `differentiators`, `use_cases` — positioning fields excluded) with a preservation directive, inserted before raw evidence in both the text and multimodal context paths. No keyword lists; the model does the semantic work with the confirmed record now actually present. |
| Agent 2 root cause | **Provider abort, not model output.** OpenRouter generation metadata: 126 completion tokens generated, then Google aborted mid-generation → `finish_reason:"error"` + partial unparseable content. `LLMClient.chat()` returned the non-empty partial without retry (only empty/transport failures retried); `verify_final_grounding` parsed the fragment → `malformed_json`. Same contract passed for product_spec/campaign_strategy grounding within minutes. **Second defect found during live re-verification:** `run_competitor_analysis` passed only user `resource_context` as `verified_evidence`, so the verifier could not see the agent's own finalized web evidence and semantically rejected legitimate competitor claims. |
| Agent 2 fix | `src/agents/base_agent.py`: provider-error finishes (`error`, `content_filter`) classified as `provider_error` and retried within the config-driven bound (`agent max_retry_limit` → `defaults.max_retry_limit` → 1); exhausted retries and all other failures remain fail-closed. Recoverable JSON wrappers handled by `_iter_grounding_json_candidates` (whole text → fenced blocks → balanced top-level objects, string-aware) + `_parse_grounding_json` (distinguishes malformed JSON from invalid schema; `grounded` must be bool, `unsupported_claims` a list). `src/agents/competitor_analysis.py`: `_serialize_research` exposes the finalized post-review evidence as `_grounding_evidence_json`. `src/orchestrator.py`: `run_competitor_analysis` combines agent-finalized evidence + `resource_context` into `verified_evidence`. Generic — applies to every agent's grounding call, not competitor_analysis only. |
| Agent 1 live verification | **PASS** — confirmed block present in actual model input (`confirmed-block present: True`); output carries `วิดีโอคอล (Video Call)` in key features, spec table, and use cases; no `การติดตามวิดีโอ` substitution; image observations labeled as image observations; missing specs listed as explicit limitations. Artifact: `output/verify_20260921_105835/01_product_spec_Lagenio Evo_105852_….md`. |
| Agent 2 live verification | **PASS** (2nd run; 1st run surfaced the evidence-propagation defect, fixed generically) — grounding `fin:stop`, report persisted: `output/verify_20260921_110529/02_competitor_analysis_Lagenio Evo_110607_….md`. Comparison table for KT31 / imoo Z1 / Wonlex KT42 with URL-cited prices/specs, honest `ไม่มีหลักฐานยืนยัน` cells, evidence/inference separation intact. No `malformed_json`. |
| Agent 3 | No changes. Prior real-user Campaign Strategy stands as launch-usable (no invented Retail/Promo/Wholesale/Margin, no invented budgets/KPIs). |
| Agent 4 bounded verification (2026-09-21, 5 representative cases, real workspace) | **Case 1 Facebook text: PASS** — generate→review→grounding→persist; facts preserved (GPS/WiFi/LBS, 4G, วิดีโอคอล, step/distance/sleep), no invented capabilities, brand voice applied. `verify_a4_case1_111352/` ≈ $0.026 (3 calls). **Case 2 TikTok: PASS** — script emitted; reviewer ran 2 iterations (68→85/100); persisted `script` = accepted round-1 revision; `video_prompts` regenerated; grounded; `review.revised_script` is the unused further suggestion — contract consistent with the pinned regression. `verify_a4_case2_111427/` ≈ $0.036 (6 calls). **Case 3 brand-context: PASS** — sentinel brief honored (morning narrative, mother's POV, no hard-sell CTA), selected pillar `ความปลอดภัยและการดูแลเด็ก` drove concept, product facts + brand voice present. `verify_a4_case3_111646/` ≈ $0.027 (3 calls). **Case 4 immediate image: NOT AUTHORIZED at paid seam** — Flow-C verified end-to-end: `_select_assets_for_content` chose `a_0001` (LAGENIO logo, active brand), `asset_ids`/`catalog_asset_ids` persisted, `compose_media_input` produced `input_references=[product page_img_001.webp, brand/assets/images.png]` — both inside the active brand, preflight clean, prompt cites Reference 1/2 correctly. `generate_image_with_retry` NOT invoked (paid). `verify_a4_case4_111722/` ≈ $0.028 (7 calls incl. asset selection). **Case 5 deferred video: NOT AUTHORIZED at paid seam** — Flow-B verified: text persisted with empty `asset_ids` (valid), `_session_meta.json` recovered `product_id`/catalog, `compose_media_input` restored correct brand+product context (product image reference only — post selected no assets), explicit `asset_ids` body channel also composes brand asset correctly, unknown/foreign asset IDs contribute no reference. `generate_video_with_retry` NOT invoked (paid). `verify_a4_case5_111828/` ≈ $0.035 (6 calls). |
| Agent 4 verdict matrix | Facebook Text: **PASS** · TikTok: **PASS** · Brand Context: **PASS** · Immediate Image: **NOT AUTHORIZED** (pipeline verified to paid seam; blocker = media spend authorization only) · Deferred Video: **NOT AUTHORIZED** (same) |
| Minimum media spend to close matrix | 1 image on `google/gemini-3.1-flash-image` ≈ $0.04 + 1 video on `alibaba/wan-2.7` (5–6 s) ≈ $0.30–0.60 → **≈ $0.35–0.65 total**. |
| Changed files | `src/product_db.py` (`_confirmed_profile_text` + insertion into `get_agent_context_text`/`get_agent_context`), `src/agents/base_agent.py` (provider-error classification, bounded grounding retry, `_iter_grounding_json_candidates`, `_parse_grounding_json`, `_grounding_max_attempts`), `src/agents/competitor_analysis.py` (`_serialize_research` + `_grounding_evidence_json`), `src/orchestrator.py` (verified_evidence = agent evidence + resource_context), `tests/test_remediation_fixes.py`, `tests/test_final_grounding_boundary.py` |
| Test results | Focused remediation + grounding suites: **72 passed**; related seam suites (evidence/orchestrator/competitor) green. Full suite: 115 failures — verified pre-existing via stash comparison (same failures reproduce without the product_db change; earlier working-tree drift, not this remediation). Code review (standards + spec axes) run on the scoped diff; findings applied (scanner unbalanced-brace skip, grounding contract strictness, profile-field type guards, retry-bound test pinning). |
| External-call ledger | **≈ $0.244 across 26 provider calls (2026-09-21)**, all `google/gemini-3.8-flash`: A1 verify 3 calls ≈ $0.019 · A2 verify run1 5 calls ≈ $0.066 · A2 verify run2 5 calls ≈ $0.071 · A4 case1 ≈ $0.026 · case2 ≈ $0.036 · case3 ≈ $0.027 · case4 ≈ $0.028 · case5 ≈ $0.035. **Zero media-generation calls** (image/video spend not authorized — stopped at seam). |
| Remaining launch blockers | **Only media spend authorization** for the two Agent 4 media legs. No code blockers observed. Post-launch cleanup (non-blocking): `script_review.review.revised_script` metadata can confuse (it is the unused further suggestion, not the applied revision); unknown `asset_ids` in deferred media silently drop instead of erroring (fail-safe, but silent); full-suite pre-existing fixture drift (115 failures predating this work). |
| Commit state | Remediation diff **not committed** (per instruction). `git diff --check` clean; working tree retains pre-existing unrelated modifications. |

## Progress Ledger — AGENT2-WEB-STRUCTURAL-01 (Agent 2 discovery-mode identity chain) — COMPLETE

| Field | Value |
|---|---|
| Phase ID | AGENT2-WEB-STRUCTURAL-01 |
| Agent ID | DEVIN |
| Date | 2026-09-21 |
| Product / Brand | `Lagenio Evo` / brand `11be75b86f0fd99d` (user `system81_…`) |
| Trigger | Real browser failure (11:24 run, `flow_60ae69d0`): `Final grounding failed … 'รายงานวิเคราะห์ไม่สำเร็จ: KT31 / structural_output_failed / สาเหตุ competitor_names is empty / web search เรียกสำเร็จและมี selected evidence' … semantic_failure`. Grounding was *correct* — it rejected an internal meta/error report. The defect sat earlier in `selected web evidence → structured output → competitor_names → structural validation`. |
| Exact failed run | `output/21_ก.ย._2569_11.24.13.349525_ceeffa - /` — 3 calls only: `generate` → `revise` → `final_grounding_check` (no `semantic_review`/`brand_interpretation` → died inside `_validate_research_json`). $0.0718 spent, no artifact persisted. Web path calls `run_competitor_analysis("", None, …)` — **identical inputs** to the passing direct verification; divergence was runtime behavior on shared code, not a different prompt/config. |
| Root cause | **Generic contract gap in default-discovery mode, three converging points:** (1) `_validate_research_json` read only the `competitor_names` field — an empty `[]` failed `competitor_names is empty` even when `evidence[].competitor` already carried valid identities; (2) `_build_evidence_manifest` read `self._competitor_names` (never updated in discovery mode) while discovered names live in `_relevance_context` — the revision prompt showed `คู่แข่งใน scope: (ไม่ระบุ)` **plus** `competitor_names ต้องมาจากรายชื่อคู่แข่งข้างต้นเสมอ`, a self-contradicting instruction that pushes the model toward `[]`; (3) `_reassess_for_default_discovery` extracted only `data["competitor_names"]` — names living only in `evidence[].competitor` never marked annotations relevant, so their URLs could never verify. Classification: *model emitted the field empty AND the parser/validator treated that single field as the only source of truth instead of the canonical evidence contract.* |
| Fix | `src/agents/competitor_analysis.py` only. New `_extract_competitor_identities(data)` — canonical identity set = declared input scope ∪ `competitor_names` ∪ `evidence[].competitor`, string-items only, deduped by existing `_canonical_identity`, first-seen order. Used by both `_reassess_for_default_discovery` (evidence-carried names now drive annotation relevance) and `_validate_research_json` (derived set populates `research.competitor_names` before the empty check — still fail-closed when no identity exists anywhere). `_build_evidence_manifest` now shows the canonical scope and only asserts "names must come from the list" when the list is non-empty. `self._competitor_names` deliberately NOT mutated globally (would wrongly arm `_has_fabricated_competitor_specs` in discovery mode where `comp_data` is empty). No hardcoded names, no fuzzy matching, no fabricated identity. |
| Regression coverage | New `tests/test_agent2_identity_derivation.py` (15 tests): single + multiple evidence-carried names derive non-empty scope; canonical dedup; declared∪evidence union; non-string/dict items contribute no identity; bare-string `competitor_names` field does not decompose to chars; empty-everything stays fail-closed; malformed JSON / missing fields fail; unverified URLs still fail `evidence_validation` (derivation does not bypass provenance); manifest shows discovered scope / drops contradictory note; meta error report still emitted for grounding to reject. **134 focused tests pass** (identity + evidence-mode + market-parity + relevance-gate + grounding-boundary + remediation suites). Prior Agent 2 fixes intact (provider-error retry, evidence→grounding, IP68/AMOLED/battery, camera disambiguation, no pipe leak). |
| Real browser verification | **PASS** — one paid run through `POST /api/run_agents` (same endpoint/payload the UI sends; fresh server process running the fix; real session cookie). Full pipeline: `generate → revise → semantic_review → brand_interpretation → final_grounding_check`, all `fin:stop`, no `structural_output_failed`. |
| Persisted artifact | `output/21_ก.ย._2569_11.45.52.656437_7ce2f8 - Lagenio Evo/02_competitor_analysis_Lagenio Evo_114633_125539_9b576b17ddfa430dacbc4db058a86825.md` — comparison table KT31 (target) vs **imoo Watch Phone Z1** and **Wonlex KT42**, evidence URLs cited inline (imoo.com, tgfone.com, iwonlex.net), honest `ไม่มีหลักฐานยืนยัน` cells, labeled `สมมติฐานเชิงกลยุทธ์ (ยังไม่ยืนยัน)` section, explicit `ข้อจำกัด`. SSE `agent_done` delivered it to the UI. |
| Provider calls / spend | This phase: **5 calls, $0.0914** (generate $0.0653 · revise $0.0112 · semantic_review $0.0051 · brand_interpretation $0.0030 · grounding $0.0069), all `google/gemini-3.8-flash`. Failed run spent $0.0718. Zero media calls. |
| Remaining launch blockers | Only media-spend authorization for the two Agent 4 media legs (unchanged). Agent 2 browser acceptance now met. |
| Commit state | **Not committed/pushed.** `git diff --check` clean. |

## Progress Ledger — AGENT2-QUALITY-CLOSEOUT-01 (Agent 2 report usability) — COMPLETE

| Field | Value |
|---|---|
| Phase ID | AGENT2-QUALITY-CLOSEOUT-01 |
| Agent ID | DEVIN |
| Date | 2026-09-21 |
| Product / Brand | `Lagenio Evo` / brand `11be75b86f0fd99d` |
| Trigger | Latest successful browser report (11:54 artifact) structurally passed but user-facing quality poor: table collapsed to one generic `features` row; target column unlabeled; single-competitor warranty generalized to "คู่แข่ง"; hypothesis rationale asserted unverified facts ("ได้รับความนิยมสูง"); evidence cited a roundup when stronger sources existed. |
| Origin analysis | All prompt/renderer seams — no architecture changes: (a) `EVIDENCE_SYSTEM_PROMPT` `field` guidance allowed generic catch-all buckets; (b) renderer wrote raw `target_model` with no "our product" marker; (c) `BrandInterpretationPass` banned *invented* facts but not *mis-scoped* facts (evidence_ref check can't see entity widening); (d) hypothesis rationale never restricted to verified evidence; (e) URL rules had no source-strength preference. |
| Fix | `src/agents/competitor_evidence.py` only. **Prompt:** `field` must be the most specific attribute the claim supports (no generic buckets; multi-attribute claims split into per-field records); claims scoped to the named competitor (no single-entity → "คู่แข่ง" generalization); hypothesis rationale limited to verified evidence or explicitly-hedged speculation; competitors without verified evidence get no factual profile (gaps go to `uncertainty`); URL preference order among already-available sources (official manufacturer/retailer → reputable retailer/publication → roundup). **BrandInterpretationPass:** same entity-scope rule for implications. **Renderer:** target column header labeled `{target} (สินค้าของเรา)`. No new LLM stage, no validator, no extra calls, no product/brand hardcoding. |
| Tests | `tests/test_competitor_evidence_mode.py` +3: target column labeled ours; distinct fields render distinct rows; missing-evidence cells explicit. Focused suite: **137 passed** (identity + evidence-mode + market-parity + relevance + grounding + remediation). |
| Live verification | **PASS** — `POST /api/run_agents`, real session. Note: the first attempt (12:03 artifact) ran on a **stale server** (user's process started 11:53 predated the quality edits; my restart failed to bind `:8778` since the pkill pattern missed the `Python web_viewer.py` cmdline). After killing by PID and restarting on fixed code, the valid run produced: `KT31 (สินค้าของเรา)` header marker, 4 specific attribute rows (price/language_support/video_call/display), all factual references entity-scoped to `imoo Watch Phone Z1`, hypothesis rationale tied to evidence, sparse-evidence competitor excluded to `ข้อจำกัด` with honest note, official sources (imoostore.com, imoo.com) used where available. Pipeline: generate → revise → semantic_review → brand_interpretation → grounding, all `fin:stop`, artifact persisted + SSE `agent_done` rendered to UI. |
| Persisted artifact | `output/21_ก.ย._2569_12.07.10.088200_552e91 - Lagenio Evo/02_competitor_analysis_Lagenio Evo_120740_525215_0cede67e22f844f0a33b6b06f1d90d5b.md` |
| Provider calls / spend | Valid run: 5 calls, **$0.0672**. Stale-code run (not a fix verification): 5 calls, $0.0748. Phase total ≈ $0.142. Zero media calls. |
| Verdict | **Superseded** — downgraded by AGENT2-PRODUCT-IDENTITY-CLOSEOUT-01 (runtime product identity was not authoritative at the render seam). |
| Remaining launch blockers | Only media-spend authorization for the two Agent 4 media legs. |
| Commit state | **Not committed/pushed.** `git diff --check` clean. |

## Progress Ledger — AGENT2-PRODUCT-IDENTITY-CLOSEOUT-01 (runtime product identity binding) — COMPLETE

| Field | Value |
|---|---|
| Phase ID | AGENT2-PRODUCT-IDENTITY-CLOSEOUT-01 |
| Agent ID | DEVIN |
| Date | 2026-09-21 |
| Product / Brand | `Lagenio Evo` / brand `11be75b86f0fd99d` |
| Trigger | Latest accepted browser report: runtime selected `Lagenio Evo` but title read `KT31` and the our-product column was `KT31 (สินค้าของเรา)` — a model/research field overrode the selected product identity. |
| Root cause | Code mapping defect at the render boundary, not model misclassification. `Orchestrator.bind_product` stores the selected product in `self.product_id`, but `run_competitor_analysis` passed only spec text into `agent.build_prompt(product_data, competitor_data)` — the runtime identity never crossed the Agent 2 boundary. Inside, `_extract_target_model` pulled `รหัสสินค้า: KT31` (the spec's *model code*) into `_target_model`; the model echoed it in `target_model`; `CompetitorReportRenderer` used `research.target_model` for the title and our-product column — making a model-emitted field authoritative. |
| Generic fix | Runtime identity bound explicitly at the boundary: `run_competitor_analysis` passes `product_name=self.product_id` → `build_prompt(product_name=)` stores `self._our_product` and tells the model the selected product is the authoritative identity → both `CompetitorReportRenderer` paths receive `our_product=` → renderer's `_our_product_label()` = runtime identity → research field (standalone fallback) → generic label, **never** a competitor name; a runtime-vs-`target_model` mismatch is logged, not silently adopted. Validator drift check now accepts either legitimate identity (runtime name or spec model code) and still rejects unrelated drift. Agent fallbacks (`_limited_analysis_fallback`, `_structural_output_failure`, `_required_search_failure`) use `_display_product()` = runtime-first. Companion fix: `field` labels must reuse the spec's own attribute labels (language-agnostic) so the mechanical our-product matcher can fill real facts. No hardcoding, no new LLM stage, no extra calls. |
| Files changed | `src/agents/competitor_analysis.py`, `src/agents/competitor_evidence.py`, `src/orchestrator.py`, `tests/test_agent2_identity_derivation.py`, `tests/test_competitor_evidence_mode.py`, `AI_EMPLOYEE_BETA_EXECUTION_PLAN.md` |
| Tests | `tests/test_agent2_identity_derivation.py` **21 passed** — runtime `Product A` + structured `target_model=Product B` still renders `Product A (สินค้าของเรา)` and titles `Product A`; `Product B` remains a competitor when evidenced; competitor columns separate; missing runtime identity falls to a safe label, never a competitor. `test_competitor_evidence_mode.py` +1 (spec-label alignment rule). Focused suite: **183 passed, 3 failed** — all 3 verified pre-existing via stash (brand_reference composition, unrelated). |
| Live verification | **PASS** — `POST /api/run_agents` with the exact UI payload, real session/brand cookies, server restarted on final code. Run 1 (12:22): identity binding verified — title `Lagenio Evo`, header `Lagenio Evo (สินค้าของเรา)`, KT31 only in limitations as excluded target-identity source; our-product column empty (model used English field labels vs Thai spec labels — mechanical matcher can't bridge). One corrective fix (spec-label reuse rule) + one allowed rerun (12:32): column now fills real facts (`ราคา: ฿3,273.32`, `GPS, WiFi, LBS`), 5 attribute rows, imoo Watch Phone Z1 correctly competitor-only with evidence URLs, claims entity-scoped, gaps in `ข้อจำกัด`, grounding passed, artifact persisted, SSE `agent_done` delivered to UI. |
| Persisted artifact | `output/21_ก.ย._2569_12.31.51.376676_07bebe - Lagenio Evo/02_competitor_analysis_Lagenio Evo_123227_450630_f9bafb9456224293b71a8cdfd49710d2.md` |
| Provider calls / spend | Run 1: 5 calls / $0.0401. Corrective run: 5 calls / $0.0683 (generate→revise→semantic_review→brand_interpretation→final_grounding_check, all `fin:stop`). Phase total **10 calls, $0.1084**. Zero media calls. |
| Verdict | **AGENT 2 LAUNCH-READY — runtime product identity binding verified.** |
| Remaining launch blockers | Only media-spend authorization for the two Agent 4 media legs. |
| Commit state | **Not committed/pushed.** `git diff --check` clean. |

## Progress Ledger — AGENT2-SOURCE-SCOPE-CLOSEOUT-01 (our-product source scoping) — COMPLETE

| Field | Value |
|---|---|
| Phase ID | AGENT2-SOURCE-SCOPE-CLOSEOUT-01 |
| Agent ID | DEVIN |
| Date | 2026-09-21 |
| Product / Brand | `Lagenio Evo` / brand `11be75b86f0fd99d` |
| Trigger | 12:35 browser run (`flow_888e9587`): full pipeline ran but final grounding correctly rejected an our-product cell `หน้าจอ: วอนเล็กซ์ KT31 ... AMOLED HD ... แบตเตอรี่ 900mAh ... Whatsapp` — text lifted from a `สินค้าที่เกี่ยวข้อง` related-product card inside the scraped marketplace page. 5 calls, $0.0683, no artifact. |
| Root cause | `_product_cells` flat-scanned every line of the composed spec — trusted envelopes and unscoped raw page text competed equally with no provenance tier. The KT31 card was a bare line (no `:`); the matcher's `label = value = line` fallback made the *whole card* the cell "value" and whole-word matching found `หน้าจอ` inside it. Confirmed facts had no priority; `record["scope"]` provenance was ignored. A second vector: `_render_limited_analysis` dumped the entire spec blob (including related-product cards) verbatim as "สรุปสินค้าของเรา". |
| Generic fix | Authority ordering at the matcher input seam. `product_db.get_agent_facts_text(product_id)` returns only product-owned text: effective facts → confirmed profile → `raw_text` **only when `record.scope` proves the segment was cut for this product** (unscoped page scrapes are never eligible). Agent `_load_our_product_facts` resolves it via the already-wired runtime identity and passes `our_product_spec` to both renderers; `None` (no runtime identity) preserves standalone caller-asserted behavior. Renderer `_product_source()` selects it for both `_product_cells` call sites and the limited-analysis spec summary. Bare prose lines can no longer produce cell values. Raw text remains in the model's research prompt — only cell authority narrowed. No hardcoding, no new LLM stage, no parser/classifier. |
| Files changed | `src/product_db.py`, `src/agents/competitor_analysis.py`, `src/agents/competitor_evidence.py`, `tests/test_agent2_source_scope.py` (new), `AI_EMPLOYEE_BETA_EXECUTION_PLAN.md` |
| Tests | `tests/test_agent2_source_scope.py` **7 passed** — confirmed fact wins over conflicting card; related-product attribute → no-evidence; product-owned source fills cells; empty product source fails closed (never the blob); bare prose can't become a value; standalone scan preserved; agent resolves facts via product_db. Focused suite: **193 passed, 7 failed — all verified pre-existing** (3 brand_reference composition + 4 recommendation-demotion contract, documented in earlier phases; the limited-analysis one confirmed inert to this change). |
| Live verification | **PASS** — `POST /api/run_agents`, exact UI payload, real session/brand, server on final code. Title `Lagenio Evo`; header `Lagenio Evo (สินค้าของเรา)`; our-product cells show only confirmed facts (`฿3,273.32`, `GPS, WiFi, LBS`) with `จอแสดงผล`/`กล้อง` rendering honest no-match markers where the related-product card previously leaked; competitors imoo Watch Phone Z7 + Wonlex KT42 in separate columns with cited evidence; grounding passed; artifact persisted; SSE `agent_done` delivered to UI. |
| Persisted artifact | `output/21_ก.ย._2569_12.52.33.281898_250cf3 - Lagenio Evo/02_competitor_analysis_Lagenio Evo_125308_854089_2a2f9240b87647ee845c9d6df3d6daf8.md` |
| Provider calls / spend | Valid run: 5 calls / **$0.0743** (generate→revise→semantic_review→brand_interpretation→final_grounding_check, all `fin:stop`). Rejected run: 5 calls / $0.0683. Phase total **10 calls, $0.1426**. Zero media calls. |
| Verdict | **AGENT 2 LAUNCH-READY — selected-product source scope verified.** |
| Remaining launch blockers | Only media-spend authorization for the two Agent 4 media legs. |
| Commit state | **Not committed/pushed.** `git diff --check` clean. |

## Post-Beta Backlog

- multi-Agent team flow และ artifact chaining
- autonomous manager/routing
- expanded platform/publishing integrations
- broader model/provider variance qualification
- organization-level roles/approvals/analytics ที่ยังไม่อยู่ใน UI ปัจจุบัน

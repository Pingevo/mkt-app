# AI Employee Readiness — Beta and Production Specification

เอกสารนี้เป็น source of truth สำหรับตัดสินความพร้อมของ user-facing Agent ทั้งสี่ตัวใน MKTApp

อ่านคู่กับ:

- `AI_EMPLOYEE_PRODUCT_VISION.md` — ภาพผลิตภัณฑ์ วิธีใช้งาน และ value proposition
- `AI_EMPLOYEE_BETA_EXECUTION_PLAN.md` — ลำดับงาน ขอบเขต เวลา และ session handoff สำหรับพา Agent ทั้งสี่ขึ้น Beta
- `AGENT_ORCHESTRATION_SPEC.md` — team flow และ artifact handoff ในอนาคต

## Product boundary

User-facing Agent มีสี่ตำแหน่ง:

1. `product_spec` — นักวิเคราะห์สินค้า
2. `competitor_analysis` — นักวิเคราะห์คู่แข่ง
3. `campaign_strategy` — นักวางกลยุทธ์แคมเปญ
4. `content_creator` — นักสร้างคอนเทนต์

`manager` เป็น internal component สำหรับ intent/product selection ไม่ใช่พนักงานที่นำมาจัดสถานะ Beta ร่วมกับสี่ตัวนี้

ปัจจุบัน wizard เปิดให้เลือกหนึ่ง Agent ต่อ Flow (`MAX_AGENTS_PER_FLOW = 1`). Team flow หลาย Agent เป็น roadmap และไม่เป็น blocker ของ standalone Beta

## User contract verified from current code

### Required choices

- เลือกสินค้า หรือ Auto selection
- เลือก Agent
- กดยืนยันและรัน

### Optional inputs

- Quick Brief
- ไฟล์/resource แนบ
- persistent Agent Settings/preset/custom instructions
- scheduling

### Typed UI options for Content Creator

- Facebook และ/หรือ TikTok
- จำนวนโพสต์ต่อแพลตฟอร์ม 1–20
- รูป วิดีโอ หรือทั้งสอง
- สร้างสื่อทันทีหรือถามก่อน

Agent ต้องทำงานหลักได้เมื่อ Quick Brief ว่าง และต้องไม่บังคับให้ user เขียน prompt เพื่ออธิบาย job description หรือแก้ defect ของระบบ

## Requirement interpretation rule

เอกสารนี้ตัดสิน Agent ในฐานะพนักงานตาม `AI_EMPLOYEE_PRODUCT_VISION.md`:

- role requirement ระบุผลลัพธ์และความรับผิดชอบที่ผู้ใช้คาดหวัง ไม่ใช่ template ของคำตอบ
- default job มีไว้ให้กดรันได้ทันที; เมื่อมี Quick Brief/Settings ต้องยอมให้ model เลือกโครงสร้างที่เหมาะกับคำสั่ง
- ห้ามใช้จำนวนหัวข้อ ชื่อหัวข้อ ลำดับ section ตาราง หรือถ้อยคำตายตัวเป็น Beta gate เว้นแต่ผู้ใช้ร้องขอรูปแบบนั้นโดยตรง หรือเป็น typed UI/downstream contract
- structured schema, evidence manifest และ provenance สามารถคงที่ภายในได้ แต่ user-facing deliverable ต้องไม่ถูกลดทอนเป็น schema presentation เดียว
- acceptance ให้ตัดสิน usefulness, correctness, instruction following, brand/product fit และ effort ที่ลดให้ผู้ใช้ ไม่ใช่ความเหมือนกับ golden wording

## Release classifications

### NOT READY

มี defect ในเส้นทางหลักที่ทำให้งานผิดสินค้า/แบรนด์ ใช้หลักฐานปลอม ละเมิด UI selection, crash, หรือส่งงานที่ใช้ไม่ได้เป็น success

### BETA

เปิดให้ user ใช้งานจริงได้แบบมีขอบเขต โดยต้องครบทั้งหมด:

1. **Zero-prompt default:** เลือกสินค้า + Agent + UI options แล้วได้ default deliverable ที่ใช้ต่อได้
2. **Quick Brief steering:** คำสั่งเสริมเปลี่ยนมุม/รายละเอียดตามที่ขอ โดยไม่ทำลาย identity หรือ UI selection
3. **Persistent customization:** Agent Settings ที่ UI เปิดให้ใช้ส่งถึง model และเปลี่ยนพฤติกรรมจริง
4. **Critical reliability:** ไม่มี critical defect ตามหมวดที่กำหนดด้านล่างใน representative acceptance
5. **Real-model evidence:** มีอย่างน้อยหนึ่ง UI-equivalent run ต่อโหมดที่อยู่ใน Beta scope ด้วย model/config ที่จะเปิดใช้
6. **Honest boundary:** ฟีเจอร์ที่ยังไม่พิสูจน์ถูกปิดหรือระบุ experimental ชัดเจน

Beta ไม่ต้องพิสูจน์ทุก prompt หรือทุก edge case และไม่ต้องผ่าน team-flow roadmap

### PRODUCTION READY

ผ่าน Beta และเพิ่ม:

1. ทุก UI path ที่เปิดให้ user ใช้มี end-to-end acceptance
2. Auto, manual, scheduler, attachments, settings และ failure recovery ทำงานสอดคล้องกัน
3. model/provider/config version ที่ release ผ่าน regression และ side-by-side quality review
4. ไม่มี critical defect เปิดอยู่; medium defect มี owner/mitigation/telemetry
5. cost, latency, retry, usage, artifact และ error trace ตรวจย้อนหลังได้
6. rollback/config fallback พร้อมเมื่อ model/provider เปลี่ยน
7. manual UAT โดยคนที่ทำงานการตลาดจริงผ่าน
8. เอกสาร/UI อธิบาย capability และข้อจำกัดตรงกับของจริง

Production Ready ไม่ได้หมายถึง probabilistic model ไม่มีวันผิด แต่หมายถึงระบบตรวจพบ จำกัดผลกระทบ และ recover ได้ตาม contract

## Critical defects: hard blockers only

ก่อน Beta ให้ hard-block เฉพาะหมวดต่อไปนี้:

- wrong product, brand หรือ selected asset
- fabricated critical product fact, price-as-fact หรือ external source
- explicit UI selection/hard business constraint ถูกละเมิด
- unauthorized external action
- malformed, empty หรือ truncated output ถูกแสดงเป็น success
- unbounded retry/cost หรือไม่มี audit trail ของ paid call
- failed/unverified artifact ถูกส่งต่อหรือบันทึกเป็นงานสำเร็จ

Style, response shape, recommendation quality, จำนวนหัวข้อ และ semantic judgment ที่ไม่อยู่ในหมวดนี้ใช้ LLM/human rubric และ telemetry ไม่เพิ่ม regex/hard validator ใหม่โดยอัตโนมัติ

## Shared Beta contract

### A. Default employee behavior

- Quick Brief ว่างแล้วรู้ default job ของตำแหน่ง
- ใช้ product/brand/assets/settings ที่ระบบเลือกให้ถูก scope
- ได้ user-facing deliverable ไม่ใช่ raw provider payload หรือ internal JSON ที่อ่านไม่ได้
- ถ้าข้อมูลไม่พอ ให้แยก missing/inference หรือขอข้อมูลเท่าที่จำเป็น
- เลือกโครงสร้างและระดับรายละเอียดให้เหมาะกับงาน; default format เป็น fallback ไม่ใช่ข้อบังคับเมื่อ user สั่งต่างออกไป

### B. UI and instruction authority

- UI selection เป็น typed input และชนะ soft prompt defaults
- Quick Brief เป็น optional per-run steering
- Agent Settings เป็น persistent behavior
- hard safety/brand/business constraints ชนะทั้งสองอย่าง
- field ที่มี UI control โดยตรงไม่ควรถูกตั้งซ้ำผ่าน Instructions

### C. Completion and failure

- ตรวจ blank/truncated/malformed output
- repair/retry มีขอบเขต; failure ไม่วนไม่รู้จบ
- ถ้า repair ทำให้คำตอบแย่ลง ต้องเก็บ draft และไม่แสดง invalid result เป็น success
- UI บอก user ได้ว่าเป็น data, model, tool, validation หรือ timeout failure

### D. Evidence and provenance

- product facts trace กลับ product data/image/selected artifact ได้
- external fact ใช้ selected evidence ที่รองรับ claim
- fact, inference, estimate และ recommendation แยกความหมายได้
- artifact เก็บ producer, product scope, sources/assets และ validation/failure state

### E. Observability

- บันทึก model/config, product, Agent, tools, usage, cost, latency, attempts และ result state
- local usage และ Hub reconciliation ไม่หายเมื่อเกิด exception
- ไม่มี secret ใน artifact/log/report

## Frontier-quality evaluation

คำว่า “ทัดเทียมหรือดีกว่า frontier” ประเมินด้วย side-by-side output ไม่ใช่จำนวน unit tests

ต่อ Agent ใช้ representative jobs 3–5 งาน ครอบคลุม:

1. default run ไม่มี Quick Brief
2. UI options ที่ Agent นั้นรองรับ
3. Quick Brief ที่เปลี่ยนมุมงานอย่างมีสาระ
4. persistent setting หนึ่งแบบ
5. ข้อมูลไม่พอหรือขัดแย้งหนึ่งแบบ

ส่งสินค้า/ไฟล์/โจทย์เดียวกันให้ MKTApp และ frontier model baseline แล้ว blind-review ตาม usefulness, factuality, instruction/UI-option following, brand/product/asset fit และ effort ที่ user ต้องใช้

Beta ผ่านเมื่อไม่มี critical defect และผลงาน MKTApp ไม่ด้อยกว่าอย่างมีนัยสำคัญในงานหลัก พร้อมได้เปรียบด้าน brand/product context อย่างเห็นได้จริง

ห้ามขยายชุดก่อน Beta จากความผิดพลาดด้าน style รายครั้ง ใช้ชุดเดิมแบบ versioned เพื่อให้มีเส้นจบ

## Agent 1 — Product Analyst (`product_spec`)

### Default job

เปลี่ยน raw product data และรูปของสินค้าที่เลือกเป็น product brief/spec ที่ทีมใช้ต่อได้ โดยแยก fact, visible-from-image observation, inference และ missing/conflict

### Beta scope and pass criteria

- single product จาก text/file และ text + image
- Quick Brief ว่างแล้วสร้าง default product deliverable
- Quick Brief เปลี่ยนระดับรายละเอียด/กลุ่มผู้อ่านได้
- persistent tone/must/forbid settings ทำงาน
- product identity ถูกต้องและ critical specs ไม่ถูกสร้างจากความเดา
- image claim จำกัดสิ่งที่มองเห็นหรือระบุ inference
- missing/conflicting data แสดงชัด

### Production Ready additions

- multi-product/catalog scoping และรุ่นชื่อคล้ายผ่าน end-to-end
- provenance ของ critical spec ระบุ text/image/inference
- attachments, Auto selection, scheduler และ stale product data paths ผ่าน
- output renderer ยืดหยุ่นตาม Quick Brief โดย internal artifact ยัง stable
- frontier side-by-side ผ่านชุดเต็มและ manual UAT

### Current classification

**BETA PASS — selected-product data scope.** Qualification ล่าสุด `20260831_160709` ผ่านทั้ง zero-prompt และ Quick Brief + Agent Settings ด้วยโมเดลจริง ผลลัพธ์ถูกสินค้าและใช้ต่อได้ โดยรอบนี้ไม่ได้ส่ง product image เข้า model จึงยังไม่ถือเป็นหลักฐานใหม่ของ image-derived claim path

## Agent 2 — Competitor Analyst (`competitor_analysis`)

### Default job

ค้นและวิเคราะห์คู่แข่งที่ตรงกับสินค้า/category/geography แล้วเปลี่ยน selected evidence เป็นข้อสรุปทางการตลาด ไม่ใช่ URL dump

### Beta scope and pass criteria

- standalone จาก product context ที่เลือก
- live web discovery → relevant/selected evidence → analysis
- Quick Brief จำกัดคู่แข่ง มุม หรือ geography ได้
- persistent source/evidence preferences ทำงาน
- target product ไม่เปลี่ยนรุ่น/category
- final fact ผูกกับ selected source ที่เกี่ยวข้อง
- discovery result ที่ไม่เกี่ยวข้องไม่เข้า final evidence
- uncertainty/missing evidence แสดงตรงไปตรงมา
- user ได้ analysis ที่ใช้ต่อได้ ไม่ใช่ JSON/search dump อย่างเดียว

### Production Ready additions

- geography/currentness/source ranking ผ่านหลายหมวดสินค้า
- conflicting sources และ unavailable/out-of-stock listing แสดงสถานะถูกต้อง
- Auto/manual/scheduler/attachments paths ผ่าน
- flexible user-facing renderer แยกจาก structured internal research artifact
- frontier side-by-side และ manual research UAT ผ่าน

### Current classification

**LIMITED BETA PASS — default discovery และ explicit competitor analysis.** Qualification จริงรอบล่าสุด (`beta_rerun_a2_20260901_033048`) สร้าง analysis ที่ใช้ต่อได้ ระบุคู่แข่ง `imoo Watch Phone Z1`, `imoo Watch Phone Z7`, `myFirst Fone R1c` พร้อม evidence URLs ที่เกี่ยวข้อง แยก fact, inference และ missing evidence ชัดเจน ทำตาม Quick Brief (เน้นราคาและฟีเจอร์ GPS tracking) ไม่แต่ง critical facts ผ่านด้วย `google/gemini-3.5-flash`, 2 paid calls (generate + repair), ค่าใช้จ่าย `$0.176891`, Hub delivery COMPLETE (2/2)

## Agent 3 — Campaign Strategist (`campaign_strategy`)

### Default job

สร้างกลยุทธ์แคมเปญจาก product/brand/business/competitor context ที่มี โดยแยก observed facts, strategic hypotheses, recommendations และข้อมูลที่ต้องยืนยัน

### Beta scope and pass criteria

- product-only default plan โดย Quick Brief ว่าง
- selected competitor evidence/source-integrity pressure
- missing financial/baseline context โดยไม่รับประกันผล
- live-web current competitor pricing เฉพาะเมื่อเปิด capability นี้
- Quick Brief เปลี่ยน campaign emphasis/constraints ได้
- persistent must/forbid/budget/discount settings ทำงาน
- default run ใช้งานได้และไม่ crash
- product identity และ hard business constraints ถูกต้อง
- external fact/price มี selected evidence
- recommendation/estimate ไม่ถูกนำเสนอเป็น fact หรือ guarantee
- user request ได้รับคำตอบ ไม่ถูก validator/repair ทำให้ safe-but-useless
- หาก live-web อยู่ใน Beta scope ต้องมี green UI-equivalent real-model run หลัง code ล่าสุด

### Production Ready additions

- response shape ยืดหยุ่นตาม Quick Brief ขณะที่ internal strategy artifact stable
- product-only, evidence-backed, financial constraints, conflicts และ web paths ผ่าน side-by-side suite
- graceful draft/failure recovery และ observability ผ่านทุก execution path
- Auto/manual/scheduler/attachments และ future artifact handoff ผ่าน
- manual strategist UAT และ model-change regression ผ่าน

### Current classification

**LIMITED BETA PASS — context-grounded strategy, live-web not yet proven.** Qualification จริงรอบล่าสุด (`beta_rerun_a3_20260901_034644`) สร้างกลยุทธ์แคมเปญที่ใช้ตัดสินใจได้จริง มี campaign idea, target audience, channels, KPIs พร้อมแยก fact/estimate/uncertainty ชัดเจน ทำตาม Quick Brief (วางแคมเปญเปิดตัว LAGENIO K2) ไม่แต่ง critical facts ราคา งบประมาณ หรือเป้าหมายตัวเลข ใช้ product facts ถูกต้อง ผ่านด้วย google/gemini-3.7-flash, 2 paid calls (generate + repair), ค่าใช้จ่าย $0.029765, Hub delivery 2/2 ยืนยัน ส่วน live-web เป็น capability แยกและต้องพิสูจน์เฉพาะเมื่อจะเปิดให้ user

## Agent 4 — Content Creator (`content_creator`)

### Default job

สร้างโพสต์ตาม platform, count และ media controls ที่ UI เลือก โดยใช้ product/brand/assets/history และ optional campaign/competitor context

### Beta scope and pass criteria

- Facebook และ TikTok ตาม UI selection
- 1–20 posts ต่อ platform ตาม UI
- image, video หรือ both ตาม UI
- auto-generate media หรือ ask-before-generate ตาม UI
- Quick Brief ว่างแล้วสร้าง default content ที่ใช้ได้
- Quick Brief เปลี่ยน concept/tone/emphasis โดยไม่ override platform/count/media
- persistent Agent Settings และ content history ทำงาน
- จำนวนโพสต์/platform ตรง UI
- caption/script/hashtag/media prompt ตรง platform contract
- product/brand claims ไม่แต่งจากข้อมูลที่ไม่มี
- asset ids และ media mode ถูก scope
- multi-post output ไม่ซ้ำอย่างมีนัยสำคัญ
- ask-before-generate ไม่สร้าง media โดยไม่ได้รับอนุญาต
- malformed JSON/media failure ไม่ถูกแสดงเป็น success

### Production Ready additions

- platform/media constraints ครบทุก option ที่ UI เปิด
- generated asset ผ่าน duration/aspect/resolution/capability validation จริง
- factual/safety claims, stale artifacts และ conflicting context มี mitigation
- concurrency/history/scheduler/attachments ผ่าน end-to-end
- frontier side-by-side, content-team UAT และ downstream publishing handoff ผ่าน

### Current classification

**LIMITED BETA PASS — Facebook single-post text + image prompt, other platforms/media not yet proven.** Qualification จริงรอบล่าสุด (`beta_rerun_a4_20260901_035324`) สร้าง 1 โพสต์ Facebook ที่มี concept, caption, hashtags, image prompt ใช้ product facts ถูกต้อง (AMOLED 1.78", กล้อง 5MP, GPS, Heart Rate, SpO2) ไม่แต่งราคา/งบ/KPI ทำตาม Quick Brief (ว่าง) ผ่าน review ด้วย google/gemini-3.7-flash, 2 paid calls (generate + review), ค่าใช้จ่าย $0.038109, Hub delivery 1/2 ยืนยันโดย local accounting ครบ ส่วน TikTok, multi-post, actual image generation, และ video เป็น capability แยกและต้องพิสูจน์เฉพาะเมื่อจะเปิดให้ user

## Manager — internal component

Manager ไม่เป็น user-facing Beta Agent. ขอบเขตปัจจุบันคือ parse intent/resolve product สำหรับ Auto paths และคืน structured selection/error โดยห้ามเปลี่ยน UI selection หรือเพิ่ม Agent โดย user ไม่รู้

เมื่อ team flow เปิดใช้ Manager อาจเสนอ flow template ได้ แต่ไม่ควร route แบบ autonomous โดยอัตโนมัติใน v1

## Test strategy with a stopping rule

### Offline tests

ใช้ FakeLLM/mocks สำหรับ wiring, typed UI options, settings/Quick Brief propagation, schema, hard constraints, bounded failure และ observability

### Real-model acceptance

ใช้เฉพาะ representative jobs ที่ versioned แล้ว ไม่สร้าง paid case ใหม่จากทุก output variation. หนึ่ง logical run + bounded repair ต่อ job เพียงพอสำหรับ Beta; production regression อาจเพิ่ม variance runs ตามความเสี่ยง

### Manual UAT

Product Owner/ผู้ใช้สายงานตรวจ output จริงจาก UI และ side-by-side baseline โดยตอบว่า “เอาไปใช้ต่อได้หรือไม่” ไม่ใช้ heading count เป็นตัวแทนคุณภาพ

### Stop conditions

- critical defect → หยุด release scope นั้นและแก้ root cause
- medium/style defect → บันทึก post-Beta ไม่เพิ่ม hard validator โดยอัตโนมัติ
- offline green แต่ model behavior ยังไม่พิสูจน์ → รันเฉพาะ representative real-model gate ที่ขาด
- capability ที่ยังไม่ผ่านอาจปิด/ติด experimental โดยไม่บล็อก Agent ทั้งตัว

## Current evidence snapshot

- Latest all-Agent qualification: `data/all_agents_beta_qualification/20260831_160709/`
- Latest Agent 2 qualification (final): `data/all_agents_beta_qualification/beta_rerun_a2_20260901_033048/`
- Latest Agent 3 qualification (final): `data/all_agents_beta_qualification/beta_rerun_a3_20260901_034644/`
- Latest Agent 4 qualification (final): `data/all_agents_beta_qualification/beta_rerun_a4_20260901_035324/`
- Offline suite: 776 passed, 0 failed
- Paid usage: `$0.229751` over 12 requests (all-Agent) + `$0.176891` Agent 2 final + `$0.029765` Agent 3 final + `$0.038109` Agent 4 final
- Agent 1: zero-prompt และ Quick Brief + Settings ส่งงานที่ใช้ได้
- Agent 2: **LIMITED BETA PASS** — default discovery และ explicit competitor analysis เปิดใช้ได้
- Agent 3: **LIMITED BETA PASS** — context-grounded campaign strategy ผ่าน, live-web research ยังไม่ถูกพิสูจน์
- Agent 4: **LIMITED BETA PASS (Facebook single-post + image brief)** — 1 post, caption, hashtags, image prompt ไม่แต่งราคา, review ผ่าน; TikTok/multi-post/actual image gen/video ยังไม่ถูกพิสูจน์
- Stage B video ไม่ได้เริ่ม
- Hub reconciliation จาก qualification runner ยังใช้ตัดสินไม่ได้ เนื่องจาก collector snapshot เกิดก่อน async flush เสร็จ ต้องแก้ measurement ก่อนตรวจซ้ำ

Automated tests เป็นหลักฐาน reliability ของ code path ไม่ใช่ใบรับรองคุณภาพ frontier model output

## Release decision table

| Agent | Beta today | Production Ready today | Honest user-facing label |
|---|---|---|---|
| Product Analyst | **Yes — selected-product data scope** | No | Beta — Product Analyst |
| Competitor Analyst | **Yes — Limited Beta (default discovery + explicit)** | No | Limited Beta — Competitor Analyst |
| Campaign Strategist | **Yes — Limited Beta (context-grounded strategy)** | No | Limited Beta — Campaign Strategist (live-web research not yet proven) |
| Content Creator | **Yes — Limited Beta (Facebook single-post + image brief)** | No | Limited Beta — Content Creator (Facebook text + image prompt only; TikTok/multi-post/image gen/video not proven) |

ไม่มี Agent ตัวใด Production Ready จากหลักฐานปัจจุบัน และห้ามใช้จำนวน unit tests เพียงอย่างเดียวเปลี่ยนสถานะนี้

## Agent 2 — Known Limitations (post-Beta feedback)

- **Provenance check ตรวจได้แค่ URL อยู่ใน evidence set ไม่ใช่ semantic grounding** — semantic grounding เป็นความรับผิดชอบร่วมของ model และการประเมินคุณภาพ ไม่สร้าง regex gate เพิ่ม
- **ถ้อยคำอย่าง "ถนอมสายตา"** เป็น marketing language ที่ model ใส่เอง ไม่ใช่ factual claim แต่อาจต้องปรับในภายหลัง
- ห้ามสร้าง regex หรือยิงทดสอบ Agent 2 เพิ่มเพื่อแก้ known limitations เหล่านี้

## Agent 3 — Known Limitations (post-Beta feedback)

- **Live-web research ยังไม่ถูกพิสูจน์** — model เลือกใช้ context ที่มีแทนการค้นเว็บ เมื่อ context เพียงพอ web_search: true หมายถึงอนุญาตให้ใช้ ไม่ได้หมายความว่าต้องเรียกทุกงาน
- **ไม่บังคับ tool call เมื่อ context เพียงพอ** — การบังคับให้เรียก web ทั้งที่ไม่จำเป็นจะเพิ่มต้นทุนและอาจลดคุณภาพ
- ห้ามยิง Agent 3 เพิ่มในรอบ Beta นี้

## Agent 4 — Known Limitations (post-Beta feedback)

- **พิสูจน์เฉพาะ Facebook single-post + caption + hashtags + image prompt** — ยังไม่ได้พิสูจน์ TikTok, multi-post, actual image generation, หรือ video
- **Hub delivery unconfirmed บาง request** เนื่องจาก flush timeout ของ collector แต่ local accounting ครบ
- ถ้าต้องการเปิด TikTok + multi-post + actual image/video ตาม UI ต้องยิงเคสเพิ่มอีกครั้ง
- ห้ามยิง Agent 4 เพิ่มในรอบ Beta นี้

## Definition of Done

Agent ถือว่า Production Ready เมื่อ:

- shared และราย-Agent Production additions ผ่าน
- ทุก capability ที่ UI เปิดผ่าน end-to-end real-model acceptance
- blind frontier comparison และ manual UAT ผ่าน
- critical defects เป็นศูนย์
- failure/cost/artifact/rollback พร้อมใช้งานจริง
- user กดรัน default job ได้โดยไม่ต้องเขียน prompt และ optional controls/settings/Quick Brief ทำงานตรงตาม UI

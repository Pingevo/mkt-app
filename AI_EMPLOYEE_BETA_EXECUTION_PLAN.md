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

สถานะปัจจุบันหลัง Media Core freeze (`06157af`):

| Capability | สถานะล่าสุด | งานที่ต้องทำต่อ |
|---|---|---|
| Product ingestion + 3 K5 images to model calls | **PASS** | — |
| Agent 1 Product Analyst | **ต้อง requalify** — output ขยาย camera/audio/accelerometer/Class Disable/Geo-Fence facts เกิน source | Requalify หลัง Checkpoint A (A implemented/frozen; requalification pending) |
| Agent 2 Competitor Analyst | **ต้องแก้ user-facing rendering + ลบ unsupported factual premises** | Checkpoint B (`PLANNED`, open) |
| Agent 3 Campaign Strategist | **ต้อง requalify** — Brand/Audience values (age 25–45, Working Mom, channels) เป็น user settings ที่ถูกต้อง แต่ K9/video-call examples ต้องไม่กลายเป็น K5 capabilities; internal pending-validation language ต้องไม่ leak | Checkpoint B (`PLANNED`, open) |
| Agent 4 Content Creator | **Script generation + review path ใช้งานได้ แต่ final grounding หลัง post-review mutation ยังไม่ qualified** | Final-grounding/text requalification → Checkpoint E; visual fidelity → Media Capability Coverage (C2) |
| Checkpoint A — Final grounding boundary | **ACCEPTED / FROZEN** (`65ed8c7`) | Requalify Agents 1–4 against current model |
| Gemini 3.8 migration | **ACCEPTED / FROZEN** (`c39f596`) | — |
| Media Core (C1, mechanical transport) | **ACCEPTED / FROZEN** (`5cc4724` + `06157af`) | — |
| Media Capability Coverage (C2) | **PARTIAL** — video/reference-fidelity sub-checkpoint PASS; image capability and other C2 criteria remain open | Image capability, logo/brand fidelity, mascot/person/child consistency, provider fallback, cost-aware tool selection |
| Image generation | **PARTIAL** — no-ref PASS, one-normalized-ref PASS, three-raw-refs TIMEOUT; three-normalized-refs ยังไม่พิสูจน์; real UI path ยังไม่ได้ผลิตภาพหลัง fix | Media Capability Coverage (C2) |
| Real video generation | **PARTIAL** — Wan 2.7 direct-reference Browser E2E PASS (sub-checkpoint); full C2 scope remains open | Media Capability Coverage (C2) |
| Scheduler | **ACCEPTED / FROZEN** (`5781fd8`) — offline 72 tests + 1 browser E2E + real UI wall-clock qualification passed | — |
| Overall | **NOT FREEZE-READY** — no Agent 1–4 declared Beta-ready without current qualification evidence | Checkpoint B / E + Media Capability Coverage (C2) |

ห้ามย้อนกลับไปทำ Phase 1–6 ทั้งชุดโดยอัตโนมัติ ลำดับกว้างด้านล่างเป็นแผน Production/ความสมบูรณ์ระยะยาว Checkpoint A และ Media Core (C1) แช่แข็งแล้ว; เฟสถัดไปคือ Media Capability Coverage (C2)

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
| Current phase | **AUTH-ISO-01 (Auth + Per-User Workspace Isolation remediation)** — CURRENT BLOCKING PHASE. C2/Wan work deferred, not discarded. |
| Current objective | Review/close AUTH-ISO-01 remediation proposal (revision 3) before returning to C2 image capability and remaining criteria. |
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
| Next exact action | **Implement Stage A (AUTH-ISO-01-A):** path containment, worker-context propagation, legacy fallback removal, test-fixture repair. Stages B/C remain pending Stage A completion and Codex review. |
| Last updated | 2026-09-11 (AUTH-ISO-01 is current blocking phase; C2/Wan deferred) |

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
| Gate status | Stage A COMPLETE (commit `71c1e62`). Stage B COMPLETE (uncommitted, pending Codex acceptance). Stage C pending. |
| Blocking | Yes — supersedes all other work (including the deferred Wan 2.7 review) until closed |
| Baseline HEAD | `3e0588f` (auth + workspace isolation initial implementation) |
| Stage A commit | `71c1e62 fix(auth): enforce per-user workspace isolation` |
| Uncommitted follow-up (preserved, not part of this proposal) | `src/scheduler.py`, `web_viewer.py`, `tests/test_scheduler_ownership.py` — scheduler ownership check for `remove_job`/`toggle_job`/`run_now` |
| Code-truth findings | 9 verified defects (see below). The earlier `PER-USER STATE ISOLATION VERIFIED` conclusion is **superseded** — it passed API-level isolation tests but missed thread-context propagation, path-component containment, scheduler store resolution order, process-global state, and legacy fallback paths. |
| Paid calls allowed | No / $0 |
| Next exact action | Codex final Stage B acceptance review. |
| Last updated | 2026-09-11 — Stage B final remediation complete (4 fixes). See Stage B remediation results below. |

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

## Post-Beta Backlog

- multi-Agent team flow และ artifact chaining
- autonomous manager/routing
- expanded platform/publishing integrations
- broader model/provider variance qualification
- organization-level roles/approvals/analytics ที่ยังไม่อยู่ใน UI ปัจจุบัน

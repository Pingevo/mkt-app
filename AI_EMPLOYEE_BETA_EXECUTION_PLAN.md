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

สถานะปัจจุบันหลัง remediation checkpoint `f50ec8e`:

| Capability | สถานะล่าสุด | งานที่ต้องทำต่อ |
|---|---|---|
| Product ingestion + 3 K5 images to model calls | **PASS** | — |
| Agent 1 Product Analyst | **ต้อง requalify** — output ขยาย camera/audio/accelerometer/Class Disable/Geo-Fence facts เกิน source | Requalify หลัง Checkpoint A |
| Agent 2 Competitor Analyst | **ต้องแก้ user-facing rendering + ลบ unsupported factual premises** | Checkpoint A + B |
| Agent 3 Campaign Strategist | **ต้อง requalify** — Brand/Audience values (age 25–45, Working Mom, channels) เป็น user settings ที่ถูกต้อง แต่ K9/video-call examples ต้องไม่กลายเป็น K5 capabilities; internal pending-validation language ต้องไม่ leak | Checkpoint A + B |
| Agent 4 Content Creator | **Script generation + review path ใช้งานได้ แต่ final grounding หลัง post-review mutation ยังไม่ qualified** | Checkpoint A + C |
| Image generation | **PARTIAL** — no-ref PASS, one-normalized-ref PASS, three-raw-refs TIMEOUT; three-normalized-refs ยังไม่พิสูจน์; real UI path ยังไม่ได้ผลิตภาพหลัง fix | Checkpoint C |
| Real video generation | **NOT QUALIFIED** | Checkpoint C |
| Scheduler | **NOT QUALIFIED** — unit tests ไม่พอพิสูจน์ UI save → scheduled fire → execution → history | Checkpoint D |
| Overall | **NOT FREEZE-READY** จนกว่า Checkpoints A–E ผ่านครบ | — |

ห้ามย้อนกลับไปทำ Phase 1–6 ทั้งชุดโดยอัตโนมัติ ลำดับกว้างด้านล่างเป็นแผน Production/ความสมบูรณ์ระยะยาว งานเร่งด่วนตอนนี้คือ Checkpoints A–E

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

Separate machine-readable status from user-facing language.

- Agent 2: keep verified/unverified/inference state internally; replace internal phrases ("ค่าที่จับคู่ได้", "evidence", "inference/recommendation") with concise natural Thai; omit non-informative rows or use a short natural missing-data note; remove concrete premises whose supporting evidence fails validation.
- Agent 3: preserve financial/operational safety internally; use one concise Thai readiness note only where cost-bearing mechanics exist; do not repeat "Pending financial/operational validation" on every item; present unapproved promotions as optional ideas; do not apply financial disclaimers to qualitative recommendations.
- Tests must inspect complete rendered output, not merely search for safety keywords.

### Checkpoint C — Media completion

1. Prove three normalized K5 references at the provider boundary.
2. Record original sizes, normalized sizes, total request size, duration, provider response, and generated file.
3. Run one real image through the actual UI manual-generation path: elapsed status visible; per-image error visible if it fails; final status not stuck; one real output image exists and is displayed; generated media cost is recorded.
4. Do not generate two images when one representative image is sufficient.
5. After Agent 4 final script is grounded, generate one representative real video and verify saved file, status, duration/model parameters, cost, and UI visibility.

### Checkpoint D — Scheduler qualification

- UI flow serialization into schedule save; one-time job save/list; recurring job save/list; invalid schedule returns error and is not persisted; toggle off/on; deletion; server restart reload; missed job and stuck-run recovery; manual run-now; history and rerun; failure status; full parity of selected product, Agent, Quick Brief, Agent Settings, platform, content count, media mode, attachments/resource references, and auto/manual media consent.
- Real representative proof: one low-cost scheduled one-time flow with media generation OFF; schedule for near-future time and let APScheduler fire it naturally; verify UI/API save → registered next_run → timed fire → real Agent output → run history → cost/trace; confirm scheduled Agent receives the same runtime contract as manual flow; delete test schedule after preserving evidence.

### Checkpoint E — Qualification and test rule

1. Direct unit tests for the changed seam.
2. Affected integration groups.
3. Read every final persisted output completely.
4. One representative paid UAT per Agent only after offline tests pass.
5. One real image and one real video only.
6. One real scheduled run.
7. Run the full suite once, immediately before the final freeze decision, because shared production code changed.

Do not repeatedly run the full suite after small edits. Do not accept a run based only on finish_reason=stop, call counts, or absence of one previously observed phrase. For every real UAT, compare the complete final output against the exact runtime inputs and classify every questionable statement. No remediation is allowed during a qualification run. Preserve failed runs as immutable evidence.

## Progress Ledger

อัปเดตตารางนี้ทุกครั้งที่ Devin/Codex จบงาน ไม่สร้าง progress report แยกกระจัดกระจายหากไม่จำเป็น

| Field | Current value |
|---|---|
| Current phase | Remediation cycle — Checkpoints A–E |
| Current objective | Generic final-output truth boundary + user-facing presentation + media completion + scheduler qualification + final qualification |
| Completed | Checkpoint commit `f50ec8e` pushed to `origin/dev`: post-review mutation seam (source_context to script_reviewer), Agent 2 fail-closed drop of unverified evidence_based recommendations, streaming cost extraction test replacement, image probe diagnosis (3 probes), UI elapsed timer, image auto-resize |
| Evidence | Image probes: `evaluation_artifacts/image_probe_20260908_092915/`; Final Agent 4 UAT: `evaluation_artifacts/final_agent4_uat_20260908_094512/`; Original failed UAT preserved: `evaluation_artifacts/real_uat_20260908_083822_FAILED_PARTIAL/` |
| Blocking owner decision | ไม่มี — Product Owner อนุญาต paid testing และ autonomous remediation ผ่าน Checkpoints A–E |
| Paid calls allowed now | Yes — สำหรับ Checkpoint C (1 image + 1 video), Checkpoint D (1 scheduled run), และ Checkpoint E (1 UAT ต่อ Agent) |
| Next exact action | Checkpoint A — implement generic final-output truth boundary for all Agents 1–4 |
| Last updated | 2026-09-08 |

## Post-Beta Backlog

- multi-Agent team flow และ artifact chaining
- autonomous manager/routing
- expanded platform/publishing integrations
- broader model/provider variance qualification
- organization-level roles/approvals/analytics ที่ยังไม่อยู่ใน UI ปัจจุบัน

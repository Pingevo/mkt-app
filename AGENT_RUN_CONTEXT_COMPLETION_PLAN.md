# Agent Run Context Completion Plan

## 1. เป้าหมาย

ทำให้การแนบไฟล์สำหรับ agent เดี่ยวสมบูรณ์ทั้ง Manual และ Auto mode โดยทุกจุดที่ LLM ใช้ตัดสินใจหรือสร้างผลลัพธ์สามารถเข้าถึง Quick Brief และ resources ของงานชุดเดียวกันได้อย่างสม่ำเสมอ

งานนี้ต่อยอดจาก `AGENT_FILE_ATTACHMENTS_MVP_PLAN.md` และมีเป้าหมายสองข้อ:

1. เปิดใช้ single-agent attachment แบบ full function ได้เร่งด่วน
2. สร้าง Run/Step Context contract ที่ multi-step orchestration นำไปใช้ต่อได้ โดยไม่ต้องรื้อ upload, resource storage, resolver หรือ agent signatures ซ้ำอีกครั้ง

งานนี้ไม่ใช่การสร้าง orchestration engine เต็มรูปแบบ

## 2. ลำดับการส่งมอบ

### Checkpoint A — Commit implementation ปัจจุบัน

ก่อนเริ่มแก้ Run Context ให้ commit implementation attachment ปัจจุบันเป็น checkpoint แยก โดยต้อง:

- ตรวจ `git diff` และเลือก stage เฉพาะไฟล์ที่เกี่ยวข้อง
- ไม่รวม cache, data, staging artifacts, logs หรือไฟล์ทดลองที่ไม่เกี่ยวข้อง
- รัน full test suite และบันทึกผลใน commit/handoff
- ใช้ commit message ที่สื่อว่าเป็น run-scoped attachment MVP

Checkpoint นี้ทำให้ย้อนกลับหรือ review attachment layer แยกจาก context-propagation layer ได้

### Checkpoint B — Run Context completion

สร้าง context contract และเปลี่ยนทุก decision point ให้รับ context ผ่าน interface กลาง จากนั้น commit เป็นชุดที่สองเมื่อ acceptance criteria ผ่าน

## 3. ปัญหาปัจจุบัน

Attachment ปัจจุบันถูก resolve และส่งเข้าบาง execution path แล้ว แต่ context ยังเดินทางผ่าน parameter แยก เช่น:

```python
quick_brief=...
resource_context=...
extra_image_paths=...
```

ผลคือ phase ใหม่หรือ phase ที่อยู่นอก agent generation อาจไม่เห็น resource โดยไม่ตั้งใจ ตัวอย่างปัจจุบันคือ Auto mode ส่ง resource ให้ Content Creator แต่ขั้นเลือกสินค้าและ concept ยังพิจารณาเฉพาะ Quick Brief

หากแก้ด้วยการเพิ่ม parameter ราย function ต่อไป ระบบจะเกิด context propagation debt และมีโอกาสหลุดซ้ำเมื่อเพิ่ม workflow phase ใหม่

## 4. หลักการออกแบบ

### 4.1 Resource เป็น input ของงาน ไม่ใช่ input ของ function ใด function หนึ่ง

Quick Brief, product refs, uploaded resources และ upstream artifacts ต้องรวมอยู่ภายใต้ context ของ run/step แล้วแต่ละ phase ขอ view ที่เหมาะสมจาก context เดียวกัน

### 4.2 LLM เป็นผู้ตีความความเกี่ยวข้อง

ระบบไม่กำหนด rule ตายตัวว่าไฟล์ชนิดใดใช้กับ agent หรือ phase ใด LLM พิจารณาจาก:

- เป้าหมายของ user
- Quick Brief
- เนื้อหาของ resource
- หน้าที่ของ agent/phase
- artifacts ที่มีอยู่

ระบบรับผิดชอบเฉพาะ scope, security, context budget, provenance และ instruction hierarchy

### 4.3 ทุก decision point ต้องประกาศ context policy

LLM call ใดที่ตัดสินใจหรือสร้างผลลัพธ์ให้ user ต้องรับ `StepRunContext` หรือ view ที่สร้างจาก object นี้ ห้ามเพิ่ม LLM decision phase ใหม่โดยเรียก Quick Brief/resources แยกเอง

### 4.4 Context contract ต้องรองรับหนึ่ง step วันนี้และหลาย stepภายหลัง

MVP ปัจจุบันสร้าง workflow หนึ่ง step แต่ context ต้องใช้ identity ต่อไปนี้ตั้งแต่ต้น:

- `workflow_id`
- `step_id`
- `agent_key`
- typed `input_refs`

อนาคตเพิ่ม `step:{step_id}` และ artifact refs โดยไม่เปลี่ยน resource contract

## 5. Target Context Model

เพิ่ม module กลาง เช่น `src/run_context.py`

```python
@dataclass(frozen=True)
class StepRunContext:
    workflow_id: str
    step_id: str
    agent_key: str
    quick_brief: str
    input_refs: tuple[str, ...]
    product_refs: tuple[str, ...]
    resource_refs: tuple[str, ...]
    resource_text: str
    resource_image_paths: tuple[str, ...]
    resource_trace: tuple[ResourceTrace, ...]
    warnings: tuple[str, ...]
    upstream_artifacts: tuple[ArtifactRef, ...] = ()
```

ข้อกำหนด:

- ใช้ immutable dataclass หรือ object ที่ไม่ถูกแก้ระหว่าง phase
- ไม่เก็บ filesystem path ที่ client ส่งมา
- resource paths ต้องมาจาก server-side resolver เท่านั้น
- `input_refs` เป็น source of truth สำหรับ trace/routing
- `resource_text` เป็น bounded, untrusted context ที่สร้างจาก resource store
- image paths แยกจาก text representation
- รองรับ empty context โดยไม่เปลี่ยนพฤติกรรม legacy

ชื่อ class เปลี่ยนได้ระหว่าง implementation แต่ semantic contract ต้องคงตามนี้

## 6. Context Construction

เพิ่ม factory กลาง เช่น:

```python
build_step_run_context(
    workflow_id=...,
    step_id=...,
    agent_key=...,
    quick_brief=...,
    product_refs=...,
    resource_refs=...,
    upload_session_id=...,
) -> StepRunContext
```

Factory ต้อง:

1. validate typed refs
2. resolve resource ภายใน scope
3. สร้าง bounded resource text
4. รวบรวม image paths
5. เก็บ truncation/parse warnings
6. สร้าง resource trace
7. คืน context เดียวที่ทุก phase reuse

ห้าม resolve attachment ซ้ำในแต่ละ phase เพราะอาจเกิด context ไม่ตรงกัน, TTL เปลี่ยนกลาง run หรือ trace ไม่สอดคล้อง

## 7. Context Views

เพื่อไม่ส่งข้อมูลเกินจำเป็น ให้ context สร้าง view ตาม phase ผ่าน method/helper กลาง:

```python
context.for_phase("selection")
context.for_phase("generation")
context.for_phase("review")
context.for_phase("media_planning")
```

MVP สามารถใช้ bounded resource text และ image paths ชุดเดียวกันก่อน แต่ interface ต้องพร้อมให้เปลี่ยนภายในเป็น retrieval/reranking ภายหลัง

ตัวอย่างผลลัพธ์:

```python
PhaseContext(
    instruction_text=...,
    image_paths=(...),
    input_refs=(...),
    trace_metadata={...},
)
```

ห้าม hard-code mapping ตาม extension หรือ agent typeใน phase view

## 8. Decision Point Inventory

ต้องตรวจทุก LLM call ใน current single-agent execution และจัดประเภทอย่างน้อยดังนี้

### 8.1 Selection / Planning

- Auto product selection
- Concept/pillar selection
- Combined-product selection
- การเลือก asset หรือข้อมูลประกอบ หากการเลือกมีผลต่อ user-visible result

Requirement:

- เห็น Quick Brief และ resource text
- เห็น resource images เมื่อ model/phase รองรับและเกี่ยวข้อง
- trace ระบุ `phase=selection`

### 8.2 Agent Generation

- Product Spec
- Competitor Analysis
- Campaign Strategy
- Content Creator
- Auto Content Creator

Requirement:

- รับ context ผ่าน interface กลาง
- Agent ยังคงเป็นผู้พิจารณาว่า resource ใดเกี่ยวข้อง
- legacy call ที่ไม่มี context ยังทำงานได้ช่วง migration

### 8.3 Review / Repair

- Review/refine
- Structured-output repair
- Validation retry ที่เรียก LLM

Requirement:

- reviewer/repair ต้องเห็น task intent และ resource constraints เดียวกับ generator
- ห้าม review output โดยไม่เห็น resource ที่ generator ใช้
- trace ระบุ phase ที่ชัดเจน

### 8.4 Media Planning

- การสร้าง media prompt
- การเลือก image/video direction ที่อาศัย brief หรือ visual reference

Requirement:

- รับ relevant context หรือ upstream artifact ที่รักษาข้อกำหนดจาก resource ครบ
- ไม่จำเป็นต้องส่งไฟล์ทุกชนิดเข้า media provider โดยตรง
- original images ที่ต้องใช้เป็น reference ต้องรักษา provenance และ scope

### 8.5 Non-decision Operations

Operations เช่น save file, cost calculation และ UI status ไม่ต้องอ่านข้อความ resource แต่ต้องรับ identity/trace metadata เมื่อจำเป็น

## 9. Migration Strategy

เปลี่ยนแบบ incremental เพื่อไม่ทำให้ agent เดี่ยวหยุดทำงาน

### Phase 1 — Introduce context object

- เพิ่ม `StepRunContext` และ factory
- สร้าง context ใน Manual และ Auto handler ครั้งเดียว
- ยังแปลง context เป็น parameters เดิมผ่าน adapter ได้
- เพิ่ม tests ของ construction, isolation และ empty context

### Phase 2 — Propagate to all decision points

- เปลี่ยน Auto selection ให้รับ selection view
- เปลี่ยน agent runner/orchestrator ให้รับ context หรือ adapter กลาง
- ตรวจ review/repair path
- ตรวจ media planning path
- ห้าม resolve resources ซ้ำด้านใน

### Phase 3 — Remove scattered propagation

- ลดการส่ง `resource_context` และ `extra_image_paths` แบบแยกจาก handler
- ให้ adapter แปลงจาก `StepRunContext` ที่ขอบของ legacy function เท่านั้น
- เพิ่ม lint/contract test หรือ registry test ป้องกัน phase ใหม่ข้าม context policy

ยังไม่จำเป็นต้องลบ legacy optional parametersทั้งหมดในรอบเดียว หากทำให้ risk สูง แต่ต้องมี source of truth เพียง `StepRunContext`

## 10. Manual Flow Behavior

Manual single-agent flow ต้องทำงานดังนี้:

```text
User selects product + agent
        ↓
Upload resources
        ↓
Build one StepRunContext
        ↓
Agent generation
        ↓
Review/repair with same context
        ↓
Output + trace
```

ต้องรองรับ agent ทั้งสี่ชนิดด้วย contract เดียวกัน และไม่ผูก resource routing กับ `agent_key`

## 11. Auto Flow Behavior

Auto single-agent flow ต้องทำงานดังนี้:

```text
Quick Brief + resources
        ↓
Build one StepRunContext
        ↓
Select product/concept using context
        ↓
Add selected product refs to derived context
        ↓
Generate content using same run resources
        ↓
Review/repair using same context
        ↓
Output + phase traces
```

ข้อกำหนดสำคัญ:

- Selection ต้องเห็น attachment ก่อนเลือกสินค้าและ concept
- เมื่อเลือกสินค้าแล้ว ให้ derive context ใหม่โดยคง `workflow_id`, `step_id`, resources และ provenance เดิม
- ห้าม mutate context เดิมใน place
- `input_refs` ใน final flow metadata ต้องรวม selected products และ resources จริง

ตัวอย่างที่ต้องทำงาน:

> ดูไฟล์ยอดขายที่แนบ แล้วเลือกสินค้าที่ควรทำคอนเทนต์ พร้อมอธิบายเหตุผลจากข้อมูล

## 12. Multi-Agent Auto Compatibility

แม้เป้าหมายรอบนี้คือ single-agent แต่ Auto path ปัจจุบันอาจรับ agent มากกว่าหนึ่งตัว ห้ามทำให้ path นี้ถดถอย

ในรอบนี้สามารถใช้ workflow/step context เดียวเป็น compatibility behavior ได้ แต่ต้อง:

- บันทึก limitation ว่ายังไม่ใช่ step identity ราย agent
- ห้ามออกแบบ API ใหม่ที่ผูก resources กับลำดับ agent แบบ hard-coded
- เตรียม factory ให้รับ `step_id` จาก orchestration engine ในอนาคต

ไม่ต้องสร้าง multi-step artifact routing ในงานนี้

## 13. Trace Contract

ทุก LLM decision call ต้องบันทึกอย่างน้อย:

```json
{
  "workflow_id": "flow_...",
  "step_id": "flow_..._step_0",
  "agent_key": "content_creator",
  "phase": "selection",
  "input_refs": [
    "resource:res_..."
  ],
  "resource_refs_available": [
    "resource:res_..."
  ],
  "context_truncated": false
}
```

หลังเลือกสินค้า phase ถัดไปอาจมี:

```json
{
  "phase": "generation",
  "input_refs": [
    "product:K9",
    "resource:res_..."
  ]
}
```

คำว่า `available` หมายถึง phase ได้รับ context ไม่ได้ยืนยันว่า LLM ใช้ข้อมูลนั้นจริง

Flow metadata ต้องเก็บ phase traces ได้มากกว่าหนึ่งรายการ ไม่ overwrite trace ก่อนหน้า

## 14. Security และ Budget Invariants

- Resource IDs และ scope validation ใช้ `RunResourceStore` เดิม
- Context factory ห้ามรับ resolved filesystem paths จาก request body
- Resource content ยังคงเป็น untrusted document content
- Quick Brief มี priority สูงกว่า instruction ที่ฝังในไฟล์
- Context budget ใช้ config เดิมและห้ามขยายซ้ำเมื่อส่งหลาย phase
- Phase view ต้องรักษา truncation warning
- Cross-flow resource access ต้องถูกปฏิเสธก่อน LLM call
- Image paths ต้องมาจาก server-side representations เท่านั้น

## 15. Tests

### 15.1 Context construction

1. สร้าง empty context โดยไม่มี resource ได้
2. สร้าง context จาก text และ image resources ได้
3. Invalid/cross-scope ref ถูกปฏิเสธ
4. Context มี workflow, step, agent และ typed input refs ถูกต้อง
5. Context immutable หรือ derive แล้วไม่แก้ original
6. Truncation warning เดินทางไป phase view

### 15.2 Manual single-agent

7. Agent ทั้งสี่ได้รับ context contract เดียวกัน
8. Quick Brief และ resource content แยก instruction boundary
9. Generator และ reviewer เห็น resource refs ชุดเดียวกัน
10. Run ที่ไม่มี attachment ทำงานเหมือนเดิม

### 15.3 Auto single-agent

11. Product selection prompt เห็น resource text
12. Product selection รองรับ resource image ตาม capability
13. Selected product refs ถูกเพิ่มใน derived context
14. Content generation เห็น resources เดิมหลัง selection
15. ตัวอย่างเลือกสินค้าจากไฟล์ยอดขายผ่าน integration test โดยใช้ fake LLM

### 15.4 Phase coverage

16. Registry/contract test ระบุ decision phases ปัจจุบันครบ
17. Decision phase ที่ไม่มี context policy ทำให้ test fail
18. Phase trace เก็บ selection และ generation แยกกัน
19. Media planning path รักษา relevant refs/provenance

### 15.5 Regression

20. Full existing test suite ผ่าน
21. Manual และ Auto SSE protocol ไม่เปลี่ยน
22. Scheduler/legacy payload ที่ไม่มี resources ไม่พัง
23. Parallel flows ไม่แชร์ context object หรือ resource scope

## 16. Acceptance Criteria

งานนี้ถือว่าเสร็จเมื่อ:

1. Manual single-agent ทั้งสี่ชนิดใช้ Quick Brief + files ได้
2. Auto single-agent ใช้ Quick Brief + files ตั้งแต่ product/concept selection จนถึง generation/review
3. Resources ถูก resolve ครั้งเดียวต่อ step/run แล้ว reuse ผ่าน context contract
4. ทุก current LLM decision point มี context policy ที่ตรวจสอบได้
5. Generator และ reviewer ไม่ได้รับ context คนละชุดโดยไม่ตั้งใจ
6. Flow metadata มี workflow/step/input refs และ phase-level traces
7. No-attachment behavior backward compatible
8. Security, scope และ context budget invariants เดิมยังผ่าน
9. Full test suite ผ่าน
10. ไม่มี handler ใหม่ที่ต้องส่ง Quick Brief, resource text และ image paths แบบ scattered propagation เป็น source of truth

## 17. Out of Scope

- Multi-step workflow execution
- Artifact store และ `step:{step_id}` resolution จริง
- LLM planner ที่สร้าง routing หลาย agent
- Dynamic re-planning
- UI เลือก resource ราย step
- Vector database/RAG เต็มรูปแบบ
- Permission model หลาย user/organization
- การพิสูจน์ว่า LLM ใช้ resource ใดจริงจาก reasoning ภายใน

สิ่งเหล่านี้เป็นงาน orchestration phase ถัดไป แต่ต้องต่อเข้ากับ `StepRunContext` และ typed `input_refs` เดิม

## 18. Future Orchestration Path

เมื่อพัฒนา multi-step flow:

```text
Workflow planner/executor
        ↓
สร้าง StepRunContext ต่อ step
        ↓
input_refs:
  - product:...
  - resource:...
  - step:...
  - artifact:...
        ↓
ใช้ decision-phase interface เดิม
```

ส่วนที่ reuse ได้โดยไม่รื้อ:

- Resource upload API
- RunResourceStore
- resource IDs และ scope validation
- resource context builder
- StepRunContext/phase views
- agent context boundary
- phase trace contract
- security และ budget policies

งาน orchestration จะเพิ่ม planner, executor และ artifact resolver ไม่ต้องย้าย attachment ไป data model ใหม่

## 19. Definition of Done

- Checkpoint attachment MVP ถูก commit แยกอย่างสะอาด
- Run Context completion ถูก commit แยกหลัง full suite ผ่าน
- Manual และ Auto agent เดี่ยวผ่าน integration tests
- Auto selection ใช้ข้อมูลจาก attachment ได้จริง
- Context propagation มี source of truth เดียว
- Phase trace ตรวจได้ว่า context พร้อมให้ decision point ใดบ้าง
- ไม่มี known single-agent decision path ที่ Quick Brief เห็นแต่ attachment ไม่เห็นโดยไม่มี policy อธิบาย
- เอกสาร `CONTEXT.md` อัปเดตนิยาม Run/Step Context และขอบเขต MVP


# Dynamic Agent Orchestration — Implementation Spec

เอกสารนี้เป็น source of truth สำหรับการทำงานร่วมกันของ agent ใน MKTApp และใช้เป็น handoff สำหรับ implementation

คุณภาพและเกณฑ์ Production Ready ของ agent แต่ละตัวให้ยึด `AGENT_PRODUCTION_READINESS_SPEC.md` แยกจาก orchestration

## คำตัดสินของ Product Owner

ระบบต้องไม่บังคับ flow ตายตัว `product_spec → competitor_analysis → campaign_strategy → content_creator`

Agent ทุกตัวต้อง:

- รันเดี่ยวได้
- เป็นจุดเริ่มต้นของงานได้
- รับ output ที่เข้ากันได้จาก agent อื่นเป็น context ได้
- ส่ง output ให้ agent อื่นตัวใดทำงานต่อก็ได้ เมื่อ data contract รองรับ
- ถูกเรียกซ้ำใน flow เดียวกันได้ เช่น `campaign → competitor → campaign`
- ไม่บังคับให้ user รัน agent ที่ไม่เกี่ยวข้อง

เป้าหมายคือ user-defined directed workflow ไม่ใช่ pipeline หมายเลข 1–4 และไม่จำเป็นต้องเป็น autonomous multi-agent chat

## Product Decision — Agent Experience

ประสบการณ์หลักไม่บังคับให้ user เขียน prompt. User เลือกสินค้า เลือก Agent ตั้งตัวเลือกที่ UI มีให้ แล้วกดรันได้ทันทีจาก default job ของตำแหน่งนั้น. Quick Brief เป็น optional steering และ Agent Settings เป็นพฤติกรรมถาวร อ่าน product concept เต็มใน `AI_EMPLOYEE_PRODUCT_VISION.md`.

Agent ที่รัน standalone ต้องใช้ UI selection และ Quick Brief (ถ้ามี) เป็น intent ของงาน ไม่บังคับ user เดินตามรูปแบบรายงาน/ลำดับงานภายใน. ระบบอาจมี default deliverable และ required internal contracts ได้ แต่:

- hard contract คุม identity, facts/evidence, constraints, provenance และ failure state
- presentation contract ต้องยืดหยุ่นตาม deliverable ที่ user ขอ เช่น quick answer, brief, analysis หรือ full plan
- agent ห้ามเพิ่ม agent อื่น, research step หรือ section ที่ไม่จำเป็นอย่างเงียบ ๆ
- หากงานต้องใช้ข้อมูลหรือหลักฐานเพิ่ม ให้บอกสิ่งที่ขาดและเหตุผล ไม่สร้างข้อมูลเพื่อให้ template ครบ
- ตัวเลือกที่มี UI โดยตรง เช่น platform, จำนวนโพสต์ และ media mode ต้องส่งเป็น typed run options ไม่ตีความซ้ำจาก prompt
- downstream Agent ต้องอ่าน semantic artifact/context ไม่ควรพึ่งชื่อหัวข้อหรือลำดับ section ใน user-facing text เพราะ presentation สามารถเปลี่ยนตาม Quick Brief/Settings ได้

งาน response-mode/renderer ที่ครบถ้วนเป็นการพัฒนาต่อเนื่อง; ระหว่างนั้น standalone core flow ที่มีอยู่ต้องยังใช้งานได้โดยไม่อ้างว่ารูปแบบตายตัวเป็นคุณสมบัติของ AI agent.

## Current State

ระบบปัจจุบันเน้นการรัน Agent แบบ standalone. Wizard มี `MAX_AGENTS_PER_FLOW = 1` จึงเลือกได้หนึ่ง Agent ต่อ Flow ขณะนี้ และ user สามารถสร้างหลาย Flow แยกกันได้. UI มี product/Auto selection, Agent Settings, optional Quick Brief, attachments, scheduling และ Content Creator options สำหรับ platform/count/media.

Backend ยังมี `AGENT_DEPENDENCIES`, `AGENT_ORDER` และโค้ด context handoff จากผลก่อนหน้า ซึ่งเป็นร่องรอย/ฐานรองรับ flow หลาย Agent แต่ยังไม่ใช่ capability ที่ UI เปิดให้ใช้ในวันนี้.

ตัวอย่างที่ต้องเลิกใช้เป็น source of truth:

```text
product_spec: []
competitor_analysis: [product_spec]
campaign_strategy: [product_spec, competitor_analysis]
content_creator: [product_spec, competitor_analysis, campaign_strategy]
```

ค่าต่อไปนี้ยังมีอยู่ใน codebase แต่ห้ามใช้เป็น product contract ของ flow ในอนาคต:

```text
product_spec: []
competitor_analysis: [product_spec]
campaign_strategy: [product_spec, competitor_analysis]
content_creator: [product_spec, competitor_analysis, campaign_strategy]
```

ปัจจุบัน UI lock ป้องกันไม่ให้ user เข้า flow หลาย Agent; การปลด lock ต้องทำหลัง typed artifact/input contract พร้อม ห้ามเพียงเปลี่ยน `MAX_AGENTS_PER_FLOW` แล้วถือว่า team flow เสร็จ

Agent บางตัวรองรับ optional context แล้ว แต่ยังไม่มี context contract กลางและ routing ที่ทำให้สลับ agent ได้ครบทุกทิศทาง

## Target Behavior

### 1. Standalone เป็นค่าเริ่มต้น

เมื่อ user เลือก agent ตัวเดียว ระบบต้องรันตัวนั้นโดยไม่เติม agent อื่นโดยอัตโนมัติ

ตัวอย่าง:

- `product_spec`
- `competitor_analysis`
- `campaign_strategy`
- `content_creator`

ถ้าข้อมูลไม่พอ agent ต้องบอกสิ่งที่ขาดหรือแสดง uncertainty ไม่ใช่สร้างข้อมูล และไม่ควรบังคับรัน agent อื่นโดยไม่ได้รับการเลือกจาก user

### 2. User กำหนดลำดับได้

ลำดับต่อไปนี้ต้องเป็น workflow ที่ถูกต้องได้ทั้งหมด:

```text
competitor → campaign
campaign → content
product_spec → content
competitor → content
content → campaign
campaign → competitor → campaign
product_spec → competitor → content
```

รายการนี้เป็นเพียงตัวอย่าง ห้ามนำไปสร้าง allowlist ที่จำกัดเฉพาะลำดับเหล่านี้

### 3. Agent อาจปรากฏซ้ำ

ห้ามระบุ identity ของ step ด้วย `agent_key` เพียงอย่างเดียว เพราะ agent เดิมอาจถูกใช้มากกว่าหนึ่งครั้ง

แต่ละ step ต้องมีอย่างน้อย:

```json
{
  "step_id": "unique-per-workflow",
  "agent_key": "campaign_strategy",
  "input_refs": ["product_db:PRODUCT_ID", "step:competitor_1"],
  "quick_brief": "ปรับแคมเปญจากข้อมูลคู่แข่งล่าสุด"
}
```

### 4. ส่ง artifact ไม่ใช่ผูก parameter ตามชื่อ agent ก่อนหน้า

Output ทุก step ต้องถูกเก็บเป็น artifact ที่อ้างอิงได้ โดยอย่างน้อยมี:

```json
{
  "artifact_id": "unique-id",
  "producer_step_id": "competitor_1",
  "producer_agent": "competitor_analysis",
  "artifact_type": "competitor_analysis",
  "product_ids": ["PRODUCT_ID"],
  "content": "...",
  "created_at": "ISO-8601"
}
```

Consumer เลือก artifact ผ่าน `input_refs` แล้วระบบแปลงเป็น semantic context ของ agent นั้น ห้าม hard-code ว่า campaign ต้องอ่านเฉพาะผลจาก step หมายเลข 2 หรือ content ต้องอ่านเฉพาะ step 1–3

### 5. Context contract ต้องแยกจาก routing

Routing ตอบคำถามว่า “step ไหนส่งอะไรให้ step ไหน”

Context adapter ตอบคำถามว่า “artifact เหล่านั้นถูกจัดรูปเป็น input ของ agent ปลายทางอย่างไร”

แต่ละ agent ต้องประกาศ:

- required base input
- optional context types ที่รับได้
- output artifact type
- วิธีทำงานเมื่อ optional context ไม่มี

ห้ามใช้ dependency map เพื่อแทน context contract

## Proposed Contracts

เวอร์ชันแรกใช้ contract แบบกว้างเพื่อไม่ล็อกระบบเร็วเกินไป:

| Agent | Required base input | Optional artifact context | Output type |
|---|---|---|---|
| `product_spec` | raw product data หรือ product DB | artifact ใดที่ user เลือกเป็น reference | `product_spec` |
| `competitor_analysis` | product DB หรือ product description | `product_spec`, `campaign_strategy`, `content`, previous `competitor_analysis` | `competitor_analysis` |
| `campaign_strategy` | product DB หรือ product description | `product_spec`, `competitor_analysis`, `content`, previous `campaign_strategy` | `campaign_strategy` |
| `content_creator` | product DB หรือ product description | `product_spec`, `competitor_analysis`, `campaign_strategy`, previous `content` | `content` |

หาก context type ไม่มี adapter เฉพาะ ให้ส่งภายใต้ section กลาง `Additional selected context` โดยรักษาชื่อ producer, artifact type และ step id ไว้ ห้ามทิ้ง context เงียบ ๆ

## UI Requirements

- User เพิ่ม ลบ และเรียง step ได้เอง
- User เลือก agent ซ้ำได้
- แต่ละ step แสดง input ที่จะใช้จริง
- User เลือก output จาก step ก่อนหน้าเป็น input ได้
- ระบบไม่เพิ่ม prerequisite agent แบบเงียบ ๆ
- ถ้า required base input ขาด ให้แจ้งที่ step นั้นก่อน run
- UI อาจเสนอ recommended flow เป็น template ได้ แต่ template ไม่ใช่ dependency บังคับ
- Preset, instruction และ quick brief ยังเป็นการตั้งค่าราย agent/รายรอบตามเดิม ไม่ใช่ routing rule

## Execution Requirements

- Execute ตามลำดับ step ที่ user กำหนด
- resolve `input_refs` ด้วย `step_id`/`artifact_id`
- ถ้า referenced step ล้มเหลว ให้ mark downstream step เป็น blocked พร้อมเหตุผล ห้ามใช้ output เก่าหรือ output อื่นแทนโดยเงียบ ๆ
- ป้องกัน reference ไปยัง step ในอนาคตใน sequential v1
- จำกัดจำนวน step ต่อ run ด้วย config แต่ไม่จำกัดชนิดหรือลำดับ agent
- เก็บ trace ว่า artifact ใดถูกส่งเข้า step ใด เพื่อ debug และ audit ได้
- พฤติกรรม standalone เดิมต้องยังใช้ได้

## Non-goals สำหรับ v1

- ไม่ต้องใช้ CrewAI หรือ framework multi-agent ใหม่
- ไม่ต้องให้ Manager LLM เลือก routing เอง
- ไม่ต้องให้ agent สนทนากันแบบอิสระ
- ไม่ต้องสร้าง parallel execution
- ไม่ต้องสร้าง cycle ที่ runtime ย้อนกลับไปรัน step เดิมอัตโนมัติ การใช้ agent ซ้ำให้สร้างเป็น step ใหม่
- ไม่รวมงานปรับคุณภาพ prompt ของ agent แต่ละตัว

## Migration Plan

1. สร้าง workflow schema ที่มี `step_id`, `agent_key`, `input_refs`, `quick_brief`
2. สร้าง artifact schema และ artifact store สำหรับผลแต่ละ step
3. แยก context adapters ออกจาก individual runner
4. เปลี่ยน runner ให้รับ resolved semantic context/artifacts
5. ถอด hard-coded dependency expansion จาก UI/backend
6. ทำ UI เพิ่ม/ลบ/เรียง step และเลือก input refs
7. คง compatibility กับการรัน agent เดี่ยวและ flow ที่บันทึกไว้เดิม
8. เพิ่ม tests ตาม acceptance criteria ด้านล่าง

## Acceptance Criteria

ต้องมี automated tests และ manual smoke test อย่างน้อยดังนี้:

1. รัน agent ทั้ง 4 ตัวแยกเดี่ยวได้ โดยไม่มี agent อื่นถูกเพิ่ม
2. รัน `competitor → campaign` และ campaign ได้รับ artifact ของ competitor จริง
3. รัน `campaign → content` โดยไม่ต้องมี product_spec step
4. รัน `content → campaign` และ campaign เห็น content ใน additional/typed context
5. รัน `campaign_1 → competitor → campaign_2` ได้ โดย campaign สอง step มี `step_id` คนละค่า
6. ลบหรือสลับ step แล้ว `input_refs` ไม่ชี้ผิด artifact
7. referenced step ล้มเหลวแล้ว downstream ถูก blocked พร้อมข้อความชัดเจน
8. ไม่มี code path ใดเพิ่ม prerequisite ตามเลข 1–4 โดยอัตโนมัติ
9. legacy standalone API/UI ยังทำงานได้
10. trace ระบุได้ว่าแต่ละ step ใช้ product, artifact, preset และ quick brief ใด

## Definition of Done

งานนี้เสร็จเมื่อ acceptance criteria ทั้ง 10 ข้อผ่าน ไม่ใช่เมื่อสามารถลากกล่อง agent บน UI ได้เท่านั้น

หาก implementation จำเป็นต้องจำกัด context บางชนิด ให้บันทึกข้อจำกัดใน contract และแสดงต่อ user ห้ามย้อนกลับไปบังคับลำดับ 1–4 โดยไม่แก้ spec และไม่ได้รับการตัดสินใจจาก Product Owner

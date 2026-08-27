# Domain Glossary — MKTApp

คำศัพท์ในระบบ นิยามให้ชัด เพื่อให้คุยกันเข้าใจตรงกัน

## คอนเทนต์

### Content Pillar (เสาหลักคอนเทนต์)
หมวดใหญ่ 3-5 หมวดที่แบรนด์พูดเสมอ เป็นกรอบระยะยาว ตั้งครั้งเดียว ใช้ทุกครั้งที่สร้างคอนเทนต์

ตัวอย่าง: "รีวิวสินค้า", "เปรียบเทียบ", "ทดสอบความทน", "ฟังก์ชันเด่น", "ไลฟ์สไตล์"

ไม่ใช่: คำสั่งเฉพาะครั้ง (ดู Instruction)

### Concept (แนวคิด)
การตีความ Content Pillar ในแต่ละโพสต์ เกิดใหม่ทุกโพสต์ ต้องไม่ซ้ำของเดิม

ตัวอย่าง: pillar "รีวิวสินค้า" → concept "K5 เปลี่ยนดีไซน์ตัวเรือน"

ความสัมพันธ์: Content Pillar → Concept → คอนเทนต์จริง

### Angle (มุมมอง) — deprecated
คำเดิมที่เปลี่ยนเป็น Concept แล้ว โค้ดเก่าอาจยังเห็น แปลเท่ากับ Concept

### Instruction (การตั้งค่า agent)
การตั้งค่าที่ติดกับ agent ตั้งถาวร โหลดจาก `config/agent_instructions.json` ใส่ใน system prompt ทุกครั้งที่ agent ทำงาน ไม่ใช่คำสั่งเฉพาะครั้ง

ตัวอย่าง: preset "balanced", focus ["USP", "differentiation"], rules_must ["แยก fact กับ assumption"], tone ["friendly", "professional"]

ในโค้ด: โหลดตอนสร้าง agent (`__init__`) เก็บใน `self.instructions` ใส่ใน system prompt ทุกครั้ง

ไม่ใช่: คำสั่งเฉพาะรอบ (ดู Quick Brief)

### Quick Brief (คำสั่งเฉพาะรอบ)
คำสั่งบังคับจาก user สำหรับรอบนั้น ส่งเป็น parameter ใน `run()` ทุกครั้ง ใช้ครั้งนั้นแล้วหมดไป

ตัวอย่าง: "ทำเปรียบเทียบ K2 กับ K9", "เน้นความทนทาน"

ในโค้ด: ส่งใน `run(quick_brief=...)` แปะท้าย user prompt พร้อมข้อความ "คำสั่งบังคับจากผู้ใช้ ต้องทำตาม"

ความสัมพันธ์กับ Pillar: Instruction (ตั้งถาวร) + Quick Brief (เฉพาะรอบ) ทำงานภายในกรอบ Content Pillar ทั้งคู่ ไม่ใช่ทดแทนกัน

## แบรนด์

### Brand Voice (เสียงแบรนด์)
ลักษณะเฉพาะของแบรนด์ที่ทำให้คนรู้ว่า "นี่คือแบรนด์นี้" แม้ไม่เห็นโลโก้

ในระบบนี้: เก็บใน `brand/` folder (voice.json, terms.json, audience.json, brand_profile.md, visual.json) โหลดโดย `brand_loader.py` ส่งให้ทุก agent อัตโนมัติ

ไม่ต้องทำเพิ่ม — มีอยู่แล้วและทำงานอยู่

### Asset (วัตถุดิบแบรนด์)
ไฟล์ที่ใช้ซ้ำข้ามการรัน ข้ามสินค้า (โลโก้ รูปพรีเซนเตอร์ เพลง แบนเนอร์) ต่างจาก product data (ของเฉพาะสินค้าใน `data/{product}/`) และ brand rules (text กฎใน `brand/*.json`)

ในระบบนี้: เก็บใน `brand/assets/` (ไฟล์จริง) + `cache/assets/db.json` (catalog + embeddings) จัดการโดย `asset_library.py`

### Auto-tagging
LLM บรรยาย + ติด tag ตาม taxonomy ใน `config/assets.yaml` ครั้งเดียวตอนอัปโหลด — จ่าย LLM ครั้งเดียวต่อไฟล์ ถ้า hash ไม่เปลี่ยนจะข้าม

### Hybrid Search
วิธีค้น asset: filter ด้วย structured field (type/subject ตาม taxonomy) ก่อน แล้ว rank ด้วย embedding similarity — ใช้ embedding model เดียวกับ content_history

ความสัมพันธ์กับ Auto Mode: agent เรียก `list_assets(query, type, subject)` ผ่าน tool calling เพื่อค้นและเลือก asset เอง เหมือนที่ `select_product_auto` เรียก `list_products()`

## การตรวจซ้ำ

### Embedding
ลายนิ้วมือของข้อความ เป็น vector 1536 มิติ ใช้เปรียบเทียบความเหมือนทางความหมาย

### Cosine Similarity
คะแนนความเหมือนระหว่าง 2 embeddings 0-1 (1 = เหมือนกันมาก)

### Dedup Threshold
ค่าคะแนนที่ถือว่า "ซ้ำ" ปัจจุบันตั้ง 0.85 ถ้า similarity เกินค่านี้ → ถือว่าซ้ำ

### Dedup Window
ระยะเวลาย้อนหลังที่ตรวจซ้ำ ปัจจุบัน 30 วัน

## สถานะสินค้า

6 สถานะ: empty / pending / no_usable_data / processing / ready / stale

ดูนิยามเต็มใน `config/ingestion.yaml` ส่วน `status_labels`

## ตำแหน่งสินค้า (Product Positioning)

### Brand Voice (เสียงแบรนด์) vs Product Positioning (ตำแหน่งสินค้า)
- **Brand Voice** = ตัวตน "เราเป็นใคร" ตั้งครั้งเดียว ใช้ทุกรุ่น คงที่ข้ามสินค้าทุกตัว (เสียงพูด คำใช้ สี ประวัติ)
- **Product Positioning** = ตำแหน่ง "รุ่นนี้เอาไปแข่งกับใคร ทำไมซื้อ" ตั้งใหม่ทุกรุ่น เปลี่ยนตาม product update (กลุ่มเป้าหมายเฉพาะรุ่น คู่แข่ง จุดขาย ระดับราคา use case ปรับโทน)

ความสัมพันธ์: Voice เป็นกรอบ — Positioning ปรับภายในกรอบ ไม่ใช่เสียงใหม่
ตัวอย่าง: Lagenio voice = "เหมือนพ่อแม่ที่เข้าใจเทคโนโลยี" (ทุกรุ่น). K9 positioning = พรีเมียม มั่นใจ (ปรับโทนภายใน voice เดิม ไม่ใช่เสียงใหม่)

### Product Profile (ไฟล์ตำแหน่งสินค้า)
ไฟล์ `data/{product}/product_profile.json` เก็บเฉพาะสิ่งที่ต่างจากแบรนด์ ถ้าฟิลด์ไหนไม่มี → ใช้ของแบรนด์

ฟิลด์: audience (ทับของแบรนด์), competitors, differentiators, use_cases, price_tier, tone_adjustment, visual_override

กฎรวม: ทับทั้งฟิลด์ — สินค้ามีฟิลด์ไหน → ใช้ของสินค้าทั้งก้อน ไม่ผสมลึกระดับฟิลด์ย่อยกับแบรนด์

ในระบบนี้: โหลดโดย `brand_loader.py` (รับ `product_id` พารามิเตอร์) รวมกับ brand config อัตโนมัติ ผู้เรียกไม่ต้องรวมเอง

ไม่ใช่: Brand Voice (ตัวตน — อยู่ที่ `brand/`), Terms (คำใช้ — อยู่ที่ `brand/terms.json`), ประวัติแบรนด์ (อยู่ที่ `brand/brand_profile.md`)

## Agent Input Context

> Source of truth สำหรับ routing และการสลับลำดับ agent คือ `AGENT_ORCHESTRATION_SPEC.md` ระบบเป้าหมายไม่ใช่ flow ตายตัว 1→2→3→4; agent ต้องรันเดี่ยว เรียงใหม่ และใช้ agent เดิมซ้ำเป็นคนละ step ได้
>
> Source of truth สำหรับสถานะ ความพร้อม และเกณฑ์ release ของ agent แต่ละตัวคือ `AGENT_PRODUCTION_READINESS_SPEC.md`

### Agent Input Context (context สำหรับ agent)
ชุดข้อมูลที่ส่งให้ agent ทำงาน ไม่ผูกกับชื่อ agent หรือ flow ที่ส่งมา มี semantic contract ชัดเจน ว่าฟิลด์ไหนมีความหมายอะไร

ฟิลด์หลักสำหรับ CampaignStrategyAgent:
- `product` — ข้อมูลสินค้า (required)
- `competitors` — ผลวิเคราะห์คู่แข่ง (optional)
- `market` — ข้อมูลตลาด/เทรนด์ (optional)
- `customers` — กลุ่มเป้าหมาย (optional)
- `business` — วัตถุประสงค์ งบ ช่องทาง ต้นทุน margin (optional)

ไม่ใช่: `quick_brief` อยู่ใน context (`quick_brief` เป็นคำสั่งเฉพาะรอบ ผ่าน `BaseAgent.run(..., quick_brief=...)`)

### Standalone Agent
agent ที่สามารถทำงานได้โดยไม่ต้องพึ่ง output ของ agent อื่น ถ้าข้อมูลบางอย่างขาด agent ต้องระบุ uncertainty แทนที่จะหยุดทำงานหรือสร้างข้อมูล

### Workflow Step
หนึ่งตำแหน่งใน workflow ที่ user กำหนด มี `step_id` ไม่ซ้ำและมี `agent_key` ระบุ agent ที่จะรัน Agent เดิมปรากฏหลาย step ได้

### Artifact
ผลลัพธ์ของ workflow step ที่มี identity และ metadata ของ producer ใช้ `artifact_id` หรือ `step_id` อ้างอิงเป็น context ให้ step อื่น โดยไม่ผูกกับลำดับหมายเลข agent

### Dynamic Agent Routing
การที่ user เลือก agent ลำดับ และ artifact context ของแต่ละ step ได้เอง ไม่ได้แปลว่า Manager LLM ต้องตัดสินใจแทน user และไม่ใช่การบังคับ pipeline 1→2→3→4

### Uncertainty Statement
คำอธิบายใน output ที่บอกว่าสิ่งใด agent ไม่รู้หรือไม่สามารถสรุปได้ เนื่องจากขาดข้อมูล ไม่ใช่การเติมข้อมูลเพื่อให้ output ดูครบ

### Evidence-Driven Search
การใช้ web search เฉพาะเมื่อต้องการข้อมูลภายนอกเพื่อตัดสินใจ ไม่ใช่ขั้นตอนบังคับก่อนทุกงาน

## AI Usage

### Actor (user)
ใครสั่นให้เรียก AI — แทนด้วยช่องทางการทำงาน เช่น `web`, `cli`, `scheduler:<job_id>`, `ingestion` เมื่อระบบยังไม่มี user login จริง

### Subject (reference)
เรื่องที่ AI call นี้เกี่ยวข้อง — ปกติใช้ `product_id` หรือ `session/flow id` ถ้ายังไม่ทราบสินค้า

### Flow ID
thread-local correlation id สำหรับผูก usage logs เข้ากับ flow เดียวกัน ไม่ใช่ actor หรือ subject

## โหมดการสร้าง

### Separate Mode
1 สินค้าต่อ 1 โพสต์

### Combined Mode
หลายสินค้าใน 1 โพสต์ (เช่น เปรียบเทียบ 2-3 รุ่น)

### Auto Mode
AI เลือกสินค้า + แนวคิด เอง โดยอยู่ในกรอบ Content Pillar + ไม่ซ้ำกับประวัติ

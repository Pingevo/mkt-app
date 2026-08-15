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

ในระบบนี้: เก็บใน `brand/` folder (brand_profile.md, tone_of_voice.md, target_audience.md, visual_guidelines.md) โหลดโดย `brand_loader.py` ส่งให้ทุก agent อัตโนมัติ

ไม่ต้องทำเพิ่ม — มีอยู่แล้วและทำงานอยู่

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

## โหมดการสร้าง

### Separate Mode
1 สินค้าต่อ 1 โพสต์

### Combined Mode
หลายสินค้าใน 1 โพสต์ (เช่น เปรียบเทียบ 2-3 รุ่น)

### Auto Mode
AI เลือกสินค้า + แนวคิด เอง โดยอยู่ในกรอบ Content Pillar + ไม่ซ้ำกับประวัติ

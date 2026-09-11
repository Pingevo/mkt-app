# Marketing Agent System

ระบบพนักงาน AI สำหรับทีมการตลาด — ใช้งานหลักผ่าน Web UI และรองรับ CLI สำหรับ automation/backward compatibility

## Documentation authority

เอกสารที่ใช้ตัดสินงานปัจจุบัน (อ่านตามลำดับนี้ก่อนเริ่มงาน):

1. [`AGENTS.md`](AGENTS.md) — กฎวิศวกรรมที่ผูกพันทุกงานใน repo นี้
2. [`AI_EMPLOYEE_PRODUCT_VISION.md`](AI_EMPLOYEE_PRODUCT_VISION.md) — ภาพผลิตภัณฑ์ “พนักงาน AI” ที่เสถียร
3. [`AGENT_PRODUCTION_READINESS_SPEC.md`](AGENT_PRODUCTION_READINESS_SPEC.md) — เกณฑ์ Beta / Production Ready ราย Agent
4. [`AGENT_ORCHESTRATION_SPEC.md`](AGENT_ORCHESTRATION_SPEC.md) — สถาปัตยกรรม team flow ในอนาคต (อ่านเมื่อทำ team flow เท่านั้น)
5. [`AI_EMPLOYEE_BETA_EXECUTION_PLAN.md`](AI_EMPLOYEE_BETA_EXECUTION_PLAN.md) — **แผนส่งมอบและ Progress Ledger เพียงฉบับเดียวที่ใช้บอกงานถัดไป**

เอกสารรายงาน/qualification ในอดีต (เช่น `M6_*`, `PAID_*`, `OFFLINE_*`, `audit_*`, `consult_*`) เป็นหลักฐานทางประวัติศาสตร์ที่ไม่เปลี่ยน — ห้ามใช้เป็นคำสั่งงานปัจจุบัน การตั้งค่าโมเดล การอนุญาต paid call หรือสถานะความพร้อม สถานะการส่งมอบปัจจุบันอยู่ใน `AI_EMPLOYEE_BETA_EXECUTION_PLAN.md` และเกณฑ์ความพร้อมอยู่ใน `AGENT_PRODUCTION_READINESS_SPEC.md`

โมเดล production ปัจจุบันสำหรับ text/reasoning อ่านจาก `config/agents.yaml` — อย่าคัดลอกชื่อโมเดลลงเอกสาร ให้อ้าง config

> **คำเตือนสำหรับ AI/engineer:** ก่อนเขียนถึง input ของ Agent ต้องตรวจ execution path ของ Web UI ปัจจุบันจากโค้ดจริง สัญญา CLI pipeline และสัญญา future orchestration ต้องไม่ถูกนำเสนอเป็นพฤติกรรมของ Web UI ปัจจุบัน — ดู `For AI / Engineer Handoff` ท้ายเอกสาร

## Product documents

- [`AI_EMPLOYEE_PRODUCT_VISION.md`](AI_EMPLOYEE_PRODUCT_VISION.md) — ภาพผลิตภัณฑ์ “พนักงาน AI”, วิธีใช้งานจริง และเหตุผลด้านความคุ้มค่า
- [`AGENT_PRODUCTION_READINESS_SPEC.md`](AGENT_PRODUCTION_READINESS_SPEC.md) — เกณฑ์ Beta และ Production Ready ราย Agent
- [`AI_EMPLOYEE_BETA_EXECUTION_PLAN.md`](AI_EMPLOYEE_BETA_EXECUTION_PLAN.md) — แผนส่งมอบ Beta, ระยะเวลา, stopping rule และ handoff ข้าม session
- [`AGENT_ORCHESTRATION_SPEC.md`](AGENT_ORCHESTRATION_SPEC.md) — สัญญาการทำงานเป็นทีม/ส่ง artifact ในอนาคต

Web UI ปัจจุบันรองรับการเลือกสินค้า เลือก Agent หนึ่งตัวต่อ Flow, optional Quick Brief, ไฟล์แนบ, Agent Settings และตัวเลือก Content Creator (Facebook/TikTok, จำนวนโพสต์, รูป/วิดีโอ, สร้างทันทีหรือถามก่อน). CLI ด้านล่างยังคงใช้ได้สำหรับ automation และ backward compatibility.

## Agent ทั้ง 4 ตัว

หน้าที่ของแต่ละ Agent อธิบายตามบทบาท ส่วน **input จริงใน Web UI ปัจจุบัน** อ้างอิงจากโค้ด production (ดู `CURRENT WEB UI BEHAVIOR` ด้านล่าง) — ไม่ใช่ pipeline 1→2→3→4

### Agent 1 — นักวิเคราะห์สินค้า (Product Analyst)

สร้างสเปค/รายงานสินค้าจากข้อมูลดิบที่เลือก

Input ใน Web UI ปัจจุบัน:
- ข้อมูลสินค้าจาก **Product DB** (scoped context ของสินค้าที่เลือก)
- รูปภาพสินค้าจริง (ส่งเป็น multimodal ให้ LLM vision)
- Quick Brief (optional)
- Agent Settings (optional)
- ไฟล์แนบ/resources (optional)
- brand context (voice/terms/audience)

ผลลัพธ์:
- สเปค/รายงานสินค้า
- บันทึกลง run/session output เท่านั้น
- **ไม่เขียนกลับลง Product DB** — Product DB ถูก populate โดย ingestion pipeline ไม่ใช่โดย Agent 1

### Agent 2 — นักวิเคราะห์คู่แข่ง (Competitor Analyst)

วิเคราะห์เปรียบเทียบคู่แข่ง

Input ใน Web UI ปัจจุบัน:
- ข้อมูลสินค้าจาก **Product DB** (ไม่ใช่ output ของ Agent 1)
- รูปภาพสินค้าจริง
- Quick Brief (optional)
- Agent Settings (optional)
- ไฟล์แนบ/resources (optional)
- brand context
- ค้นเว็บหาคู่แข่งอัตโนมัติเมื่อ config เปิดและไม่มีข้อมูลคู่แข่งที่ผู้ใช้ระบุ

ผลลัพธ์:
- รายงานวิเคราะห์คู่แข่ง (ตารางเปรียบเทียบ + คำแนะนำ)
- บันทึกลง run/session output

**ไม่ต้องรัน Agent 1 ก่อน** ใน Web UI ปัจจุบัน

### Agent 3 — นักวางกลยุทธ์แคมเปญ (Campaign Strategist)

คิดแคมเปญ + ราคาแนะนำ

Input ใน Web UI ปัจจุบัน:
- ข้อมูลสินค้าจาก **Product DB** (ไม่ใช่ output ของ Agent 1)
- Quick Brief (optional)
- Agent Settings (optional)
- ไฟล์แนบ/resources (optional)
- brand context
- web research เมื่อ config/Quick Brief กำหนด

ใน Flow ที่มี Agent 3 เดี่ยว (กรณีเดียวที่ Web UI อนุญาตวันนี้) output ของ Agent 2 ไม่ถูกส่งเข้า เพราะ Web UI lock 1 Agent ต่อ Flow

ผลลัพธ์:
- แคมเปญ + โครงสร้างราคา
- บันทึกลง run/session output

**ไม่ต้องรัน Agent 1 หรือ Agent 2 ก่อน** ใน Web UI ปัจจุบัน

### Agent 4 — นักสร้างคอนเทนต์ (Content Creator)

สร้าง content + prompt รูป/วิดีโอ + hashtag

Input ใน Web UI ปัจจุบัน:
- ข้อมูลสินค้าจาก **Product DB** (ไม่ใช่ output ของ Agent 1)
- รูปสินค้า/วัตถุดิบอ้างอิง (reference assets)
- Quick Brief (optional)
- Agent Settings (optional)
- ไฟล์แนบ/resources (optional)
- brand/visual context
- Content History (เพื่อห้ามซ้ำมุมมองเดิม)
- ตัวเลือก UI: platform (Facebook/TikTok), จำนวนโพสต์, media type, สร้างรูป/วิดีโอทันทีหรือถามก่อน
- Content Pillars (optional)

ใน Flow ที่มี Agent 4 เดี่ยว output ของ Agent 1/2/3 ไม่ถูกส่งเข้า เพราะ Web UI lock 1 Agent ต่อ Flow

ผลลัพธ์:
- โพสต์ FB/TikTok + image/video prompt + hashtag
- บันทึกเป็น `.json` (structured) + `.md` (rendered) ลง run/session output
- บันทึก concept ลง Content History

### การสร้างรูป/วิดีโอ (Media generation)

การสร้างรูป/วิดีโอเป็นความสามารถ downstream ของ Content Creator — ทำงานหลัง Agent 4 สร้าง prompt แล้ว โดยใช้ prompt ที่ Agent 4 เขียน + รูปอ้างอิงที่เลือก ผ่าน media generation provider ที่ตั้งใน config ไม่ใช่ขั้นตอนของ Agent 1–3

## CURRENT WEB UI BEHAVIOR

Web UI ปัจจุบัน lock ที่ **1 Agent ต่อ Flow** (`MAX_AGENTS_PER_FLOW = 1` ใน `src/wizard_ui.js`) ผู้ใช้เลือกสินค้า เลือก Agent หนึ่งตัว ใส่ Quick Brief/ไฟล์แนบ/ตัวเลือก แล้วรัน แต่ละ Agent อ่านข้อมูลสินค้าจาก **Product DB** โดยตรง — ไม่ได้รับ output จาก Agent อื่น เพราะแต่ละ Flow มี Agent เดียว

```text
Product ingestion (data/ → ingestion pipeline)
      ↓
Product DB (cache/{product_id}/product.json)
  ├── Agent 1: Product Analyst      (อ่าน Product DB)
  ├── Agent 2: Competitor Analyst     (อ่าน Product DB + web search)
  ├── Agent 3: Campaign Strategist   (อ่าน Product DB)
  └── Agent 4: Content Creator        (อ่าน Product DB + assets + history)
```

**ไม่ใช่** pipeline `Agent 1 → Agent 2 → Agent 3 → Agent 4` ใน Web UI ปัจจุบัน — ผู้ใช้เลือก Agent ใด Agent หนึ่งรันได้โดยอิสระ และทุก Agent ดึงข้อมูลสินค้าจาก Product DB ไม่ใช่จาก output ของ Agent ก่อนหน้า

Product DB ถูก populate โดย ingestion pipeline (ไม่ใช่โดย Agent 1) ดังนั้น Agent 2/3/4 ทำงานได้โดยไม่ต้องรัน Agent 1 ก่อน

## การติดตั้ง

```bash
# 1. ติดตั้ง dependencies
pip install -r requirements.txt

# 2. ตั้งค่า API key
cp .env.example .env
# แก้ .env ใส่ OPENROUTER_API_KEY ของคุณ
```

## วิธีใช้งาน

> **หมายเหตุ:** ส่วน CLI ด้านล่างเป็น **CLI / backward compatibility** — เป็น runtime surface แยกจาก Web UI ใน CLI pipeline ผลลัพธ์ของ Agent ก่อนหน้าถูกส่งต่อเป็น string parameter ให้ Agent ถัดไป แต่นี่ไม่ใช่พฤติกรรมของ Web UI ปัจจุบัน (ดู `CURRENT WEB UI BEHAVIOR` ด้านบน) แม้ใน CLI เมื่อระบุ `--product-id` และ Product DB พร้อม orchestrator จะดึงข้อมูลสินค้าจาก Product DB ไม่ใช่จาก parameter ที่ส่งมา

### Interactive mode (ง่ายสุด)

```bash
python main.py
```

ระบบจะแสดง:
1. รายชื่อสินค้าที่มีใน `data/` — พิมพ์ชื่อสินค้าที่ต้องการ
2. ตารางไฟล์ที่พบ (ข้อมูลดิบ, รูปภาพ, ข้อมูลคู่แข่ง)
3. เมนูเลือก Agent:
   - `1` — นักวิเคราะห์สินค้า
   - `2` — นักวิเคราะห์คู่แข่ง
   - `3` — นักวางกลยุทธ์แคมเปญ
   - `4` — นักสร้างคอนเทนต์
   - `5` — รันทั้ง 4 Agent (pipeline)
4. ยืนยัน → รัน

### CLI mode (สำหรับ automation)

### รันทั้ง 4 Agent พร้อมกัน (Pipeline)

```bash
python main.py pipeline \
  --raw-file data/product_raw.txt \
  --competitor-file data/competitors.txt
```

ผลลัพธ์จะถูกบันทึกใน `output/` เป็น 4 ไฟล์ markdown ตามลำดับ

### รัน Agent เดี่ยว

```bash
# Agent 1: สร้างสเปคสินค้า
python main.py product-spec --raw-file data/product_raw.txt

# Agent 2: วิเคราะห์คู่แข่ง
python main.py competitor \
  --product-spec-file output/01_product_spec_*.md \
  --competitor-file data/competitors.txt

# Agent 3: คิดแคมเปญ
python main.py campaign \
  --product-spec-file output/01_product_spec_*.md \
  --analysis-file output/02_competitor_analysis_*.md

# Agent 4: สร้าง content
python main.py content \
  --product-spec-file output/01_product_spec_*.md \
  --analysis-file output/02_competitor_analysis_*.md \
  --campaign-file output/03_campaign_strategy_*.md
```

### ใส่ข้อมูลแบบ inline (ไม่ใช้ไฟล์)

```bash
python main.py product-spec --raw-text "สินค้าคือ โทรศัพท์มือถือ ราคาต้นทุน 5000 บาท มีกล้อง 108MP..."
```

### เปลี่ยนโฟลเดอร์ output

```bash
python main.py pipeline --raw-file data/raw.txt --competitor-file data/comp.txt --output-dir results/
```

### ไม่ระบุข้อมูลคู่แข่ง (ให้ Agent ค้นหาเอง)

```bash
python main.py pipeline --raw-file data/raw.txt
```

ถ้าไม่ระบุ `--competitor-file` หรือ `--competitor-text` CompetitorAnalysisAgent จะค้นหาข้อมูลคู่แข่งจาก web อัตโนมัติ โดยใช้โมเดลของ Agent (อ่านจาก `config/agents.yaml`) ร่วมกับ OpenRouter agentic web search tool (ตั้งค่า engine/parameters ใน `config/web_search.yaml`)

### เพิ่มรูปภาพสินค้าเพิ่มเติม

```bash
python main.py pipeline --raw-file data/raw.txt --product-image data/product.jpg
```

ใช้ `--product-image` เพื่อเพิ่มรูปภาพสินค้าเพิ่มเติม (.png, .jpg, .jpeg) — ระบบส่งรูปจริงให้ LLM vision เป็น multimodal input (ไม่ใช่ OCR) ให้ Agent วิเคราะห์ร่วมกับข้อมูลดิบ

### Auto-detect ไฟล์จากโฟลเดอร์ data/

```bash
python main.py pipeline
```

ถ้าไม่ระบุไฟล์ ระบบจะ auto-detect ไฟล์จากโฟลเดอร์ `data/` ตาม naming convention:

### โครงสร้างแบบแยกสถานะ (แนะนำ)

แยกข้อมูลดิบและข้อมูลที่ผ่านการดูแล้ว:

```
data/
└── product1/
    ├── raw/                    # ข้อมูลดิบที่ยังไม่ได้ผ่านการดู (ingestion จะประมวลผล)
    │   ├── product_raw.txt
    │   ├── product_image_1.jpg
    │   └── product_image_2.jpg
    └── ready/                  # ข้อมูลที่ผู้ใช้เตรียมไว้ (เช่น competitors.txt)
        └── competitors.txt
```

> **หมายเหตุ:** ใน Web UI ปัจจุบัน Agent 2/3/4 อ่านข้อมูลสินค้าจาก **Product DB** (ผ่าน ingestion pipeline) ไม่ใช่จาก `data/{product_id}/ready/` โฟลเดอร์ `ready/` เป็นที่เก็บไฟล์ที่ผู้ใช้เตรียมไว้เท่านั้น ไม่ใช่แหล่งข้อมูล runtime ของ Agent

### โครงสร้างแบบรวม (backward compatible)

```
data/
└── product1/
    ├── product_raw.txt
    ├── product_image_1.jpg
    ├── product_image_2.jpg
    └── competitors.txt
```

| ชื่อไฟล์ | สำหรับ | รองรับ format | โฟลเดอร์ |
|---------|--------|---------------|---------|
| `product_raw.*` | ข้อมูลดิบสินค้า (ingestion ประมวลผล → Product DB) | .txt, .md, .pdf, .jpg, .jpeg, .png | raw/ |
| `product_image.*` หรือ `product_image_1.*`, `product_image_2.*`, ... | รูปภาพสินค้า (รองรับหลายรูป) | .jpg, .jpeg, .png | raw/ |
| `competitors.*` | ข้อมูลคู่แข่ง (optional — ถ้าไม่มี Agent 2 ค้นเว็บเอง) | .txt, .md, .pdf, .jpg, .jpeg, .png | ready/ |

**ลำดับความสำคัญ:** CLI argument > Auto-detect > Error

### รองรับสินค้าหลายตัว

```bash
python main.py pipeline --product-id product1
python main.py pipeline --product-id product2
```

ใช้ `--product-id` เพื่อระบุสินค้าแต่ละตัว — ระบบจะ:
- อ่านไฟล์ดิบจาก `data/{product_id}/raw/` (หรือ `data/{product_id}/` ถ้าไม่มี `raw/`) เพื่อ ingestion
- ดึงข้อมูลสินค้า runtime จาก **Product DB** (`cache/{product_id}/product.json`) ให้ Agent 1–4
- บันทึกผลลัพธ์ใน `output/{product_id}/`

> **หมายเหตุ:** ผลลัพธ์ของ Agent 1 (product_spec) ถูกบันทึกลง `output/` session folder เท่านั้น — ไม่มี code path ที่เขียนกลับลง `data/{product_id}/ready/product_spec.txt` และ Agent 2/3/4 ไม่ได้อ่านจาก path นั้น

ตัวอย่างโครงสร้างสำหรับสินค้าหลายตัว:
```
data/
├── product1/
│   ├── raw/
│   │   ├── product_raw.txt
│   │   ├── product_image_1.jpg
│   │   └── product_image_2.jpg
│   └── ready/
│       └── competitors.txt
└── product2/
    ├── raw/
    │   ├── product_raw.pdf
    │   └── product_image_1.png
    └── ready/
        └── competitors.jpg

output/
├── product1/                # ผลลัพธ์ของ product1
│   ├── 01_product_spec.md
│   ├── 02_competitor_analysis.md
│   ├── 03_campaign_strategy.md
│   └── 04_content_creator.md
└── product2/                # ผลลัพธ์ของ product2
    ├── 01_product_spec.md
    ├── 02_competitor_analysis.md
    ├── 03_campaign_strategy.md
    └── 04_content_creator.md
```

## รูปแบบไฟล์ที่รองรับ

ระบบรองรับไฟล์ข้อมูลดิบและข้อมูลคู่แข่งในหลาย format:

- **Text files** — `.txt`, `.md` (อ่านตรงๆ)
- **PDF** — `.pdf` (ดึงข้อความออกมาด้วย PyPDF2)
- **รูปภาพ** — `.png`, `.jpg`, `.jpeg` (ส่งรูปจริงให้ LLM vision เป็น multimodal input ไม่ใช่ OCR)

### ติดตั้งเพิ่มเติมสำหรับ PDF และรูปภาพ

```bash
pip install -r requirements.txt
```

สำหรับรูปภาพ ระบบส่งรูปจริงให้ LLM vision เป็น multimodal input (ไม่ใช่ OCR) แต่ ingestion pipeline อาจใช้ tesseract OCR เป็น fallback ในการดึง text จากรูปเมื่อจำเป็น — ติดตั้ง tesseract OCR engine บนระบบ:
- **macOS**: `brew install tesseract`
- **Ubuntu/Debian**: `sudo apt-get install tesseract-ocr`
- **Windows**: ดาวน์โหลดจาก https://github.com/UB-Mannheim/tesseract/wiki

### ตัวอย่างการใช้ไฟล์ PDF

```bash
python main.py pipeline --raw-file data/product.pdf --competitor-file data/competitors.pdf
```

### ตัวอย่างการใช้ไฟล์รูปภาพ

```bash
python main.py pipeline --raw-file data/product.jpg --competitor-file data/competitors.png
```

## ข้อมูลแบรนด์อ้างอิง (Brand Reference)

โฟลเดอร์ `brand/` เก็บข้อมูลของแบรนด์เรา — ทุก Agent จะอ่านข้อมูลจากที่นี่แล้วนำไปใช้เป็นบริบทในการทำงาน

### ไฟล์ในโฟลเดอร์ `brand/`

| ไฟล์ | สิ่งที่ใส่ |
|------|----------|
| `brand_profile.md` | ประวัติแบรนด์ วิสัยทัศน์ ค่านิยม ตำแหน่งในตลาด |
| `tone_of_voice.md` | โทนเสียง บุคลิก คำที่ใช้/ห้ามใช้ ระดับความเป็นทางการ |
| `visual_guidelines.md` | สีหลัก สไตล์ภาพ ฟอนต์ แนวทางสำหรับ AI prompt |
| `target_audience.md` | กลุ่มเป้าหมาย ไลฟ์สไตล์ พฤติกรรม ช่องทางที่ใช้ |

แก้ไฟล์เหล่านี้ให้เป็นข้อมูลของแบรนด์คุณ — เพิ่ม/ลบไฟล์ได้ ระบบจะอ่านไฟล์ `.md` และ `.txt` ทั้งหมดในโฟลเดอร์อัตโนมัติ

### เปลี่ยนโฟลเดอร์ brand

```bash
python main.py pipeline --raw-file data/raw.txt --competitor-file data/comp.txt --brand-dir my_brand/
```

ถ้าไม่มีโฟลเดอร์ `brand/` หรือโฟลเดอร์ว่าง — Agent จะทำงานได้ปกติแต่ไม่มีบริบทแบรนด์

## การตรวจงาน (Review)

ทุก Agent มี 2 ขั้นตอน:
1. **สร้างงาน** — LLM สร้างผลลัพธ์ตาม system prompt
2. **ตรวจงาน** — ส่งผลลัพธ์กลับให้ LLM ตรวจว่าครบตามรูปแบบหรือไม่ ถ้ามีข้อบกพร่องจะแก้ไขให้

ปรับแต่งการตรวจงานได้ใน `config/agents.yaml`:
```yaml
max_review_iterations: 1     # จำนวนรอบตรวจ (0 = ปิด, 1 = ตรวจ 1 รอบ, 2 = ตรวจ 2 รอบ)
review_temperature: 0.2      # temperature ตอนตรวจ (ต่ำ = เข้มงวดกว่าตอนสร้าง)
review_prompt: |              # prompt ของผู้ตรวจงาน
  คุณคือ "ผู้ตรวจงาน" (QA Reviewer) ...
```

## ปรับแต่ง Agent

แก้ `config/agents.yaml` เพื่อ:
- เปลี่ยน model (เช่น `openai/gpt-4o`, `google/gemini-2.0-flash-exp`)
- ปรับ temperature (ความสร้างสรรค์ vs ความแม่นยำ)
- ปรับ max_tokens (ความยาวผลลัพธ์)
- แก้ system prompt (รูปแบบผลลัพธ์)
- ปรับ max_retry_limit (จำนวนครั้ง retry ตอน API error)
- ปรับ max_review_iterations (จำนวนรอบตรวจงาน)
- ปรับ review_temperature (ความเข้มงวดตอนตรวจ)
- แก้ review_prompt (เงื่อนไขการตรวจงาน)

## โครงสร้างโปรเจกต์

```
MKTApp/
├── main.py                      # CLI entry point
├── requirements.txt
├── .env.example
├── config/
│   └── agents.yaml              # ตั้งค่า agent ทั้งหมด
├── brand/                       # ข้อมูลแบรนด์อ้างอิง (แก้ไข้ที่นี่)
│   ├── brand_profile.md
│   ├── tone_of_voice.md
│   ├── visual_guidelines.md
│   └── target_audience.md
├── src/
│   ├── __init__.py
│   ├── ai_usage.py             # log AI usage ไป Hub + local JSONL (fire-and-forget)
│   ├── asset_library.py        # จัดการ asset library + embedding
│   ├── brand_loader.py         # โหลดไฟล์ brand/ → brand context
│   ├── brand_migrate.py        # migrate brand folder structure
│   ├── brand_priority.py       # hard/soft rules + conflict detection
│   ├── config_loader.py        # โหลด YAML config
│   ├── content_history.py      # ประวัติคอนเทนต์ + duplicate detection
│   ├── content_schema.py       # schema ของ content output
│   ├── cost_summary.py         # รวมค่าใช้จ่าย LLM ต่อ flow → _cost_summary_*.json
│   ├── data_loader.py          # สแกนไฟล์ข้อมูลดิบ
│   ├── file_loader.py          # โหลดไฟล์ตาม type
│   ├── flow_context.py         # thread-local flow_id (ผูก LLM call เข้า flow)
│   ├── flow_runner.py          # รัน agent ตามลำดับใน flow + ส่ง context ต่อ
│   ├── ingestion.py            # ประมวลผลไฟล์ดิบ → product DB
│   ├── llm_client.py           # OpenRouter API client
│   ├── media_gen.py            # orchestration สำหรับสร้างรูป/วิดีโอผ่าน configured media provider
│   ├── orchestrator.py         # ประสานงาน agent + auto mode
│   ├── pillar_manager.py       # content pillar management
│   ├── product_db.py           # product database (ready/stale status)
│   ├── scheduler.py            # APScheduler — scheduled jobs
│   ├── script_reviewer.py      # ตรวจ script ก่อนใช้
│   ├── voice_learner.py        # เรียนรู้ tone of voice
│   ├── web_searcher.py         # web search helper
│   └── agents/
│       ├── __init__.py
│       ├── base_agent.py        # Base class (generate + review + brand context)
│       ├── product_spec.py      # Agent 1
│       ├── competitor_analysis.py  # Agent 2
│       ├── campaign_strategy.py    # Agent 3
│       └── content_creator.py      # Agent 4
├── data/                        # ใส่ไฟล์ข้อมูลดิบที่นี่
└── output/                      # ผลลัพธ์ (auto-generated)
```

## For AI / Engineer Handoff

กฎสั้น ๆ สำหรับ AI หรือ engineer ที่จะแก้/เขียนต่อ — ป้องกันการเข้าใจผิดซ้ำ:

1. README อธิบาย product ในระดับสูงเท่านั้น — รายละเอียด runtime ต้องตรวจจากโค้ด
2. **CURRENT WEB UI** และ **CLI pipeline** เป็นคนละ runtime surface — อย่าสื่อสารสัญญาของอันหนึ่งเป็นอีกอัน
3. **Product DB** (`cache/{product_id}/product.json`) คือแหล่งข้อมูลสินค้ากลางของ Agent 1–4 ใน Web UI ปัจจุบัน — ไม่ใช่ `data/{product_id}/ready/` และไม่ใช่ output ของ Agent 1
4. อย่าสมมติว่า output ของ Agent ถูก chain ใน Web UI — ปัจจุบัน lock 1 Agent ต่อ Flow และทุก Agent อ่าน Product DB โดยตรง
5. อย่าสมมติว่า `AGENT_ORCHESTRATION_SPEC.md` เป็นพฤติกรรมปัจจุบัน — เป็น future design ที่ยังไม่ใช่ runtime
6. ก่อนเปลี่ยน team-flow architecture ต้องยืนยัน requirement ปัจจุบันจาก Product Owner ก่อน

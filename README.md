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

โมเดล production ปัจจุบันสำหรับ text/reasoning อ่านจาก `config/agents.yaml` (ขณะนี้คือ `google/gemini-3.8-flash`) — อย่าคัดลอกชื่อโมเดลลงเอกสาร ให้อ้าง config

## Product documents

- [`AI_EMPLOYEE_PRODUCT_VISION.md`](AI_EMPLOYEE_PRODUCT_VISION.md) — ภาพผลิตภัณฑ์ “พนักงาน AI”, วิธีใช้งานจริง และเหตุผลด้านความคุ้มค่า
- [`AGENT_PRODUCTION_READINESS_SPEC.md`](AGENT_PRODUCTION_READINESS_SPEC.md) — เกณฑ์ Beta และ Production Ready ราย Agent
- [`AI_EMPLOYEE_BETA_EXECUTION_PLAN.md`](AI_EMPLOYEE_BETA_EXECUTION_PLAN.md) — แผนส่งมอบ Beta, ระยะเวลา, stopping rule และ handoff ข้าม session
- [`AGENT_ORCHESTRATION_SPEC.md`](AGENT_ORCHESTRATION_SPEC.md) — สัญญาการทำงานเป็นทีม/ส่ง artifact ในอนาคต

Web UI ปัจจุบันรองรับการเลือกสินค้า เลือก Agent หนึ่งตัวต่อ Flow, optional Quick Brief, ไฟล์แนบ, Agent Settings และตัวเลือก Content Creator (Facebook/TikTok, จำนวนโพสต์, รูป/วิดีโอ, สร้างทันทีหรือถามก่อน). CLI ด้านล่างยังคงใช้ได้สำหรับ automation และ backward compatibility.

## Agent ทั้ง 4 ตัว

| # | Agent | หน้าที่ | รับ input | ผลลัพธ์ |
|---|-------|--------|----------|---------|
| 1 | นักวิเคราะห์สินค้า | สร้างสเปคสินค้าจากข้อมูลดิบ | ข้อมูลดิบ (text/file) | สเปคสินค้ามาตรฐาน |
| 2 | นักวิเคราะห์คู่แข่ง | วิเคราะห์เปรียบเทียบคู่แข่ง | สเปคสินค้า + ข้อมูลคู่แข่ง | ตารางเปรียบเทียบ + คำแนะนำ |
| 3 | นักวางกลยุทธ์แคมเปญ | คิดแคมเปญ + ราคาแนะนำ | สเปคสินค้า + ผลวิเคราะห์คู่แข่ง | แคมเปญ + โครงสร้างราคา |
| 4 | นักสร้างคอนเทนต์ | สร้าง content + prompt + hashtag | สเปคสินค้า + ผลวิเคราะห์ + แคมเปญ | FB/TikTok content + gen image/video prompt |

## การติดตั้ง

```bash
# 1. ติดตั้ง dependencies
pip install -r requirements.txt

# 2. ตั้งค่า API key
cp .env.example .env
# แก้ .env ใส่ OPENROUTER_API_KEY ของคุณ
```

## วิธีใช้งาน

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

ใช้ `--product-image` เพื่อเพิ่มรูปภาพสินค้าเพิ่มเติม (.png, .jpg, .jpeg) — ระบบจะใช้ OCR ดึงข้อความจากรูปภาพแล้วส่งให้ ProductSpecAgent วิเคราะห์ร่วมกับข้อมูลดิบ

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
    ├── raw/                    # ข้อมูลดิบที่ยังไม่ได้ผ่านการดู (ใช้เฉพาะ ProductSpecAgent)
    │   ├── product_raw.txt
    │   ├── product_image_1.jpg
    │   └── product_image_2.jpg
    └── ready/                  # ข้อมูลที่ผ่านการดูแล้ว พร้อมใช้ทำงาน (ใช้ agents อื่นๆ)
        ├── competitors.txt
        └── product_spec.txt     # สเปคสินค้าจาก ProductSpecAgent
```

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
| `product_raw.*` | ข้อมูลดิบสินค้า | .txt, .md, .pdf, .jpg, .jpeg, .png | raw/ |
| `product_image.*` หรือ `product_image_1.*`, `product_image_2.*`, ... | รูปภาพสินค้า (รองรับหลายรูป) | .jpg, .jpeg, .png | raw/ |
| `competitors.*` | ข้อมูลคู่แข่ง | .txt, .md, .pdf, .jpg, .jpeg, .png | ready/ |
| `product_spec.*` | สเปคสินค้า (จาก ProductSpecAgent) | .txt, .md | ready/ |

**ลำดับความสำคัญ:** CLI argument > Auto-detect > Error

### รองรับสินค้าหลายตัว

```bash
python main.py pipeline --product-id product1
python main.py pipeline --product-id product2
```

ใช้ `--product-id` เพื่อระบุสินค้าแต่ละตัว — ระบบจะ:
- ค้นหาไฟล์ใน `data/{product_id}/raw/` สำหรับข้อมูลดิบ (ProductSpecAgent)
- ค้นหาไฟล์ใน `data/{product_id}/ready/` สำหรับข้อมูลที่ผ่านการดูแล้ว (agents อื่นๆ)
- บันทึกผลลัพธ์ใน `output/{product_id}/`
- **บันทึก product_spec ลง `data/{product_id}/ready/product_spec.txt` อัตโนมัติหลังจาก ProductSpecAgent ทำงานเสร็จ (อัพเดทถ้ามีไฟล์อยู่แล้ว)**

ตัวอย่างโครงสร้างสำหรับสินค้าหลายตัว:
```
data/
├── product1/
│   ├── raw/
│   │   ├── product_raw.txt
│   │   ├── product_image_1.jpg
│   │   └── product_image_2.jpg
│   └── ready/
│       ├── competitors.txt
│       └── product_spec.txt
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
- **รูปภาพ** — `.png`, `.jpg`, `.jpeg` (OCR ดึงข้อความด้วย pytesseract)

### ติดตั้งเพิ่มเติมสำหรับ PDF และรูปภาพ

```bash
pip install -r requirements.txt
```

สำหรับรูปภาพ (OCR) ต้องติดตั้ง tesseract OCR engine บนระบบ:
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
│   ├── media_gen.py            # สร้างรูป/วิดีโอ (Gemini image/video)
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

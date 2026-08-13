# Architecture & Design

## Goal

ระบบ Marketing Agent สำหรับทีมการตลาด — รับข้อมูลสินค้าดิบจาก user แล้วสร้างผลลัพธ์การตลาด (สเปคสินค้า, วิเคราะห์คู่แข่ง, แคมเปญ, คอนเทนต์) โดยใช้ AI agents 4 ตัว ทำงานบนข้อมูลจาก Product DB

## สถาปัตยกรรมปัจจุบัน — 3 ชั้น

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Raw Data       │     │  Product DB      │     │  Deliverables   │
│  (user upload)  │────►│  (ingestion)     │◄────│  (agent output) │
│                 │     │                  │     │                 │
│ data/{id}/      │     │ data/{id}/       │     │ cache/{id}/     │
│  ไฟล์ดิบ         │     │  product.json    │     │  product_spec   │
│  PDF/รูป/วิดีโอ    │     │  fields + desc   │     │  competitor_    │
│                 │     │  + transcripts   │     │   analysis      │
└─────────────────┘     └──────────────────┘     └─────────────────┘
                               ▲
                               │
                    ┌──────────┼──────────┐
                    │          │          │
              competitor   campaign   content
              _analysis   _strategy  _creator
              (agent)     (agent)    (agent)
                    │          │          │
                    └──────────┴──────────┘
                               │
                               ▼
                          output/{session}/
                           ผลลัพธ์สำหรับ user
```

### ชั้นที่ 1: Raw Data (`data/{product_id}/`)

ไฟล์ที่ user โยนเข้ามา — เป็นของ user ระบบไม่แตะ ไม่ลบ ไม่ย้าย

รองรับ: text (.txt, .md, .pdf, .xlsx, .xls, .docx, .csv), image (.jpg, .jpeg, .png, .webp), video (.mp4, .mov), audio (.wav, .mp3, .m4a) — config ใน `config/ingestion.yaml`

ไฟล์ที่ไม่รองรับ → ไม่ทำลาย แค่ mark "unsupported" ใน DB แล้วโชว์ใน UI

### ชั้นที่ 2: Product DB (`data/{product_id}/product.json`)

สิ่งที่ ingestion สร้างจาก raw data — เป็น "คลังข้อมูลบริษัท" ที่ agent การตลาคดึงไปใช้

เก็บ: structured fields (ชื่อ, หมวด, ราคา, คุณสมบัติ), raw_text, image_descriptions, video_transcripts, audio_transcripts

**ถูกเขียนตอนเดียว: ตอน ingestion เท่านั้น** — agent ไม่ได้เขียนลง DB เลย

### ชั้นที่ 3: Deliverables (`cache/{product_id}/`)

ผลงานที่ agent สร้างให้ user — เป็นเอกสารสำหรับ user อ่าน ไม่ใช่ data source ของ agent อื่น

เก็บ: `product_spec.txt` (จาก product_spec agent), `competitor_analysis.txt` (จาก competitor_analysis agent)

## สถานะสินค้า 6 แบบ

| สถานะ | ความหมาย | เกิดตอนไหน |
|---|---|---|
| `empty` | ไม่มีไฟล์เลย | โฟลเดอร์ใหม่ หรือลบไฟล์หมด |
| `pending` | มีไฟล์รองรับแต่ยังไม่ได้ ingest | user upload ไฟล์ใหม่ ยังไม่กดประมวลผล |
| `no_usable_data` | มีไฟล์แต่ไม่รองรับทั้งหมด | user upload ไฟล์นามสกุลแปลกๆ |
| `processing` | ingestion กำลังทำ | user กดประมวลผลข้อมูล |
| `ready` | ingestion เสร็จ ข้อมูลครบ | พร้อมรัน agent |
| `stale` | raw data เปลี่ยน รอ re-ingest | user เพิ่ม/แก้/ลบไฟล์หลัง ingest |

## Agents ทั้ง 4 ตัว

| Agent | ดึงข้อมูลจาก | ผลลัพธ์ | เก็บที่ไหน |
|---|---|---|---|
| product_spec | raw data (data/) | เอกสารสเปคสินค้า | cache/ + output/ |
| competitor_analysis | product DB | วิเคราะห์คู่แข่ง (ค้น web เอง) | cache/ + output/ |
| campaign_strategy | product DB + competitor_analysis | กลยุทธ์แคมเปญ + ราคาแนะนำ | output/ |
| content_creator | product DB + (เลือก: competitor/campaign) | คอนเทนต์ + prompt + hashtag | output/ |

**สำคัญ:** product_spec ไม่ใช่ data source ของ agent อื่นอีกต่อไป — มันเป็น deliverable สำหรับ user เท่านั้น agent การตลายดึงข้อมูลสินค้าจาก product DB ตรงๆ

## การเลือก Context — สิทธิ์อยู่ที่ User

User เลือกเองว่าจะใช้ context อะไรตอนรัน agent ไม่บังคับ dependency:

- **content_creator แบบ "สินค้าเพียวๆ"** → ใช้แค่ product DB → ไม่ต้องรัน competitor/campaign ก่อน
- **content_creator แบบ "ใช้คู่แข่ง"** → ใช้ product DB + competitor_analysis → ถ้ายังไม่มี competitor_analysis ระบบรันให้ก่อน
- **content_creator แบบ "ใช้ครบ"** → ใช้ product DB + competitor_analysis + campaign_strategy → รันตามลำดับ

**ทำไมให้ user เลือก:** บางครั้ง user แค่ต้องการ "เขียนโพสต์แนะนำสินค้า" ไม่ต้องรู้คู่แข่ง บางครั้งต้องการเปรียบเทียบ บางครั้งต้องการตามกลยุทธ์ — บังคับทั้งหมดเสียเวลาและ token โดยใช่่เหตุ

## การสร้างหลายชุด (Multi-Content Loop)

เมื่อ user ต้องการ "10 คอนเทนต์" ของสินค้าเดียว — ระบบวนลูป:

```
รอบที่ 1: content_creator สร้างชุดที่ 1 (ใช้ product DB + context ที่เลือก)
          → เก็บผลลัพธ์

รอบที่ 2: content_creator สร้างชุดที่ 2
          prompt มี "ชุดที่ 1 ที่สร้างไปแล้ว: [A]"
          → LLM รู้ว่าต้องไม่ซ้ำชุดที่ 1
          → เก็บผลลัพธ์

รอบที่ 3: content_creator สร้างชุดที่ 3
          prompt มี "ชุดที่ 1-2 ที่สร้างไปแล้ว: [A, B]"
          → LLM รู้ว่าต้องไม่ซ้ำ
          → เก็บผลลัพธ์

... วนจนครบ 10
```

**ทำไมไม่สร้าง 10 ชุดในครั้งเดียว:** `max_tokens` มีจำกัด (8192 สำหรับ content_creator) 10 ชุดเต็มๆ อาจเกิน → ถูกตัด วนลูปทีละชุดปลอดภัยกว่า และแต่ละชุดเห็นชุดก่อนหน้า → ไม่ซ้ำกันจริง

**ทำไมไม่ใช้ CrewAI สำหรับสิ่งนี้:** งานนี้ไม่ต้องการ feedback loop ไม่ต้องการ dynamic routing ใช้ LLM ตัวเดียวทำได้ และ coherent กว่าเพราะคนเดียวเขียน copy + image prompt + video script พร้อมกัน

## Hub & Spoke vs Chain

```
Chain (CrewAI):              Hub & Spoke (เรา):

raw → A → B → C → D         raw → ingestion → DB
                                              ↓ ↓ ↓ ↓
                                          A  B  C  D
                                          (แต่ละตัวดึงเอง)
```

- **ข้อมูลสินค้า** → hub & spoke (ทุก agent ดึงจาก DB)
- **ผลงานวิเคราะห์** → chain เล็กน้อย (campaign ต้องเห็น competitor, content ต้องเห็น campaign) แต่ user เลือกได้ว่าจะใช้ไหม

## Trade-off: ทำไมไม่ใช้ CrewAI

| | CrewAI | เรา | เหตุผลที่เลือกแบบเรา |
|---|---|---|---|
| การสื่อสาร | agent คุยกันผ่าน chat | แลกผ่าน DB + cache/ | ความแน่นอน > ความยืดหยุ่น |
| การสั่งงาน | Manager LLM ตัดสินใจ | user เลือก context | user รู้ว่าต้องการอะไร |
| Token | เสียกับการคุย | ใช้เฉพาะตอนทำงาน | ประหยัด |
| Debug | อ่าน log การสนทนา | เปิด DB + cache/ ดู | โปร่งใส |
| ปรับแต่ง | ปรับ Manager กระทบทั้งทีม | ปรับ agent แต่ละตัวอิสระ | ควบคุมเฉพาะตัว |
| ทำ 10 ชุด | ต้องมีหลาย agent คุยกัน | loop 1 agent ส่งผลก่อนหน้า | เร็ว + coherent |

**เลือกแบบนี้เพราะ:** งานการตลาดของเรามีลำดับคงที่ ไม่ต้องตัดสินใจแบบ dynamic ความแน่นอนและความประหยัดสำคัญกว่าความยืดหยุ่น

**เมื่อไหร่ถึงควร CrewAI:**
- มี image API จริง (DALL-E, Midjourney) → แยก graphic designer เป็น agent ใหม่ ใช้ tool คนละแบบ
- ต้องการ feedback loop → copywriter เขียน → designer ดูแล้วบอกแก้ → copywriter แก้
- ต้องการ dynamic routing → ถ้าสินค้าเป็นอาหารใช้ agent A ถ้าเป็นเสื้อผ้าใช้ agent B

ตอนนี้ยังไม่มีเงื่อนไขข้อใด → ไม่ต้อง CrewAI

## Trade-off: ทำไม content_creator ไม่แยกเป็น copywriter + graphic designer

| | แยก role | คนเดียวทำหมด (ตอนนี้) | เหตุผล |
|---|---|---|---|
| เครื่องมือ | ใช้คนละแบบจริงๆ | ใช้ LLM ตัวเดียว เขียน text ทั้งหมด | ตอนนี้ยังไม่มี image API จริง |
| ความ coherent | ต้องเอามาประกอบกัน | copy + image prompt + video script เข้ากัน | คนเดียวเขียน → เข้ากันดีกว่า |
| ความเร็ว | 3 คำขอ API | 1 คำขอ API | เร็ว 3 เท่า |
| ความยาก | ต้องมี concept "ทีม" ใน UI | ตั้งค่า 1 หน้า | ง่ายกว่า |

**เลือกแบบนี้เพราะ:** ตอนนี้ content_creator สร้าง text ทั้งหมด (copy, image prompt, video script, hashtag) ไม่ได้เรียก image API จริง → แยกไม่ได้ประโยชน์อะไร แค่ช้า + แพง + ซับซ้อน

**เมื่อไหร่ถึงควรแยก:** ตอนมี image API จริง (DALL-E ผ่าน OpenRouter หรืออื่นๆ) → graphic designer ใช้เครื่องมือคนละแบบจริงๆ จึงคุ้มที่จะแยก

## Trade-off: ทำไมใช้ JSON ไม่ใช่ SQLite/MongoDB

| | JSON | SQLite | MongoDB |
|---|---|---|---|
| ความซับซ้อน | ต่ำสุด | ปานกลาง | สูง |
| อ่าน/เขียน | ไฟล์เดียว อ่านง่าย | ต้องมี schema | ต้องมี server |
| สเกล | รองรับหลักพันสินค้า | รองรับหลักแสน | รองรับหลักล้าน |
| ย้ายข้อมูล | copy โฟลเดอร์ | export | dump |
| Debug | เปิดไฟล์ดู | ต้อง query | ต้อง query |

**เลือก JSON เพราะ:** ตอนนี้สเกลหลักพันสินค้า JSON ยังรองรับได้ และง่ายสุดในการ debug — เปิดไฟล์ดูได้เลย ไม่ต้อง query

**เมื่อไหร่ถึงควรย้าย:** สินค้าเกินหลักหมื่น → ย้ายไป MongoDB (แก้แค่ `product_db.py` ไฟล์เดียว เพราะแยก abstraction ไว้แล้ว)

## Ingestion Pipeline

```
User กด "ประมวลผลข้อมูล"
       ↓
1. Scan โฟลเดอร์ → แยกไฟล์ตามประเภท (text/image/video/audio/unsupported)
       ↓
2. ตรวจ hash → ข้ามไฟล์ที่ไม่เปลี่ยน (ประหยัด token)
       ↓
3. เรียก preprocessor ตามประเภท:
   - text: extract text (pandas, pdfplumber, python-docx)
   - image: ส่งเข้า LLM เป็น image input → บรรยายเป็น text
   - video: ffmpeg ดึง key frames → ส่งเข้า LLM
   - audio: whisper transcribe (TODO)
       ↓
4. ส่งข้อมูลที่ได้ทั้งหมดเข้า LLM → extract structured fields (ชื่อ, หมวด, ราคา, ฯลฯ)
       ↓
5. บันทึกลง product DB (product.json)
       ↓
6. อัปเดตสถานะ → ready
```

- **Manual trigger** — user กดปุ่มเอง ไม่ auto ตอน upload
- **Hash-based** — ถ้าไฟล์ไม่เปลี่ยน ข้าม re-ingest
- **Progressive** — แสดง progress + ETA ระหว่างทำ
- **Background thread** — ไม่ block web server

## Self-Review

หลังทำเสร็จ agent ตรวจผลของตัวเอง:
- ถ้าไม่ผ่านเกณฑ์ → แก้และทำใหม่ (จำกัดจำนวนครั้งใน config: `max_review_iterations`)
- ถ้าผ่าน → เซฟลง cache/ และ output/
- user เห็นสถานะ "กำลังตรวจงาน" ใน UI

## ปรับแต่ง Agent

User ปรับแต่ง agent ผ่าน `config/agents.yaml` (model, temperature, max_tokens, system_prompt) และ `config/agent_instructions.json` (preset, focus, tone, rules)

เหมือนจ้างพนักงาน — บอกหน้าที่ให้ชัด ลองให้ทำงาน ถ้าไม่ตรงใจก็ปรับ job description จนดี แล้วหลังจากนั้นก็ทำงานได้เอง

## กฎสำคัญ

1. **Raw data เป็นของ user** — ระบบไม่แตะ ไม่ลบ ไม่ย้าย
2. **Product DB ถูกเขียนตอน ingestion เท่านั้น** — agent ไม่เขียนลง DB
3. **Deliverables เก็บใน cache/** — แยกจาก raw data ของ user
4. **User เลือก context** — ไม่บังคับ dependency ให้รันทุกตัว
5. **Brand context** — ทุกไฟล์ใน `brand/` ถูกส่งให้ทุก agent เป็นบริบท
6. **คู่แข่ง** — competitor_analysis ค้นหาเองจาก web ไม่ต้องมีไฟล์ input
7. **ไม่จำกัดชื่อไฟล์** — โยนไฟล์อะไรลงโฟลเดอร์สินค้าก็ได้ ระบบแยกตามประเภท
8. **ไฟล์ไม่รองรับ** — ไม่ทำลาย แค่ mark "unsupported" ใน DB แล้วโชว์ใน UI

## Media Generation — สร้างรูปและวิดีโอจริง

content_creator เขียน prompt ภาษาอังกฤษ → ระบบ parse prompt ออกมา → เรียก OpenRouter Image/Video API สร้างไฟล์จริง → แสดงในหน้าเว็บ

### โฟลว์

```
content_creator เสร็จ
       ↓
parse_media_prompts() แยก image + video prompts จาก output
       ↓
┌─ Auto mode (เปิดใน settings) ──→ สร้างทันทีหลัง content_creator เสร็จ
│                                  วนลูปทุก prompt → เรียก API → เซฟไฟล์
│
└─ Manual mode (default) ────────→ user กดปุ่ม "🎨 สร้างรูป/วิดีโอ"
                                  ใน flow step → สร้างทั้งหมดในคลิกเดียว
```

### Models (config/media.yaml)

| ประเภท | Model | ราคา | คุณสมบัติ |
|---|---|---|---|
| Image | `google/gemini-3.1-flash-image` | ~$0.04/ภาพ | เร็ว ถูก คุณภาพดี |
| Video | `bytedance/seedance-2.0-fast` | ~$0.40/คลิป 5 วิ | character consistency, 5-10 วิ |

### API ที่ใช้

| Endpoint | ประเภท | วิธีการ |
|---|---|---|
| `POST /api/v1/images` | Synchronous | ส่ง prompt → รับ base64 รูปกลับมาเลย |
| `POST /api/v1/videos` | Asynchronous | ส่ง prompt → ได้ job_id → poll จนเสร็จ → download |

### การแยก prompt (parser)

`parse_media_prompts()` อ่าน content_creator output แยก section ตาม heading `##` แล้วหา:
- `## 3. Prompt สำหรับ Gen Image` → แยก image prompts (รองรับหลายรูปแบบ heading)
- `## 4. Prompt สำหรับ Gen Video` → แยก video prompts

แต่ละ prompt ดึง:
- **prompt text** — บรรทัดภาษาอังกฤษที่ยาว 40+ ตัวอักษร
- **usage** — "ใช้ที่: feed post" / "use: ad" ฯลฯ

### ไฟล์ที่เก็บ

```
output/{session}/
├── 04_content_creator_ชุดที่1_120530.md   # content + prompts
├── image_ชุด1_1.png                       # รูปที่สร้างจริง
├── image_ชุด1_2.png
├── image_ชุด1_3.png
├── video_ชุด1_1.mp4                       # วิดีโอที่สร้างจริง
└── video_ชุด1_2.mp4
```

### Trade-off: ทำไมไม่แยก graphic designer เป็น agent

ตอนนี้ content_creator เขียน prompt แล้ว `media_gen.py` เรียก API สร้างไฟล์ — ไม่ได้ใช้ LLM ตัวที่ 2 คิดอะไรเพิ่ม

| | แยก graphic designer agent | ใช้ media_gen.py (ตอนนี้) |
|---|---|---|
| ความฉลาด | LLM ตัวที่ 2 ปรับ prompt ได้ | ใช้ prompt ตรงจาก content_creator |
| ความเร็ว | ช้า 2 เท่า (LLM + API) | เร็ว (API อย่างเดียว) |
| ความยืดหยุ่น | ปรับแต่งได้เยอะ | ตายตัว ตาม prompt ที่เขียน |

**เลือกแบบนี้เพราะ:** content_creator มี context ครบ (สินค้า + คู่แข่ง + แคมเปญ) → เขียน prompt ได้ดีอยู่แล้ว ไม่ต้องมี LLM ตัวที่ 2 คิดซ้ำ

**เมื่อไหร่ถึงควรแยก:** ถ้าต้องการ feedback loop — "สร้างรูป → LLM ดู → บอกว่าไม่เข้ากัน → แก้ prompt → สร้างใหม่" → ตอนนั้นถึงควรมี graphic designer agent คุยกับ content_creator

## อนาคต (Phase ถัดไป ยังไม่ได้ implement)

- **โพสต์อัตโนมัติ** — เชื่อม Facebook Graph API + TikTok Business API + ระบบ approval + scheduler
- **Job queue** — เก็บสถานะงานใน DB ปิด browser แล้วเปิดใหม่ก็เห็นงานค้าง
- **Concurrency limiter** — จำกัดการรันขนานเพื่อไม่ให้โดน rate limit
- **MongoDB** — ย้ายจาก JSON เมื่อสินค้าเกินหลักหมื่น

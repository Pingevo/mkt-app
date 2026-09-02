# M6.1 Failure Attribution Audit

**วันทีทำ:** 2026-09-02
**โหมด:** Offline only — ไม่เรียก model, ไม่ web/image/video, ไม่ rerun/rejudge, ไม่แก้ code/config, ไม่ commit/push
**Artifacts ทีใช้:** `data/m6_frontier_uat/20260902_050655/` (outputs, mapping, judge raw/scores, evidence)

---

## 1. Measurement validity / parity

### 1.1 สิ่มทีส่งให้ judge ทั้งหมด

`scripts/m6_judge_runner.py` สร้าง payload ดังนี้ (ต่อ scenario):
- product context จาก `product_db.get_scoped_context_text(product_ids)`
- user_request / quick_brief / agent settings (เหมือน `m6_frontier_uat.SCENARIOS`)
- `S*_X.txt` และ `S*_Y.txt` (blind)
- rubric และ JSON schema

**ข้อจำกัดของ evaluation setup:**
- ไม่ส่งรูปภาพสินค้า (S1) ทำให้ `Brand / asset fit` ทุก scenario ได้ `insufficient_evidence: true` ในบางส่วน
- ไม่ส่ง MKTApp หรือ Frontier `system prompt` ที่แท้จริง ตัดสินเฉพาะ output ต่อ source pack
- Source pack เหมือนกันสำหรับ X/Y ของ scenario เดียวกัน → เป็นธรรมต่อการเปรียบเทียบ X/Y แต่อาจไม่ครอบคลุม context ทีแต่ละ model เห็น

### 1.2 X/Y mapping

จาก `m6_mapping_secret.json`:

| Scenario | X | Y |
|---|---|---|
| S1 | Frontier | MKTApp |
| S2 | Frontier | MKTApp |
| S3 | Frontier | MKTApp |
| S4 | Frontier | MKTApp |

Mapping ไม่เคยถูกส่งให้ judge (ยืนยันว่า `m6_judge_runner.py` ไม่อ่าน mapping ก่อนเรียก API)

### 1.3 ข้อกล่าวอ้าง "วิดีโอคอล" ที S1

- Source pack ทุก scenario ไม่มีคำว่า "วิดีโอคอล" / "video call" หรือข้อมูลทีบอกว่ารองรับ
- ทั้ง MKTApp และ Frontier กล่าวถึง "วิดีโอคอล" ใน output (S1, S2, S3, S4)
- ตัวอย่าง:
  - `S2_X.txt` (Frontier) บรรทัด 19, 28-29: "video call", "video calling"
  - `S4_Y.txt` (MKTApp) บรรทัด 19: "crisp 5MP video call"
  - `S3_X.txt` (Frontier) บรรทัด 42: "วิดีโอคอล" และ `S3_Y.txt` (MKTApp) บรรทัด 16: "โทรวิดีโอคอล"
- นี่คือ **hallucination ของทั้งสองฝั่ง** ไม่ใช่ปัญหาเฉพาะ MKTApp
- Judge ให้คะแนน Factuality ทั้งคู่ใกล้เคียงกัน แต่คงมีผลต่อ Brand/User effort โดยรวม

### 1.4 Validity ของแต่ละ scenario

| Scenario | สถานะ | คำอธิบาย |
|---|---|---|
| S1 | **Invalid parity** | MKTApp ไม่ได้รับคำสั่น "one-page" จริง ในขณะที Frontier ได้รับ ทำให้ Instruction following / User effort ของ MKTApp ถูกลงโทษโดยไม่เป็นธรรม |
| S2 | **Partly invalid parity** | MKTApp รับรูปแบบ bullet/no-table แต่ agent ของ MKTApp ใช้ web_search แล้วสรุปตื้นกว่า; Frontier ใช้ web_search 2 รอบ ได้ context มากกว่า |
| S3 | **Invalid parity** | MKTApp ไม่เห็น/ไม่นํา `budget_max=5000 THB` ไปใช้ ในขณะที Frontier ได้รับ constraint ชัดเจน ทำให้ Instruction following ตก |
| S4 | **Mostly valid** | ทั้งสองฝั่งเห็น user request คล้ายกัน MKTApp ทำโพสต์พร้อมใช้กว่า Frontier ทำ storyboard จบกลางทาง |

---

## 2. Attribution ต่อ hard-gate failure

### 2.1 S1 — hard gate fail (Instruction following Δ = -3.0)

**Observed:**
- Frontier (`S1_X.txt`): ตารางเปรียบเทียบ K2/K3 one-page, ใช้งานง่าย
- MKTApp (`S1_Y.txt`): สเป็คสินค้ายาว, แยก K2/K3 ออกมาไม่เป็น one-page

**Root cause:**
- **input/context parity** เป็นหลัก: MKTApp `product_spec` ใน `config/agents.yaml` ระบุ "ถ้า quick_brief ระบุ one-page ให้ทำตาม" แต่ quick_brief ของ S1 ใน MKTApp เป็นค่าว่าง (หรือไม่ถูกส่งไปถึง agent)
- Frontier `m6_frontier_uat` ใส่ user request ว่า "สร้างสเปคสินค้าแบบ one-page" ไว้ใน `system` โดยตรง
- ดังนั้น MKTApp ไม่ได้รับคำสั่นเดียวกัน จึงไม่สามารถ follow ได้
- **effective prompt/authority order** ต่างกัน: MKTApp อาศัย quick_brief แต่ quick_brief ไม่มี; Frontier มี system-level instruction

**ห้ามสรุปว่า:** MKTApp ทำ one-page ไม่ได้เพราะ model อ่อนกว่า

### 2.2 S2 — hard gate fail (Instruction following Δ = -1.0)

**Observed:**
- Frontier (`S2_X.txt`): วิเคราะห์ลึก มี positioning แต่ข้อมูลภายนอกเยอะและถูกตัดจบ
- MKTApp (`S2_Y.txt`): bullet กระชับ แยก fact/hypothesis ชัด แต่ลึกไม่พอ

**Root cause:**
- **web/evidence path ต่างกัน**: Frontier ใช้ `openrouter:web_search` 2 รอบ ได้ context คู่แข่งมาก; MKTApp `competitor_analysis` ใช้ web_search 1 รอบแล้วรายงานสั้น
- **model capability / final renderer**: MKTApp ใช้ `google/gemini-3.5-flash` ซึ่งถูกกว่าและมี context จำกัดกว่า; นี่เป็นความต่างของ model quality มากกว่า bug
- **effective prompt/authority**: MKTApp อาจได้รับ quick_brief "สรุปแบบ bullet executive brief ห้ามใช้ตาราง" แต่ agent ของ MKTApp ออกแบบมาให้ใช้ web search แล้วสรุป ไม่ได้กำหนด depth/length ชัด

**Factuality ของ MKTApp ดีกว่า Frontier** (+1.0) แสดงว่า MKTApp ไม่ได้ด้อยกว่าทุกมิติ

### 2.3 S3 — hard gate fail (Instruction following Δ = -2.0)

**Observed:**
- Frontier (`S3_X.txt`): ใช้งบ 5,000 THB จัดสรรชัด หลีกเลี่ยง discount/flash/BOGO
- MKTApp (`S3_Y.txt`): บอกว่า "ไม่มีเพดานงบประมาณ" แม้ source pack ระบุ `budget_max=5000`

**Root cause:**
- **input/context parity** เป็นหลัก: MKTApp `campaign_strategy` อาจไม่ได้รับ/ไม่เห็น `budget_max=5000` ในขั้นตอนที่ agent ทำงานจริง
- จาก `agents.yaml` `campaign_strategy` ระบุ "ถ้า quick_brief ระบุรูปแบบ เช่น executive brief ให้ทำตาม" แต่ไม่ได้บังคับให้อ่าน `budget_max`
- Frontier ใส่ `resource_context` โดยตรงใน system prompt
- Judge ตรวจพบ factual error: Y บอกว่าไม่มี budget ceiling ในขณะที source pack ระบุ 5,000

**ห้ามสรุปว่า:** MKTApp ไม่เข้าใจงบประมาณ

### 2.4 S4 — hard gate pass

**Observed:**
- MKTApp (`S4_Y.txt`): โพสต์ TikTok ครบ caption, script, hashtags, CTA
- Frontier (`S4_X.txt`): storyboard วิดีโอ 35-45 วินาที แต่สคริปต์จบกลางทาง

**Root cause:**
- **effective prompt / model behavior**: MKTApp ได้รับ "สร้างโพสต์ TikTok 1 โพสต์" ชัดเจน จึงส่งมอบครบ
- Frontier ได้ `max_tokens=2000` และสร้าง storyboard ยาวจนถูกตัด (max_tokens ไม่พอ) ทำให้ output ไม่ครบ
- นี่เป็น **Frontier output cap / presentation bug** มากกว่า MKTApp ถูกกว่า

---

## 3. Remediation candidates (ยังไม่ implement)

### 3.1 ต้องแก้ production / MKTApp input

| # | ปัญหา | Fix ทีเหมาะสม | Expected impact | Regression tests |
|---|---|---|---|---|
| 1 | S1 MKTApp ไม่ได้รับ "one-page" | ส่ง `quick_brief` / `user_request` ทีระบุ one-page เข้า `product_spec` agent อย่างชัดเจน | S1 Instruction following + User effort ขึ้น | `test_mktapp_product_spec_one_page` |
| 2 | S3 MKTApp ไม่เห็น `budget_max=5000` | ส่ง `budget_max` และ `forbid_tactics` เป็น agent context ของ `campaign_strategy` | S3 Instruction following + Factuality ขึ้น | `test_mktapp_campaign_budget_constraint` |
| 3 | S2 MKTApp วิเคราะห์ตื้น | เพิ่ม depth/length constraint ใน `competitor_analysis` prompt หรือเลือก model ทีมี context มากกว่า | Usefulness / Instruction following ขึ้น | `test_mktapp_competitor_depth` |

### 3.2 ต้องแก้ evaluation setup

| # | ปัญหา | Fix ทีเหมาะสม | Expected impact |
|---|---|---|---|
| 4 | `Brand / asset fit` ขาดข้อมูล | รวม product images, brand guidelines, logo/สีใน source pack สำหรับ S1/S4 | ลด `insufficient_evidence` และ brand ชัดเจนขึ้น |
| 5 | "วิดีโอคอล" hallucination ทั้งสองฝั่ง | เพิ่ม explicit note ใน source pack ว่า "ไม่มีข้อมูล video call" หรือเพิ่ม constraint ใน prompt | ลด factual error ทั้งสองฝั่ง |
| 6 | Frontier S4 สคริปต์จบกลางทาง | ใช้ `max_tokens` สูงกว่า 2000 หรือร้องขอ output แบบไม่ต้องยาว | S4 Frontier Instruction following ขึ้น |

### 3.3 ไม่ใช่ bug — เป็นความต่างของ model quality

- **S2 Frontier วิเคราะห์ลึกกว่า**: เกิดจาก `claude-fable-5.1` ใช้ web_search 2 รอบ + reasoning สูงกว่า `gemini-3.5-flash`
- **MKTApp S2 Factuality ดีกว่า**: แสดงว่า `gemini-3.5-flash` มีจุดแข็งด้าน evidence discipline
- หากต้องการ parity ระดับเดียวกัน ต้อง upgrade MKTApp model หรือเพิ่ม review iteration ไม่ใช่แก้โค้ดทีมี bug

---

## 4. สรุปท้าย

### M6.1 status ทีถูกต้อง
- **ยังไม่ผ่าน** ตามหลักเกณฑ์ non-inferiority ทีตั้งไว้
- แต่ความล้มเหลวส่วนใหญ่ **ไม่ใช่ model capability ล้วน** แต่เกิดจาก **input parity / prompt authority** ระหว่าง MKTApp กับ Frontier
- S1 และ S3 มี parity issue ชัดเจนทีต้องแก้ก่อน rejudge

### Valid findings ทีนำไปแก้ได้
1. MKTApp S1 ขาดคำสั่น one-page
2. MKTApp S3 ไม่เห็น budget_max=5000
3. Frontier S4 ถูก max_tokens 2000 ตัด
4. ทั้งสองฝั่ง hallucinate "วิดีโอคอล" — source pack ต้องชัดเจนขึ้น

### Findings ทีต้อง re-evaluate
- `Brand / asset fit` ทุก scenario ขาด image/brand guidelines ทำให้ judge ตัดสินโดยมี insufficient evidence
- S2 ต้อง re-run หลัง fix input parity แล้วเท่านั้นจึงจะรู้วาเป็น model quality หรือ setup

### Recommended next action (ไม่เสียเงิน)
1. แก้ MKTApp agent context ให้ MKTApp ได้รับ `quick_brief`, `budget_max`, `forbid_tactics`, และ one-page constraint ชัดเจน
2. แก้ `m6_judge_runner.py` ให้ส่ง product images และ brand guidelines ใน source pack
3. แก้ source pack ให้ระบุฟีเจอร์ทีไม่มีข้อมูล (เช่น "ไม่มีข้อมูล video call")
4. Re-run M6.1 ครั้งเดียวหลัง fix (ต้องได้ PO approval และ budget cap ใหม่)
5. ไม่ต้องสร้าง M6.2 หรือ image add-on จนกว่า M6.1 จะผ่าน

### หลักฐานว่าไม่มี paid call
- Audit นี้ใช้เฉพาะการอ่านไฟล์ local และ code/config ทีมีอยู่
- ไม่มี `POST /chat/completions`, `POST /images`, `POST /web_search` หรือ model generation
- ไม่มีการแก้ไข code/config/commit/push

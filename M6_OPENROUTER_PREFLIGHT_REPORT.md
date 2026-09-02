# M6 OpenRouter Pre-flight Report

**วันทีตรวจสอบ:** 2026-09-02  
**โหมด:** Read-only  — ไม่มี `chat/completions`, ไม่มี `web_search` จริง, ไม่มี image/video generation, ไม่มี commit/push  
**วัตถุประสงค์:** เลือก Frontier candidate สำหรับ M6.1 Direct-User baseline และประมาณต้นทุนก่อนมี model call ใด ๆ

---

## 1. สรุปผล OpenRouter read-only queries

- **Account status (`/api/v1/auth/key`):** ไม่ใช่ free tier, weekly limit $15.00, เหลือ ~$10.38
- **Model list (`/api/v1/models`):** 420 models ปรากฏใน list สำหรับ account นี้
- **Endpoint details (`/api/v1/models/{id}/endpoints`):** ตรวจสอบ 7 candidate IDs แล้ว (ไม่มี 404 / ไม่มี "unavailable")
- **Billable calls ทีเกิดขึ้น:** 0 — มีเฉพาะ `GET` read-only

---

## 2. Final recommendation: `anthropic/claude-fable-5.1`

| Model ID | Provider | Context | Modality | Web tool | Pricing (2026-09-02) |
|----------|----------|---------|----------|----------|----------------------|
| **anthropic/claude-fable-5.1** | Anthropic / Amazon Bedrock / Google / Azure | 1,000,000 tokens | text + image + file → text | `openrouter:web_search` | prompt $0.000010, completion $0.000050, web_search $0.010, internal_reasoning $0.000050 |

**เหตุผล:**
- คะแนน `artificial_analysis` สูงสุดในบรรดา model ระดับสูงที่ account นี้เปิดให้ใช้ (intelligence 65.7, coding 81.6, agentic 61.3)
- รองรับ `openrouter:web_search` (verified จาก `tools` ใน endpoint ของ OpenRouter)
- `per_request_limits` = null แสดงว่า account ใช้ได้จริง
- รองรับ input image สำหรับ S1 (product images)

---

## 3. Enforceable run policy

### 3.1 Reserve definitions

- **Calculated reserve** = ค่าใช้จ่ายสูงสุดทีคำนวณได้จาก OpenRouter pricing + `config/qualification.yaml` ต่อ scenario
- **Contingency** = สำรองเพิ่มสำหรับ web-search overrun, reasoning variance และ token variance
- **Approval cap** = calculated reserve + contingency รวมทุก scenario
- **Stop rule:** ก่อนส่งทุก `chat/completions` คำนวณ reserve ของ request นั้น ถ้าทำให้ cumulative > approval cap หยุดทันที

### 3.2 Frontier reserve formula (`claude-fable-5.1`)

Fable มี reasoning บังคับ (`reasoning.mandatory: true`) ดังนั้นต้อง reserve output + reasoning รวมกัน

```
reserve = (input_tokens × $0.000010)
        + ((output_tokens + reasoning_tokens) × $0.000050)
        + (web_search_uses × $0.010)
```

เพื่อ executable จึงตั้ง `max_tokens` รวม output+reasoning ไว้ทีคอลัมน์ `Frontier output cap` ข้างล่าง

### 3.3 Token ceilings และ per-scenario reserve

| # | Scenario | Frontier input cap | Frontier output+reasoning cap | Max web uses | Frontier reserve | MKTApp cap |
|---|----------|-------------------:|------------------------------:|-------------:|-----------------:|-----------:|
| S1 | Multi-product product brief (K2 + K3) | 10,000 tokens | 4,000 tokens | 0 | $0.30 | $0.10 |
| S2 | Competitor analysis + web evidence | 10,000 tokens | 4,000 tokens | 2 | $0.32 | $0.20 |
| S3 | Campaign with budget / constraints | 10,000 tokens | 4,000 tokens | 2 | $0.32 | $0.30 |
| S4 | TikTok 1 post — text/content | 6,000 tokens | 2,000 tokens | 0 | $0.16 | $0.10 |

- **S1 Frontier reserve:** 10,000×0.000010 + 4,000×0.000050 = $0.10 + $0.20 = $0.30
- **S2/S3 Frontier reserve:** 10,000×0.000010 + 4,000×0.000050 + 2×0.010 = $0.32
- **S4 Frontier reserve:** 6,000×0.000010 + 2,000×0.000050 = $0.06 + $0.10 = $0.16

### 3.4 MKTApp cap (from `config/qualification.yaml`)

- **S1:** 2 text × $0.05 = $0.10
- **S2:** 1 web_search × $0.15 + 1 text × $0.05 = **$0.20**
- **S3:** 1 web_search × $0.15 + 3 text × $0.05 = $0.30
- **S4 text:** 2 text × $0.05 = $0.10
- **S4 image add-on (optional):** 1 image × $0.08 = $0.08

---

## 4. Web parity rule

- **ทั้งสองฝั่งใช้ live `openrouter:web_search` สำหรับ S2/S3**
- **MKTApp side:** `competitor_analysis` และ `campaign_strategy` เปิด `web_search: true` ตาม `agents.yaml`
- **Frontier side:** ส่ง `tools: [{"type": "openrouter:web_search"}]` ใน `chat/completions` ของ `anthropic/claude-fable-5.1`
- **Enforceable tool limit:** Frontier จำกัด 2 `web_search` tool uses ต่อ request (`max_web_uses = 2`) หาก model เรียกครั้งที 3 ขึ้นไป ให้หยุด request ทันที
- **MKTApp side:** 1 `web_search` `chat/completions` ต่อ S2/S3 (model จัดการ search loop ภายใน 1 request)
- **ห้าม exclude scenario หลังเห็น output** — ถ้า Frontier ไม่เรียก web_search ให้บันทึก `web_not_invoked` แต่ไม่ตัดคะแนน

---

## 5. S4 separation

- **M6.1 mandatory:** เปรียบเทียบ text/content ของ TikTok post (caption, script, hashtags, content plan)
- **Optional media add-on:** เปรียบเทียบ image output โดยใช้ `openai/gpt-5-image-mini` หรือ `google/gemini-3.1-flash-image`
- **ผลกระทบ:** image comparison ไม่มีผลต่อ pass/fail ของ M6.1 text parity

---

## 6. Stop rules

1. ก่อนส่ง `chat/completions` คำนวณ reserve — ถ้า cumulative > approval cap หยุด
2. Frontier เรียก `web_search` เกิน 2 ครั้ง ต่อ request หยุด
3. Frontier output เกิน `max_tokens` ทีตั้งไว้ หยุด/truncate ไม่ส่ง repair
4. MKTApp `PaidCallGuard` บล็อกถ้าครบ hard call cap ตาม `qualification.yaml`
5. Image add-on ล้มเหลว ให้ข้าม image comparison ไม่ skip text comparison

---

## 7. Final M6.1 cap table

| รายการ | MKTApp cap | Frontier cap |
|--------|-----------:|-------------:|
| S1 Multi-product brief | $0.10 | $0.30 |
| S2 Competitor + web | $0.20 | $0.32 |
| S3 Campaign + web | $0.30 | $0.32 |
| S4 TikTok text | $0.10 | $0.16 |
| **Calculated subtotal (mandatory)** | **$0.70** | **$1.10** |
| **Total calculated reserve** | — | **$1.80** |
| Contingency | — | $0.20 |
| **Approval cap (PO)** | — | **$2.00** |
| **Web rule** | — | live `openrouter:web_search`, max 2 uses/req for S2/S3 |
| **Image add-on (optional)** | +$0.08 | +$0.05 |

ถ้าเปิด S4 image add-on: **Total UAT cap = $2.13** ($2.00 + $0.08 + $0.05)

---

## 8. สิ่มทีต้องขอ Product Owner อนุมัติ

1. **Exact Frontier model:** `anthropic/claude-fable-5.1`
2. **Baseline level:** M6.1 Direct-User only
3. **Web parity:** live `openrouter:web_search`, max 2 uses/req ฝั่ง Frontier
4. **S4 image add-on:** เปิด/ปิด
5. **Total approval cap:** $2.00 (หรือ $2.13 ถ้าเปิด image)
6. **Reviewers:** รายชื่อ + domain/language
7. **Non-inferiority threshold:** 0.5-point rule

---

## 9. หลักฐานว่าไม่มี billable model call

API calls ทีเกิดขึ้นในพรีฟลายทีเดียวนี้ทั้งหมดเป็น **read-only `GET`**:

- `GET https://openrouter.ai/api/v1/auth/key`
- `GET https://openrouter.ai/api/v1/models`
- `GET https://openrouter.ai/api/v1/models/{id}/endpoints` สำหรับ 7 candidate IDs:
  - `anthropic/claude-fable-5.1`
  - `anthropic/claude-opus-5`
  - `anthropic/claude-sonnet-5`
  - `openai/gpt-5.6-sol`
  - `openai/gpt-5.5`
  - `x-ai/grok-4.6`
  - `google/gemini-3.7-flash`

**ไม่มีการเรียกใช้:**
- `POST /chat/completions`
- `POST /images` หรือ image generation endpoint ใด ๆ
- `POST /web_search` หรือ `POST /embeddings`
- ไม่มี model token ถูก gen
- ไม่มีการแก้ไข code/config/prompt/test
- ไม่มี commit หรือ push

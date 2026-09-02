# M6 LLM-as-a-Judge Pre-flight Report

**วันทีตรวจสอบ:** 2026-09-02
**โหมด:** Read-only — ไม่เรียก `chat/completions`, ไม่สร้าง web/image/video, ไม่อ่าน/ส่ง `m6_mapping_secret.json` ให้ judge, ไม่ rerun M6, ไม่ commit/push
**วัตถุประสงค์:** เลือก independent top-tier judge สำหรับ blind scoring ของ M6.1 paired outputs โดยอัตโนมัติ

---

## 1. Independent judge candidate selected: `openai/gpt-5.6-sol`

### 1.1 ทำไมถึงเลือก
- **ไม่ใช่ตระกูล Anthropic** — ต่างครอบครัวจาก `anthropic/claude-fable-5.1` (Frontier baseline)
- **Top-tier reasoning flagship** — OpenAI GPT-5.6 Sol ทีสุดของ provider นี้, strong ด้าน complex reasoning, coding, agentic workflows
- **Account ใช้ได้จริง** — `/api/v1/models/{id}/endpoints` คืน `per_request_limits: null`
- **รองรับ input image** — ถ้า S1 ต้องส่งรูปสินค้าให้ judge ดูตาม source pack
- **ไม่ต้องใช้ web** — judge รับ source pack + outputs ครบแล้ว

### 1.2 Pricing (endpoint `OpenAI | openai/gpt-5.6-sol-20260709`)

| รายการ | ราคาต่อ token |
|---|---:|
| prompt | $0.000001 |
| completion | $0.000005 |
| context length | 1,050,000 tokens |
| max completion tokens | 128,000 tokens |
| web_search | $0.010 (ไม่ใช้) |

---

## 2. Judge call design

### 2.1 หลักการ
- 1 call ต่อ 1 scenario แยกกันหมด (S1–S4) ป้องกัน context ปนกัน
- ส่งเฉพาะ:
  - user-visible input/source pack ของ scenario นั้น
  - Output X (anonymized)
  - Output Y (anonymized)
  - รูปภาพสินค้า (เฉพาะ S1, ถ้าต้องการ)
  - rubric 1–5 ทั้ง 6 มิติ
  - rule ห้ามเดา source / ต้องคืน strict JSON
- **ไม่ส่ง `m6_mapping_secret.json`** และไม่มี clue ใด ๆ บอกว่า X/Y คือ MKTApp/Frontier

### 2.2 Prompt template (per scenario)

```text
System:
You are an impartial, independent judge evaluating two anonymous AI-generated outputs (X and Y) for a Thai marketing task. You must not guess which system produced which output. Score each output independently on a 1–5 scale for every dimension. Return ONLY a strict JSON object matching the requested schema. No markdown, no explanation outside JSON.

User:
--- Source pack ---
[scenario-specific user-visible source pack]

--- Output X ---
[anonymized output text]

--- Output Y ---
[anonymized output text]

--- Rubric (1–5) ---
Usefulness: 1=no value, 5=highly useful
Factuality: 1=factual errors, 5=fully supported by source pack
Instruction following: 1=missed key requirements, 5=perfect compliance
Brand / asset fit: 1=misaligned, 5=well aligned with brand and product
Evidence quality: 1=no evidence, 5=strong cited/grounded evidence
User effort: 1=needs heavy editing, 5=ready to use

--- Rules ---
- Do not identify or mention which output is from which source.
- Flag any factual claim that contradicts the source pack.
- If the source pack does not contain enough evidence to score a dimension, set "insufficient_evidence": true and explain why.
- Return valid JSON only.
```

### 2.3 ข้อมูลทีส่งแยกต่อ scenario

| # | Scenario | Source pack ประกอบ |
|---|---|---|
| S1 | Multi-product brief (K2 + K3) | `product_db.get_scoped_context_text(["Lagenio K2", "Lagenio K3"])` + รูปสินค้า (ถ้าต้อง) + คำสั่น one-page brief |
| S2 | Competitor analysis | `product_db.get_scoped_context_text(["Lagenio K2"])` + quick brief + agent settings (direct/deep/positioning) |
| S3 | Campaign strategy | `product_db.get_scoped_context_text(["Lagenio K2"])` + quick brief + budget/constraints |
| S4 | TikTok content | `product_db.get_scoped_context_text(["Lagenio K2"])` + quick brief + platform TikTok |

---

## 3. Required JSON output schema (after reveal mapping)

```json
{
  "scenario_id": "S1",
  "judge_model": "openai/gpt-5.6-sol",
  "mktapp_score": {
    "Usefulness": 4,
    "Factuality": 3,
    "Instruction following": 4,
    "Brand / asset fit": 3,
    "Evidence quality": 3,
    "User effort": 4
  },
  "frontier_score": {
    "Usefulness": 3,
    "Factuality": 4,
    "Instruction following": 5,
    "Brand / asset fit": 3,
    "Evidence quality": 4,
    "User effort": 4
  },
  "delta_mktapp_minus_frontier": {
    "Usefulness": 1.0,
    "Factuality": -1.0,
    "Instruction following": -1.0,
    "Brand / asset fit": 0.0,
    "Evidence quality": -1.0,
    "User effort": 0.0
  },
  "scores": {
    "Usefulness": {
      "mktapp_score": 4,
      "frontier_score": 3,
      "delta_mktapp_minus_frontier": 1.0,
      "winner": "MKTApp",
      "tie": false,
      "reason": "short decisive reason",
      "insufficient_evidence": false,
      "insufficient_evidence_reasons": []
    },
    "Factuality": {
      "mktapp_score": 3,
      "frontier_score": 4,
      "delta_mktapp_minus_frontier": -1.0,
      "winner": "Frontier",
      "tie": false,
      "reason": "short decisive reason",
      "factual_errors": [
        {
          "output": "MKTApp",
          "claim": "the product has ECG sensor",
          "source_reference": "Technical spec table line 23: ECG not listed",
          "correction": "remove ECG claim"
        }
      ],
      "insufficient_evidence": false,
      "insufficient_evidence_reasons": []
    },
    "Instruction following": {
      "mktapp_score": 4,
      "frontier_score": 5,
      "delta_mktapp_minus_frontier": -1.0,
      "winner": "Frontier",
      "tie": false,
      "reason": "short decisive reason",
      "insufficient_evidence": false,
      "insufficient_evidence_reasons": []
    },
    "Brand / asset fit": {
      "mktapp_score": 3,
      "frontier_score": 3,
      "delta_mktapp_minus_frontier": 0.0,
      "winner": null,
      "tie": true,
      "reason": "short decisive reason",
      "brand_asset_advantage_pass": false,
      "insufficient_evidence": false,
      "insufficient_evidence_reasons": []
    },
    "Evidence quality": {
      "mktapp_score": 3,
      "frontier_score": 4,
      "delta_mktapp_minus_frontier": -1.0,
      "winner": "Frontier",
      "tie": false,
      "reason": "short decisive reason",
      "insufficient_evidence": false,
      "insufficient_evidence_reasons": []
    },
    "User effort": {
      "mktapp_score": 4,
      "frontier_score": 4,
      "delta_mktapp_minus_frontier": 0.0,
      "winner": null,
      "tie": true,
      "reason": "short decisive reason",
      "insufficient_evidence": false,
      "insufficient_evidence_reasons": []
    }
  },
  "overall": {
    "mktapp_mean_score": 3.50,
    "frontier_mean_score": 3.83,
    "mean_delta_mktapp_minus_frontier": -0.33,
    "hard_gate_pass": false,
    "brand_asset_advantage_pass": false,
    "winner": "Frontier",
    "tie": false,
    "decisive_reasons": [
      "Frontier has fewer factual errors",
      "MKTApp follows instructions slightly better"
    ],
    "confidence": 0.82,
    "insufficient_evidence": false,
    "insufficient_evidence_reasons": []
  }
}
```

---

## 4. Estimated cost per scenario

ข้อมูล char count จาก `data/m6_frontier_uat/20260902_050655/outputs/`:

| Scenario | Input chars (est. tokens) | Output X/Y chars (est. tokens) | Prompt tokens | Completion cap | Prompt cost | Completion cost | Per-scenario cost |
|---|---:|---:|---:|---:|---:|---:|---:|
| S1 | ~3,500 (1,750) | 5,131 + 12,065 (~8,600) | 11,000 | 4,000 | $0.0110 | $0.0200 | **$0.0310** |
| S2 | ~2,500 (1,250) | 8,909 + 5,156 (~7,000) | 8,500 | 4,000 | $0.0085 | $0.0200 | **$0.0285** |
| S3 | ~2,500 (1,250) | 10,501 + 10,308 (~10,400) | 12,000 | 4,000 | $0.0120 | $0.0200 | **$0.0320** |
| S4 | ~2,500 (1,250) | 4,768 + 3,411 (~4,100) | 5,500 | 4,000 | $0.0055 | $0.0200 | **$0.0255** |
| **Total** | — | — | **37,000** | **16,000** | **$0.0370** | **$0.0800** | **$0.1170** |

- **Contingency (10% token variance):** ~$0.012
- **Expected cost (4 calls, no retry):** **$0.117 total**
- **Approved judge cap:** **$0.15 total**
- **Absolute stop cap:** **$0.20 total**

---

## 5. Non-inferiority rule (MKTApp vs Frontier)

เป้าหมาย: ตรวจสอบวัา **MKTApp non-inferior ต่อ Frontier** สำหรับ M6.1 direct-user baseline

1. แปลง JSON judge `X`/`Y` เป็น `mktapp_score` และ `frontier_score` ด้วย `m6_mapping_secret.json` หลัง judge จบ
2. คำนวณ `d = mktapp_score - frontier_score` สำหรับทุก dimension
3. **MKTApp non-inferior ต่อมิติ:** `d >= -0.5`
4. **MKTApp non-inferior โดยรวม:** `mean(d) >= -0.5`
5. **Hard dimensions (Factuality, Instruction following):**
   - ถ้า `d < -0.5` ใน scenario ใด สำหรับ hard dimension ใด → **M6 fail pending remediation**
   - สะท้อนใน `overall.hard_gate_pass` ของทุก scenario
6. **Insufficient evidence rule:**
   - ถ้า dimension ใดมี `insufficient_evidence: true` scenario นั้นไม่ถูกนับ pass/fail อัตโนมัติ สำหรับ dimension นั้น
   - บันทึก `insufficient_evidence_reasons` แล้วส่งให้ Product Owner ตัดสิน

## 6. Product value gate: Brand / asset fit

1. สำหรับแต่ละ scenario คำนวณ `d_brand = mktapp_score - frontier_score` ของ dimension **Brand / asset fit**
2. `brand_asset_advantage_pass` สำหรับ scenario นั้น = `true` เมือ `d_brand >= +0.5`
3. นับจำนวน scenarios ทีผ่าน gate
4. **MKTApp ผ่าน product value gate เมื่อผ่านอย่างน้อย 50% ของ scenarios ทีมี evidence** (นั่นคือ ≥ 2/4 สำหรับ M6.1)
5. ถ้า `insufficient_evidence: true` ใน **Brand / asset fit** ของ scenario ใด scenario นั้นไม่ถูกนับเข้า 50% และบันทึกเหตุผล

### 6.1 Corrected scoring table

| Condition | Math | Outcome |
|---|---|---|
| Per-dimension non-inferiority | `d = mktapp_score - frontier_score >= -0.5` | MKTApp acceptable in that dimension |
| Overall non-inferiority | `mean(d) >= -0.5` | MKTApp acceptable overall |
| Hard dimension pass (per scenario) | `d_Factuality >= -0.5` AND `d_Instruction_following >= -0.5` | `overall.hard_gate_pass = true` |
| Hard dimension fail (per scenario) | `d_Factuality < -0.5` OR `d_Instruction_following < -0.5` | **M6 fail pending remediation** |
| Brand value advantage (per scenario) | `d_Brand_asset_fit >= +0.5` | `Brand / asset fit.brand_asset_advantage_pass = true` |
| Product value gate (overall) | count(`brand_asset_advantage_pass`) / count(attempted) >= 0.5 | MKTApp passes product value gate |
| Insufficient evidence | `insufficient_evidence = true` | Scenario/dimension not auto-counted; record reason for PO |

---

## 7. Reveal mapping (post-judge)

1. Judge รับแค่ `S1_X.txt`, `S1_Y.txt`, ... `S4_Y.txt` — ไม่มี `m6_mapping_secret.json`
2. หลัง judge คืน JSON พร้อม `X`/`Y` scores
3. ใช้ `data/m6_frontier_uat/20260902_050655/m6_mapping_secret.json` ที่ไม่เคยถูกส่ง แปลง `X`/`Y` กลับเป็น `MKTApp`/`Frontier`
4. คำนวณ `d` และ non-inferiority ตาม section 5
5. คำนวณ product value gate ตาม section 6
6. การ reveal mapping เกิดขึ้นหลัง judge ทังหมดจบแล้วเท่านั้น

---

## 8. Automated judge only — not M7 approver

- `openai/gpt-5.6-sol` เป็น **automated blind scoring tool** เท่านั้น
- ผลลัพธ์คือ **candidate scoring report** สำหรับ Product Owner
- ไม่ใช่ final approval ของ M7; Product Owner ยังต้องอนุมัติหลัง reveal mapping
- ถ้า `hard_gate_pass` ผิด หรือ `insufficient_evidence` มีมาก PO ตัดสินใจวา fail/extend/re-run

## 9. Approved judge cap (no retry/rerun)

| รายการ | ค่า |
|---|---:|
| Expected cost (4 calls) | $0.117 |
| Approved judge cap | $0.15 |
| Absolute stop cap | $0.20 |
| จำนวน calls | 4 calls (S1–S4) |
| Retry / rerun | ไม่มี |

- หากผล judge ไม่อ่านออก/invalid JSON ให้บันทึก error แล้วส่ง PO ตัดสินใจ ไม่ rerun โดยอัตโนมัติ

---

## 10. Evidence: no billable model call

API calls ทีเกิดขึ้นในพรีฟลายนี้ทั้งหมดเป็น **read-only `GET`**:

- `GET https://openrouter.ai/api/v1/models/openai/gpt-5.6-sol/endpoints`
- `GET https://openrouter.ai/api/v1/models/openai/gpt-5.5/endpoints`
- `GET https://openrouter.ai/api/v1/models/x-ai/grok-4.6/endpoints`
- `GET https://openrouter.ai/api/v1/models/google/gemini-3.7-flash/endpoints`
- `GET https://openrouter.ai/api/v1/models` (filter 4 IDs)

**ไม่มีการเรียกใช้:**
- `POST /chat/completions`
- `POST /images`
- `POST /videos`
- `POST /web_search`
- ไม่มี model token ถูก generate
- ไม่ส่ง/อ่าน `m6_mapping_secret.json` ใน judge path

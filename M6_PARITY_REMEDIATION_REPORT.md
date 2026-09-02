# M6 Parity-Harness Remediation

**วันทีทำ:** 2026-09-02
**โหมด:** Offline only — ไม่เรียก model, ไม่ web/image/video, ไม่ rerun M6, ไม่ commit/push
**ขอบเขต:** แก้ M6 harness/evaluation setup เท่านั้น ไม่แก้ production Agent/model/config

---

## 1. สิ่งทีแก้

### 1.1 S1 — one-page parity

- `m6_frontier_uat.SCENARIOS`: S1 `quick_brief` เปลี่ยนจากค่าว่างเป็น `"one-page"`
- `m6_judge_runner.SCENARIOS`: S1 `quick_brief` เปลี่ยนเป็น `"one-page"`
- `qual_runner.run_case`: เพิ่มรับ/ส่ง `resource_context` ให้ `BaseAgent` (ปัญหาหลักของรอบแรกคือ MKTApp ไม่เห็น resource_context)
- `m6_frontier_uat._run_mktapp_scenario`: ส่ง `quick_brief` + `resource_context` ผ่าน `qual_runner.run_case`

### 1.2 S3 — budget / forbid_tactics เข้า MKTApp

- `qual_runner.run_case`: ส่ง `resource_context` ลง `BaseAgent` ซึ่ง append เข้า user prompt
- `m6_frontier_uat._run_mktapp_scenario`: `resource_context` รวม `budget_max=5000` และ `forbid_tactics=[heavy_discount,flash,bogo]`
- ทั้ง MKTApp และ Frontier ได้รับข้อมูลเดียวกัน

### 1.3 Brand / assets parity

- เพิ่ม `_get_brand_guidelines()` ใน `m6_frontier_uat.py` และ `m6_judge_runner.py` อ่านจาก `brand/brand_profile.md`, `tone_of_voice.md`, `visual_guidelines.md`, `terms.json`
- `m6_frontier_uat._build_frontier_messages`: แทรก brand guidelines และ `SOURCE_GROUNDED_RULE` ใน `resource_context` + system prompt
- `m6_frontier_uat._run_mktapp_scenario`: ส่ง brand guidelines + source-grounded rule ผ่าน `resource_context`
- `m6_judge_runner._get_source_pack`: แทรก brand guidelines, source-grounded rule และ product image paths
- ทั้งสองฝั่งใช้ชุด brand guidelines เดียวกัน; ไม่มีการส่ง MKTApp validator หรือ hidden assets ให้ Frontier

### 1.4 Shared hallucination rule

- `SOURCE_GROUNDED_RULE` ชุดเดียวกัน:
  "ห้ามอ้างคุณสมบัติหรือความสามารถใด (เช่น video call) หากไม่มีข้อมูลรองรับโดยตรงใน source pack; หากไม่พบข้อมูลให้ระบุว่า 'ไม่มีข้อมูลระบุ' เท่านั้น"
- ถูกส่งให้ทั้ง MKTApp, Frontier และ judge ทุก scenario
- บันทึกว่านี่เป็น **evaluation constraint** ไม่ใช่การแก้ปัญหาเฉพาะ MKTApp

### 1.5 S4 — Frontier max_tokens

- `SCENARIOS` S4 `frontier_output_tokens` จาก 2000 → 3500
- `frontier_reserve` S4 จาก 0.13 → 0.24 (คิดจาก 5000 input + 1100 image + 3500 output)
- Output ไม่ควรถูกตัดกลางทางอีก

### 1.6 S2 — open quality question

- ไม่มีการเปลี่ยน production model หรือเพิ่ม web calls
- MKTApp `competitor_analysis` ยังคงใช้ `google/gemini-3.5-flash` + web 1 รอบ
- Frontier ใช้ `anthropic/claude-fable-5.1` + web 2 รอบ
- ความแตกต่าง depth/quality ของ S2 ยังเป็นคำถามเปิดเท่านั้น

---

## 2. Corrected scenario inputs

| Scenario | MKTApp quick_brief | MKTApp resource_context | Frontier quick_brief | Frontier output tokens |
|---|---|---|---|---|
| S1 | `one-page` | brand + rule | (via user_prefix) | 4000 |
| S2 | `สรุปแบบ bullet executive brief ห้ามใช้ตาราง` | brand + rule + competitor_types/direct/deep/positioning | (system) | 4500 |
| S3 | `executive brief` | brand + rule + budget_max=5000 + forbid_tactics | (system) | 4500 |
| S4 | `เด็กเดินทางคนเดียวปลอดภัย` | brand + rule | (system) | 3500 |

---

## 3. Dry-run budget ใหม่

```
MKTApp reserve:   $0.70
Frontier reserve: $1.72
Contingency:      $0.20
Required worst-case cap: $2.62
Approved cap:     $2.00
Status: BLOCKED — requires separate PO budget approval for $2.62
```

คำนวณจาก `m6_frontier_uat._preflight_budget_check()` โดยไม่มี paid call.

---

## 4. Test results

- Targeted tests: `31 passed`
- Full offline suite: `844 passed, 43 warnings`
- `git diff --check`: clean

Regression tests ใหม่:
- `test_s4_max_tokens_non_truncating`
- `test_s1_one_page_brief`
- `test_s3_budget_tactics_in_frontier_prompt`
- `test_source_grounded_rule_in_frontier_messages`
- `test_brand_guidelines_in_frontier_messages`
- `test_mktapp_dry_run_carries_one_page_and_grounded_rule`
- `test_mktapp_dry_run_s3_budget_and_tactics`
- `test_source_pack_has_grounded_rule_and_brand`
- `test_s1_judge_quick_brief_one_page`
- `test_s3_judge_resource_context_budget`

---

## 5. M6 status

**M6.1 initial run invalid for parity verdict; artifacts retained for diagnostic evidence; fair rerun requires separate PO budget approval.**

---

## 6. ยืนยัน no paid call

- ไม่มี `POST /chat/completions`, `POST /images`, `POST /web_search` หรือ model generation
- ไม่มีการ commit หรือ push
- ไม่แก้ production `src/` หรือ `agents.yaml` / `agent_instructions.json`
- `qual_runner.py` ถูกแก้เฉพาะส่ง `resource_context` เข้า `BaseAgent` (harness/evaluation-only อยู่แล้ว)

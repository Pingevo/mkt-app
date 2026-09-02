# M6 Parity Code-Review Report

**วันทีทำ:** 2026-09-02
**โหมด:** Offline only — ไม่เรียก model, ไม่ web/image/video, ไม่ rerun M6, ไม่ commit/push
**Files reviewed:** `scripts/m6_frontier_uat.py`, `scripts/qual_runner.py`, `scripts/m6_judge_runner.py`, `tests/test_m6_frontier_uat.py`, `tests/test_m6_judge_runner.py`

---

## 1. S3 settings parity — fixed

### Finding
รอบแรก M6 ส่ง `budget_max=5000` และ `forbid_tactics` ผ่าน `resource_context` เท่านั้น ซึ่งเป็นทางลัด prompt ไม่ใช่ typed UI/Agent Settings path ที user จริงใช้.

### Fix
- เพิ่ม `_parse_agent_settings_override()` ใน `scripts/m6_frontier_uat.py` เพื่ออ่าน `Agent settings:` จาก `resource_context`
- `m6_frontier_uat._run_mktapp_scenario` ส่ง `agent_settings_override` ให้ `qual_runner.run_case()`
- `qual_runner` เดิมรองรับ `agent_settings_override` และเขียน `config/agent_instructions.json` แบบเดียวกับ UI
- `m6_frontier_uat._run_mktapp_scenario` backup `agent_instructions.json` ก่อนเรียก `qual_runner` และ restore ใน `finally` ทุกกรณี ป้องกัน config production เปลี่ยนแปลงถาวร
- ตัด typed settings ออกจาก prompt `resource_context` เหลือเฉพาะ brand guidelines และ `SOURCE_GROUNDED_RULE`

### Regression tests
- `test_mktapp_dry_run_s3_budget_and_tactics`: ตรวจ `agent_settings_override` มี `budget_max=5000` และ `forbid_tactics`
- `test_s3_agent_settings_parsed_for_typed_ui`: ตรวจ parser แยก bracketed list ถูกต้อง
- ตรวจ `resource_context` ไม่เหลือ `5000` หรือ `forbid_tactics` อีกต่อไป

---

## 2. Brand/image parity — fixed

### Finding
- Frontier `build_multimodal_content()` ใช้ data URL จริง แต่ judge แทรกเฉพาะ local filesystem path string ใน source pack
- MKTApp รับ product images ผ่าน `qual_runner`/`BaseAgent` ตาม flow production อยู่แล้ว

### Fix
- `m6_judge_runner._build_messages()` ใช้ `build_multimodal_content()` แปลง image เป็น data URL ก่อนส่งให้ judge
- `m6_judge_runner._get_image_paths_for_judge()` คืน tuple path แล้วส่งผ่าน `build_multimodal_content()`
- source pack ของ judge ไม่แทรก path string อีกต่อไป

### Regression tests
- `test_frontier_messages_have_data_url_images`: ตรวจ `messages[1].content` มี `image_url` ที `url` ขึ้นต้น `data:image/`
- `test_judge_messages_have_data_url_images_for_s1`: ตรวจ judge user content เป็น multimodal และมี data URL

---

## 3. Shared hallucination rule — verified

### Finding
กฎ "ห้ามอ้าง video call" ต้องเป็น user-visible/source constraint เดียวกันทั้งสองฝั่ง.

### Verification
- `m6_frontier_uat.SOURCE_GROUNDED_RULE` ถูกแทรกใน:
  - Frontier `system` prompt
  - Frontier `resource_context` / user prompt
  - MKTApp `resource_context` ผ่าน `_run_mktapp_scenario`
- `m6_judge_runner.SOURCE_GROUNDED_RULE` ถูกแทรกใน:
  - Judge `system` prompt
  - Judge source pack
- ไม่มี hidden advantage ให้ฝั่งใด

### Regression tests
- `test_source_grounded_rule_in_frontier_messages`
- `test_mktapp_dry_run_carries_one_page_and_grounded_rule`
- `test_source_pack_has_grounded_rule_and_brand`

---

## 4. Budget — verified

```
MKTApp reserve:   $0.70
Frontier reserve: $1.72
Contingency:      $0.20
Required cap:     $2.62
Approved cap:     $2.00
```

- `APPROVAL_CAP` ยังคง $2.00; ไม่มีการปรับ cap เอง
- Fair rerun ต้องขอ PO อนุมัติ cap ใหม่ $2.62 หรือสูงกว่าก่อน

---

## 5. Test results

- Targeted: `34 passed`
- Full offline suite: `847 passed, 43 warnings`
- `git diff --check`: clean

---

## 6. No paid call

- ไม่มี `POST /chat/completions`, `POST /images`, `POST /web_search` หรือ model generation
- ไม่ commit/push
- ไม่แก้ production `src/`, `agents.yaml`, `agent_instructions.json` ถาวร
- `qual_runner` ยังเขียน `agent_instructions.json` ระหว่างรัน แต่ `m6_frontier_uat` backup/restore ทุกกรณี

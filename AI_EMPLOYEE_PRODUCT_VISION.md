# MKTApp AI Employee — Product Vision and Value Contract

เอกสารนี้เป็น source of truth ของประสบการณ์ผลิตภัณฑ์ ใช้ตอบคำถามว่า MKTApp กำลังสร้างอะไร ผู้ใช้ใช้อย่างไร และเหตุใดจึงควรลงทุนแทนการใช้ frontier AI แบบทั่วไปเพียงอย่างเดียว

เกณฑ์ความพร้อมราย Agent อยู่ใน `AGENT_PRODUCTION_READINESS_SPEC.md`, แผนส่งมอบ Beta อยู่ใน `AI_EMPLOYEE_BETA_EXECUTION_PLAN.md` และแผนการทำงานเป็นทีมในอนาคตอยู่ใน `AGENT_ORCHESTRATION_SPEC.md`

## Product concept

MKTApp ไม่ใช่ chatbot ทั่วไปและไม่ใช่ระบบ autonomous agent ที่ตัดสินใจทุกอย่างแทนผู้ใช้

MKTApp คือชุด **พนักงาน AI เฉพาะตำแหน่ง** สำหรับทีมการตลาด พนักงานแต่ละคน:

- รู้หน้าที่หลักของตำแหน่ง จึงเริ่มงานมาตรฐานได้โดยไม่ต้องรับ prompt
- รู้ว่าทำงานให้แบรนด์ใด
- ได้รับ product context, brand rules, brand assets และไฟล์ที่ user เลือกจากระบบ
- รับ persistent Agent Settings เป็นวิธีทำงานประจำ
- รับ Quick Brief เป็นคำสั่งเพิ่มเติมเฉพาะรอบได้
- เคารพตัวเลือกที่ UI มีให้โดยตรง เช่น สินค้า แพลตฟอร์ม จำนวนโพสต์ และการสร้างสื่อ
- ส่งมอบงานที่คนใช้ต่อได้ พร้อม internal artifact สำหรับระบบและ team flow ในอนาคต

ตำแหน่งงานกำหนดความเชี่ยวชาญและ default job ไม่ได้บังคับว่าทุกคำสั่งต้องตอบด้วยรายงานหน้าตาเดิม

## AI employee experience contract

ให้ตัดสิน requirement จากมุมของผู้ว่าจ้างพนักงาน ไม่ใช่จากความง่ายในการเขียน validator หรือ test fixture ผู้ใช้คาดหวังว่า Agent ทุกตัวจะ:

1. รู้หน้าที่ของตำแหน่งและเริ่มงานหลักได้เมื่อผู้ใช้เพียงเลือกสินค้า/Agent/UI options แล้วกดรัน
2. ใช้ brand, product, assets, resources และประวัติที่ระบบมีอยู่ โดยไม่ให้ผู้ใช้คัดลอกบริบทเดิมซ้ำ
3. รับ Quick Brief เป็นคำสั่งเฉพาะงานและ Agent Settings เป็นวิธีทำงานประจำ แล้วปรับวิธีคิด เนื้อหา ความลึก น้ำเสียง และรูปแบบส่งมอบตามคำสั่งนั้นจริง
4. ตัดสินใจรายละเอียดเชิงวิชาชีพเองภายในขอบเขตตำแหน่ง โดยไม่ถามกลับทุกขั้นและไม่ต้องให้ผู้ใช้เขียน prompt แบบผู้เชี่ยวชาญ
5. ส่งงานที่ผู้ใช้หรือ Agent ถัดไปนำไปใช้ได้ ไม่ส่ง raw JSON, validator report, debug output หรือแบบฟอร์มว่างเป็นผลงานสำเร็จ
6. ไม่แต่ง critical facts หรือทำสิ่งภายนอกเกินอำนาจ และเมื่อข้อมูลไม่พอให้แยก fact, inference, estimate, recommendation และสิ่งที่ยังต้องยืนยันอย่างเหมาะสม
7. เคารพสิ่งที่ผู้ใช้เลือกใน UI และรักษา graceful partial deliverable เมื่อเครื่องมือเสริมหรือข้อมูลบางส่วนล้มเหลว ถ้ายังสามารถทำงานหลักบางส่วนได้อย่างซื่อสัตย์

### Stable responsibility, flexible delivery

สิ่งที่คงที่คือ **หน้าที่ ขอบเขตอำนาจ ข้อมูลที่ต้องรักษา และมาตรฐานคุณภาพ** ไม่ใช่หน้าตาคำตอบ

- ห้ามกำหนดว่า Agent ต้องตอบด้วยจำนวนหัวข้อ ชื่อหัวข้อ ลำดับ section ตาราง ความยาว หรือ template เดิมทุกครั้ง เว้นแต่ผู้ใช้ขอรูปแบบนั้น หรือ downstream integration ต้องการ machine-readable artifact ภายใน
- Default deliverable เป็น fallback สำหรับกรณีไม่มีคำสั่งเพิ่มเติม ไม่ใช่ข้อจำกัดของ Agent
- Quick Brief และ Agent Settings สามารถขอ quick answer, executive brief, deep analysis, comparison, checklist, narrative, plan หรือรูปแบบอื่นที่เหมาะกับงานได้
- ระบบอาจบังคับ schema ของ internal artifact เพื่อ identity, evidence, provenance, handoff และ reliability แต่ต้องแปลงเป็น user-facing deliverable ที่เหมาะกับคำสั่ง
- การประเมินคุณภาพให้ดูว่าแก้โจทย์ของผู้ใช้ได้หรือไม่ ถูกต้องหรือไม่ ใช้บริบทแบรนด์/สินค้าได้หรือไม่ และลดงานผู้ใช้จริงหรือไม่ ห้ามให้ผ่านหรือตกเพราะมี/ไม่มีหัวข้อสำเร็จรูปเพียงอย่างเดียว

## How users work today

### Required interaction

เส้นทางหลักคือ:

1. เลือกสินค้า หรือเลือก Auto ให้ระบบเลือกสินค้า
2. เลือก Agent หนึ่งตัว
3. ตั้ง UI options ที่เกี่ยวข้อง
4. กดยืนยันและรัน

Quick Brief และไฟล์แนบเป็น optional ผู้ใช้ต้องได้งานหลักที่มีประโยชน์แม้ปล่อย Quick Brief ว่าง

### UI-controlled options

ตัวเลือกที่มี UI โดยตรงต้องถือ UI เป็น source of truth ไม่ผลักให้ user เขียน prompt ซ้ำ:

- สินค้า: เลือกหนึ่ง/หลายสินค้า หรือ Auto selection
- Agent: เลือกตำแหน่งงาน
- Content Creator: Facebook และ/หรือ TikTok
- Content Creator: จำนวนโพสต์ต่อแพลตฟอร์ม 1–20
- Content Creator: รูป วิดีโอ หรือทั้งสอง
- Content Creator: สร้างสื่อทันทีหรือถามก่อน
- ไฟล์แนบและ resource ของงาน
- การตั้งเวลา

Agent Settings ห้ามใช้แทนตัวเลือก UI เหล่านี้ และ backend ควรตรวจ conflict ให้ user เข้าใจได้

### Optional steering

- **Quick Brief:** คำสั่งเฉพาะรอบ เช่น มุมที่ต้องเน้น ระดับรายละเอียด กลุ่มเป้าหมาย หรือข้อจำกัดเพิ่มเติม
- **Agent Settings:** วิธีทำงานประจำ เช่น tone, must/forbid rules, evidence preference และ custom instruction
- **Brand/Product context:** ความรู้ขององค์กรที่ระบบเติมให้ ไม่ควรให้ user คัดลอกใหม่ทุกครั้ง

ลำดับ authority:

1. factual integrity, safety, authorization และ hard business/brand constraints
2. UI selections ของ run นั้น
3. Quick Brief ของ run นั้น
4. persistent Agent Settings
5. default job ของตำแหน่ง

Quick Brief ชนะ soft defaults แต่ไม่ควรเปลี่ยนค่าที่ user เลือกผ่าน UI อย่างเงียบ ๆ

## Default jobs

| Agent | Default job เมื่อ user ไม่ใส่ Quick Brief |
|---|---|
| Product Analyst | เปลี่ยนข้อมูลดิบและรูปของสินค้าที่เลือกเป็น product brief/spec ที่แยก fact, inference และ missing data |
| Competitor Analyst | ค้นและวิเคราะห์คู่แข่งที่เกี่ยวข้อง พร้อม selected evidence และข้อเสนอแนะที่นำไปใช้ได้ |
| Campaign Strategist | สร้างกลยุทธ์แคมเปญที่เหมาะกับข้อมูลสินค้า ธุรกิจ และคู่แข่งที่มี พร้อมแยก facts, estimates และสิ่งที่ต้องยืนยัน |
| Content Creator | สร้างจำนวนโพสต์ตามแพลตฟอร์มและ media options ที่ UI กำหนด โดยใช้ brand/product/assets ที่เลือก |

Default deliverable มีไว้ให้กดแล้วทำงานได้ ไม่ใช่ presentation contract ที่บังคับทุก Quick Brief

## Role requirements from the employer's perspective

### Product Analyst

เปลี่ยนข้อมูลและภาพของสินค้าที่เลือกเป็นความเข้าใจสินค้าที่ทีมใช้ตัดสินใจหรือส่งต่อได้: สินค้าคืออะไร เหมาะกับใคร มีคุณค่า/ข้อจำกัดอะไร อะไรเป็น fact สิ่งที่เห็นจากภาพ inference หรือข้อมูลที่ยังขาด ผู้ใช้ต้องสั่งให้เน้น audience, angle, depth หรือ deliverable รูปแบบอื่นได้โดยไม่ถูกบังคับกลับเข้า template เดิม

### Competitor Analyst

เมื่อมีเพียงสินค้า ให้ค้นและเลือกคู่แข่งที่เกี่ยวข้องเอง; เมื่อผู้ใช้ระบุคู่แข่ง ให้ทำตาม scope นั้น เปลี่ยนหลักฐานเป็นความเข้าใจเรื่องราคา คุณสมบัติ positioning จุดแข็ง จุดอ่อน และช่องว่างที่ใช้ตัดสินใจได้ แยกตลาด/ความไม่แน่นอนตามหลักฐาน และส่ง limited analysis ที่มีประโยชน์เมื่อข้อมูลไม่ครบ ไม่ใช่ URL dump หรือ failure report เปล่า

### Campaign Strategist

สร้างกลยุทธ์ที่เหมาะกับสินค้า แบรนด์ ธุรกิจ คู่แข่ง งบและข้อจำกัดที่มี โดยให้ target audience, campaign idea, offer, channels, sequencing และ measurement เท่าที่โจทย์ต้องการ พร้อมเหตุผลและสมมติฐาน ผู้ใช้ต้องเปลี่ยน objective, emphasis, constraints, depth และรูปแบบแผนได้ Agent ห้ามกรอกหัวข้อเดิมเพียงเพื่อให้ template ครบหรือรับประกันตัวเลขโดยไม่มีฐาน

### Content Creator

สร้างจำนวนชิ้นงานตาม platform และ media controls ที่ UI เลือก ใช้ brand voice, product facts, assets, history และ optional campaign context ให้เหมาะกับช่องทาง ผู้ใช้ต้องเปลี่ยน concept, tone, audience, hook style หรือรายละเอียดงานได้ ผลงานหลายชิ้นต้องมีความหลากหลายและพร้อมตรวจ/ใช้ต่อ หาก media generation ล้มเหลวให้รักษา caption/script/media brief ที่ยังใช้ได้แทนการทำให้งานทั้งหมดหาย

## Internal artifact vs user-facing deliverable

ระบบต้องแยกสองชั้น:

- **Internal artifact:** product identity, selected sources, claims, assumptions, asset ids, platform, provenance, validation และ failure state
- **User-facing deliverable:** ผลงานที่อ่านง่ายและเหมาะกับงานที่ user เลือก/สั่ง

Structured schema ใช้เพื่อความน่าเชื่อถือและส่งงานต่อได้ แต่ไม่ควรทำให้ user ต้องอ่าน JSON หรือรับคำตอบรูปแบบเดียวทุกครั้ง

## What “frontier-quality” means

เป้าหมายไม่ใช่เลียนแบบหน้า chat หรือ autonomy ของ provider ทุกอย่าง แต่ต้องใช้ frontier model ได้เต็มความสามารถในงานของตำแหน่งนั้น

เมื่อเปรียบเทียบด้วยสินค้า ไฟล์ และโจทย์เดียวกัน:

- คุณภาพงานหลักต้องไม่ด้อยกว่า frontier AI อย่างมีนัยสำคัญ
- instruction following และความยืดหยุ่นต้องอยู่ในระดับเดียวกัน
- MKTApp ควรดีกว่าใน brand fit, product correctness, asset use, reuse of company context และความพร้อมกดใช้งานโดยไม่ต้องเขียน prompt ยาว
- MKTApp ต้องให้ auditability, repeatability, cost visibility และ team workflow ที่ frontier chat ทั่วไปไม่มีเป็นค่าเริ่มต้น

การประเมินใช้ blind side-by-side review ของงานหลัก 3–5 งานต่อ Agent ไม่สร้าง edge-case list แบบไม่รู้จบ

## Why MKTApp can be worth more than direct frontier AI

การลงทุนคุ้มเมื่อองค์กรใช้ AI ทำงานการตลาดซ้ำ ๆ และต้องป้อนบริบทเดิมหลายครั้ง เพราะมูลค่าที่เพิ่มขึ้นคือ:

1. **Context compounding:** ลงข้อมูลแบรนด์ สินค้า และ assets ครั้งเดียว แล้วทุกงานใช้ต่อได้
2. **Zero-prompt productivity:** เลือกพนักงานและกดรันงานมาตรฐานได้ ไม่ต้องมีคนเขียน prompt เก่งทุกครั้ง
3. **Operational consistency:** ทีมใช้ settings, brand rules และ source of truth เดียวกัน
4. **Asset activation:** AI ใช้รูป โลโก้ guideline และ product resources ขององค์กรโดยตรง
5. **Repeatable workflows:** งานที่เคยทำซ้ำด้วยมือสามารถตั้งเวลา รันซ้ำ และต่อเป็น team flow ได้
6. **Governance:** รู้ว่าใช้ model ใด ใช้ข้อมูลใด มี evidence อะไร ค่าใช้จ่ายเท่าไร และผิดตรงไหน
7. **Institutional memory:** งานและการตั้งค่าอยู่กับบริษัท ไม่กระจายอยู่ใน chat ส่วนตัวของพนักงาน

MKTApp ไม่ได้คุ้มกว่าเสมอ หากผู้ใช้ทำงานครั้งคราว ไม่มีข้อมูลแบรนด์ให้ reuse และพอใจกับการป้อนบริบทใหม่ใน frontier chat ทุกครั้ง ในกรณีนั้นใช้ frontier AI โดยตรงง่ายกว่า

## Why product development takes longer than using frontier AI directly

Frontier AI ส่งมอบความฉลาดทั่วไปที่สร้างสำเร็จแล้ว ผู้ใช้จึงได้คำตอบทันที แต่ MKTApp ต้องสร้าง product layer รอบโมเดล:

- ingestion และ product database
- brand context และ Agent Settings
- asset selection และ media workflow
- UI controls และ zero-prompt defaults
- source/evidence handling
- scheduling, history, artifacts และ future team flow
- cost, usage, error และ audit trail
- compatibility เมื่อ model/provider เปลี่ยน

เวลาพัฒนาเหล่านี้คุ้มเมื่อมันลดต้นทุนการป้อนบริบท การควบคุมคุณภาพ และงานซ้ำของผู้ใช้จำนวนมากในระยะยาว

อย่างไรก็ตาม semantic judgment ไม่ควรถูกเขียนเลียนแบบด้วย regex หรือกฎรายกรณี งานจะยืดไม่รู้จบหากทุก model mistake กลายเป็น validator ใหม่ หลักการคือ:

- ใช้ code สำหรับ hard facts, authorization, UI contract, schema, cost และ failure handling
- ใช้ LLM สำหรับความหมาย คุณภาพ กลยุทธ์ และการปรับตามบริบท
- ใช้ human UAT/side-by-side evaluation สำหรับคำว่า “งานดีพอ”

## Scope freeze rule

ก่อน Beta ห้ามเพิ่ม deterministic rule จาก edge case ใหม่ เว้นแต่ defect นั้นอยู่ในหนึ่งในหมวดต่อไปนี้:

- wrong product/brand/asset
- fabricated critical fact or external source
- violation of explicit UI selection or hard business constraint
- unauthorized external action
- malformed/truncated output shown as success
- unbounded cost/retry or missing audit trail

ข้อบกพร่องด้าน style, recommendation quality, response shape และ judgment ให้แก้ที่ prompt/context/model หรือเก็บเป็น post-Beta learning ไม่สร้าง release gate ใหม่โดยอัตโนมัติ

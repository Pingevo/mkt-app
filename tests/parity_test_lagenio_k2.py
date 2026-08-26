"""Parity test: รัน CampaignStrategyAgent กับ Lagenio K2 1 ครั้ง เทียบกับ Gemini ตรงเว็บ.

Usage:
    set -a; source .env; set +a
    python3 tests/parity_test_lagenio_k2.py

Saves output to output/parity_test/agent3_parity_<timestamp>.md
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agents.campaign_strategy import CampaignStrategyAgent
from src.config_loader import get_agent_config, load_config
from src.llm_client import LLMClient


def main() -> None:
    load_dotenv()
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("ERROR: OPENROUTER_API_KEY not set")
        sys.exit(1)

    cfg = load_config()
    agent_cfg = dict(get_agent_config(cfg, "campaign_strategy"))
    # ใช้ config จริงตาม patch: max_review_iterations=0, web_search=true, verify_urls=false
    print(f"=== Config ===")
    print(f"  model: {agent_cfg.get('model')}")
    print(f"  web_search: {agent_cfg.get('web_search')}")
    print(f"  verify_urls: {agent_cfg.get('verify_urls')}")
    print(f"  max_review_iterations: {agent_cfg.get('max_review_iterations')}")
    print()

    llm = LLMClient(
        api_key=os.environ["OPENROUTER_API_KEY"],
        base_url=cfg.get("defaults", {}).get("base_url", "https://openrouter.ai/api/v1"),
        default_model=cfg.get("defaults", {}).get("model", "anthropic/claude-sonnet-4"),
        timeout=cfg.get("defaults", {}).get("timeout_seconds", 120),
    )

    # โหลด brand context เหมือนที่ orchestrator ใช้จริง
    from src.brand_loader import load_brand_rules, load_brand_reference
    from src.brand_priority import load_brand_priority
    import json

    brand_context = load_brand_rules("brand")
    brand_reference = load_brand_reference("brand")
    brand_rules = load_brand_priority("brand")
    instructions = {}
    instr_path = Path("config/agent_instructions.json")
    if instr_path.exists():
        instructions = json.loads(instr_path.read_text(encoding="utf-8")).get("campaign_strategy", {})

    agent = CampaignStrategyAgent(
        agent_cfg, llm,
        brand_context=brand_context,
        brand_reference=brand_reference,
        brand_rules=brand_rules,
        instructions=instructions,
    )

    # Context เดียวกับที่ให้ Gemini ตรงเว็บ
    product_text = """Lagenio K2 smartwatch สำหรับเด็ก

SPECIFICATION:
- Display: AMOLED 1.78 นิ้ว ความสว่าง 600 nits
- Memory: RAM 1GB, ROM 8GB
- Front Camera: 5MP
- Sensor: Accelerometer, SpO2, Heart Rate
- Network: 2G/3G/4G LTE, WiFi, GPS
- Battery: 680mAh
- Waterproof: IP68
- SIM: Nano SIM
- OS: Android 8.1
- Dimensions: 51.1x42.65x15.5mm, 56g

Selling Points: GPS Tracking, Family Group Chat, Geo-Fence, Class Disable, Make friends

กลุ่มเป้าหมาย: ผู้ปกครอง 28-45 ปี ที่ต้องการความปลอดภัยให้ลูก 6-12 ปี
คู่แข่ง: imoo Watch Phone Z6, Huawei Watch Kids 4 Pro, Xiaomi Smart Kids Watch"""

    context = {
        "product": product_text,
        # ไม่ใส่ competitors/business — เทียบเงื่อนไขเดียวกับที่ให้ Gemini ตรงเว็บ
    }

    prompt = agent.build_prompt(context)
    print(f"=== User prompt (len={len(prompt)}) ===")
    print(prompt)
    print()
    print("=== กำลังรัน agent (web_search=true, อาจใช้เวลา 30-90 วินาที) ===")
    print()

    try:
        output = agent.run(prompt)
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    print("=== OUTPUT ของระบบเรา ===")
    print(output)
    print()
    print(f"=== Output length: {len(output)} chars ===")

    # บันทึกผลลัพธ์
    output_dir = Path("output/parity_test")
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = output_dir / f"agent3_parity_{ts}.md"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"# Parity Test — Lagenio K2\n\n")
        f.write(f"**Timestamp:** {ts}\n")
        f.write(f"**Model:** {agent_cfg.get('model')}\n")
        f.write(f"**web_search:** {agent_cfg.get('web_search')}\n")
        f.write(f"**verify_urls:** {agent_cfg.get('verify_urls')}\n")
        f.write(f"**max_review_iterations:** {agent_cfg.get('max_review_iterations')}\n\n")
        f.write(f"## User Prompt\n\n```\n{prompt}\n```\n\n")
        f.write(f"## Output\n\n{output}\n")
    print(f"\nบันทึกผลลัพธ์ที่: {out_path}")


if __name__ == "__main__":
    main()

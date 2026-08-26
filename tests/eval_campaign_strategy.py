"""Real-world evaluation runner for CampaignStrategyAgent (Agent 3).

Usage:
    python tests/eval_campaign_strategy.py
    python tests/eval_campaign_strategy.py --product-id "Lagenio K2"

Saves markdown + JSON results under ``output/eval_campaign/``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# Ensure project root is on path when running directly from tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agents.campaign_strategy import CampaignStrategyAgent
from src.config_loader import get_agent_config, load_config
from src.llm_client import LLMClient


def build_contexts(product_text: str, competitor_text: str = "") -> dict[str, dict]:
    """Build the 3 real-world evaluation contexts for Agent 3.

    Args:
        product_text: Raw product context (e.g. from product_db).
        competitor_text: Optional competitor analysis text.
    Returns:
        dict mapping case id to a semantic context dict for ``build_prompt``.
    """
    return {
        "A_product_only": {
            "product": product_text,
        },
        "B_with_competitor": {
            "product": product_text,
            "competitors": competitor_text or "(ไม่มีข้อมูลคู่แข่งเฉพาะ)",
        },
        "C_missing_financials": {
            "product": product_text,
            "business": (
                "เป้าหมายแคมเปญ: เปิดตัวสินค้า\n"
                "ช่องทางหลัก: ออนไลน์ (TikTok, Facebook, Shopee, Lazada)\n"
                "กลุ่มเป้าหมาย: พ่อแม่ผู้ปกครองลูกเล็กในเมืองใหญ่\n"
                "ระยะเวลาแนะนำ: 1 เดือน"
            ),
        },
    }


def save_results(results: list[dict], output_dir: Path, timestamp: str | None = None) -> list[Path]:
    """Write a combined markdown report and a JSON metadata file."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    base = output_dir / f"agent3_eval_{ts}"
    md_path = base.with_suffix(".md")
    json_path = base.with_suffix(".json")

    with open(md_path, "w", encoding="utf-8") as f:
        for i, r in enumerate(results, 1):
            case_id = r.get("case_id", "?")
            case_name = r.get("case_name", "")
            description = r.get("case_description", "")
            preview = r.get("prompt_preview", "")
            output = r.get("output", "")
            f.write(f"# {i}. Case {case_id} — {case_name}\n\n")
            if description:
                f.write(f"**Scenario:** {description}\n\n")
            if preview:
                f.write("**Prompt (first 500 chars):**\n\n")
                f.write(f"```\n{preview}\n```\n\n")
            f.write("**Output:**\n\n")
            f.write(output)
            f.write("\n\n---\n\n")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    return [md_path]


@dataclass
class _ProductChoice:
    product_id: str
    product_text: str
    competitor_text: str


def _load_product(product_id: str) -> _ProductChoice:
    from src import data_loader, product_db

    product_text = product_db.get_agent_context_text(product_id)
    files = data_loader.detect_data_files(product_id=product_id)
    competitor_path = files.get("competitor")
    competitor_text = ""
    if competitor_path and Path(competitor_path).exists():
        competitor_text = Path(competitor_path).read_text(encoding="utf-8")
    return _ProductChoice(product_id, product_text, competitor_text)


def _setup_agent() -> CampaignStrategyAgent:
    load_dotenv()
    cfg = load_config()
    defaults = cfg.get("defaults", {})
    agent_cfg = dict(get_agent_config(cfg, "campaign_strategy"))
    # Real-world eval: keep cost predictable by disabling review/web search.
    agent_cfg["max_review_iterations"] = 0
    agent_cfg["web_search"] = False

    llm = LLMClient(
        api_key=os.environ["OPENROUTER_API_KEY"],
        base_url=defaults.get("base_url", "https://openrouter.ai/api/v1"),
        default_model=defaults.get("model", "anthropic/claude-sonnet-4"),
        timeout=defaults.get("timeout_seconds", 120),
    )
    return CampaignStrategyAgent(agent_cfg, llm)


def run_case(agent: CampaignStrategyAgent, case_id: str, case_name: str, case_description: str, context: dict) -> dict:
    prompt = agent.build_prompt(context)
    output = agent.run(prompt)
    return {
        "case_id": case_id,
        "case_name": case_name,
        "case_description": case_description,
        "context": {k: v for k, v in context.items()},
        "prompt_preview": prompt[:500],
        "output": output,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run real-world evaluation for CampaignStrategyAgent")
    parser.add_argument(
        "--product-id",
        default="Lagenio K2",
        help="Product ID to evaluate (default: Lagenio K2)",
    )
    parser.add_argument(
        "--output-dir",
        default="output/eval_campaign",
        help="Directory to save evaluation results",
    )
    args = parser.parse_args()

    print(f"Loading product context for: {args.product_id}")
    choice = _load_product(args.product_id)
    contexts = build_contexts(choice.product_text, choice.competitor_text)

    cases = [
        ("A_product_only", "Standalone (Product only)", "ทดสอบว่า Agent คิดแคมเปญจากสินค้าอย่างเดียวได้"),
        ("B_with_competitor", "Product + Competitor", "ทดสอบว่า Agent ใช้ข้อมูลคู่แข่งได้จริง"),
        ("C_missing_financials", "Missing COGS/Margin/Budget", "ทดสอบว่า Agent ไม่แต่งตัวเลขทางการเงิน"),
    ]

    print("=== Initializing CampaignStrategyAgent ===")
    agent = _setup_agent()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    results: list[dict] = []
    for case_id, case_name, description in cases:
        print(f"\n=== Running {case_id}: {case_name} ===")
        context = contexts[case_id]
        try:
            result = run_case(agent, case_id, case_name, description, context)
        except Exception as exc:
            result = {
                "case_id": case_id,
                "case_name": case_name,
                "case_description": description,
                "context": {k: v for k, v in context.items()},
                "prompt_preview": agent.build_prompt(context)[:500],
                "output": f"ERROR: {type(exc).__name__}: {exc}",
            }
        results.append(result)
        print(f"Output length: {len(result['output'])} chars")

    saved = save_results(results, Path(args.output_dir), timestamp=timestamp)
    print(f"\n=== Saved: {saved[0]} ===")
    print(json.dumps({
        "product_id": choice.product_id,
        "product_text_len": len(choice.product_text),
        "competitor_text_len": len(choice.competitor_text),
        "timestamp": timestamp,
        "saved_md": str(saved[0]),
        "cases": [{"case_id": r["case_id"], "output_len": len(r["output"])} for r in results],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

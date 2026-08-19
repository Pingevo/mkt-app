"""Flow runner — sequential agent execution with context passing.

แต่ละ flow รัน agent ตามลำดับ ผลลัพธ์ของ agent ก่อนหน้าไปส่งต่อให้ agent ถัดไป
โดยไม่ต้องพึ่ย cache/ (read deliverable) — ส่งผ่าน memory ภายใน flow เลย
"""
from typing import Dict, List, Optional


def build_context_for_agent(
    agent_key: str,
    results: Dict[str, str],
    dependencies: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, object]:
    """สร้าง context สำหรับ agent จากผลลัพธ์ของ agent ก่อนหน้าใน flow.

    Args:
        agent_key: agent ที่กำลังจะรัน
        results: mapping agent_key → result_text ของ agent ก่อนหน้า
        dependencies: mapping agent_key → รายชื่อ agent ที่ต้องส่งต่อเป็น context

    Returns:
        context dict สำหรับ _run_single_agent โดยมีข้อมูล upstream ติดไป
    """
    context: Dict[str, object] = {}
    deps = dependencies.get(agent_key, []) if dependencies else []
    for dep in deps:
        if dep in results:
            context[dep] = results[dep]
    return context


def run_flow_steps(
    flow_agents: List[str],
    product_folders: List[str],
    run_one_agent,  # callable(agent_key, product, context) -> result_text
    dependencies: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, str]:
    """รัน agent ตามลำดับใน flow โดยส่ง context ต่อกัน.

    Args:
        flow_agents: ลำดับ agent ใน flow (เช่น ["product_spec", "competitor_analysis", "content_creator"])
        product_folders: สินค้าที่ใช้ใน flow (1 หรือหลายตัว → รวม)
        run_one_agent: callback รัน 1 agent รับ (agent_key, product, context) คืน result_text
        dependencies: mapping agent_key → upstream agents ที่ต้องส่งต่อ

    Returns:
        mapping agent_key → result_text ทั้งหมด
    """
    results: Dict[str, str] = {}
    product = " + ".join(product_folders)
    for agent in flow_agents:
        context = build_context_for_agent(agent, results, dependencies)
        result = run_one_agent(agent, product, context)
        results[agent] = result
    return results

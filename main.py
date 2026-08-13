#!/usr/bin/env python3
"""Marketing Agent System — CLI entry point.

Usage examples:

  # Run full pipeline (all 4 agents)
  python main.py pipeline --raw-file data/product_raw.txt --competitor-file data/competitors.txt

  # Run single agent
  python main.py product-spec --raw-file data/product_raw.txt
  python main.py competitor --product-spec-file output/01_product_spec_*.md --competitor-file data/competitors.txt
  python main.py campaign --product-spec-file output/01_product_spec_*.md --analysis-file output/02_competitor_analysis_*.md
  python main.py content --product-spec-file output/01_product_spec_*.md --analysis-file output/02_competitor_analysis_*.md --campaign-file output/03_campaign_strategy_*.md

  # Pass data inline
  python main.py product-spec --raw-text "สินค้าคือ โทรศัพท์มือถือ ราคาต้นทุน 5000 บาท ..."

  # Save results to custom directory
  python main.py pipeline --raw-file data/raw.txt --competitor-file data/comp.txt --output-dir results/
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.table import Table

from src.data_loader import detect_data_files
from src.file_loader import load_file
from src.orchestrator import Orchestrator

console = Console()


# ---------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------

def _read_arg(arg_text: str | None, arg_file: str | None, label: str) -> str:
    """Read content from --text or --file argument."""
    if arg_text:
        return arg_text
    if arg_file:
        path = Path(arg_file)
        if not path.exists():
            console.print(f"[red]Error:[/red] {label} file not found: {path}")
            sys.exit(1)
        try:
            return load_file(path)
        except ValueError as e:
            console.print(f"[red]Error:[/red] Failed to load {label} file: {e}")
            sys.exit(1)
    console.print(f"[red]Error:[/red] Must provide --{label.replace(' ', '-')}-text or --{label.replace(' ', '-')}-file")
    sys.exit(1)


def _print_result(title: str, content: str, saved_to: Path | None = None) -> None:
    """Display agent result in a panel."""
    table = Table(show_header=False, box=None, padding=(0, 0))
    table.add_row(f"[bold cyan]{title}[/bold cyan]")
    if saved_to:
        table.add_row(f"[dim]Saved to: {saved_to}[/dim]")
    console.print(table)
    console.print(Panel(content, border_style="cyan", expand=True))


# ---------------------------------------------------------------------
#  Commands
# ---------------------------------------------------------------------

def cmd_product_spec(args: argparse.Namespace) -> None:
    raw_data = _read_arg(args.raw_text, args.raw_file, "raw")
    product_images = [args.product_image] if args.product_image else []
    orch = Orchestrator(args.config, brand_dir=args.brand_dir, product_images=product_images, product_id=args.product_id)
    console.print("\n[bold]Agent: นักวิเคราะห์สินค้า[/bold]\n")
    result = orch.run_product_spec(raw_data, product_images)
    orch.results["product_spec"] = result
    saved = orch.save_results(args.output_dir)
    _print_result("สเปคสินค้า", result, saved.get("product_spec"))


def cmd_competitor(args: argparse.Namespace) -> None:
    product_spec = _read_arg(args.product_spec_text, args.product_spec_file, "product-spec")
    competitor_data = _read_arg(args.competitor_text, args.competitor_file, "competitor")
    product_images = [args.product_image] if args.product_image else []
    orch = Orchestrator(args.config, brand_dir=args.brand_dir, product_images=product_images, product_id=args.product_id)
    console.print("\n[bold]Agent: นักวิเคราะห์คู่แข่ง[/bold]\n")
    result = orch.run_competitor_analysis(product_spec, competitor_data)
    orch.results["competitor_analysis"] = result
    saved = orch.save_results(args.output_dir)
    _print_result("วิเคราะห์คู่แข่ง", result, saved.get("competitor_analysis"))


def cmd_campaign(args: argparse.Namespace) -> None:
    product_spec = _read_arg(args.product_spec_text, args.product_spec_file, "product-spec")
    analysis = _read_arg(args.analysis_text, args.analysis_file, "analysis")
    product_images = [args.product_image] if args.product_image else []
    orch = Orchestrator(args.config, brand_dir=args.brand_dir, product_images=product_images, product_id=args.product_id)
    console.print("\n[bold]Agent: นักวางกลยุทธ์แคมเปญ[/bold]\n")
    result = orch.run_campaign_strategy(product_spec, analysis)
    orch.results["campaign_strategy"] = result
    saved = orch.save_results(args.output_dir)
    _print_result("แคมเปญ + ราคาแนะนำ", result, saved.get("campaign_strategy"))


def cmd_content(args: argparse.Namespace) -> None:
    product_spec = _read_arg(args.product_spec_text, args.product_spec_file, "product-spec")
    analysis = _read_arg(args.analysis_text, args.analysis_file, "analysis")
    campaign = _read_arg(args.campaign_text, args.campaign_file, "campaign")
    product_images = [args.product_image] if args.product_image else []
    orch = Orchestrator(args.config, brand_dir=args.brand_dir, product_images=product_images, product_id=args.product_id)
    console.print("\n[bold]Agent: นักสร้างคอนเทนต์[/bold]\n")
    result = orch.run_content_creator(product_spec, analysis, campaign)
    orch.results["content_creator"] = result
    saved = orch.save_results(args.output_dir)
    _print_result("คอนเทนต์ + Prompt", result, saved.get("content_creator"))


def cmd_pipeline(args: argparse.Namespace) -> None:
    # Auto-detect files from data/ if not specified
    detected = detect_data_files(product_id=args.product_id)
    
    # Get raw data
    if args.raw_text:
        raw_data = args.raw_text
    elif args.raw_file:
        raw_data = load_file(args.raw_file)
    elif detected["raw"]:
        raw_data = load_file(detected["raw"])
        console.print(f"[dim]Auto-detected: {detected['raw']}[/dim]")
    else:
        console.print("[red]Error:[/red] Must provide --raw-file, --raw-text, or place product_raw.* in data/")
        sys.exit(1)
    
    # Get competitor data (optional)
    competitor_data = None
    if args.competitor_text:
        competitor_data = args.competitor_text
    elif args.competitor_file:
        competitor_data = load_file(args.competitor_file)
    elif detected["competitor"]:
        competitor_data = load_file(detected["competitor"])
        console.print(f"[dim]Auto-detected: {detected['competitor']}[/dim]")
    
    # Get product images (optional) - can be multiple
    product_images = []
    if args.product_image:
        product_images = [args.product_image]
    elif detected["images"]:
        product_images = detected["images"]
        for img in product_images:
            console.print(f"[dim]Auto-detected: {img}[/dim]")
    
    # Create output subdirectory if product_id is specified
    output_dir = Path(args.output_dir)
    if args.product_id:
        output_dir = output_dir / args.product_id
        output_dir.mkdir(parents=True, exist_ok=True)
    
    orch = Orchestrator(args.config, brand_dir=args.brand_dir, product_images=product_images, product_id=args.product_id)
    console.print("\n[bold]Marketing Agent Pipeline — รันทั้ง 4 Agent[/bold]\n")
    if competitor_data is None:
        console.print("[dim]ข้อมูลคู่แข่งไม่ได้ระบุ — Agent จะค้นหาจาก web อัตโนมัติ[/dim]\n")
    results = orch.run_pipeline(raw_data, competitor_data)
    saved = orch.save_results(str(output_dir))

    console.print("\n[bold green]✓ Pipeline เสร็จสมบูรณ์![/bold green]\n")
    table = Table(title="ผลลัพธ์", show_header=True, header_style="bold cyan")
    table.add_column("Agent", style="white")
    table.add_column("ไฟล์ผลลัพธ์", style="dim")
    labels = {
        "product_spec": "1. นักวิเคราะห์สินค้า",
        "competitor_analysis": "2. นักวิเคราะห์คู่แข่ง",
        "campaign_strategy": "3. นักวางกลยุทธ์แคมเปญ",
        "content_creator": "4. นักสร้างคอนเทนต์",
    }
    for key, label in labels.items():
        if key in saved:
            table.add_row(label, str(saved[key]))
    console.print(table)
    console.print()


# ---------------------------------------------------------------------
#  Interactive prompt mode
# ---------------------------------------------------------------------

def list_products() -> list[str]:
    """List product directories in data/ that have raw data files."""
    project_root = Path(__file__).resolve().parent
    data_dir = project_root / "data"
    if not data_dir.exists():
        return []
    products = []
    for item in sorted(data_dir.iterdir()):
        if not item.is_dir() or item.name.startswith("."):
            continue
        detected = detect_data_files(product_id=item.name)
        if detected["raw"]:
            products.append(item.name)
    return products


def has_files_in_data_root() -> bool:
    """Check if there are raw data files directly in data/ (no subdirectory)."""
    detected = detect_data_files(product_id=None)
    return detected["raw"] is not None


def show_product_info(product_id: str | None) -> None:
    """Show detected files for a product."""
    detected = detect_data_files(product_id=product_id)
    label = product_id if product_id else "data/ (โฟลเดอร์หลัก)"
    table = Table(title=f"ไฟล์ของ {label}", show_header=True, header_style="bold cyan")
    table.add_column("ประเภท", style="white")
    table.add_column("ไฟล์", style="dim")
    
    # ข้อมูลดิบ
    if detected["raw"]:
        table.add_row("ข้อมูลดิบ", detected["raw"])
    else:
        if product_id:
            table.add_row("ข้อมูลดิบ", f"[red]ไม่พบ — โยนไฟล์ (txt, pdf) ลง data/{product_id}/ ก่อน[/red]")
        else:
            table.add_row("ข้อมูลดิบ", "[red]ไม่พบ — โยนไฟล์ (txt, pdf) ลง data/ ก่อน[/red]")
    
    # รูปภาพ
    if detected["images"]:
        for img in detected["images"]:
            table.add_row("รูปภาพสินค้า", img)
    else:
        table.add_row("รูปภาพสินค้า", "[dim]ไม่มี (optional)[/dim]")
    
    # ข้อมูลพร้อมใช้ (ready/)
    if detected["product_spec"]:
        table.add_row("สเปคสินค้า (ready)", detected["product_spec"])
    else:
        table.add_row("สเปคสินค้า (ready)", "[dim]ยังไม่มี — รัน Agent 1 ก่อน[/dim]")
    
    if detected["competitor"]:
        table.add_row("วิเคราะห์คู่แข่ง (ready)", detected["competitor"])
    else:
        table.add_row("วิเคราะห์คู่แข่ง (ready)", "[dim]ยังไม่มี — รัน Agent 2 ก่อน[/dim]")
    
    console.print(table)


AGENTS = [
    ("1", "product-spec", "นักวิเคราะห์สินค้า", "สร้างสเปคสินค้าจากข้อมูลดิบ"),
    ("2", "competitor", "นักวิเคราะห์คู่แข่ง", "วิเคราะห์เปรียบเทียบคู่แข่ง"),
    ("3", "campaign", "นักวางกลยุทธ์แคมเปญ", "คิดแคมเปญ + ราคาแนะนำ"),
    ("4", "content", "นักสร้างคอนเทนต์", "สร้าง content + prompt + hashtag"),
    ("5", "pipeline", "รันทั้ง 4 Agent", "รัน pipeline ทั้งหมดตามลำดับ"),
]


def list_all_products() -> list[str]:
    """List all product directories in data/."""
    project_root = Path(__file__).resolve().parent
    data_dir = project_root / "data"
    if not data_dir.exists():
        return []
    products = []
    for item in sorted(data_dir.iterdir()):
        if item.is_dir() and not item.name.startswith(".") and item.name != "ready":
            products.append(item.name)
    return products


def _agent_display_name(key: str) -> str:
    names = {
        "product_spec": "นักวิเคราะห์สินค้า",
        "competitor_analysis": "นักวิเคราะห์คู่แข่ง",
        "campaign_strategy": "นักวางกลยุทธ์แคมเปญ",
        "content_creator": "นักสร้างคอนเทนต์",
    }
    return names.get(key, key)


def interactive_mode() -> None:
    """Chat-based interface — คุยกับ Manager สั่งงานเป็นภาษาธรรมดา."""
    console.print(Panel(
        "[bold cyan]Marketing Agent System[/bold cyan]\n\n"
        "[bold]ทีมของคุณมี 4 ตำแหน่ง:[/bold]\n"
        "  1. นักวิเคราะห์สินค้า — สร้างสเปคสินค้าจากข้อมูลดิบ\n"
        "  2. นักวิเคราะห์คู่แข่ง — วิเคราะห์เปรียบเทียบคู่แข่ง (ค้นหา web เอง)\n"
        "  3. นักวางกลยุทธ์แคมเปญ — คิดแคมเปญ + ราคาแนะนำ\n"
        "  4. นักสร้างคอนเทนต์ — สร้าง content + prompt + hashtag\n\n"
        "พิมพ์เป็นภาษาไทยได้เลย สั่งงานอะไรก็ได้ที่ทีมทำได้\n"
        "พิมพ์ 'exit' เพื่อออก",
        border_style="cyan",
    ))
    
    orch = Orchestrator(brand_dir="brand")
    conversation: list[dict[str, str]] = []
    session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    while True:
        console.print()
        user_input = Prompt.ask("[bold green]คุณ[/bold green]")
        
        if user_input.strip().lower() in ("exit", "quit", "ออก", "bye"):
            console.print("[dim]ลาก่อน[/dim]")
            break
        
        if not user_input.strip():
            continue
        
        conversation.append({"role": "user", "content": user_input})
        
        # Manager วิเคราะห์
        console.print("\n[cyan]Manager กำลังวิเคราะห์...[/cyan]\n")
        
        try:
            plan = orch.run_manager(user_input, conversation)
        except Exception as e:
            console.print(f"[red]เกิดข้อผิดพลาด: {e}[/red]")
            continue
        
        reply = plan.get("reply", "")
        action = plan.get("action", "none")
        agents_to_run = plan.get("agents", [])
        product_id = plan.get("product_id")
        missing = plan.get("missing", [])
        
        conversation.append({"role": "manager", "content": reply})
        
        if reply:
            console.print(f"[bold cyan]Manager:[/bold cyan] {reply}")
        
        if missing:
            console.print(f"[yellow]ขาด: {', '.join(missing)}[/yellow]")
        
        if action != "run" or not agents_to_run:
            continue
        
        if not product_id:
            console.print("[yellow]ไม่ระบุสินค้า ลองใหม่[/yellow]")
            continue
        
        # ตรวจข้อมูลก่อนรัน
        detected = detect_data_files(product_id=product_id)
        output_dir = Path("output") / session_ts
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # รันตามลำดับที่ manager สั่ง
        for agent_key in agents_to_run:
            console.print(f"\n[bold]กำลังรัน: {_agent_display_name(agent_key)}[/bold]\n")
            
            try:
                if agent_key == "product_spec":
                    if not detected["raw"]:
                        console.print(f"[red]ไม่พบข้อมูลดิบสำหรับ {product_id}[/red]")
                        break
                    raw_data = load_file(detected["raw"])
                    product_images = detected["images"] or []
                    orch.product_id = product_id
                    orch.product_images = product_images
                    result = orch.run_product_spec(raw_data, product_images)
                    orch.results["product_spec"] = result
                    saved = orch.save_results(str(output_dir))
                    _print_result("สเปคสินค้า", result, saved.get("product_spec"))
                    detected = detect_data_files(product_id=product_id)
                
                elif agent_key == "competitor_analysis":
                    if not detected["product_spec"]:
                        console.print("[yellow]ต้องรัน product_spec ก่อน[/yellow]")
                        break
                    product_spec = load_file(detected["product_spec"])
                    result = orch.run_competitor_analysis(product_spec, None)
                    orch.results["competitor_analysis"] = result
                    saved = orch.save_results(str(output_dir))
                    _print_result("วิเคราะห์คู่แข่ง", result, saved.get("competitor_analysis"))
                    detected = detect_data_files(product_id=product_id)
                
                elif agent_key == "campaign_strategy":
                    if not detected["product_spec"]:
                        console.print("[yellow]ต้องรัน product_spec ก่อน[/yellow]")
                        break
                    product_spec = load_file(detected["product_spec"])
                    comp_file = output_dir / "02_competitor_analysis.md"
                    if detected["competitor"]:
                        analysis = load_file(detected["competitor"])
                    elif comp_file.exists():
                        analysis = comp_file.read_text(encoding="utf-8")
                    else:
                        console.print("[yellow]ต้องรัน competitor_analysis ก่อน[/yellow]")
                        break
                    result = orch.run_campaign_strategy(product_spec, analysis)
                    orch.results["campaign_strategy"] = result
                    saved = orch.save_results(str(output_dir))
                    _print_result("แคมเปญ + ราคาแนะนำ", result, saved.get("campaign_strategy"))
                
                elif agent_key == "content_creator":
                    if not detected["product_spec"]:
                        console.print("[yellow]ต้องรัน product_spec ก่อน[/yellow]")
                        break
                    product_spec = load_file(detected["product_spec"])
                    comp_file = output_dir / "02_competitor_analysis.md"
                    camp_file = output_dir / "03_campaign_strategy.md"
                    if detected["competitor"]:
                        analysis = load_file(detected["competitor"])
                    elif comp_file.exists():
                        analysis = comp_file.read_text(encoding="utf-8")
                    else:
                        console.print("[yellow]ต้องรัน competitor_analysis ก่อน[/yellow]")
                        break
                    if not camp_file.exists():
                        console.print("[yellow]ต้องรัน campaign_strategy ก่อน[/yellow]")
                        break
                    campaign = camp_file.read_text(encoding="utf-8")
                    result = orch.run_content_creator(product_spec, analysis, campaign)
                    orch.results["content_creator"] = result
                    saved = orch.save_results(str(output_dir))
                    _print_result("คอนเทนต์ + Prompt", result, saved.get("content_creator"))
                
                else:
                    console.print(f"[yellow]ไม่รู้จัก agent: {agent_key}[/yellow]")
            
            except Exception as e:
                console.print(f"\n[red]เกิดข้อผิดพลาด: {e}[/red]")
                break
        
        console.print("\n[bold green]✓ เสร็จสิ้น[/bold green]")
        
        # Manager สรุปผลงาน
        completed = [a for a in agents_to_run if a in orch.results]
        if completed:
            summary_msg = f"เพิ่งทำงานเสร็จ: {', '.join(completed)} สำหรับสินค้า {product_id} สรุปผลให้ user สั้นๆ"
            try:
                console.print("\n[cyan]Manager กำลังสรุป...[/cyan]")
                summary_plan = orch.run_manager(summary_msg, conversation)
                summary_reply = summary_plan.get("reply", "")
                if summary_reply:
                    console.print(f"\n[bold cyan]Manager:[/bold cyan] {summary_reply}")
                    conversation.append({"role": "manager", "content": summary_reply})
            except Exception:
                pass


# ---------------------------------------------------------------------
#  CLI parser
# ---------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mktapp",
        description="Marketing Agent System — 4 AI agents สำหรับทีมการตลาด",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "ตัวอย่าง:\n"
            "  python main.py pipeline --raw-file data/raw.txt --competitor-file data/comp.txt\n"
            "  python main.py product-spec --raw-text 'สินค้าคือ...'\n"
        ),
    )
    parser.add_argument("--config", default=None, help="Path to agents.yaml (default: config/agents.yaml)")
    parser.add_argument("--output-dir", default="output", help="Output directory (default: output/)")
    parser.add_argument("--brand-dir", default="brand", help="โฟลเดอร์เก็บไฟล์ข้อมูลแบรนด์อ้างอิง (default: brand/)")
    parser.add_argument("--product-image", default=None, help="ไฟล์รูปภาพสินค้าเพิ่มเติม (.png, .jpg, .jpeg)")
    parser.add_argument("--product-id", default=None, help="ID ของสินค้า (สำหรับโฟลเดอร์ย่อย data/{product_id}/)")

    sub = parser.add_subparsers(dest="command", required=False)

    # pipeline
    p_pipeline = sub.add_parser("pipeline", help="รันทั้ง 4 agent ตามลำดับ")
    p_pipeline.add_argument("--raw-file", default=None, help="ไฟล์ข้อมูลดิบของสินค้า")
    p_pipeline.add_argument("--raw-text", default=None, help="ข้อมูลดิบของสินค้า (inline)")
    p_pipeline.add_argument("--competitor-file", default=None, help="ไฟล์ข้อมูลคู่แข่ง")
    p_pipeline.add_argument("--competitor-text", default=None, help="ข้อมูลคู่แข่ง (inline)")
    p_pipeline.add_argument("--product-image", default=None, help="ไฟล์รูปภาพสินค้าเพิ่มเติม (.png, .jpg, .jpeg)")
    p_pipeline.set_defaults(func=cmd_pipeline)

    # product-spec
    p_product = sub.add_parser("product-spec", help="Agent 1: สร้างสเปคสินค้าจากข้อมูลดิบ")
    p_product.add_argument("--raw-file", default=None, help="ไฟล์ข้อมูลดิบของสินค้า")
    p_product.add_argument("--raw-text", default=None, help="ข้อมูลดิบของสินค้า (inline)")
    p_product.add_argument("--product-image", default=None, help="ไฟล์รูปภาพสินค้าเพิ่มเติม (.png, .jpg, .jpeg)")
    p_product.set_defaults(func=cmd_product_spec)

    # competitor
    p_comp = sub.add_parser("competitor", help="Agent 2: วิเคราะห์เปรียบเทียบคู่แข่ง")
    p_comp.add_argument("--product-spec-file", default=None, help="ไฟล์สเปคสินค้า (จาก agent 1)")
    p_comp.add_argument("--product-spec-text", default=None, help="สเปคสินค้า (inline)")
    p_comp.add_argument("--competitor-file", default=None, help="ไฟล์ข้อมูลคู่แข่ง")
    p_comp.add_argument("--competitor-text", default=None, help="ข้อมูลคู่แข่ง (inline)")
    p_comp.set_defaults(func=cmd_competitor)

    # campaign
    p_camp = sub.add_parser("campaign", help="Agent 3: คิดแคมเปญ + ราคาแนะนำ")
    p_camp.add_argument("--product-spec-file", default=None, help="ไฟล์สเปคสินค้า")
    p_camp.add_argument("--product-spec-text", default=None, help="สเปคสินค้า (inline)")
    p_camp.add_argument("--analysis-file", default=None, help="ไฟล์ผลวิเคราะห์คู่แข่ง (จาก agent 2)")
    p_camp.add_argument("--analysis-text", default=None, help="ผลวิเคราะห์คู่แข่ง (inline)")
    p_camp.set_defaults(func=cmd_campaign)

    # content
    p_content = sub.add_parser("content", help="Agent 4: สร้าง content + prompt + hashtag")
    p_content.add_argument("--product-spec-file", default=None, help="ไฟล์สเปคสินค้า")
    p_content.add_argument("--product-spec-text", default=None, help="สเปคสินค้า (inline)")
    p_content.add_argument("--analysis-file", default=None, help="ไฟล์ผลวิเคราะห์คู่แข่ง")
    p_content.add_argument("--analysis-text", default=None, help="ผลวิเคราะห์คู่แข่ง (inline)")
    p_content.add_argument("--campaign-file", default=None, help="ไฟล์แคมเปญ (จาก agent 3)")
    p_content.add_argument("--campaign-text", default=None, help="แคมเปญ (inline)")
    p_content.set_defaults(func=cmd_content)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    
    if not getattr(args, "command", None):
        interactive_mode()
    else:
        args.func(args)


if __name__ == "__main__":
    main()

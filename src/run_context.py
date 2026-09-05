from __future__ import annotations

import base64
import io
import mimetypes
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .run_resources import RunResourceStore


@dataclass(frozen=True)
class ResourceTrace:
    workflow_id: str
    step_id: str
    resource_id: str
    name: str
    status: str
    truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "step_id": self.step_id,
            "resource_id": self.resource_id,
            "name": self.name,
            "status": self.status,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class PhaseTrace:
    workflow_id: str
    step_id: str
    agent_key: str
    phase: str
    input_refs: tuple[str, ...]
    resource_refs_available: tuple[str, ...]
    context_truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "step_id": self.step_id,
            "agent_key": self.agent_key,
            "phase": self.phase,
            "input_refs": list(self.input_refs),
            "resource_refs_available": list(self.resource_refs_available),
            "context_truncated": self.context_truncated,
        }


def _encode_image_resized(img_path: str, max_dim: int = 1024) -> tuple[str, str]:
    """Encode รูปเป็น base64 data URL พร้อม resize ถ้าใหญ่เกิน max_dim.

    ถ้า PIL อ่านไม่ได้ → fallback ส่งต้นฉบับดิบเพื่อรักษา backward compatibility
    กับไฟล์ที่ยังไม่สมบูรณ์หรือทดสอบ.
    """
    p = Path(img_path)
    if not p.exists():
        raise OSError(f"ไม่พบรูป: {img_path}")

    mime, _ = mimetypes.guess_type(str(p))
    if not mime:
        mime = "image/png"

    try:
        from PIL import Image as PILImage
        img = PILImage.open(p)
    except Exception:
        return base64.b64encode(p.read_bytes()).decode("ascii"), mime

    w, h = img.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        new_size = (int(w * scale), int(h * scale))
        img = img.resize(new_size, PILImage.LANCZOS)

    buf = io.BytesIO()
    fmt = "PNG" if mime in ("image/png", None) else img.format or "JPEG"
    img.save(buf, format=fmt)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return b64, mime


def build_multimodal_content(text: str, image_paths: tuple[str, ...]) -> str | list[dict[str, Any]]:
    """สร้าง OpenAI/-compatible message content แบบ text + รูป."""
    if not image_paths:
        return text

    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for img_path in image_paths:
        try:
            b64, mime = _encode_image_resized(img_path)
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            })
        except OSError:
            continue

    if len(content) == 1:
        return text
    return content


@dataclass(frozen=True)
class PhaseContext:
    instruction_text: str
    image_paths: tuple[str, ...]
    input_refs: tuple[str, ...]
    trace_metadata: dict[str, Any]

    def as_llm_content(self) -> str | list[dict[str, Any]]:
        return build_multimodal_content(self.instruction_text, self.image_paths)


@dataclass(frozen=True)
class StepRunContext:
    """Single source of truth for all inputs of one workflow step/run.

    Built once per step and reused by every decision phase. Immutable;
    derived contexts use `replace()` so the original is never mutated.
    """

    workflow_id: str
    step_id: str
    agent_key: str
    quick_brief: str
    input_refs: tuple[str, ...]
    product_refs: tuple[str, ...]
    resource_refs: tuple[str, ...]
    resource_text: str
    resource_image_paths: tuple[str, ...]
    resource_trace: tuple[ResourceTrace, ...]
    warnings: tuple[str, ...]
    phase_traces: tuple[PhaseTrace, ...] = ()
    upstream_artifacts: tuple[Any, ...] = ()
    brand_dir: str = "brand"

    def for_phase(self, phase: str) -> PhaseContext:
        """Return a phase view of this context.

        MVP uses the same bounded resource text and image paths for every
        phase, but the interface is ready for per-phase retrieval/reranking.
        """
        truncated = any(t.truncated for t in self.resource_trace) or any(
            w.startswith("truncated") for w in self.warnings
        )
        return PhaseContext(
            instruction_text=self.resource_text,
            image_paths=self.resource_image_paths,
            input_refs=self.input_refs,
            trace_metadata={
                "workflow_id": self.workflow_id,
                "step_id": self.step_id,
                "agent_key": self.agent_key,
                "phase": phase,
                "input_refs": list(self.input_refs),
                "resource_refs_available": list(self.resource_refs),
                "context_truncated": truncated,
            },
        )

    def with_phase(self, phase: str) -> "StepRunContext":
        """Return a derived context that also records a phase trace."""
        view = self.for_phase(phase)
        trace = PhaseTrace(
            workflow_id=self.workflow_id,
            step_id=self.step_id,
            agent_key=self.agent_key,
            phase=phase,
            input_refs=tuple(view.trace_metadata["input_refs"]),
            resource_refs_available=tuple(view.trace_metadata["resource_refs_available"]),
            context_truncated=view.trace_metadata["context_truncated"],
        )
        return replace(self, phase_traces=self.phase_traces + (trace,))

    def with_products(self, product_ids: list[str]) -> "StepRunContext":
        """Derive a new context with selected products added.

        `product_ids` may be raw product IDs; they are normalized to typed refs.
        Duplicate product refs are removed while preserving order.
        Keeps workflow_id, step_id, resources and provenance unchanged.
        """
        product_refs = tuple(dict.fromkeys(
            p if ":" in p else f"product:{p}" for p in product_ids
        ))
        new_product_refs = tuple(dict.fromkeys(product_refs + self.product_refs))
        new_input_refs = tuple(dict.fromkeys(new_product_refs + self.input_refs))
        return replace(
            self,
            input_refs=new_input_refs,
            product_refs=new_product_refs,
        )

    def with_quick_brief(self, quick_brief: str) -> "StepRunContext":
        """Derive a new context with a different quick brief for a phase."""
        return replace(self, quick_brief=quick_brief)

    def with_agent_key(self, agent_key: str) -> "StepRunContext":
        """Derive a new context with a different agent key."""
        return replace(self, agent_key=agent_key)

    def as_legacy_kwargs(self) -> dict[str, Any]:
        """Backward-compatible kwargs for functions still taking old params."""
        return {
            "resource_context": self.resource_text,
            "extra_image_paths": list(self.resource_image_paths),
        }


def build_step_run_context(
    resource_store: RunResourceStore,
    workflow_id: str,
    step_id: str,
    agent_key: str,
    quick_brief: str,
    product_refs: list[str],
    resource_refs: list[str],
    upload_session_id: str,
    brand_dir: str | None = None,
) -> StepRunContext:
    """Build a single StepRunContext for one step.

    Resolves typed refs once, applies context budget, and collects warnings.
    No resource is resolved more than once per step.

    Runtime multi-brand contract:
      - brand_dir: the brand directory selected for this run (default "brand").
    """
    warnings: list[str] = []
    records: list[dict[str, Any]] = []
    missing: list[str] = []

    for ref in resource_refs:
        if not ref.startswith("resource:"):
            warnings.append(f"unsupported resource ref type: {ref}")
            missing.append(ref)
            continue
        rec = resource_store.resolve_input_ref(ref, upload_session_id)
        if rec:
            records.append(rec)
        else:
            missing.append(ref)

    if missing:
        warnings.append(f"missing or invalid resource refs: {', '.join(missing)}")

    # Preflight: detect explicitly referenced resources that exist but are
    # not ready (rejected extension, parser error, extraction failure, etc.).
    # These must NOT be silently dropped — the user explicitly attached them
    # and the agent must not proceed as if the attachment was available.
    non_ready: list[str] = []
    for rec in records:
        status = rec.get("status", "")
        if status != "ready":
            name = rec.get("name", rec.get("resource_id", ""))
            non_ready.append(f"{name} (status: {status})")
    if non_ready:
        warnings.append(
            f"referenced resource(s) not ready — cannot provide usable context: "
            f"{', '.join(non_ready)}"
        )

    # Build bounded resource text and image paths through the existing store
    if records:
        built = resource_store.build_resource_context(
            records, workflow_id=workflow_id, step_id=step_id,
        )
        resource_text = built["text"]
        image_paths = tuple(built.get("image_paths", []))
        raw_trace = built.get("trace", [])
    else:
        resource_text = ""
        image_paths = ()
        raw_trace = []
    resource_trace = tuple(
        ResourceTrace(
            workflow_id=t.get("workflow_id", workflow_id),
            step_id=t.get("step_id", step_id),
            resource_id=t.get("resource_id", ""),
            name=t.get("name", ""),
            status=t.get("status", ""),
            truncated=t.get("truncated", False),
        )
        for t in raw_trace
    )

    input_refs = tuple(product_refs) + tuple(resource_refs)
    product_refs_tuple = tuple(product_refs)
    resolved_brand_dir = (brand_dir or "brand").strip()

    return StepRunContext(
        workflow_id=workflow_id,
        step_id=step_id,
        agent_key=agent_key,
        quick_brief=quick_brief,
        input_refs=input_refs,
        product_refs=product_refs_tuple,
        resource_refs=tuple(resource_refs),
        resource_text=resource_text,
        resource_image_paths=image_paths,
        resource_trace=resource_trace,
        warnings=tuple(warnings),
        brand_dir=resolved_brand_dir,
    )

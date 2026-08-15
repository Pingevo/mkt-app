"""JSON schema สำหรับ content_creator output — ใช้กับ OpenRouter Structured Outputs.

แทนการ parse markdown ด้วย regex (ที่พังทุกครั้งที่ LLM เปลี่ยน format)
เราบังคับให้ LLM คืน JSON ที่ตรง schema นี้ผ่าน response_format: json_schema

LLM คืน JSON ที่มี:
  - posts: list ของ post ที่มี field ครบ (platform, title, content, hashtags, image_prompts, video_prompts)

เรา generate markdown สำหรับ user ดูจาก posts เอง (render_posts_to_markdown)
— ไม่ต้องให้ LLM เขียน markdown แยก เพราะมันมักขัดแย้งกับ posts และไม่เติม prompt จริง
"""

from __future__ import annotations


# Schema สำหรับ OpenRouter Structured Outputs
# ใช้กับ response_format: {"type": "json_schema", "json_schema": {...}}
CONTENT_SCHEMA: dict = {
    "name": "content_output",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "posts": {
                "type": "array",
                "description": "list ของโพสต์ที่สร้างขึ้น",
                "items": {
                    "type": "object",
                    "properties": {
                        "platform": {
                            "type": "string",
                            "description": "แพลตฟอร์ม เช่น Facebook, TikTok, Instagram",
                        },
                        "concept": {
                            "type": "string",
                            "description": "แนวคิด/มุมมองที่ใช้ (เช่น ราคา, คุณสมบัติ, lifestyle)",
                        },
                        "title": {
                            "type": "string",
                            "description": "หัวข้อโพสต์ (ภายในระบบ — ไม่ได้โพสต์จริง)",
                        },
                        "caption": {
                            "type": "string",
                            "description": "ข้อความ caption พร้อมโพสต์จริง — ตามลักษณะของแต่ละ platform (TikTok: สั้น 80-150 ตัวอักษร, FB: 2-4 ย่อหน้า, IG: สั้น+emoji). ห้ามใส่ label Hook:/Body:/CTA: — เขียนเป็นข้อความตามธรรมชาติ",
                        },
                        "script": {
                            "type": "string",
                            "description": "สคริปต์วิดีโอ/voiceover สำหรับสร้างวิดีโอ (ถ้าเป็นวิดีโอ) — มี timestamp + scene + voiceover. ถ้าไม่ใช่วิดีโอ → สตริงว่าง",
                        },
                        "hashtags": {
                            "type": "string",
                            "description": "hashtags คั่นด้วย space เช่น #tag1 #tag2",
                        },
                        "image_prompts": {
                            "type": "array",
                            "description": "list ของ prompt สำหรับสร้างรูป (ถ้าโพสต์นี้ไม่ต้องมีรูป → array ว่าง)",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "prompt": {
                                        "type": "string",
                                        "description": "prompt ภาษาอังกฤษสำหรับ image generation",
                                    },
                                    "aspect_ratio": {
                                        "type": "string",
                                        "description": "aspect ratio เช่น 9:16, 16:9, 1:1 — ต้องตรงกับ platform (TikTok/IG Reels = 9:16, Facebook/Youtube = 16:9)",
                                    },
                                    "resolution": {
                                        "type": "string",
                                        "description": "resolution เช่น 1024x1024, 768x1366",
                                    },
                                },
                                "required": ["prompt"],
                                "additionalProperties": False,
                            },
                        },
                        "video_prompts": {
                            "type": "array",
                            "description": "list ของ prompt สำหรับสร้างวิดีโอ (ถ้าโพสต์นี้ไม่ต้องมีวิดีโอ → array ว่าง)",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "prompt": {
                                        "type": "string",
                                        "description": "prompt ภาษาอังกฤษสำหรับ video generation",
                                    },
                                    "duration": {
                                        "type": "number",
                                        "description": "duration เป็นวินาที",
                                    },
                                    "aspect_ratio": {
                                        "type": "string",
                                        "description": "aspect ratio เช่น 9:16, 16:9 — ต้องตรงกับ platform",
                                    },
                                    "resolution": {
                                        "type": "string",
                                        "description": "resolution เช่น 720p, 1080p",
                                    },
                                },
                                "required": ["prompt"],
                                "additionalProperties": False,
                            },
                        },
                        "asset_ids": {
                            "type": "array",
                            "description": "ID ของวัตถุดิบแบรนด์ (asset) ที่ใช้ในโพสต์นี้ — เช่น ['a_0001', 'a_0002'] (ถ้าไม่ใช้ → array ว่าง)",
                            "items": {"type": "string"},
                        },
                    },
                    "required": [
                        "platform",
                        "concept",
                        "title",
                        "caption",
                        "script",
                        "hashtags",
                        "image_prompts",
                        "video_prompts",
                        "asset_ids",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["posts"],
        "additionalProperties": False,
    },
}


# response_format สำหรับส่งให้ OpenRouter
CONTENT_RESPONSE_FORMAT: dict = {
    "type": "json_schema",
    "json_schema": CONTENT_SCHEMA,
}


def render_posts_to_markdown(parsed: dict) -> str:
    """แปลง structured output (posts) เป็น markdown สำหรับ user ดู.

    สร้าง format เดียวกับที่ content_creator เคยสร้าง:
      ## 1. ข้อมูลโพสต์
      ## 2. Prompt สำหรับ Gen Image
      ## 3. Prompt สำหรับ Gen Video

    เรา generate เองจาก posts เพื่อให้ markdown กับ structured data ตรงกันเสมอ
    """
    posts = parsed.get("posts", [])
    if not posts:
        return ""

    parts: list[str] = []
    for idx, post in enumerate(posts, 1):
        # ## 1. ข้อมูลโพสต์
        parts.append(f"## {idx}. ข้อมูลโพสต์")
        parts.append(f"- **แพลตฟอร์ม** — {post.get('platform', '')}")
        parts.append(f"- **มุมมอง** — {post.get('concept', '')}")
        parts.append(f"- **หัวข้อ** — {post.get('title', '')}")
        parts.append(f"- **Caption (พร้อมโพสต์)** — ")
        parts.append(post.get("caption", ""))
        parts.append(f"- **Hashtag** — {post.get('hashtags', '')}")
        asset_ids = post.get("asset_ids", [])
        if asset_ids:
            parts.append(f"- **วัตถุดิบแบรนด์ที่ใช้** — {', '.join(asset_ids)}")
        parts.append("")

        # ## 2. Script (ถ้ามี)
        script = post.get("script", "")
        if script:
            parts.append(f"## {idx}. Script สำหรับวิดีโอ")
            parts.append(script)
            parts.append("")

        # ## 2. Prompt สำหรับ Gen Image
        image_prompts = post.get("image_prompts", [])
        if image_prompts:
            parts.append(f"## {idx}. Prompt สำหรับ Gen Image")
            for i, ip in enumerate(image_prompts, 1):
                parts.append(f"- **{i} ภาพ**")
                parts.append(f"- **Prompt:** {ip.get('prompt', '')}")
                if ip.get("aspect_ratio"):
                    parts.append(f"- **Aspect Ratio:** {ip['aspect_ratio']}")
                if ip.get("resolution"):
                    parts.append(f"- **Resolution:** {ip['resolution']}")
            parts.append("")

        # ## 3. Prompt สำหรับ Gen Video
        video_prompts = post.get("video_prompts", [])
        if video_prompts:
            parts.append(f"## {idx}. Prompt สำหรับ Gen Video")
            for i, vp in enumerate(video_prompts, 1):
                parts.append(f"- **{i} วิดีโอ**")
                parts.append(f"- **Prompt:** {vp.get('prompt', '')}")
                if vp.get("duration"):
                    parts.append(f"- **Duration:** {vp['duration']} วินาที")
                if vp.get("aspect_ratio"):
                    parts.append(f"- **Aspect Ratio:** {vp['aspect_ratio']}")
                if vp.get("resolution"):
                    parts.append(f"- **Resolution:** {vp['resolution']}")
            parts.append("")

    return "\n".join(parts)

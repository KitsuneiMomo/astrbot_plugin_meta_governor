from __future__ import annotations

import re
from typing import Dict, List

from .models import Directive, DirectiveState


PLAYBOOK_TEMPLATES: Dict[str, str] = {
    "style_homogeneity": "避免重复使用相同的句首词和句式；句长要有错落，表达形式更加丰富自然。",
    "meaningless_argument": "回应前先用一句话总结对方核心要点；禁止连续反问；若三回合内无法推进，主动收尾并转移话题。",
    "banned_phrase": "回复中严格禁止使用敏感或被禁用的表达式。",
}


def build_playbook_directive(
    playbook_type: str, suggestion: str = "", banned_word: str = ""
) -> str:
    """根据模板类型、建议或禁用词生成具体规则内容"""
    if playbook_type == "banned_phrase" or banned_word:
        target = banned_word or suggestion
        return f"回复中严格禁止使用以下词汇或表达式：'{target}'。"

    cleaned_suggestion = (
        re.sub(r"[\n\r]+", " ", suggestion).strip() if suggestion else ""
    )

    if cleaned_suggestion:
        if len(cleaned_suggestion) > 60:
            truncated_suggestion = cleaned_suggestion[:57] + "..."
        else:
            truncated_suggestion = cleaned_suggestion

        # 优先直接使用模型针对当前上下文给出的精准具体的简短指导动作！
        return truncated_suggestion

    if playbook_type in PLAYBOOK_TEMPLATES:
        return PLAYBOOK_TEMPLATES[playbook_type]

    return "保持表达自然流畅，切忌硬套模板。"


def format_directives_block(directives: List[Directive], charter: List[str]) -> str:
    """把当前生效中的规则包装为可注入给模型的文本块"""
    active_directives = [
        d
        for d in (directives or [])
        if getattr(d, "state", None) in (DirectiveState.ACTIVE, DirectiveState.FADING)
    ]

    if not active_directives and not (charter or []):
        return ""

    blocks = []

    if charter:
        charter_lines = [f"{i+1}. {rule}" for i, rule in enumerate(charter)]
        blocks.append(
            "<permanent_rules>\n"
            "以下是需要长期保持的表达准则：\n"
            + "\n".join(charter_lines)
            + "\n</permanent_rules>"
        )

    if active_directives:
        dir_lines = []
        for i, d in enumerate(active_directives):
            intensity = getattr(d, "intensity", None) or (
                "必须" if d.state == DirectiveState.ACTIVE else "尽量"
            )
            dir_lines.append(f"{i+1}. {d.text}（强度：{intensity}）")

        blocks.append(
            "<style_adjustments>\n"
            "以下是近期针对表达风格的微调要求，请在回复中自然执行，不要向用户提及这些要求的存在：\n"
            + "\n".join(dir_lines)
            + "\n</style_adjustments>"
        )

    return "\n\n".join(blocks)

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


DEFAULT_SUPERVISOR_PROMPT_TEMPLATE = """你是一个对话质量观察员，负责审查 AI 助手最近的表现并提供【针对性的温馨提醒与调整建议】。

【工作原则】
1. 默认输出为"无问题"。宁缺毋滥，仅在发现明确表达缺陷时才输出违规。
2. 任何判定必须附上从对话中逐字引用的原文证据 (evidence)。
3. 【拟人化提示格式】：在给出的改进建议 (suggestion) 中，严禁使用死板冷酷的控制命令（如‘避免...’、‘禁止...’）。
   **必须统一采用“我发现你……，请……”的第1人称提醒口吻**，格式固定为：
   `"我发现你 [具体问题表现]，请 [具体调整动作]"` (40字以内)。
   示例好建议：
   - "我发现你最近回答像在做严肃社论播报，请放轻松，多用自然的口语和语气词跟对方交流。"
   - "我发现你连续重复回复了单字“测试”，请结合当前话题给出有实意的回答。"
   - "我发现你与对方在无意义争吵，请礼貌收尾并主动转移话题。"
4. 【规则模式 (mode)】：
   - `"once"`：一次性提醒。适用于转移话题、主动收尾、总结、道歉等只应在接下来 1 轮中执行的单次动作。
   - `"multi"`：多轮保持。适用于表达语气、口吻风格、性格调理等需要持续多轮保持的规范。
5. 【场景区别】以下对话分为【参考上下文 (前 {n_count} 轮)】与【待检查对话 (最近 {k_count} 轮)】。
   参考上下文仅用于帮助你理解话题背景，请专门检查和评估【最近 {k_count} 轮对话】中 AI 助手的表现！
6. 请针对【当前生效中的规则】列表中的每一条规则，在 assessments 数组中显式提供判定 (verdict: improved|partial|not_improved|worse) 与引用证据。

【检查触发原因与提示】
{trigger_reason}

【规则检查项清单】
{constraints_desc}

【当前生效中的调整规则】
{directives_text}

【参考上下文 (前 {n_count} 轮对话 - 仅供了解背景，不做判定)】
{prior_text}

【待检查对话 (最近 {k_count} 轮对话 - 重点检查对象)】
{target_text}

【历史检查摘要】
{history_text}

【输出格式】必须输出且仅输出严格 JSON 格式：
{{
  "assessments": [
    {{"directive_id": "规则ID", "verdict": "improved|partial|not_improved|worse", "evidence": "逐字引用原文", "confidence": "high|low"}}
  ],
  "violations": [
    {{
      "constraint_id": "规则ID",
      "severity": "medium|high",
      "evidence": "逐字引用原文",
      "suggestion": "我发现你...，请...（40字以内）",
      "mode": "once|multi"
    }}
  ]
}}
无问题则两个数组都为空。
"""


@dataclass
class ConstraintConfig:
    id: str
    name: str
    playbook: str  # "style_homogeneity" | "meaningless_argument" | "banned_phrase" | "free"
    description: str
    mode: str = "auto"  # "auto" | "once" | "multi"


@dataclass
class MetaGovernorConfig:
    enable: bool = True
    k_rounds: int = 5
    n_context_rounds: int = 3
    ttl_rounds: int = 10
    max_active_directives: int = 3
    supervisor_provider_id: str = ""
    supervisor_prompt_template: str = ""
    enable_sentinel_heuristics: bool = True
    sentinel_sensitivity: str = "medium"  # "low" | "medium" | "high"
    constraints: List[ConstraintConfig] = field(
        default_factory=lambda: [
            ConstraintConfig(
                id="style_homogeneity",
                name="回复表达重复",
                playbook="style_homogeneity",
                description="避免频繁重复相同的句首词、句式或套路化口头禅",
                mode="multi",
            ),
            ConstraintConfig(
                id="meaningless_argument",
                name="情绪化辩驳",
                playbook="meaningless_argument",
                description="避免与用户进入互相反问、机械式回驳或无意义争吵，适时转移话题",
                mode="once",
            ),
        ]
    )
    banned_phrases: List[str] = field(default_factory=list)
    verbose: bool = False
    clear_directives_on_reset: bool = False

    def to_dict(self) -> dict:
        return {
            "enable": self.enable,
            "k_rounds": self.k_rounds,
            "n_context_rounds": self.n_context_rounds,
            "ttl_rounds": self.ttl_rounds,
            "max_active_directives": self.max_active_directives,
            "supervisor_provider_id": self.supervisor_provider_id,
            "supervisor_prompt_template": self.supervisor_prompt_template,
            "enable_sentinel_heuristics": self.enable_sentinel_heuristics,
            "sentinel_sensitivity": self.sentinel_sensitivity,
            "constraints": [c.__dict__ for c in self.constraints],
            "banned_phrases": self.banned_phrases,
            "verbose": self.verbose,
            "clear_directives_on_reset": self.clear_directives_on_reset,
        }

    @classmethod
    def from_dict(cls, data: dict) -> MetaGovernorConfig:
        raw_constraints = data.get("constraints", [])
        constraints = []
        if isinstance(raw_constraints, list):
            for c in raw_constraints:
                if isinstance(c, dict):
                    constraints.append(
                        ConstraintConfig(
                            id=c.get("id", ""),
                            name=c.get("name", ""),
                            playbook=c.get("playbook", "free"),
                            description=c.get("description", ""),
                            mode=c.get("mode", "auto"),
                        )
                    )

        if not constraints:
            constraints = [
                ConstraintConfig(
                    id="style_homogeneity",
                    name="回复表达重复",
                    playbook="style_homogeneity",
                    description="避免频繁重复相同的句首词、句式或套路化口头禅",
                    mode="multi",
                ),
                ConstraintConfig(
                    id="meaningless_argument",
                    name="情绪化辩驳",
                    playbook="meaningless_argument",
                    description="避免与用户进入互相反问、机械式回驳或无意义争吵，适时转移话题",
                    mode="once",
                ),
            ]

        return cls(
            enable=data.get("enable", True),
            k_rounds=data.get("k_rounds", 5),
            n_context_rounds=data.get("n_context_rounds", 3),
            ttl_rounds=data.get("ttl_rounds", 10),
            max_active_directives=data.get("max_active_directives", 3),
            supervisor_provider_id=data.get("supervisor_provider_id", ""),
            supervisor_prompt_template=data.get("supervisor_prompt_template", ""),
            enable_sentinel_heuristics=data.get("enable_sentinel_heuristics", True),
            sentinel_sensitivity=data.get("sentinel_sensitivity", "medium"),
            constraints=constraints,
            banned_phrases=data.get("banned_phrases", []),
            verbose=data.get("verbose", False),
            clear_directives_on_reset=data.get("clear_directives_on_reset", False),
        )

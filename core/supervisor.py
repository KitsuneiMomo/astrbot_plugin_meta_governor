from __future__ import annotations

import json
import re
from typing import Any, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.star import Context

from .config import DEFAULT_SUPERVISOR_PROMPT_TEMPLATE, MetaGovernorConfig
from .models import Directive, DirectiveState, Round, SessionState, SupervisorEvalOutput


class SupervisorEvaluator:
    """质量检查员：异步调用 LLM 对对话质量进行分析评估与微调指导"""

    def __init__(self, context: Context, config: MetaGovernorConfig):
        self.context = context
        self.config = config

    def _format_rounds(self, rounds: List[Round], label: str) -> str:
        if not rounds:
            return f"({label}: 无)"
        lines = []
        for r in rounds:
            lines.append(f"用户[{r.sender_id or 'User'}]: {r.user_text}")
            lines.append(f"Bot: {r.bot_text}")
            lines.append("---")
        return "\n".join(lines)

    def _build_prompt(
        self,
        prior_n: List[Round],
        target_k: List[Round],
        active_directives: List[Directive],
        constraints_desc: str,
        eval_history: List[dict],
        trigger_reason: str = "",
    ) -> str:
        prior_text = self._format_rounds(prior_n, "前 n 轮参考上下文")
        target_text = self._format_rounds(target_k, "最近 k 轮待检查对话")

        directives_text = "(无)"
        if active_directives:
            directives_text = "\n".join(
                f"- [规则ID: {d.id}] [类型: {d.constraint_id}] [内容: {d.text}] (当前强度: {d.intensity})"
                for d in active_directives
            )

        history_text = "(无)"
        if eval_history:
            recent_hist = eval_history[-5:]
            history_text = "\n".join(
                f"- 轮次 {h.get('round', 0)}: 触发原因={h.get('reason', '')}, 结论={h.get('verdict_summary', '')}"
                for h in recent_hist
            )

        reason_text = trigger_reason if trigger_reason else "(无)"

        template = (
            self.config.supervisor_prompt_template.strip()
            if self.config.supervisor_prompt_template and self.config.supervisor_prompt_template.strip()
            else DEFAULT_SUPERVISOR_PROMPT_TEMPLATE
        )

        try:
            return template.format(
                n_count=len(prior_n),
                k_count=len(target_k),
                trigger_reason=reason_text,
                constraints_desc=constraints_desc,
                directives_text=directives_text,
                prior_text=prior_text,
                target_text=target_text,
                history_text=history_text,
            )
        except Exception as e:
            logger.warning(
                f"[Meta-Governor] 自定义 Supervisor Prompt 格式化异常: {e}，回退使用默认模板"
            )
            return DEFAULT_SUPERVISOR_PROMPT_TEMPLATE.format(
                n_count=len(prior_n),
                k_count=len(target_k),
                trigger_reason=reason_text,
                constraints_desc=constraints_desc,
                directives_text=directives_text,
                prior_text=prior_text,
                target_text=target_text,
                history_text=history_text,
            )

    async def _call_llm(self, umo: str, prompt: str) -> str:
        """调用 LLM 生成质量分析"""
        provider_id = self.config.supervisor_provider_id
        if not provider_id and hasattr(self.context, "get_current_chat_provider_id"):
            try:
                provider_id = await self.context.get_current_chat_provider_id(umo)
            except Exception:
                provider_id = ""

        if hasattr(self.context, "llm_generate"):
            try:
                kwargs = {"prompt": prompt}
                if provider_id:
                    kwargs["chat_provider_id"] = provider_id
                res = await self.context.llm_generate(**kwargs)
                if hasattr(res, "completion_text"):
                    return res.completion_text
                elif isinstance(res, str):
                    return res
                elif hasattr(res, "text"):
                    return res.text
            except Exception as e:
                logger.warning(f"[Meta-Governor] context.llm_generate 调用异常: {e}")

        prov_mgr = getattr(self.context, "provider_manager", None)
        if prov_mgr:
            try:
                prov = None
                if provider_id:
                    if hasattr(prov_mgr, "get_provider_by_id"):
                        try:
                            prov = prov_mgr.get_provider_by_id(provider_id)
                        except Exception:
                            prov = None
                    if not prov and hasattr(prov_mgr, "get_provider"):
                        try:
                            prov = prov_mgr.get_provider(provider_id)
                        except Exception:
                            prov = None

                if not prov and hasattr(prov_mgr, "get_using_provider"):
                    try:
                        prov = prov_mgr.get_using_provider()
                    except Exception:
                        prov = None

                if not prov and hasattr(prov_mgr, "get_default_provider"):
                    try:
                        prov = prov_mgr.get_default_provider()
                    except Exception:
                        prov = None

                if prov and hasattr(prov, "text_chat"):
                    resp = await prov.text_chat(prompt=prompt)
                    if hasattr(resp, "completion_text"):
                        return resp.completion_text
                    elif isinstance(resp, str):
                        return resp
                    elif hasattr(resp, "text"):
                        return resp.text
            except Exception as e:
                logger.warning(f"[Meta-Governor] fallback text_chat 调用异常: {e}")

        raise RuntimeError("无法调用质量检查模型（llm_generate 及 provider manager 均不可用）")

    @staticmethod
    def _parse_json_response(raw_text: str) -> dict:
        clean_text = raw_text.strip()
        match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", clean_text, re.IGNORECASE)
        if match:
            clean_text = match.group(1)
        else:
            first_brace = clean_text.find("{")
            last_brace = clean_text.rfind("}")
            if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
                clean_text = clean_text[first_brace : last_brace + 1]

        return json.loads(clean_text)

    async def evaluate(
        self,
        session: SessionState,
        prior_n: List[Round],
        target_k: List[Round],
        trigger_reason: str = "",
    ) -> Optional[SupervisorEvalOutput]:
        """执行质量分析（带重试）"""
        if not target_k:
            logger.info("[Meta-Governor] 待检查对话 (target_k) 为空，放弃检查")
            return None

        active_directives = [
            d
            for d in session.directives.values()
            if d.state in (DirectiveState.ACTIVE, DirectiveState.FADING)
        ]

        constraints_lines = []
        for c in self.config.constraints:
            constraints_lines.append(f"- [规则ID: {c.id}] ({c.name}): {c.description}")
        constraints_desc = "\n".join(constraints_lines)

        prompt = self._build_prompt(
            prior_n=prior_n,
            target_k=target_k,
            active_directives=active_directives,
            constraints_desc=constraints_desc,
            eval_history=session.eval_history,
            trigger_reason=trigger_reason,
        )

        last_error = None
        for attempt in range(2):
            try:
                curr_prompt = prompt
                if attempt > 0:
                    curr_prompt += "\n\n【重要提示】上一次回复解析失败，非合法 JSON。请注意：你的输出必须且只能是纯 JSON 格式（包含 assessments 与 violations 数组），切勿输出任何 markdown 文字说明或外层包装！"
                raw_resp = await self._call_llm(session.umo, curr_prompt)
                parsed_json = self._parse_json_response(raw_resp)
                eval_output = SupervisorEvalOutput.from_dict(parsed_json)
                logger.info(
                    f"[Meta-Governor] 对话质量检查完成: assessments={len(eval_output.assessments)}, violations={len(eval_output.violations)}"
                )
                return eval_output
            except Exception as e:
                last_error = e
                logger.warning(
                    f"[Meta-Governor] 质量检查输出解析失败 (尝试 {attempt+1}/2): {e}"
                )

        logger.error(f"[Meta-Governor] 质量检查解析连续两次失败: {last_error}")
        return None

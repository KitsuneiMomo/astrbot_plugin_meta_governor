from __future__ import annotations

import uuid
from typing import Dict, List, Tuple

from astrbot.api import logger

from .config import MetaGovernorConfig
from .models import Directive, DirectiveState, SessionState, SupervisorEvalOutput
from .templates import build_playbook_directive


class DirectiveStateMachine:
    """规则生命周期状态机：平滑过渡、撤销、再次触发与常驻规则升级"""

    def __init__(self, config: MetaGovernorConfig):
        self.config = config

    def on_new_round(self, session: SessionState) -> List[str]:
        """每完成一轮对话时扣减有效轮次，自然过期解除"""
        expired_ids = []
        for d_id, directive in list(session.directives.items()):
            if directive.state in (DirectiveState.ACTIVE, DirectiveState.FADING):
                directive.remaining_ttl -= 1
                if directive.mode == "once" or directive.remaining_ttl <= 0:
                    directive.state = DirectiveState.EXPIRED
                    expired_ids.append(d_id)
                    logger.info(
                        f"[Meta-Governor] 规则 [{d_id}] ({directive.constraint_id}) ({'一次性' if directive.mode == 'once' else '多轮'}) 已自然解除"
                    )
        return expired_ids

    def process_eval_output(
        self, session: SessionState, eval_output: SupervisorEvalOutput
    ) -> Tuple[List[str], List[str], List[str]]:
        """处理检查模型的判定结果。

        Returns:
            (new_directives_events, revoked_events, upgraded_charter_events)
        """
        new_events = []
        revoked_events = []
        upgraded_charters = []

        # 1. 处理已有规则的判定 assessments
        for assessment in eval_output.assessments:
            d_id = assessment.directive_id
            if d_id not in session.directives:
                continue

            directive = session.directives[d_id]
            verdict = assessment.verdict.lower()
            confidence = assessment.confidence.lower()

            if confidence == "low":
                logger.info(f"[Meta-Governor] 规则 [{d_id}] 评估置信度为 low，跳过处理")
                continue

            if verdict in ("improved", "good"):
                directive.improvement_streak += 1
                if directive.mode == "once":
                    directive.state = DirectiveState.EXPIRED
                    revoked_events.append(f"一次性规则 [{d_id}] 已成功执行并解除")
                elif directive.state == DirectiveState.ACTIVE:
                    if directive.improvement_streak >= 2:
                        directive.state = DirectiveState.FADING
                        directive.intensity = "尽量"
                        directive.improvement_streak = 0
                        revoked_events.append(
                            f"规则 [{d_id}] ('{directive.constraint_id}') 表现持续改善，进入观察解除阶段"
                        )
                elif directive.state == DirectiveState.FADING:
                    if directive.improvement_streak >= 2:
                        directive.state = DirectiveState.EXPIRED
                        revoked_events.append(
                            f"规则 [{d_id}] ('{directive.constraint_id}') 表现良好，已正式解除"
                        )

            elif verdict in ("not_improved", "worse"):
                directive.improvement_streak = 0

                if directive.state == DirectiveState.FADING:
                    directive.state = DirectiveState.ACTIVE
                    directive.intensity = "必须"
                    directive.relapse_count += 1
                    logger.warning(
                        f"[Meta-Governor] 规则 [{d_id}] 再次触发 (计数={directive.relapse_count})"
                    )

                    if directive.relapse_count >= 2:
                        charter_rule = f"【常驻规则】{directive.text}"
                        if charter_rule not in session.charter:
                            session.charter.append(charter_rule)
                        directive.state = DirectiveState.EXPIRED
                        upgraded_charters.append(charter_rule)
                        logger.info(
                            f"[Meta-Governor] 规则 [{d_id}] 多次触发，已转为常驻规则"
                        )
                    else:
                        new_events.append(
                            f"规则 [{d_id}] 再次触发，恢复为必须执行"
                        )

                elif directive.state == DirectiveState.ACTIVE:
                    directive.reword_count += 1
                    if directive.reword_count <= 2:
                        if assessment.evidence:
                            directive.evidence = assessment.evidence
                        directive.text = build_playbook_directive(
                            directive.playbook, assessment.evidence or assessment.verdict
                        )
                        logger.info(
                            f"[Meta-Governor] 规则 [{d_id}] 未改善，已重新调整措辞: {directive.text}"
                        )
                    elif directive.reword_count == 3:
                        pb_text = build_playbook_directive(directive.playbook, "")
                        directive.text = pb_text
                        logger.info(
                            f"[Meta-Governor] 规则 [{d_id}] 仍未改善，切换为标准规则模板"
                        )
                    else:
                        directive.text = f"【须重点改善】{build_playbook_directive(directive.playbook, '')}"
                        new_events.append(f"规则 [{d_id}] 标记为重点改善项")

        # 2. 处理新发现的问题 violations
        constraint_map = {c.id: c for c in self.config.constraints}

        for violation in eval_output.violations:
            if violation.severity.lower() == "low":
                logger.info(f"[Meta-Governor] 忽略低严重度问题记录 ({violation.constraint_id})")
                continue

            if not violation.evidence:
                logger.info(
                    f"[Meta-Governor] 忽略缺乏引用的问题记录 ({violation.constraint_id})"
                )
                continue

            c_id = violation.constraint_id
            playbook_type = "free"
            c_config = constraint_map.get(c_id)
            if c_config:
                playbook_type = c_config.playbook

            # 确定规则模式 (once | multi)
            c_mode = getattr(c_config, "mode", "auto") if c_config else "auto"
            if c_mode in ("once", "multi"):
                effective_mode = c_mode
            else:
                v_mode = str(getattr(violation, "mode", "auto") or "").lower()
                if v_mode in ("once", "multi"):
                    effective_mode = v_mode
                else:
                    sugg = violation.suggestion or ""
                    if any(w in sugg for w in ("转移话题", "收尾", "总结", "道歉", "一句话总结")):
                        effective_mode = "once"
                    else:
                        effective_mode = "multi"

            ttl = 1 if effective_mode == "once" else self.config.ttl_rounds

            existing_active = [
                d
                for d in session.directives.values()
                if d.constraint_id == c_id
                and d.state in (DirectiveState.ACTIVE, DirectiveState.FADING)
            ]

            dir_text = build_playbook_directive(playbook_type, violation.suggestion)

            if existing_active:
                active_d = existing_active[0]

                if active_d.state == DirectiveState.FADING:
                    active_d.state = DirectiveState.ACTIVE
                    active_d.intensity = "必须"
                    active_d.relapse_count += 1
                    active_d.improvement_streak = 0
                    logger.warning(
                        f"[Meta-Governor] 观察期规则 [{active_d.id}] 再次触发 (计数={active_d.relapse_count})"
                    )

                    if active_d.relapse_count >= 2:
                        charter_rule = f"【常驻规则】{dir_text}"
                        if charter_rule not in session.charter:
                            session.charter.append(charter_rule)
                        active_d.state = DirectiveState.EXPIRED
                        upgraded_charters.append(charter_rule)
                        logger.info(
                            f"[Meta-Governor] 观察期多次触发，已转为常驻规则 [{active_d.id}]"
                        )
                        continue

                active_d.text = dir_text
                active_d.evidence = violation.evidence
                active_d.mode = effective_mode
                active_d.ttl_rounds = ttl
                active_d.remaining_ttl = ttl
                logger.info(f"[Meta-Governor] 更新已有规则 [{c_id}] 的文本与模式 ({effective_mode})")
            else:
                active_count = sum(
                    1
                    for d in session.directives.values()
                    if d.state in (DirectiveState.ACTIVE, DirectiveState.FADING)
                )

                if active_count >= self.config.max_active_directives:
                    oldest = min(
                        [
                            d
                            for d in session.directives.values()
                            if d.state in (DirectiveState.ACTIVE, DirectiveState.FADING)
                        ],
                        key=lambda x: x.created_round,
                        default=None,
                    )
                    if oldest:
                        oldest.state = DirectiveState.EXPIRED
                        logger.info(
                            f"[Meta-Governor] 达到最大生效规则数 ({self.config.max_active_directives})，自动替换最旧规则 [{oldest.id}]"
                        )

                new_id = str(uuid.uuid4())[:8]
                new_dir = Directive(
                    id=new_id,
                    constraint_id=c_id,
                    playbook=playbook_type,
                    text=dir_text,
                    state=DirectiveState.ACTIVE,
                    intensity="必须",
                    mode=effective_mode,
                    created_round=session.round_counter,
                    ttl_rounds=ttl,
                    remaining_ttl=ttl,
                    evidence=violation.evidence,
                )
                session.directives[new_id] = new_dir
                mode_label = "一次性" if effective_mode == "once" else "多轮"
                new_events.append(f"新增调整规则 [{new_id}] ({mode_label}): {dir_text}")
                logger.info(f"[Meta-Governor] 成功生成新调整规则 [{new_id}] ('{c_id}', {mode_label})")

        return new_events, revoked_events, upgraded_charters

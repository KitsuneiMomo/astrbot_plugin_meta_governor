from __future__ import annotations

import time
from collections import deque
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain

from .models import Round


class MessageCollector:
    """对话轮次采集器：监听并按 UMO 记录用户输入与 Bot 实际发出的最终回复"""

    def __init__(self, max_buffer_multiplier: int = 4):
        # per-UMO round deque buffer
        self.buffers: Dict[str, deque[Round]] = {}
        # per-event or per-UMO pending user prompt: umo -> (user_text, timestamp, sender_id)
        self.pending_user_prompts: Dict[str, Tuple[str, float, str]] = {}
        # internal broadcast message markers to ignore self-sent messages
        self.internal_message_ids: set[str] = set()
        self.max_buffer_multiplier = max_buffer_multiplier

    def record_user_request(
        self, event: AstrMessageEvent, prompt_text: str
    ) -> Optional[str]:
        """在 on_llm_request 阶段记录用户输入提示词"""
        umo = str(getattr(event, "unified_msg_origin", ""))
        if not umo or not prompt_text:
            return None

        # Check if event is internal plugin broadcast
        if getattr(event, "_meta_governor_internal", False):
            return None

        sender_id = ""
        if hasattr(event, "message_obj") and event.message_obj:
            if hasattr(event.message_obj, "sender") and event.message_obj.sender:
                sender_id = str(getattr(event.message_obj.sender, "user_id", ""))

        self.pending_user_prompts[umo] = (prompt_text, time.time(), sender_id)
        return umo

    def record_bot_response(
        self, event: AstrMessageEvent, total_round_index: int, max_rounds_keep: int = 30
    ) -> Optional[Round]:
        """在 on_decorating_result 阶段提取发出的最终纯文本，配对生成完整一轮对话"""
        umo = str(getattr(event, "unified_msg_origin", ""))
        if not umo:
            return None

        if getattr(event, "_meta_governor_internal", False):
            return None

        pending = self.pending_user_prompts.pop(umo, None)
        if not pending:
            return None

        user_text, ts, sender_id = pending

        # Extract plain text from event result chain
        result = event.get_result()
        if not result or not hasattr(result, "chain") or not result.chain:
            return None

        bot_text_parts = []
        for comp in result.chain:
            if isinstance(comp, Plain):
                bot_text_parts.append(str(comp.text))
            elif hasattr(comp, "text") and isinstance(comp.text, str):
                bot_text_parts.append(comp.text)

        bot_text = "".join(bot_text_parts).strip()
        if not bot_text:
            return None

        round_item = Round(
            user_text=user_text.strip(),
            bot_text=bot_text,
            timestamp=ts,
            sender_id=sender_id,
            round_index=total_round_index,
        )

        if umo not in self.buffers:
            self.buffers[umo] = deque(maxlen=max_rounds_keep)

        self.buffers[umo].append(round_item)
        return round_item

    def get_rounds_snapshot(
        self, umo: str, k: int, n: int
    ) -> Tuple[List[Round], List[Round]]:
        """获取 (prior_n_rounds, target_k_rounds) 快照。

        - target_k_rounds: 最近 k 轮 (需被评估的目标轮次)
        - prior_n_rounds: k 轮之前的 n 轮 (仅作为背景上下文提供给模型)
        """
        if umo not in self.buffers or not self.buffers[umo]:
            return [], []

        all_rounds = list(self.buffers[umo])
        total = len(all_rounds)

        target_k = all_rounds[-k:] if total >= k else all_rounds[:]
        rem_count = total - len(target_k)

        if rem_count > 0 and n > 0:
            prior_n = all_rounds[- (len(target_k) + min(rem_count, n)) : -len(target_k)]
        else:
            prior_n = []

        return prior_n, target_k

    def clear_session(self, umo: str) -> None:
        """清空指定 UMO 的内存轮次队列"""
        self.buffers.pop(umo, None)
        self.pending_user_prompts.pop(umo, None)

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple

from .config import MetaGovernorConfig
from .models import Round, SentinelCheckResult


class LocalSentinel:
    """自动检测器：运行于本地的轻量过滤器，实时扫描回复风格与禁用词"""

    def __init__(self, config: MetaGovernorConfig):
        self.config = config

    def _get_sensitivity_params(self) -> Tuple[float, float, float]:
        """获取 (repetition_ratio_threshold, cv_threshold, jaccard_threshold)"""
        sens = self.config.sentinel_sensitivity.lower()
        if sens == "high":
            return (0.6, 0.20, 0.45)
        elif sens == "low":
            return (0.4, 0.10, 0.60)
        else:  # medium
            return (0.5, 0.15, 0.50)

    @staticmethod
    def _get_3grams(text: str) -> Set[str]:
        text = re.sub(r"\s+", "", text)
        if len(text) < 3:
            return {text} if text else set()
        return {text[i : i + 3] for i in range(len(text) - 2)}

    @staticmethod
    def _jaccard_similarity(set_a: Set[str], set_b: Set[str]) -> float:
        if not set_a or not set_b:
            return 0.0
        intersection = len(set_a.intersection(set_b))
        union = len(set_a.union(set_b))
        return intersection / union if union > 0 else 0.0

    def check(
        self,
        recent_rounds: List[Round],
        round_counter: int,
        last_eval_round: int,
    ) -> SentinelCheckResult:
        """根据最近对话记录进行自动规则扫描"""
        if not recent_rounds:
            return SentinelCheckResult(triggered=False)

        latest_round = recent_rounds[-1]

        # 1. 禁用词检测 (硬违规，属于绝对拦截，不受自动检测预警开关影响)
        if self.config.banned_phrases:
            for phrase in self.config.banned_phrases:
                if phrase and phrase in latest_round.bot_text:
                    return SentinelCheckResult(
                        triggered=True,
                        reason=f"命中禁用词: '{phrase}'",
                        target_constraint_id="banned_phrase",
                        is_hard_violation=True,
                        evidence=f"回复中包含禁用词 '{phrase}'",
                        banned_word=phrase,
                    )

        # 仅在开启“自动预警检测” (enable_sentinel_heuristics=True) 时才扫描句首/相似度/字数单调/争吵规则
        if self.config.enable_sentinel_heuristics and (round_counter - last_eval_round) >= 2:
            rep_thresh, cv_thresh, jaccard_thresh = self._get_sensitivity_params()
            bot_texts = [r.bot_text for r in recent_rounds[-5:]]

            if len(bot_texts) >= 3:
                # 2. 句首重复率检测
                first_words = [t.strip()[:4] for t in bot_texts if t.strip()]
                if len(first_words) >= 3:
                    unique_ratio = len(set(first_words)) / len(first_words)
                    if unique_ratio < rep_thresh:
                        return SentinelCheckResult(
                            triggered=True,
                            reason=f"句首词重复偏高 (去重比例 {unique_ratio:.2f} < {rep_thresh})",
                            target_constraint_id="style_homogeneity",
                            evidence=f"最近句首词: {first_words}",
                        )

                # 3. 模板化 3-gram Jaccard 相似度
                ngram_sets = [self._get_3grams(t) for t in bot_texts]
                max_jaccard = 0.0
                for i in range(len(ngram_sets) - 1):
                    sim = self._jaccard_similarity(ngram_sets[i], ngram_sets[i + 1])
                    if sim > max_jaccard:
                        max_jaccard = sim
                if max_jaccard > jaccard_thresh:
                    return SentinelCheckResult(
                        triggered=True,
                        reason=f"回复句式相似度偏高 ({max_jaccard:.2f} > {jaccard_thresh})",
                        target_constraint_id="style_homogeneity",
                        evidence=f"最高相似度: {max_jaccard:.2f}",
                    )

            if len(bot_texts) >= 4:
                # 4. 长度单调 (CV < cv_thresh)
                lengths = [len(t) for t in bot_texts]
                mean_len = sum(lengths) / len(lengths)
                if mean_len > 10:
                    variance = sum((x - mean_len) ** 2 for x in lengths) / len(lengths)
                    std_dev = math.sqrt(variance)
                    cv = std_dev / mean_len
                    if cv < cv_thresh:
                        return SentinelCheckResult(
                            triggered=True,
                            reason=f"回复长度过于单调 (CV {cv:.2f} < {cv_thresh})",
                            target_constraint_id="style_homogeneity",
                            evidence=f"回复字数序列: {lengths}",
                        )

            # 5. 争吵检测 (需要连续 3 轮命中)
            if len(recent_rounds) >= 3:
                consecutive_arg = 0
                negation_words = {"不是", "不对", "胡说", "不可能", "凭什么", "凭啥", "怎么可能"}
                for r in recent_rounds[-3:]:
                    user_has_qm = "?" in r.user_text or "？" in r.user_text
                    bot_has_qm = "?" in r.bot_text or "？" in r.bot_text
                    has_qm = user_has_qm and bot_has_qm
                    has_neg = any(w in r.user_text for w in negation_words) or any(w in r.bot_text for w in negation_words)
                    length_ok = len(r.user_text) >= 15 or len(r.bot_text) >= 15
                    if has_qm and has_neg and length_ok:
                        consecutive_arg += 1

                if consecutive_arg >= 3:
                    return SentinelCheckResult(
                        triggered=True,
                        reason="连续 3 轮检测到反问与争执信号",
                        target_constraint_id="meaningless_argument",
                        evidence="连续3轮双方包含否定词与反问句",
                    )

        # 6. 常规检查 (满 k 轮对话例行触发)
        k = max(1, self.config.k_rounds)
        if (round_counter - last_eval_round) >= k:
            return SentinelCheckResult(
                triggered=True,
                reason=f"满 {k} 轮对话例行检查",
                target_constraint_id=None,
            )

        return SentinelCheckResult(triggered=False)

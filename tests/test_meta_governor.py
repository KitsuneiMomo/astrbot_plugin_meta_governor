from __future__ import annotations

import pytest
import time
from collections import deque

from astrbot_plugin_meta_governor.core.models import (
    Directive,
    DirectiveState,
    Round,
    SessionState,
    SupervisorEvalOutput,
    AssessmentResult,
    ViolationItem,
)
from astrbot_plugin_meta_governor.core.config import MetaGovernorConfig, ConstraintConfig
from astrbot_plugin_meta_governor.core.collector import MessageCollector
from astrbot_plugin_meta_governor.core.sentinel import LocalSentinel
from astrbot_plugin_meta_governor.core.state_machine import DirectiveStateMachine
from astrbot_plugin_meta_governor.core.templates import format_directives_block, build_playbook_directive


def test_collector_kn_snapshot():
    collector = MessageCollector()
    umo = "test_group_1"

    collector.buffers[umo] = deque(maxlen=20)
    for i in range(1, 11):
        collector.buffers[umo].append(
            Round(
                user_text=f"User question {i}",
                bot_text=f"Bot answer {i}",
                round_index=i,
            )
        )

    prior_n, target_k = collector.get_rounds_snapshot(umo, k=5, n=3)

    assert len(target_k) == 5
    assert [r.round_index for r in target_k] == [6, 7, 8, 9, 10]

    assert len(prior_n) == 3
    assert [r.round_index for r in prior_n] == [3, 4, 5]


def test_sentinel_hard_violation_and_cooldown():
    config = MetaGovernorConfig(
        k_rounds=5,
        banned_phrases=["敏感表达", "无用指令"],
        sentinel_sensitivity="medium",
    )
    sentinel = LocalSentinel(config)

    rounds1 = [Round(user_text="hi", bot_text="这句话包含了敏感表达呢")]
    res1 = sentinel.check(rounds1, round_counter=1, last_eval_round=1)
    assert res1.triggered is True
    assert res1.is_hard_violation is True
    assert res1.banned_word == "敏感表达"

    rounds2 = [
        Round(user_text=f"q{i}", bot_text=f"我是一个助手 {i}") for i in range(5)
    ]
    res2_cooldown = sentinel.check(rounds2, round_counter=5, last_eval_round=4)
    assert res2_cooldown.triggered is False

    res2_ok = sentinel.check(rounds2, round_counter=5, last_eval_round=3)
    assert res2_ok.triggered is True
    assert res2_ok.target_constraint_id == "style_homogeneity"


def test_sentinel_tightened_argument():
    config = MetaGovernorConfig(k_rounds=5, sentinel_sensitivity="medium")
    sentinel = LocalSentinel(config)

    normal_qa = [
        Round(user_text="这样会报错吗？", bot_text="不会，程序逻辑正常", round_index=i)
        for i in range(1, 4)
    ]
    res_normal = sentinel.check(normal_qa, round_counter=3, last_eval_round=0)
    assert res_normal.triggered is False or res_normal.target_constraint_id != "meaningless_argument"

    arg_rounds = [
        Round(
            user_text="你怎么能这么胡说八道呢？这难道不对吗？",
            bot_text="我完全没有胡说，你说的这些根本就不是事实好不好？",
            round_index=1,
        ),
        Round(
            user_text="凭什么说我讲的不是事实？你自己难道没看文档吗？",
            bot_text="凭啥这么说？明明是你自己根本没有看懂文档的逻辑好不好？",
            round_index=2,
        ),
        Round(
            user_text="怎么可能是我没看懂？你这不是在强词夺理吗？",
            bot_text="这不可能是我在强词夺理，明明就是你在胡说八道好吗？",
            round_index=3,
        ),
    ]
    res_arg = sentinel.check(arg_rounds, round_counter=3, last_eval_round=0)
    assert res_arg.triggered is True
    assert res_arg.target_constraint_id == "meaningless_argument"


def test_fading_directive_relapse():
    config = MetaGovernorConfig(ttl_rounds=10, max_active_directives=3)
    sm = DirectiveStateMachine(config)
    session = SessionState(umo="test_umo")

    d_id = "d123"
    directive = Directive(
        id=d_id,
        constraint_id="style_homogeneity",
        playbook="style_homogeneity",
        text="避免重复",
        state=DirectiveState.FADING,
        intensity="尽量",
        relapse_count=0,
    )
    session.directives[d_id] = directive

    eval_output = SupervisorEvalOutput(
        assessments=[],
        violations=[
            ViolationItem(
                constraint_id="style_homogeneity",
                severity="medium",
                evidence="依然在重复使用了句首词",
                suggestion="句首词丰富化",
            )
        ],
    )
    sm.process_eval_output(session, eval_output)

    assert directive.state == DirectiveState.ACTIVE
    assert directive.intensity == "必须"
    assert directive.relapse_count == 1


def test_templates_banned_phrase_formatting():
    text1 = build_playbook_directive("banned_phrase", banned_word="敏感表达")
    assert "敏感表达" in text1
    assert "严格禁止使用" in text1

    text2 = build_playbook_directive("banned_phrase", suggestion="违禁词汇")
    assert "严格禁止使用" in text2


def test_build_playbook_directive_suggestion_truncation():
    long_suggestion = "这是一个超级长超级长超级长超级长超级长超级长超级长超级长超级长超级长超级长超级长超级长超级长超级长超级长超级长超级长的具体建议"
    assert len(long_suggestion) > 60

    text = build_playbook_directive("style_homogeneity", suggestion=long_suggestion)
    expected_truncated = long_suggestion[:57] + "..."
    assert text == expected_truncated

    short_suggestion = "避免连续单字回复“测试”"
    text_short = build_playbook_directive("style_homogeneity", suggestion=short_suggestion)
    assert text_short == "避免连续单字回复“测试”"


def test_once_mode_directive_lifecycle():
    config = MetaGovernorConfig(
        ttl_rounds=10,
        constraints=[
            ConstraintConfig(
                id="topic_shift",
                name="话题转移",
                playbook="free",
                description="发现争吵时主动转移话题",
                mode="once",
            )
        ],
    )
    sm = DirectiveStateMachine(config)
    session = SessionState(umo="test_once_umo")

    eval_output = SupervisorEvalOutput(
        assessments=[],
        violations=[
            ViolationItem(
                constraint_id="topic_shift",
                severity="high",
                evidence="双方互相质问中",
                suggestion="我发现你在与对方争吵，请主动收尾并转移话题",
                mode="once",
            )
        ],
    )
    new_events, _, _ = sm.process_eval_output(session, eval_output)

    assert len(session.directives) == 1
    d = list(session.directives.values())[0]
    assert d.mode == "once"
    assert d.remaining_ttl == 1
    assert d.state == DirectiveState.ACTIVE

    # Execute 1 round
    expired = sm.on_new_round(session)
    assert d.id in expired
    assert d.state == DirectiveState.EXPIRED

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, List, Optional, Any


class DirectiveState(str, Enum):
    ACTIVE = "active"
    FADING = "fading"
    EXPIRED = "expired"


@dataclass
class Round:
    user_text: str
    bot_text: str
    timestamp: float = field(default_factory=time.time)
    sender_id: str = ""
    round_index: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Round:
        return cls(
            user_text=data.get("user_text", ""),
            bot_text=data.get("bot_text", ""),
            timestamp=data.get("timestamp", time.time()),
            sender_id=data.get("sender_id", ""),
            round_index=data.get("round_index", 0),
        )


@dataclass
class Directive:
    id: str
    constraint_id: str
    playbook: str
    text: str
    state: DirectiveState = DirectiveState.ACTIVE
    intensity: str = "必须"  # "必须" | "尽量"
    mode: str = "multi"  # "once" (一次性) | "multi" (多轮)
    created_round: int = 0
    ttl_rounds: int = 10
    remaining_ttl: int = 10
    improvement_streak: int = 0
    relapse_count: int = 0
    reword_count: int = 0
    evidence: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["state"] = self.state.value if isinstance(self.state, DirectiveState) else str(self.state)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> Directive:
        raw_state = data.get("state", DirectiveState.ACTIVE.value)
        try:
            state = DirectiveState(raw_state)
        except ValueError:
            state = DirectiveState.ACTIVE
        return cls(
            id=data.get("id", str(uuid.uuid4())[:8]),
            constraint_id=data.get("constraint_id", "general"),
            playbook=data.get("playbook", "free"),
            text=data.get("text", ""),
            state=state,
            intensity=data.get("intensity", "必须"),
            mode=data.get("mode", "multi"),
            created_round=data.get("created_round", 0),
            ttl_rounds=data.get("ttl_rounds", 10),
            remaining_ttl=data.get("remaining_ttl", 10),
            improvement_streak=data.get("improvement_streak", 0),
            relapse_count=data.get("relapse_count", 0),
            reword_count=data.get("reword_count", 0),
            evidence=data.get("evidence", ""),
        )


@dataclass
class AssessmentResult:
    directive_id: str
    verdict: str  # "improved" | "partial" | "not_improved" | "worse"
    evidence: str = ""
    confidence: str = "high"  # "high" | "low"

    @classmethod
    def from_dict(cls, data: dict) -> AssessmentResult:
        return cls(
            directive_id=data.get("directive_id", ""),
            verdict=data.get("verdict", "not_improved"),
            evidence=data.get("evidence", ""),
            confidence=data.get("confidence", "high"),
        )


@dataclass
class ViolationItem:
    constraint_id: str
    severity: str  # "low" | "medium" | "high"
    evidence: str = ""
    suggestion: str = ""
    mode: str = "auto"  # "once" | "multi" | "auto"

    @classmethod
    def from_dict(cls, data: dict) -> ViolationItem:
        return cls(
            constraint_id=data.get("constraint_id", ""),
            severity=data.get("severity", "medium"),
            evidence=data.get("evidence", ""),
            suggestion=data.get("suggestion", ""),
            mode=data.get("mode", "auto"),
        )


@dataclass
class SupervisorEvalOutput:
    assessments: List[AssessmentResult] = field(default_factory=list)
    violations: List[ViolationItem] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> SupervisorEvalOutput:
        assessments = [
            AssessmentResult.from_dict(item)
            for item in data.get("assessments", [])
            if isinstance(item, dict)
        ]
        violations = [
            ViolationItem.from_dict(item)
            for item in data.get("violations", [])
            if isinstance(item, dict)
        ]
        return cls(assessments=assessments, violations=violations)


@dataclass
class SentinelCheckResult:
    triggered: bool
    reason: str = ""
    target_constraint_id: Optional[str] = None
    is_hard_violation: bool = False
    evidence: str = ""
    banned_word: str = ""


@dataclass
class SessionState:
    umo: str
    conversation_id: str = ""
    session_name: str = ""
    round_counter: int = 0
    last_eval_round: int = 0
    directives: Dict[str, Directive] = field(default_factory=dict)
    charter: List[str] = field(default_factory=list)
    eval_history: List[dict] = field(default_factory=list)
    paused: bool = False

    def to_dict(self) -> dict:
        active_fading = {}
        expired = []
        for k, v in self.directives.items():
            st = v.state.value if isinstance(v.state, DirectiveState) else str(v.state)
            if st in (DirectiveState.ACTIVE.value, DirectiveState.FADING.value):
                active_fading[k] = v
            else:
                expired.append((k, v))

        expired.sort(key=lambda item: item[1].created_round)
        recent_expired = dict(expired[-10:]) if expired else {}

        pruned_directives = {**active_fading, **recent_expired}
        self.directives = pruned_directives

        return {
            "umo": self.umo,
            "conversation_id": self.conversation_id,
            "session_name": self.session_name,
            "round_counter": self.round_counter,
            "last_eval_round": self.last_eval_round,
            "directives": {k: v.to_dict() for k, v in pruned_directives.items()},
            "charter": self.charter,
            "eval_history": self.eval_history[-20:],
            "paused": self.paused,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SessionState:
        umo = data.get("umo", "")
        conversation_id = data.get("conversation_id", "")
        session_name = data.get("session_name", "")
        directives_data = data.get("directives", {})
        directives = {
            k: Directive.from_dict(v)
            for k, v in directives_data.items()
            if isinstance(v, dict)
        }
        return cls(
            umo=umo,
            conversation_id=conversation_id,
            session_name=session_name,
            round_counter=data.get("round_counter", 0),
            last_eval_round=data.get("last_eval_round", 0),
            directives=directives,
            charter=data.get("charter", []),
            eval_history=data.get("eval_history", []),
            paused=data.get("paused", False),
        )

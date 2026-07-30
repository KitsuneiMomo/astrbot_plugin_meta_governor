from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Dict, List, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

from .core.collector import MessageCollector
from .core.config import ConstraintConfig, MetaGovernorConfig
from .core.models import Directive, DirectiveState, SessionState
from .core.sentinel import LocalSentinel
from .core.state_machine import DirectiveStateMachine
from .core.storage import StorageManager
from .core.supervisor import SupervisorEvaluator
from .core.templates import build_playbook_directive, format_directives_block

# Dynamic import for TextPart
try:
    from astrbot.core.agent.message import TextPart
except ImportError:
    try:
        from astrbot.api.message_components import TextPart
    except ImportError:
        TextPart = None


async def _parse_req_body(request: Any = None) -> dict:
    """通用 Web API 请求体解析辅助方法"""
    req_obj = request
    if req_obj is None or not hasattr(req_obj, "json"):
        try:
            from quart import request as q_request
            req_obj = q_request
        except Exception:
            pass

    if req_obj is not None:
        if hasattr(req_obj, "json"):
            try:
                val = req_obj.json
                if asyncio.iscoroutine(val):
                    res = await val
                    if isinstance(res, dict):
                        return res
                elif callable(val):
                    res = val()
                    if asyncio.iscoroutine(res):
                        res = await res
                    if isinstance(res, dict):
                        return res
                elif isinstance(val, dict):
                    return val
            except Exception:
                pass

        if hasattr(req_obj, "get_json"):
            try:
                fn = req_obj.get_json
                res = fn()
                if asyncio.iscoroutine(res):
                    res = await res
                if isinstance(res, dict):
                    return res
            except Exception:
                pass

    if isinstance(request, dict):
        return request

    return {}


@register(
    "astrbot_plugin_meta_governor",
    "KitsuneiMomo",
    "元认知调控插件：AI 对话质量监督与回复调控，自动修正复读与语气问题",
    "1.0.0",
)
class MetaGovernorPlugin(Star):
    """元认知调控插件 (Meta-Governor)"""

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.context = context
        self.raw_config = config or {}

        self.storage = StorageManager("astrbot_plugin_meta_governor")
        saved_config = self.storage.load_plugin_config()

        merged_config = dict(self.raw_config)
        merged_config.update(saved_config)

        self.plugin_config = MetaGovernorConfig.from_dict(merged_config)
        self.collector = MessageCollector()
        self.sentinel = LocalSentinel(self.plugin_config)
        self.state_machine = DirectiveStateMachine(self.plugin_config)
        self.supervisor = SupervisorEvaluator(context, self.plugin_config)

        self.sessions: Dict[str, SessionState] = self.storage.load_all_sessions()
        self.umo_locks: Dict[str, asyncio.Lock] = {}

        self._register_web_apis()

        logger.info(
            f"[Meta-Governor] 插件初始化完成 | 启用={self.plugin_config.enable} "
            f"| k轮检查={self.plugin_config.k_rounds} | n轮参考上下文={self.plugin_config.n_context_rounds} "
            f"| 自动预警={self.plugin_config.enable_sentinel_heuristics} | 规则轮数={self.plugin_config.ttl_rounds}"
        )

    def _register_web_apis(self):
        """注册 Web 管理界面 API 接口"""
        if not hasattr(self.context, "register_web_api"):
            return

        try:
            self.context.register_web_api(
                "/astrbot_plugin_meta_governor/get_data",
                self.web_get_data,
                ["GET"],
                "获取插件运行数据与设置",
            )
            self.context.register_web_api(
                "/astrbot_plugin_meta_governor/get_providers",
                self.web_get_providers,
                ["GET"],
                "获取当前可用的 Provider 列表",
            )
            self.context.register_web_api(
                "/astrbot_plugin_meta_governor/save_config",
                self.web_save_config,
                ["POST"],
                "保存插件配置参数",
            )
            self.context.register_web_api(
                "/astrbot_plugin_meta_governor/save_constraint",
                self.web_save_constraint,
                ["POST"],
                "新增或编辑检查规则",
            )
            self.context.register_web_api(
                "/astrbot_plugin_meta_governor/delete_constraint",
                self.web_delete_constraint,
                ["POST"],
                "删除指定检查规则",
            )
            self.context.register_web_api(
                "/astrbot_plugin_meta_governor/save_banned_phrase",
                self.web_save_banned_phrase,
                ["POST"],
                "添加禁用词短语",
            )
            self.context.register_web_api(
                "/astrbot_plugin_meta_governor/delete_banned_phrase",
                self.web_delete_banned_phrase,
                ["POST"],
                "删除禁用词短语",
            )
            self.context.register_web_api(
                "/astrbot_plugin_meta_governor/trigger_eval",
                self.web_trigger_eval,
                ["POST"],
                "手动发起后台质量检查",
            )
            self.context.register_web_api(
                "/astrbot_plugin_meta_governor/toggle_pause",
                self.web_toggle_pause,
                ["POST"],
                "暂停或恢复指定会话调优",
            )
            self.context.register_web_api(
                "/astrbot_plugin_meta_governor/revoke_directive",
                self.web_revoke_directive,
                ["POST"],
                "手动解除指定的调整规则",
            )
            self.context.register_web_api(
                "/astrbot_plugin_meta_governor/update_session_name",
                self.web_update_session_name,
                ["POST"],
                "修改会话群名或备注",
            )
        except Exception as e:
            logger.warning(f"[Meta-Governor] Web API 注册异常: {e}")

    def _get_session(self, umo: str) -> SessionState:
        if umo not in self.sessions:
            self.sessions[umo] = self.storage.load_session(umo)
        return self.sessions[umo]

    def _get_lock(self, umo: str) -> asyncio.Lock:
        if umo not in self.umo_locks:
            self.umo_locks[umo] = asyncio.Lock()
        return self.umo_locks[umo]

    def _update_session_name(self, event: AstrMessageEvent, session: SessionState):
        """尝试从 event 提取真实群名/个人名称（尊重已有备注）"""
        try:
            umo = str(getattr(event, "unified_msg_origin", ""))
            is_group = "Group" in umo or bool(
                hasattr(event, "message_obj")
                and getattr(getattr(event, "message_obj", None), "group_id", None)
            )

            name = ""
            if is_group:
                if hasattr(event, "get_group_name") and callable(event.get_group_name):
                    try:
                        name = event.get_group_name() or ""
                    except Exception:
                        pass
                if not name and hasattr(event, "message_obj") and event.message_obj:
                    msg_obj = event.message_obj
                    name = getattr(msg_obj, "group_name", "") or ""
            else:
                if hasattr(event, "message_obj") and event.message_obj:
                    msg_obj = event.message_obj
                    if hasattr(msg_obj, "sender") and msg_obj.sender:
                        name = getattr(msg_obj.sender, "nickname", "") or getattr(msg_obj.sender, "name", "") or ""

            if name and name != session.session_name:
                session.session_name = name
                self.storage.save_session(session)
        except Exception as e:
            logger.warning(f"[Meta-Governor] 提取 session_name 异常: {e}")

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: Any = None):
        """1. 记录用户输入 2. 检测会话重置/切换 3. 将当前规则注入给对话模型"""
        if not self.plugin_config.enable:
            return

        umo = str(getattr(event, "unified_msg_origin", ""))
        if not umo:
            return

        if getattr(event, "_meta_governor_internal", False):
            return

        session = self._get_session(umo)
        self._update_session_name(event, session)

        curr_cid = (
            getattr(event, "conversation_id", None)
            or getattr(getattr(event, "message_obj", None), "conversation_id", "")
            or ""
        )

        prompt_str = getattr(req, "prompt", "") or ""
        if not prompt_str and hasattr(event, "message_str"):
            prompt_str = event.message_str or ""
        elif not prompt_str and hasattr(event, "get_plain_text"):
            prompt_str = event.get_plain_text() or ""

        is_reset_cmd = prompt_str.strip().lower() in ("/reset", "/new")
        is_cid_changed = bool(
            session.conversation_id and curr_cid and curr_cid != session.conversation_id
        )

        if curr_cid and not session.conversation_id:
            session.conversation_id = curr_cid

        if is_reset_cmd or is_cid_changed:
            self.collector.clear_session(umo)
            session.round_counter = 0
            if curr_cid:
                session.conversation_id = curr_cid

            if self.plugin_config.clear_directives_on_reset or is_reset_cmd:
                session.directives.clear()
                session.charter.clear()
                logger.info(f"[Meta-Governor] 会话 [{umo}] 已重置，清空历史与调整规则")
            else:
                logger.info(f"[Meta-Governor] 会话 [{umo}] 已切换 (cid={curr_cid})，清空轮次缓冲，保留有效规则")

            self.storage.save_session(session)
            if is_reset_cmd:
                return

        if prompt_str:
            self.collector.record_user_request(event, prompt_str)

        if getattr(event, "_meta_governor_injected", False):
            return

        if session.paused:
            return

        directives_block = format_directives_block(
            list(session.directives.values()), session.charter
        )

        if not directives_block:
            return

        injected = False
        if req is not None:
            if TextPart is not None:
                try:
                    part = TextPart(text=directives_block)
                    if hasattr(part, "mark_as_temp"):
                        part.mark_as_temp()
                        if not hasattr(req, "extra_user_content_parts") or req.extra_user_content_parts is None:
                            req.extra_user_content_parts = []
                        req.extra_user_content_parts.append(part)
                        injected = True
                except Exception as e:
                    logger.warning(f"[Meta-Governor] extra_user_content_parts 注入异常: {e}")

            if not injected and hasattr(req, "system_prompt"):
                curr_sys = req.system_prompt or ""
                if directives_block not in curr_sys:
                    req.system_prompt = f"{curr_sys}\n\n{directives_block}".strip()
                    injected = True

        if injected:
            event._meta_governor_injected = True

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """处理生成的最终回复，记录对话轮数并触发自动检查"""
        if not self.plugin_config.enable:
            return

        umo = str(getattr(event, "unified_msg_origin", ""))
        if not umo:
            return

        if getattr(event, "_meta_governor_internal", False):
            return

        session = self._get_session(umo)
        self._update_session_name(event, session)

        if session.paused:
            return

        completed_round = self.collector.record_bot_response(
            event,
            total_round_index=session.round_counter + 1,
            max_rounds_keep=2 * (self.plugin_config.k_rounds + self.plugin_config.n_context_rounds) + 10,
        )

        if not completed_round:
            return

        session.round_counter += 1

        self.state_machine.on_new_round(session)
        self.storage.save_session(session)

        recent_rounds = list(self.collector.buffers.get(umo, []))

        sentinel_res = self.sentinel.check(
            recent_rounds=recent_rounds,
            round_counter=session.round_counter,
            last_eval_round=session.last_eval_round,
        )

        if sentinel_res.triggered:
            if sentinel_res.is_hard_violation:
                logger.info(f"[Meta-Governor] 会话 [{umo}] 本地检测命中禁用词: {sentinel_res.reason}")
                self._handle_hard_violation_short_circuit(event, session, sentinel_res)
            else:
                logger.info(f"[Meta-Governor] 会话 [{umo}] 触发自动检测预警: {sentinel_res.reason}")
                asyncio.create_task(self._run_async_eval(event, umo, sentinel_res.reason))

    def _handle_hard_violation_short_circuit(
        self, event: AstrMessageEvent, session: SessionState, sentinel_res: Any
    ):
        """硬性禁用词处理"""
        banned_word = getattr(sentinel_res, "banned_word", "") or sentinel_res.evidence
        dir_text = build_playbook_directive("banned_phrase", banned_word=banned_word)

        existing = [
            d for d in session.directives.values()
            if d.constraint_id == "banned_phrase" and d.state in (DirectiveState.ACTIVE, DirectiveState.FADING)
        ]

        if existing:
            active_d = existing[0]
            active_d.text = dir_text
            active_d.remaining_ttl = self.plugin_config.ttl_rounds
            msg_event = f"更新禁用词屏蔽规则: {dir_text}"
        else:
            new_id = str(uuid.uuid4())[:8]
            new_dir = Directive(
                id=new_id,
                constraint_id="banned_phrase",
                playbook="banned_phrase",
                text=dir_text,
                state=DirectiveState.ACTIVE,
                intensity="必须",
                mode="multi",
                created_round=session.round_counter,
                ttl_rounds=self.plugin_config.ttl_rounds,
                remaining_ttl=self.plugin_config.ttl_rounds,
                evidence=sentinel_res.evidence,
            )
            session.directives[new_dir.id] = new_dir
            msg_event = f"添加禁用词规则 [{new_id}]: {dir_text}"

        session.last_eval_round = session.round_counter
        self.storage.save_session(session)

        if self.plugin_config.verbose and event is not None:
            broadcast_msg = f"[元认知调控通知]\n⛔ {msg_event}"
            try:
                event._meta_governor_internal = True
                if hasattr(event, "send") and callable(event.send):
                    asyncio.create_task(event.send(event.plain_result(broadcast_msg)))
            except Exception as e:
                logger.warning(f"[Meta-Governor] 通知发送失败: {e}")

    async def _run_async_eval(self, event: Optional[AstrMessageEvent], umo: str, reason: str):
        """异步执行质量检查与规则更新"""
        lock = self._get_lock(umo)
        if lock.locked():
            logger.info(f"[Meta-Governor] 会话 [{umo}] 当前已有质量检查在进行中，跳过本次触发")
            return

        async with lock:
            session = self._get_session(umo)
            if session.paused:
                return

            prior_n, target_k = self.collector.get_rounds_snapshot(
                umo, k=self.plugin_config.k_rounds, n=self.plugin_config.n_context_rounds
            )

            if not target_k:
                logger.info(f"[Meta-Governor] 会话 [{umo}] 没有可用于检查的对话记录")
                return

            eval_output = await self.supervisor.evaluate(
                session=session,
                prior_n=prior_n,
                target_k=target_k,
                trigger_reason=reason,
            )

            if not eval_output:
                return

            new_events, revoked_events, upgraded_charters = (
                self.state_machine.process_eval_output(session, eval_output)
            )

            summary = (
                f"新规则={len(new_events)}, 观察/解除={len(revoked_events)}, 常驻规则={len(upgraded_charters)}"
            )
            session.eval_history.append(
                {
                    "round": session.round_counter,
                    "timestamp": time.time(),
                    "reason": reason,
                    "verdict_summary": summary,
                    "new_events": new_events,
                    "revoked_events": revoked_events,
                    "upgraded_charters": upgraded_charters,
                }
            )

            if len(session.eval_history) > 50:
                session.eval_history = session.eval_history[-50:]

            session.last_eval_round = session.round_counter
            self.storage.save_session(session)

            if self.plugin_config.verbose and event is not None and (new_events or revoked_events or upgraded_charters):
                broadcast_lines = ["[元认知调控通知]"]
                if new_events:
                    broadcast_lines.extend([f"➕ {e}" for e in new_events])
                if revoked_events:
                    broadcast_lines.extend([f"🔄 {e}" for e in revoked_events])
                if upgraded_charters:
                    broadcast_lines.extend([f"📜 {e}" for e in upgraded_charters])

                broadcast_msg = "\n".join(broadcast_lines)
                try:
                    event._meta_governor_internal = True
                    if hasattr(event, "send") and callable(event.send):
                        await event.send(event.plain_result(broadcast_msg))
                except Exception as e:
                    logger.warning(f"[Meta-Governor] 通知发送失败: {e}")

    # ==================== Web Dashboard APIs ====================

    async def web_get_data(self, request: Any = None) -> dict:
        """API: 获取插件数据"""
        providers = await self._fetch_available_providers()
        serialized_sessions = {umo: s.to_dict() for umo, s in self.sessions.items()}
        serialized_constraints = [c.__dict__ for c in self.plugin_config.constraints]

        return {
            "status": "success",
            "config": {
                "enable": self.plugin_config.enable,
                "k_rounds": self.plugin_config.k_rounds,
                "n_context_rounds": self.plugin_config.n_context_rounds,
                "ttl_rounds": self.plugin_config.ttl_rounds,
                "max_active_directives": self.plugin_config.max_active_directives,
                "supervisor_provider_id": self.plugin_config.supervisor_provider_id,
                "supervisor_prompt_template": self.plugin_config.supervisor_prompt_template,
                "enable_sentinel_heuristics": self.plugin_config.enable_sentinel_heuristics,
                "sentinel_sensitivity": self.plugin_config.sentinel_sensitivity,
                "verbose": self.plugin_config.verbose,
                "clear_directives_on_reset": self.plugin_config.clear_directives_on_reset,
            },
            "sessions": serialized_sessions,
            "constraints": serialized_constraints,
            "banned_phrases": self.plugin_config.banned_phrases,
            "providers": providers,
        }

    async def web_get_providers(self, request: Any = None) -> dict:
        """API: 获取 Provider 列表"""
        providers = await self._fetch_available_providers()
        return {"status": "success", "providers": providers}

    async def _fetch_available_providers(self) -> List[dict]:
        """获取所有已配置的 Provider ID"""
        res = []
        seen_ids = set()

        providers = []
        if hasattr(self.context, "get_all_providers"):
            try:
                providers = self.context.get_all_providers() or []
            except Exception as e:
                logger.warning(f"[Meta-Governor] get_all_providers() 调用失败: {e}")

        inst_to_id = {}
        prov_mgr = getattr(self.context, "provider_manager", None)
        if prov_mgr:
            if hasattr(prov_mgr, "inst_map") and isinstance(prov_mgr.inst_map, dict):
                for k, v in prov_mgr.inst_map.items():
                    inst_to_id[v] = k
            elif hasattr(prov_mgr, "provider_insts") and isinstance(prov_mgr.provider_insts, list):
                for pobj in prov_mgr.provider_insts:
                    if hasattr(pobj, "provider_config") and isinstance(pobj.provider_config, dict):
                        pid = pobj.provider_config.get("id", "")
                        if pid:
                            inst_to_id[pobj] = pid

        for p in providers:
            p_id = inst_to_id.get(p)
            if not p_id:
                if hasattr(p, "provider_config") and isinstance(p.provider_config, dict):
                    p_id = p.provider_config.get("id", "")
                if not p_id and hasattr(p, "meta"):
                    try:
                        p_id = p.meta().id
                    except Exception:
                        pass
                if not p_id:
                    p_id = getattr(p, "id", None) or getattr(p, "name", None) or str(p)

            if p_id and isinstance(p_id, str) and not (p_id.startswith("<") and "object at" in p_id):
                if p_id not in seen_ids:
                    seen_ids.add(p_id)
                    p_name = str(getattr(p, "name", p_id) or p_id)
                    res.append({"id": p_id, "name": p_name})

        if not res and prov_mgr:
            if hasattr(prov_mgr, "inst_map") and isinstance(prov_mgr.inst_map, dict):
                for k in prov_mgr.inst_map.keys():
                    str_k = str(k)
                    if str_k not in seen_ids:
                        seen_ids.add(str_k)
                        res.append({"id": str_k, "name": str_k})

        return res

    async def web_save_config(self, request: Any = None) -> dict:
        """API: 保存设置"""
        try:
            body = await _parse_req_body(request)
            if "enable" in body:
                self.plugin_config.enable = bool(body["enable"])
            if "k_rounds" in body:
                self.plugin_config.k_rounds = int(body["k_rounds"])
            if "n_context_rounds" in body:
                self.plugin_config.n_context_rounds = int(body["n_context_rounds"])
            if "ttl_rounds" in body:
                self.plugin_config.ttl_rounds = int(body["ttl_rounds"])
            if "max_active_directives" in body:
                self.plugin_config.max_active_directives = int(body["max_active_directives"])
            if "supervisor_provider_id" in body:
                self.plugin_config.supervisor_provider_id = str(body["supervisor_provider_id"] or "").strip()
            if "supervisor_prompt_template" in body:
                self.plugin_config.supervisor_prompt_template = str(body["supervisor_prompt_template"] or "").strip()
            if "enable_sentinel_heuristics" in body:
                self.plugin_config.enable_sentinel_heuristics = bool(body["enable_sentinel_heuristics"])
            if "sentinel_sensitivity" in body:
                self.plugin_config.sentinel_sensitivity = str(body["sentinel_sensitivity"])
            if "verbose" in body:
                self.plugin_config.verbose = bool(body["verbose"])
            if "clear_directives_on_reset" in body:
                self.plugin_config.clear_directives_on_reset = bool(body["clear_directives_on_reset"])

            self.sentinel = LocalSentinel(self.plugin_config)
            self.state_machine = DirectiveStateMachine(self.plugin_config)
            self.supervisor = SupervisorEvaluator(self.context, self.plugin_config)

            self.storage.save_plugin_config(self.plugin_config.to_dict())

            return {"status": "success", "message": "配置已成功保存"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def web_save_constraint(self, request: Any = None) -> dict:
        """API: 保存检查规则"""
        try:
            body = await _parse_req_body(request)
            cid = str(body.get("id", "")).strip()
            name = str(body.get("name", "")).strip()
            playbook = str(body.get("playbook", "free")).strip()
            description = str(body.get("description", "")).strip()
            mode = str(body.get("mode", "auto")).strip()

            if not cid or not name:
                return {"status": "error", "message": "规则 ID 和名称不能为空"}

            existing = [c for c in self.plugin_config.constraints if c.id == cid]
            if existing:
                existing[0].name = name
                existing[0].playbook = playbook
                existing[0].description = description
                existing[0].mode = mode
            else:
                self.plugin_config.constraints.append(
                    ConstraintConfig(id=cid, name=name, playbook=playbook, description=description, mode=mode)
                )

            self.storage.save_plugin_config(self.plugin_config.to_dict())

            return {"status": "success", "message": "检查规则保存成功"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def web_delete_constraint(self, request: Any = None) -> dict:
        """API: 删除检查规则"""
        try:
            body = await _parse_req_body(request)
            cid = str(body.get("id", "")).strip()
            self.plugin_config.constraints = [c for c in self.plugin_config.constraints if c.id != cid]

            self.storage.save_plugin_config(self.plugin_config.to_dict())

            return {"status": "success", "message": "规则已删除"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def web_save_banned_phrase(self, request: Any = None) -> dict:
        """API: 添加禁用词"""
        try:
            body = await _parse_req_body(request)
            phrase = str(body.get("phrase", "")).strip()
            if phrase and phrase not in self.plugin_config.banned_phrases:
                self.plugin_config.banned_phrases.append(phrase)
                self.storage.save_plugin_config(self.plugin_config.to_dict())
            return {"status": "success", "message": "禁用词已添加"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def web_delete_banned_phrase(self, request: Any = None) -> dict:
        """API: 删除禁用词"""
        try:
            body = await _parse_req_body(request)
            phrase = str(body.get("phrase", "")).strip()
            self.plugin_config.banned_phrases = [p for p in self.plugin_config.banned_phrases if p != phrase]

            self.storage.save_plugin_config(self.plugin_config.to_dict())

            return {"status": "success", "message": "禁用词已删除"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def web_trigger_eval(self, request: Any = None) -> dict:
        """API: 手动触发检查"""
        try:
            body = await _parse_req_body(request)
            target_umo = str(body.get("umo", "")).strip()

            target_umos = [target_umo] if target_umo else list(self.sessions.keys())
            if not target_umos:
                return {"status": "error", "message": "当前暂无活跃的聊天窗口记录"}

            for umo in target_umos:
                asyncio.create_task(self._run_async_eval(None, umo, "Web 界面手动发起检查"))

            return {"status": "success", "message": f"已成功为 {len(target_umos)} 个聊天窗口发起后台检查"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def web_toggle_pause(self, request: Any = None) -> dict:
        """API: 暂停或恢复指定会话调优"""
        try:
            body = await _parse_req_body(request)
            umo = str(body.get("umo", "")).strip()
            pause = bool(body.get("pause", True))

            if umo in self.sessions:
                self.sessions[umo].paused = pause
                self.storage.save_session(self.sessions[umo])
            return {"status": "success", "message": f"会话 [{umo}] 已{'暂停' if pause else '恢复'}调优"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def web_revoke_directive(self, request: Any = None) -> dict:
        """API: 手动解除调整规则"""
        try:
            body = await _parse_req_body(request)
            umo = str(body.get("umo", "")).strip()
            d_id = str(body.get("directive_id", "")).strip()

            if umo in self.sessions and d_id in self.sessions[umo].directives:
                self.sessions[umo].directives[d_id].state = DirectiveState.EXPIRED
                self.storage.save_session(self.sessions[umo])
                return {"status": "success", "message": f"规则 [{d_id}] 已手动解除"}
            return {"status": "error", "message": "未找到指定的规则"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def web_update_session_name(self, request: Any = None) -> dict:
        """API: 手动修改会话群名或备注"""
        try:
            body = await _parse_req_body(request)
            umo = str(body.get("umo", "")).strip()
            session_name = str(body.get("session_name", "")).strip()

            if not umo:
                return {"status": "error", "message": "UMO 不能为空"}

            session = self._get_session(umo)
            session.session_name = session_name
            self.storage.save_session(session)

            return {"status": "success", "message": "会话备注修改成功"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ==================== 管理指令 ====================

    @filter.command_group("meta")
    def meta_group(self):
        """元认知调控指令组"""
        pass

    @meta_group.command("status")
    async def meta_status(self, event: AstrMessageEvent):
        """查看当前聊天的元认知调控运行状态与规则"""
        event._meta_governor_internal = True
        umo = str(getattr(event, "unified_msg_origin", ""))
        session = self._get_session(umo)
        self._update_session_name(event, session)

        active_dirs = [
            d for d in session.directives.values() if d.state in (DirectiveState.ACTIVE, DirectiveState.FADING)
        ]

        session_display_name = session.session_name or umo

        lines = [
            "=== 🛠️ 元认知调控运行状态 ===",
            f"当前聊天窗口: {session_display_name} ({umo})",
            f"插件状态: {'已暂停' if session.paused else '运行中'}",
            f"已记录对话轮数: {session.round_counter}",
            f"上次检查轮次: {session.last_eval_round}",
            f"当前设置: 每 {self.plugin_config.k_rounds} 轮检查一次, 参考前置 {self.plugin_config.n_context_rounds} 轮上下文",
            f"自动预警检测: {'已开启' if self.plugin_config.enable_sentinel_heuristics else '已关闭 (仅满 k 轮触发)'}",
            "",
            f"📌 当前生效中的调整规则 ({len(active_dirs)}/{self.plugin_config.max_active_directives}):",
        ]

        if active_dirs:
            for d in active_dirs:
                state_str = "生效中" if d.state == DirectiveState.ACTIVE else "观察期 (良好)"
                mode_str = "一次性" if d.mode == "once" else "多轮"
                lines.append(
                    f"- [{d.id}] ({d.constraint_id}) [{state_str} - {mode_str}]\n  内容: {d.text}\n  剩余有效轮数: {d.remaining_ttl}/{d.ttl_rounds} 轮 | 强度: {d.intensity}"
                )
        else:
            lines.append("  (暂无生效中的调整规则，AI 对话质量良好)")

        lines.append("")
        lines.append(f"📜 常驻规则 ({len(session.charter)}):")
        if session.charter:
            for rule in session.charter:
                lines.append(f"  - {rule}")
        else:
            lines.append("  (暂无常驻规则)")

        if session.eval_history:
            last_hist = session.eval_history[-1]
            lines.append("")
            lines.append(
                f"🔍 最近检查记录: 轮次={last_hist.get('round')}, Reason={last_hist.get('reason')}, 结果={last_hist.get('verdict_summary')}"
            )

        yield event.plain_result("\n".join(lines))

    @meta_group.command("check")
    async def meta_check(self, event: AstrMessageEvent):
        """立即为当前聊天触发一次质量检查"""
        event._meta_governor_internal = True
        umo = str(getattr(event, "unified_msg_origin", ""))
        asyncio.create_task(
            self._run_async_eval(event, umo, "用户手动调用 /meta check 检查")
        )
        yield event.plain_result("🚀 已发起后台对话质量检查，稍后可输入 /meta status 查看结果。")

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional

from astrbot.api import logger
from astrbot.core.star.star_tools import StarTools

from .models import SessionState


class StorageManager:
    """持久化存储管理器：使用 JSON 保存与落盘 SessionState 及配置参数"""

    def __init__(self, plugin_name: str = "astrbot_plugin_meta_governor"):
        try:
            self.data_dir: Path = StarTools.get_data_dir(plugin_name)
        except Exception:
            self.data_dir = Path("data/plugin_data") / plugin_name
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_file = self.data_dir / "sessions.json"
        self.config_file = self.data_dir / "config.json"

    def load_plugin_config(self) -> dict:
        """从 JSON 加载持久化的插件配置参数"""
        if not self.config_file.exists():
            return {}
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            logger.error(f"[Meta-Governor] 加载 config.json 存储数据失败: {e}")
        return {}

    def save_plugin_config(self, config_dict: dict) -> None:
        """保存插件配置参数到 config.json 文件（原子写入）"""
        tmp_file = self.config_file.with_suffix(".tmp")
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(config_dict, f, ensure_ascii=False, indent=2)
            tmp_file.replace(self.config_file)
            logger.info("[Meta-Governor] 插件配置已原子落盘持久化至 config.json")
        except Exception as e:
            logger.error(f"[Meta-Governor] 保存 config.json 失败: {e}")
            if tmp_file.exists():
                try:
                    tmp_file.unlink()
                except Exception:
                    pass

    def load_session(self, umo: str) -> SessionState:
        """从 JSON 加载指定 UMO 的 session 状态，不存在时初始化新 State"""
        if not self.sessions_file.exists():
            return SessionState(umo=umo)

        try:
            with open(self.sessions_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and umo in data:
                    return SessionState.from_dict(data[umo])
        except Exception as e:
            logger.error(f"[Meta-Governor] 加载 UMO={umo} 存储数据失败: {e}")

        return SessionState(umo=umo)

    def save_session(self, session: SessionState) -> None:
        """保存 SessionState 到 JSON 文件（原子写入防止损坏）"""
        all_data: Dict[str, dict] = {}
        if self.sessions_file.exists():
            try:
                with open(self.sessions_file, "r", encoding="utf-8") as f:
                    content = json.load(f)
                    if isinstance(content, dict):
                        all_data = content
            except Exception as e:
                logger.warning(f"[Meta-Governor] 读取已有 sessions.json 异常: {e}")

        all_data[session.umo] = session.to_dict()

        tmp_file = self.sessions_file.with_suffix(".tmp")
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(all_data, f, ensure_ascii=False, indent=2)
            tmp_file.replace(self.sessions_file)
        except Exception as e:
            logger.error(f"[Meta-Governor] 保存 session数据 失败: {e}")
            if tmp_file.exists():
                try:
                    tmp_file.unlink()
                except Exception:
                    pass

    def load_all_sessions(self) -> Dict[str, SessionState]:
        """加载所有记录的 session 状态"""
        if not self.sessions_file.exists():
            return {}

        res = {}
        try:
            with open(self.sessions_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    for umo, s_dict in data.items():
                        if isinstance(s_dict, dict):
                            res[umo] = SessionState.from_dict(s_dict)
        except Exception as e:
            logger.error(f"[Meta-Governor] 加载全部 sessions 失败: {e}")
        return res

import os
import sys
import types

# Add plugins dir to sys.path
current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
parent_dir = os.path.dirname(current_dir)
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

site_pkgs = os.path.abspath(os.path.join(parent_dir, "..", "site-packages"))
if os.path.exists(site_pkgs) and site_pkgs not in sys.path:
    sys.path.insert(0, site_pkgs)

# Create dummy astrbot modules if not installed in current python env
if "astrbot" not in sys.modules:
    astrbot = types.ModuleType("astrbot")
    astrbot.api = types.ModuleType("astrbot.api")
    astrbot.api.star = types.ModuleType("astrbot.api.star")
    astrbot.api.event = types.ModuleType("astrbot.api.event")
    astrbot.api.message_components = types.ModuleType("astrbot.api.message_components")
    astrbot.core = types.ModuleType("astrbot.core")
    astrbot.core.star = types.ModuleType("astrbot.core.star")
    astrbot.core.star.star_tools = types.ModuleType("astrbot.core.star.star_tools")
    astrbot.core.agent = types.ModuleType("astrbot.core.agent")
    astrbot.core.agent.message = types.ModuleType("astrbot.core.agent.message")

    class FakeLogger:
        def info(self, *args, **kwargs): pass
        def warning(self, *args, **kwargs): pass
        def error(self, *args, **kwargs): pass
        def debug(self, *args, **kwargs): pass

    astrbot.api.logger = FakeLogger()

    class Context:
        pass

    class Star:
        def __init__(self, context):
            self.context = context

    def register(name, author, desc, version, repo=None):
        def decorator(cls):
            cls.__plugin_metadata__ = {
                "name": name, "author": author, "desc": desc, "version": version
            }
            return cls
        return decorator

    astrbot.api.star.Context = Context
    astrbot.api.star.Star = Star
    astrbot.api.star.register = register

    class FilterDecorator:
        def on_llm_request(self, *args, **kwargs):
            def d(fn): return fn
            return d
        def on_decorating_result(self, *args, **kwargs):
            def d(fn): return fn
            return d
        def command_group(self, *args, **kwargs):
            def d(fn):
                fn.command = lambda *c_args, **c_kwargs: (lambda cmd_fn: cmd_fn)
                return fn
            return d
        def command(self, *args, **kwargs):
            def d(fn): return fn
            return d

    class AstrMessageEvent:
        def __init__(self, umo: str = "test_umo"):
            self.unified_msg_origin = umo
            self._extra = {}

        def get_extra(self, key, default=None):
            return self._extra.get(key, default)

        def set_extra(self, key, val):
            self._extra[key] = val

    astrbot.api.event.filter = FilterDecorator()
    astrbot.api.event.AstrMessageEvent = AstrMessageEvent

    class Plain:
        def __init__(self, text: str):
            self.text = text

    astrbot.api.message_components.Plain = Plain

    class StarTools:
        @staticmethod
        def get_data_dir(plugin_name: str):
            import tempfile
            from pathlib import Path
            p = Path(tempfile.gettempdir()) / "astrbot_test_data" / plugin_name
            p.mkdir(parents=True, exist_ok=True)
            return p

    astrbot.core.star.star_tools.StarTools = StarTools

    class TextPart:
        def __init__(self, text: str):
            self.text = text
            self._temp = False
        def mark_as_temp(self):
            self._temp = True
            return self

    astrbot.core.agent.message.TextPart = TextPart

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = astrbot.api
    sys.modules["astrbot.api.star"] = astrbot.api.star
    sys.modules["astrbot.api.event"] = astrbot.api.event
    sys.modules["astrbot.api.message_components"] = astrbot.api.message_components
    sys.modules["astrbot.core"] = astrbot.core
    sys.modules["astrbot.core.star"] = astrbot.core.star
    sys.modules["astrbot.core.star.star_tools"] = astrbot.core.star.star_tools
    sys.modules["astrbot.core.agent"] = astrbot.core.agent
    sys.modules["astrbot.core.agent.message"] = astrbot.core.agent.message

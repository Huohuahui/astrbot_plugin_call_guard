"""call_guard - 单人刷屏静默防护

检测某个群友频繁 call 机器人（艾特机器人 / 回复机器人消息），窗口内超过阈值
就把这个人拉进静默期：静默期内每次 call 只有 reply_probability（0-1）的概率
会被放行回复，其余直接丢弃，不再消耗 LLM token。

设计要点：
- 优先级 99998，仅次于 admin_commander(100000)：老板指令台先处理，本插件不拦。
- "/" 开头的指令消息不计数也不拦截（不影响管理员指令、天气、群分析等）。
- 管理员默认豁免；豁免名单里的用户永不触发。
- 静默状态持久化到 data/plugin_data/call_guard/state.json，重启不丢。
- 只对群聊生效；私聊不受影响。
"""

import json
import os
import random
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import At, Reply

STATE_FILE = os.path.join("data", "plugin_data", "call_guard", "state.json")


@register(
    "call_guard",
    "wb-helper",
    "单人刷屏静默防护：频繁call机器人的群友自动进静默期，按概率放行回复",
    "v1.0.1",
)
class CallGuardPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.state: dict = self._load_state()

    # ---------- 状态持久化 ----------

    def _load_state(self) -> dict:
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            logger.warning(f"[call_guard] 状态保存失败: {e}")

    # ---------- 识别“call 机器人” ----------

    def _is_call(self, event: AstrMessageEvent) -> bool:
        """艾特机器人 或 回复机器人消息 都算 call。"""
        self_id = str(event.get_self_id() or "")
        if not self_id:
            return False
        for seg in (event.message_obj.message or []):
            if isinstance(seg, At) and str(getattr(seg, "qq", "")) == self_id:
                return True
            if isinstance(seg, Reply) and str(getattr(seg, "sender_id", "")) == self_id:
                return True
        return False

    # ---------- 主逻辑 ----------

    @filter.event_message_type(filter.EventMessageType.ALL, priority=99998)
    async def on_message(self, event: AstrMessageEvent):
        try:
            if not self.config.get("enable", True):
                return
            text = (event.message_str or "").strip()
            # 指令放行：不计数也不拦截
            if text.startswith("/"):
                return
            gid = str(event.get_group_id() or "")
            if not gid:
                return  # 只管群聊

            groups = [str(g) for g in (self.config.get("scope_groups") or []) if str(g).strip()]
            if groups and gid not in groups and not any(gid.endswith(g) for g in groups):
                return

            sender = str(event.get_sender_id() or "")
            if not sender:
                return
            if self.config.get("exempt_admins", True) and event.is_admin():
                return
            exempt = {str(u) for u in (self.config.get("exempt_users") or []) if str(u).strip()}
            if sender in exempt:
                return

            if not self._is_call(event):
                return

            now = time.time()
            key = f"{gid}:{sender}"
            st = self.state.get(key) or {"calls": [], "silence_until": 0}
            self.state[key] = st

            window = max(10, int(self.config.get("window_seconds", 120)))
            st["calls"] = [t for t in (st.get("calls") or []) if now - float(t) <= window]

            sil_until = float(st.get("silence_until") or 0)
            if now < sil_until:
                # 静默期内：按概率放行，否则直接丢弃
                p = min(1.0, max(0.0, float(self.config.get("reply_probability", 0.2))))
                remain = int(sil_until - now)
                if random.random() < p:
                    logger.info(f"[call_guard] {key} 静默中(剩{remain}s)，概率放行本次回复")
                else:
                    logger.info(f"[call_guard] {key} 静默中(剩{remain}s)，丢弃本次 call")
                    event.stop_event()
                return

            st["calls"].append(now)
            limit = max(1, int(self.config.get("call_limit", 5)))
            if len(st["calls"]) >= limit:
                silence = max(30, int(self.config.get("silence_seconds", 600)))
                st["silence_until"] = now + silence
                st["calls"] = []
                self._save_state()
                logger.info(
                    f"[call_guard] {key} 在{window}s内call了{limit}次，进入静默{silence}s"
                )
                event.stop_event()
                return
            self._save_state()
        except Exception as e:
            logger.error(f"[call_guard] 处理异常: {e}")

    async def terminate(self):
        self._save_state()

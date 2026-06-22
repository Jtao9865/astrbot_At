import re
import json
import time
from typing import List, Optional, Tuple
from astrbot.api.star import Star, Context
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger, AstrBotConfig
from astrbot.api.provider import ProviderRequest
from astrbot.core.message.components import Plain, At, BaseMessageComponent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)


class LLMAtToolPlugin(Star):
    AT_ALL_PERMISSION_CACHE_KEY = "_at_all_permission_result"

    def __init__(self, context: Context, config: Optional[AstrBotConfig] = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.valid_at_pattern = re.compile(r"\[at:(\d+|all)\]")
        self.garbage_at_pattern = re.compile(r"\[at:[^\]]+\]")
        self.permission_verification = self.config.get("permission_verification", True)
        self.llm_prompt = self._normalize_editor_text(self.config.get("llm_prompt", ""))

    @staticmethod
    def _normalize_editor_text(text: object) -> str:
        if not isinstance(text, str):
            return ""
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        if "\n" not in normalized and (
            "\\r\\n" in normalized or "\\n" in normalized or "\\r" in normalized
        ):
            normalized = (
                normalized.replace("\\r\\n", "\n")
                .replace("\\r", "\n")
                .replace("\\n", "\n")
            )
        return normalized

    def _is_bot_super_admin(self, event: AstrMessageEvent) -> bool:
        is_admin_attr = getattr(event, "is_admin", None)
        try:
            if callable(is_admin_attr):
                return bool(is_admin_attr())
            return bool(is_admin_attr)
        except Exception as exc:
            logger.warning(f"检查 Bot 超级管理员权限失败: {exc}")
            return False

    @staticmethod
    def _build_sender_identity_reason(
        sender_id: object | None, reason_suffix: str
    ) -> str:
        if not sender_id:
            return "无法识别他的身份,拒绝执行"
        return f"{sender_id}{reason_suffix}"

    def _safe_get_group_id(self, event: AstrMessageEvent):
        """安全获取 group_id，兼容 ContextWrapper 和 AiocqhttpMessageEvent"""
        group_id = None
        try:
            gid = getattr(event, "get_group_id", None)
            if callable(gid):
                group_id = gid()
            else:
                group_id = gid
        except Exception:
            pass
        if not group_id:
            group_id = getattr(event, "group_id", None)
        return group_id

    async def _check_at_all_permission(
        self, event: AstrMessageEvent
    ) -> Tuple[bool, str]:
        group_id = self._safe_get_group_id(event)
        if not group_id:
            return False, "非群聊场景"

        if self._is_bot_super_admin(event):
            return True, ""

        if not isinstance(event, AiocqhttpMessageEvent):
            return False, "当前平台不支持 全体权限校验"

        sender_getter = getattr(event, "get_sender_id", None)
        sender_id = sender_getter() if callable(sender_getter) else None
        if not sender_id:
            return False, self._build_sender_identity_reason(
                sender_id, "的身份,拒绝执行"
            )

        try:
            group_member_info = await event.bot.api.call_action(
                "get_group_member_info",
                group_id=group_id,
                user_id=sender_id,
            )
        except Exception as exc:
            logger.warning(
                f"查询@全体权限失败: group_id={group_id}, user_id={sender_id}, error={exc}"
            )
            return False, "查询群成员权限失败"

        role = str(group_member_info.get("role", "member")).lower()
        if role in {"owner", "admin"}:
            return True, ""
        return False, self._build_sender_identity_reason(
            sender_id, " 不是群主、管理员或 Bot 超级管理员"
        )

    async def _get_at_all_permission_result(
        self, event: AstrMessageEvent
    ) -> Tuple[bool, str]:
        cached_result = event.get_extra(self.AT_ALL_PERMISSION_CACHE_KEY)
        if (
            isinstance(cached_result, tuple)
            and len(cached_result) == 2
            and isinstance(cached_result[1], str)
        ):
            return bool(cached_result[0]), cached_result[1]

        permission_result = await self._check_at_all_permission(event)
        event.set_extra(self.AT_ALL_PERMISSION_CACHE_KEY, permission_result)
        return permission_result

    @filter.on_llm_request()
    async def inject_at_instruction(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        if not self.llm_prompt:
            return

        if self.permission_verification:
            at_all_allowed, deny_message = await self._get_at_all_permission_result(
                event
            )
            if not at_all_allowed:
                safe_gid = self._safe_get_group_id(event)
                sender_getter = getattr(event, "get_sender_id", lambda: None)
                sender_id = sender_getter() if callable(sender_getter) else None
                logger.info(
                    "拦截越权@全体并降级为普通文本: "
                    f"group_id={safe_gid}, "
                    f"sender_id={sender_id}, "
                    f"reason={deny_message}"
                )
                self.llm_prompt += (
                    "\n\n注意：当前用户无权使用 @全体 功能，请不要在回复中使用 [at:all] 标签。"
                )
            else:
                self.llm_prompt += (
                    "\n\n你可以使用 [at:all] 标签来 @全体 群成员。"
                )

        req.system_prompt += self.llm_prompt

    @filter.command("atall")
    async def atall_handler(
        self, event: AstrMessageEvent
    ):
        at_all_allowed, deny_message = await self._get_at_all_permission_result(
            event
        )
        if at_all_allowed:
            await event.send_message([Plain("你可以使用 @全体 功能。")])
        else:
            await event.send_message(
                [Plain(f"你无权使用 @全体 功能。原因: {deny_message}")]
            )

    @filter.message_command("atall_check")
    async def atall_msg_check(
        self, event: AstrMessageEvent
    ):
        at_all_allowed, deny_message = await self._get_at_all_permission_result(
            event
        )
        if at_all_allowed:
            await event.send_message([Plain("你可以使用 @全体 功能。")])
        else:
            await event.send_message(
                [Plain(f"你无权使用 @全体 功能。原因: {deny_message}")]
            )

    @filter.permission_type(filter.PermissinType.GROUP)
    @filter.command_filter(lambda event: hasattr(event, "message") and "[at:" in event.message)
    async def atall_filter(self, event: AstrMessageEvent):
        result = event.get_result()
        if not result:
            return

        has_tag = False
        has_at_all_tag = False

        for comp in result.chain:
            if isinstance(comp, Plain) and "[at:" in comp.text:
                has_tag = True
                if "[at:all]" in comp.text:
                    has_at_all_tag = True

        if not has_tag:
            return

        at_all_allowed = True
        if self.permission_verification and has_at_all_tag:
            at_all_allowed, deny_message = await self._get_at_all_permission_result(
                event
            )
            if not at_all_allowed:
                safe_gid = self._safe_get_group_id(event)
                sender_getter = getattr(event, "get_sender_id", lambda: None)
                sender_id = sender_getter() if callable(sender_getter) else None
                logger.info(
                    "拦截越权@全体并降级为普通文本: "
                    f"group_id={safe_gid}, "
                    f"sender_id={sender_id}, "
                    f"reason={deny_message}"
                )

        new_chain: List[BaseMessageComponent] = []

        for comp in result.chain:
            if isinstance(comp, Plain):
                text = comp.text
                last_idx = 0

                for match in self.valid_at_pattern.finditer(text):
                    start, end = match.span()

                    if start > last_idx:
                        new_chain.append(Plain(text[last_idx:start]))

                    target_id = match.group(1)
                    if target_id == "all" and not at_all_allowed:
                        new_chain.append(Plain("@全体成员"))
                        last_idx = end
                        continue

                    new_chain.append(At(qq=target_id))
                    new_chain.append(Plain(" "))

                    last_idx = end

                if last_idx < len(text):
                    new_chain.append(Plain(text[last_idx:]))
            else:
                new_chain.append(comp)

        idx = 0
        while idx < len(new_chain):
            if isinstance(new_chain[idx], At):
                for prev_idx in range(idx - 1, -1, -1):
                    if isinstance(new_chain[prev_idx], Plain):
                        new_chain[prev_idx].text = new_chain[prev_idx].text.rstrip(
                            " \t"
                        )
                        break
                    elif not isinstance(new_chain[prev_idx], At):
                        break

                for next_idx in range(idx + 1, len(new_chain)):
                    if isinstance(new_chain[next_idx], Plain):
                        new_chain[next_idx].text = new_chain[next_idx].text.lstrip(
                            " \t"
                        )
                        break
                    elif not isinstance(new_chain[next_idx], At):
                        break
            idx += 1

        idx = 0
        while idx < len(new_chain):
            if isinstance(new_chain[idx], At):
                found_plain = False
                for next_idx in range(idx + 1, len(new_chain)):
                    if isinstance(new_chain[next_idx], Plain):
                        new_chain[next_idx].text = (
                            "\u200b \u200b" + new_chain[next_idx].text
                        )
                        found_plain = True
                        break

                if not found_plain:
                    new_chain.insert(idx + 1, Plain("\u200b \u200b"))
            idx += 1

        result.chain = new_chain

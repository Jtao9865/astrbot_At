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

    def _get_group_openid(self, event: AstrMessageEvent) -> Optional[str]:
        """获取群聊的 group_openid，优先从事件上下文中提取。"""
        group_openid = getattr(event, "group_openid", None)
        if group_openid:
            return str(group_openid)
        try:
            gid = getattr(event, "get_group_id", None)
            if callable(gid):
                result = gid()
                if result:
                    return str(result)
        except Exception:
            pass
        return None

    def _get_sender_openid(self, event: AstrMessageEvent) -> Optional[str]:
        """获取发送者的 member_openid。"""
        sender_openid = getattr(event, "sender_openid", None) or getattr(
            event, "member_openid", None
        )
        if sender_openid:
            return str(sender_openid)
        try:
            getter = getattr(event, "get_sender_id", None)
            if callable(getter):
                result = getter()
                if result:
                    return str(result)
        except Exception:
            pass
        return None

    async def _check_at_all_permission(
        self, event: AstrMessageEvent
    ) -> Tuple[bool, str]:
        group_openid = self._get_group_openid(event)
        if not group_openid:
            return False, "非群聊场景"

        if self._is_bot_super_admin(event):
            return True, ""

        if not isinstance(event, AiocqhttpMessageEvent):
            return False, "当前平台不支持@全体权限校验"

        sender_openid = self._get_sender_openid(event)
        if not sender_openid:
            return False, self._build_sender_identity_reason(
                sender_openid, "的身份,拒绝执行"
            )

        try:
            start_index = 0
            limit = 500
            while True:
                payload = {
                    "group_openid": group_openid,
                    "start_index": start_index,
                    "limit": limit,
                }
                body_json = json.dumps(payload, ensure_ascii=False)
                json.loads(body_json)

                resp = await event.bot.api._http.request(
                    "POST",
                    f"/v2/groups/{group_openid}/members",
                    json=json.loads(body_json),
                )

                members = resp.get("members", []) if isinstance(resp, dict) else []
                for member in members:
                    mid = member.get("member_openid", "")
                    if mid == sender_openid:
                        role = str(member.get("role", "member")).lower()
                        if role in {"owner", "admin"}:
                            return True, ""
                        return False, self._build_sender_identity_reason(
                            sender_openid, " 不是群主、管理员或 Bot 超级管理员"
                        )

                next_index = resp.get("next_index", 0)
                if not next_index or start_index >= next_index:
                    return (
                        False,
                        self._build_sender_identity_reason(
                            sender_openid, " 不在群成员列表中"
                        ),
                    )
                start_index = next_index

        except Exception as exc:
            logger.warning(
                f"查询@全体权限失败: group_openid={group_openid}, sender_openid={sender_openid}, error={exc}"
            )
            return False, "查询群成员权限失败"

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
        """
        在 LLM（大语言模型）发出请求前注入系统提示词。
        告知模型如何使用特定的 XML 标签格式来进行艾特操作。
        """
        instruction = self.llm_prompt
        req.system_prompt = (req.system_prompt or "") + instruction

        if not self.permission_verification:
            return

        allowed, deny_message = await self._get_at_all_permission_result(event)
        if allowed:
            req.system_prompt += (
                "\n当前操作者具备@全体权限。"
                "\n如用户明确要求且场景确有必要，你可以输出 [at:all]。"
            )
            return

        req.system_prompt += (
            "\n当前操作者不具备@全体权限。"
            f"\n原因：{deny_message}"
            "\n禁止输出 [at:all]。"
            "\n如果用户要求@全体，请直接用自然语言说明无法执行，不要输出任何 @全体 标签。"
        )

    @filter.llm_tool(name="get_group_members")
    async def get_group_members(
        self, event: AstrMessageEvent, keyword: str = ""
    ) -> str:
        """
        供 LLM 调用的工具：获取当前群聊的成员列表。

        Args:
            keyword(string): 搜索关键词，支持匹配昵称、群名片或openid。若为空则返回全员。
        """
        start_time = time.time()

        group_openid = self._get_group_openid(event)
        if not group_openid:
            return json.dumps(
                {"status": "error", "message": "当前不在群聊环境中，无法查询成员。"},
                ensure_ascii=False,
            )

        try:
            all_members = []
            start_index = 0
            limit = 500

            while True:
                payload = {
                    "group_openid": group_openid,
                    "start_index": start_index,
                    "limit": limit,
                }
                body_json = json.dumps(payload, ensure_ascii=False)
                json.loads(body_json)

                resp = await event.bot.api._http.request(
                    "POST",
                    f"/v2/groups/{group_openid}/members",
                    json=json.loads(body_json),
                )

                if not isinstance(resp, dict):
                    return json.dumps(
                        {
                            "status": "error",
                            "message": "无法获取成员列表或机器人权限不足。",
                        },
                        ensure_ascii=False,
                    )

                members_data = resp.get("members", [])
                if not members_data:
                    break

                for m in members_data:
                    member_openid = str(m.get("member_openid", ""))
                    nickname = m.get("nickname", "")
                    card = m.get("card", "")
                    role_raw = m.get("role", "member")
                    role_cn = {"owner": "群主", "admin": "管理员", "member": "成员"}.get(
                        str(role_raw), "成员"
                    )

                    search_content = f"{member_openid}{nickname}{card}"
                    if keyword and keyword not in search_content:
                        continue

                    all_members.append(
                        {
                            "member_openid": member_openid,
                            "nickname": nickname,
                            "group_card": card if card else "无",
                            "role": role_cn,
                        }
                    )

                next_index = resp.get("next_index", 0)
                if not next_index or start_index >= next_index:
                    break
                start_index = next_index

            output_data = {
                "status": "success",
                "group_openid": group_openid,
                "count": len(all_members),
                "members": all_members,
            }

            logger.debug(
                f"群成员查询成功：耗时 {time.time() - start_time:.2f}s，共找到 {len(all_members)} 人"
            )
            return json.dumps(output_data, ensure_ascii=False, indent=2)

        except Exception as e:
            logger.error(f"查询群成员过程发生异常: {e}")
            return json.dumps(
                {"status": "error", "message": f"系统内部异常: {str(e)}"},
                ensure_ascii=False,
            )

    @filter.on_decorating_result(priority=2)
    async def process_at_tags(self, event: AstrMessageEvent):
        """
        拦截器：在消息发送给用户前，对LLM输出的内容进行二次处理。
        功能：
        1. 识别[at:数字]并转换为平台原生的At组件。
        2. 自动清理 At 标签周边的空格。
        3. 注入零宽字符以防止文本渲染时出现格式错乱。
        """
        result = event.get_result()
        if not result or not result.chain:
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
                safe_gid = self._get_group_openid(event)
                sender_getter = getattr(event, "get_sender_id", lambda: None)
                sender_id = sender_getter() if callable(sender_getter) else None
                logger.info(
                    "拦截越权@全体并降级为普通文本: "
                    f"group_openid={safe_gid}, "
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


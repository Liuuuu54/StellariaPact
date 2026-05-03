"""Intake 模块 Discord UI 操作层。

负责创建/更新 Discord 帖子、消息、通知等与 Discord API 交互的操作。
与业务逻辑（IntakeLogic）和数据格式化（IntakeEmbedBuilder）分离。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from StellariaPact.share import DiscordUtils

from .views.IntakeEmbedBuilder import IntakeEmbedBuilder
from .views.IntakeSupportView import IntakeSupportView

if TYPE_CHECKING:
    from StellariaPact.dto.ProposalIntakeDto import ProposalIntakeDto
    from StellariaPact.share.StellariaPactBot import StellariaPactBot

logger = logging.getLogger(__name__)


class IntakeUI:
    """封装 Intake 模块中所有 Discord UI 操作。"""

    @staticmethod
    def resolve_forum_tag(
        forum: discord.ForumChannel,
        raw_tag_id: int | str | None,
        tag_key: str,
    ) -> discord.ForumTag | None:
        """根据配置中的标签 ID 解析论坛标签。"""
        if raw_tag_id is None:
            return None

        try:
            tag_id = int(raw_tag_id)
        except (TypeError, ValueError):
            logger.warning(f"config.tags.{tag_key} 配置值无效: {raw_tag_id}")
            return None

        tag = next((item for item in forum.available_tags if item.id == tag_id), None)
        if tag is None:
            logger.warning(
                f"在论坛 {forum.id} 的可用标签中未找到 ID 为 {tag_id} 的 {tag_key} 标签。"
            )
            return None

        return tag

    @staticmethod
    async def create_review_thread(
        bot: "StellariaPactBot",
        forum: discord.ForumChannel,
        intake_dto: "ProposalIntakeDto",
        view: discord.ui.View,
    ) -> discord.Thread:
        """在审核论坛创建审核帖，返回帖子对象。"""
        content = IntakeEmbedBuilder.build_review_thread_content(intake_dto)
        prefix = IntakeEmbedBuilder.get_status_prefix(intake_dto.status)
        thread_name = f"{prefix} {intake_dto.title}" if prefix else intake_dto.title

        tag_key = IntakeEmbedBuilder.get_status_tag_key(intake_dto.status)
        raw_tag_id = bot.config.get("intake_tags", {}).get(tag_key) if tag_key else None
        tag = IntakeUI.resolve_forum_tag(forum, raw_tag_id, tag_key or "")
        applied_tags = [tag] if tag else []

        thread_with_message = await forum.create_thread(
            name=thread_name,
            content=content,
            view=view,
            applied_tags=applied_tags,
        )
        return thread_with_message.thread

    @staticmethod
    async def update_review_thread(
        bot: "StellariaPactBot",
        intake_dto: "ProposalIntakeDto",
        *,
        view: discord.ui.View | None,
        extra_note: str | None = None,
        notify_proposer: bool = False,
    ) -> None:
        """更新审核帖首楼内容和标题/标签，并在需要时通知提案人。"""
        if not intake_dto.review_thread_id:
            logger.warning(f"草案 {intake_dto.id} 缺少 review_thread_id，无法更新消息。")
            return

        thread = await DiscordUtils.fetch_thread(bot, intake_dto.review_thread_id)
        if not isinstance(thread, discord.Thread):
            logger.warning(f"草案 {intake_dto.id} 的 review_thread_id 无效。")
            return

        try:
            # 更新首楼内容
            msg = await thread.fetch_message(thread.id)
            submitted_ts = int(msg.created_at.timestamp())
            from datetime import datetime, timezone

            content = IntakeEmbedBuilder.build_updated_review_content(
                intake_dto,
                submitted_at=datetime.fromtimestamp(submitted_ts, tz=timezone.utc),
                extra_note=extra_note,
            )
            await msg.edit(content=content, embed=None, view=view)

            # 通知提案人
            if notify_proposer and intake_dto.reviewer_id and intake_dto.reviewed_at:
                notification = IntakeEmbedBuilder.build_proposer_notification(intake_dto)
                await thread.send(notification)

        except discord.NotFound:
            logger.error(f"无法在帖子 {thread.id} 中找到起始消息。")
        except discord.Forbidden:
            logger.error(f"没有权限编辑帖子 {thread.id} 中的消息。")

        # 更新标题和标签
        forum = thread.parent
        if isinstance(forum, discord.ForumChannel):
            await IntakeUI._sync_thread_tags_and_title(
                thread, forum, bot.config, intake_dto
            )

    @staticmethod
    async def _sync_thread_tags_and_title(
        thread: discord.Thread,
        forum: discord.ForumChannel,
        config: dict,
        intake_dto: "ProposalIntakeDto",
    ) -> None:
        """同步审核帖的标题前缀和标签。"""
        tag_key = IntakeEmbedBuilder.get_status_tag_key(intake_dto.status)
        if not tag_key:
            return

        new_tags = DiscordUtils.calculate_new_tags(
            current_tags=thread.applied_tags,
            forum_tags=forum.available_tags,
            config={
                "tags": config.get("intake_tags", {}),
                "status_tag_keys": config.get("intake_status_tag_keys", []),
            },
            target_tag_name=tag_key,
        )

        prefix = IntakeEmbedBuilder.get_status_prefix(intake_dto.status)
        new_title = f"{prefix} {intake_dto.title}" if prefix else intake_dto.title
        if len(new_title) > 100:
            new_title = new_title[:97] + "..."

        edit_payload: dict = {}
        if new_title != thread.name:
            edit_payload["name"] = new_title
        if new_tags is not None:
            edit_payload["applied_tags"] = new_tags

        if not edit_payload:
            return

        try:
            await thread.edit(**edit_payload)
        except discord.Forbidden:
            logger.error(f"没有权限编辑帖子 {thread.id} 的标签或标题。")
        except Exception as e:
            logger.error(f"更新帖子 {thread.id} 的标签或标题时出错: {e}")

    @staticmethod
    async def create_discussion_thread(
        bot: "StellariaPactBot",
        forum: discord.ForumChannel,
        intake_dto: "ProposalIntakeDto",
    ) -> discord.Thread:
        """在讨论区创建讨论帖，返回帖子对象。"""
        content = IntakeEmbedBuilder.build_discussion_content(intake_dto)

        tags_config = bot.config.get("tags", {})
        discussion_tag = IntakeUI.resolve_forum_tag(
            forum, tags_config.get("discussion"), "discussion"
        )
        applied_tags = [discussion_tag] if discussion_tag else []

        thread_with_message = await forum.create_thread(
            name=f"[讨论中] {intake_dto.title}",
            content=content,
            applied_tags=applied_tags,
        )
        return thread_with_message.thread

    @staticmethod
    async def create_support_message(
        bot: "StellariaPactBot",
        channel: discord.TextChannel,
        intake_dto: "ProposalIntakeDto",
    ) -> discord.Message:
        """在公示频道发送支持票收集面板，返回消息对象。"""
        embed = IntakeEmbedBuilder.build_support_embed(intake_dto, current_votes=0)
        return await channel.send(embed=embed, view=IntakeSupportView(bot))

    @staticmethod
    async def update_support_message(
        bot: "StellariaPactBot",
        channel_id: int | None,
        intake_dto: "ProposalIntakeDto",
        current_votes: int,
    ) -> None:
        """更新公示频道中的支持票收集面板。"""
        if not intake_dto.voting_message_id or not channel_id:
            return

        channel = await DiscordUtils.fetch_channel(bot, channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        try:
            msg = await channel.fetch_message(intake_dto.voting_message_id)
            embed = IntakeEmbedBuilder.build_support_embed(intake_dto, current_votes=current_votes)
            await msg.edit(embed=embed, view=IntakeSupportView(bot))
        except discord.NotFound:
            logger.warning(f"找不到支持票消息 {intake_dto.voting_message_id}，跳过更新。")
        except discord.Forbidden:
            logger.error(f"没有权限编辑支持票消息 {intake_dto.voting_message_id}。")
        except Exception as e:
            logger.warning(f"更新支持票面板失败 {intake_dto.voting_message_id}: {e}")

    @staticmethod
    async def edit_result_message(
        bot: "StellariaPactBot",
        channel_id: int | None,
        message_id: int | None,
        embed: discord.Embed,
        view: discord.ui.View | None = None,
    ) -> None:
        """编辑公示频道中的投票消息为结果状态。"""
        if not message_id or not channel_id:
            return

        channel = await DiscordUtils.fetch_channel(bot, channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        try:
            msg = await channel.fetch_message(message_id)
            await msg.edit(embed=embed, view=view)
        except Exception as e:
            logger.warning(f"更新投票消息 {message_id} 失败: {e}")

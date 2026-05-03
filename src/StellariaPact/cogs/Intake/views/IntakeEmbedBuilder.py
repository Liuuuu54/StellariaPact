from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import discord

from StellariaPact.dto.ProposalIntakeDto import ProposalIntakeDto
from StellariaPact.share.enums import IntakeStatus

logger = logging.getLogger(__name__)


class IntakeEmbedBuilder:
    """专门负责构建预审（Intake）相关的 Embed UI 和文本内容。"""

    # ---- 状态 → 展示文字的映射 ----

    _STATUS_PREFIX_MAP: dict[int, str] = {
        int(IntakeStatus.PENDING_REVIEW): "[待审核]",
        int(IntakeStatus.SUPPORT_COLLECTING): "[已通过]",
        int(IntakeStatus.APPROVED): "[已发布]",
        int(IntakeStatus.REJECTED): "[未通过]",
        int(IntakeStatus.MODIFICATION_REQUIRED): "[需要修改]",
    }

    _STATUS_TAG_KEY_MAP: dict[int, str] = {
        int(IntakeStatus.PENDING_REVIEW): "pending_review",
        int(IntakeStatus.SUPPORT_COLLECTING): "support_collecting",
        int(IntakeStatus.APPROVED): "approved",
        int(IntakeStatus.REJECTED): "rejected",
        int(IntakeStatus.MODIFICATION_REQUIRED): "modification_required",
    }

    _STATUS_EMOJI_MAP: dict[int, str] = {
        int(IntakeStatus.SUPPORT_COLLECTING): "✅",
        int(IntakeStatus.REJECTED): "❌",
        int(IntakeStatus.MODIFICATION_REQUIRED): "🟡",
        int(IntakeStatus.APPROVED): "🎉",
    }

    _STATUS_TEXT_MAP: dict[int, str] = {
        int(IntakeStatus.SUPPORT_COLLECTING): "审核通过",
        int(IntakeStatus.REJECTED): "审核拒绝",
        int(IntakeStatus.MODIFICATION_REQUIRED): "要求修改",
        int(IntakeStatus.APPROVED): "已发布",
    }

    # ---- 通用辅助 ----

    @staticmethod
    def _get_jump_url(guild_id: int, channel_id: int, message_id: Optional[int] = None) -> str:
        if message_id:
            return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
        return f"https://discord.com/channels/{guild_id}/{channel_id}"

    @staticmethod
    def _get_review_color(status: int) -> discord.Color:
        status_map = {
            IntakeStatus.PENDING_REVIEW: discord.Color.yellow(),
            IntakeStatus.SUPPORT_COLLECTING: discord.Color.green(),
            IntakeStatus.APPROVED: discord.Color.dark_green(),
            IntakeStatus.REJECTED: discord.Color.red(),
            IntakeStatus.MODIFICATION_REQUIRED: discord.Color.orange(),
        }
        return status_map.get(IntakeStatus(status), discord.Color.default())

    @staticmethod
    def _get_review_status_text(status: int) -> str:
        status_map = {
            IntakeStatus.PENDING_REVIEW: "🔵 待审核",
            IntakeStatus.SUPPORT_COLLECTING: "🟢 审核通过",
            IntakeStatus.APPROVED: "✅ 已立案",
            IntakeStatus.REJECTED: "🔴 已拒绝",
            IntakeStatus.MODIFICATION_REQUIRED: "🟡 需要修改",
        }
        return status_map.get(IntakeStatus(status), "未知状态")

    @staticmethod
    def get_status_prefix(status: int) -> str | None:
        """根据状态获取审核帖标题前缀。"""
        return IntakeEmbedBuilder._STATUS_PREFIX_MAP.get(int(status))

    @staticmethod
    def get_status_tag_key(status: int) -> str | None:
        """根据状态获取对应的标签键名（用于 config 查找）。"""
        return IntakeEmbedBuilder._STATUS_TAG_KEY_MAP.get(int(status))

    @staticmethod
    def get_status_result_text(status: int) -> str:
        """根据状态获取审核结果文本。"""
        return IntakeEmbedBuilder._STATUS_TEXT_MAP.get(int(status), "状态更新")

    @staticmethod
    def get_status_emoji(status: int) -> str:
        """根据状态获取对应 emoji。"""
        return IntakeEmbedBuilder._STATUS_EMOJI_MAP.get(int(status), "ℹ️")

    # ---- Embed 构建 ----

    @staticmethod
    def build_review_embed(intake: ProposalIntakeDto) -> discord.Embed:
        """构建审核贴的 Embed"""
        status_text = IntakeEmbedBuilder._get_review_status_text(intake.status)
        color = IntakeEmbedBuilder._get_review_color(intake.status)

        embed = discord.Embed(
            title=f"📝 提案预审核: {intake.title}",
            description=f"**提交人**: <@{intake.author_id}>",
            color=color,
        )

        embed.add_field(name="草案ID", value=f"`{intake.id}`", inline=True)
        embed.add_field(name="状态", value=status_text, inline=True)
        embed.add_field(name="原因", value=intake.reason, inline=False)
        embed.add_field(name="动议", value=intake.motion, inline=False)
        embed.add_field(name="方案", value=intake.implementation, inline=False)
        embed.add_field(name="执行人", value=intake.executor, inline=False)
        embed.set_footer(text="最后更新于")
        embed.timestamp = discord.utils.utcnow()
        return embed

    @staticmethod
    def build_support_embed(intake: ProposalIntakeDto, current_votes: int = 0) -> discord.Embed:
        """构建用于收集支持票的嵌入消息"""
        embed = discord.Embed(
            title=f"{intake.title}",
            description=(
                "该提案已通过管理组初步审核，现进入社区支持票收集阶段。\n"
                f"达到 **{intake.required_votes}** 票支持后，将自动转为正式提案进入讨论。"
            ),
            color=discord.Color.blue(),
        )
        embed.add_field(name="发起人", value=f"<@{intake.author_id}>", inline=True)
        embed.add_field(
            name="票数", value=f"**{current_votes}** / {intake.required_votes}", inline=True
        )
        embed.add_field(name="状态", value="🟢 支持票收集中", inline=True)
        embed.add_field(name="议案标题", value=intake.title, inline=False)
        embed.add_field(name="提案原因", value=intake.reason, inline=False)
        embed.add_field(name="议案动议", value=intake.motion, inline=False)
        embed.add_field(name="执行方案", value=intake.implementation, inline=False)
        embed.add_field(name="议案执行人", value=intake.executor, inline=False)
        embed.set_footer(text="点击下方按钮以支持, 再次点击可撤回支持。")
        return embed

    @staticmethod
    def build_support_result_embed(
        intake: ProposalIntakeDto,
        success: bool,
        thread_id: Optional[int] = None,
        current_votes: int = 0,
    ) -> discord.Embed:
        """构建支持票收集结束后的结果 Embed。"""
        if success:
            thread_jump_url = None
            if thread_id and intake.guild_id:
                thread_jump_url = IntakeEmbedBuilder._get_jump_url(intake.guild_id, thread_id)

            embed_kwargs = {
                "title": f"{intake.title}",
                "description": "该提案已收集到足够的支持票，进入正式讨论阶段。",
                "color": discord.Color.green(),
            }
            if thread_jump_url:
                embed_kwargs["url"] = thread_jump_url

            embed = discord.Embed(**embed_kwargs)
            embed.add_field(name="发起人", value=f"<@{intake.author_id}>", inline=True)
            embed.add_field(
                name="票数", value=f"**{current_votes}** / {intake.required_votes}", inline=True
            )
            embed.add_field(name="状态", value="✅ 已立案", inline=True)
        else:
            embed = discord.Embed(
                title=f"❌ [收集失败] {intake.title}",
                description="当前草案未能在截止日期前获得足够支持，未能进入讨论阶段。",
                color=discord.Color.light_gray(),
            )
            embed.add_field(name="发起人", value=f"<@{intake.author_id}>", inline=True)
            embed.add_field(
                name="票数", value=f"**{current_votes}** / {intake.required_votes}", inline=True
            )
            embed.add_field(name="状态", value="❌ 收集失败", inline=True)

        embed.add_field(name="议案标题", value=intake.title, inline=False)
        embed.add_field(name="提案原因", value=intake.reason, inline=False)
        embed.add_field(name="议案动议", value=intake.motion, inline=False)
        embed.add_field(name="执行方案", value=intake.implementation, inline=False)
        embed.add_field(name="议案执行人", value=intake.executor, inline=False)

        embed.timestamp = discord.utils.utcnow()
        return embed

    # ---- 消息内容构建（原在 IntakeLogic 中内联） ----

    @staticmethod
    def build_review_thread_content(
        intake: ProposalIntakeDto,
        submitted_at: datetime | None = None,
    ) -> str:
        """构建审核帖首楼内容（用于创建或更新审核帖消息）。"""
        if submitted_at is None:
            submitted_at = datetime.now(timezone.utc)
        submitted_ts = int(submitted_at.timestamp())

        status_text = IntakeEmbedBuilder._get_review_status_text(intake.status)
        if " " in status_text:
            emoji, status_desc = status_text.split(" ", 1)
        else:
            emoji = ""
            status_desc = status_text

        content = (
            f"👤 **提案人：** <@{intake.author_id}>\n"
            f"📅 **提交时间：** <t:{submitted_ts}:f>\n"
            f"🆔 **草案ID：** `{intake.id}`\n\n"
            "---\n\n"
            f"🏷️ **议案标题**\n{intake.title}\n\n"
            f"📝 **提案原因**\n{intake.reason}\n\n"
            f"📋 **议案动议**\n{intake.motion}\n\n"
            f"🔧 **执行方案**\n{intake.implementation}\n\n"
            f"👨‍💼 **议案执行人**\n{intake.executor}\n\n"
            "---\n\n"
            f"{emoji} **状态：** {status_desc}\n"
        )
        return content.strip()

    @staticmethod
    def build_updated_review_content(
        intake: ProposalIntakeDto,
        submitted_at: datetime | None = None,
        extra_note: str | None = None,
    ) -> str:
        """构建审核帖首楼内容（含审核信息，用于审核后更新）。"""
        if submitted_at is None:
            submitted_at = datetime.now(timezone.utc)
        submitted_ts = int(submitted_at.timestamp())

        status_text = IntakeEmbedBuilder.get_status_result_text(intake.status)
        status_emoji = IntakeEmbedBuilder.get_status_emoji(intake.status)

        lines = [
            f"👤 **提案人：** <@{intake.author_id}>",
            f"📅 **提交时间：** <t:{submitted_ts}:f>",
            f"🆔 **议案ID：** `{intake.id}`",
        ]

        if intake.reviewer_id and intake.reviewed_at:
            reviewed_ts = int(intake.reviewed_at.timestamp())
            lines.extend(
                [
                    f"👨‍💼 **审核员：** <@{intake.reviewer_id}>",
                    f"📅 **审核时间：** <t:{reviewed_ts}:f>",
                ]
            )

        lines.extend(
            [
                "\n---\n",
                f"\n🏷️ **议案标题**\n{intake.title}",
                f"\n📝 **提案原因**\n{intake.reason}",
                f"\n📋 **议案动议**\n{intake.motion}",
                f"\n🔧 **执行方案**\n{intake.implementation}"
                f"\n\n👨‍💼 **议案执行人**\n{intake.executor}",
                "\n---\n",
                f"{status_emoji} **状态：** {status_text}\n",
                f"💬 **审核意见：** {intake.review_comment or '（无）'}",
            ]
        )

        if extra_note:
            lines.extend(["", f"ℹ️ {extra_note}"])

        return "\n".join(lines)

    @staticmethod
    def build_proposer_notification(intake: ProposalIntakeDto) -> str:
        """构建发给提案人的审核通知消息。"""
        status_text = IntakeEmbedBuilder.get_status_result_text(intake.status)
        status_emoji = IntakeEmbedBuilder.get_status_emoji(intake.status)

        lines = [
            f"<@{intake.author_id}> 您的议案已被审核！",
            "## 📋 审核记录",
            f"👨‍💼 **审核员：** <@{intake.reviewer_id}>",
            f"📅 **审核时间：** <t:{int(intake.reviewed_at.timestamp())}:f>",
            f"{status_emoji} **审核结果：** {status_text}",
            "",
            "💬 **审核意见：**",
            intake.review_comment or "（无）",
            "---",
            "📝 如有疑问，申请人可以联系审核员了解详细情况。",
        ]
        return "\n".join(lines)

    @staticmethod
    def build_discussion_content(intake: ProposalIntakeDto) -> str:
        """构建讨论帖首楼内容（从草案数据生成）。"""
        created_ts = int(datetime.now(timezone.utc).timestamp())
        return (
            f"***提案人: <@{intake.author_id}>***\n\n"
            f"> ## 提案原因\n{intake.reason}\n\n"
            f"> ## 议案动议\n{intake.motion}\n\n"
            f"> ## 执行方案\n{intake.implementation}\n\n"
            f"> ## 议案执行人\n{intake.executor}\n\n"
            f"*讨论帖创建时间: <t:{created_ts}:f>*"
        )

    @staticmethod
    def build_proposal_content(intake: ProposalIntakeDto) -> str:
        """构建写入 Proposal 表的提案内容。"""
        return (
            f"> ### 提案原因\n{intake.reason}\n\n"
            f"> ### 议案动议\n{intake.motion}\n\n"
            f"> ### 执行方案\n{intake.implementation}\n\n"
            f"> ### 议案执行人\n{intake.executor}"
        )

    @staticmethod
    def build_modification_notice(intake: ProposalIntakeDto) -> discord.Embed:
        """构建"提案内容已更新"的通知 Embed。"""
        embed = discord.Embed(
            title="📝 提案内容已更新",
            description="提案人对草案内容进行了修改，请管理组重新审核。",
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="修改时间",
            value=f"<t:{int(datetime.now(timezone.utc).timestamp())}:f>",
            inline=False,
        )
        embed.add_field(name="修改人", value=f"<@{intake.author_id}>", inline=False)
        return embed

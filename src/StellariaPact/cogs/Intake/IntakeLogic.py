from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord
from sqlalchemy import func, select, update

from StellariaPact.cogs.Intake.dto.SupportToggleDbResultDto import SupportToggleDbResultDto
from StellariaPact.cogs.Voting.qo.CreateVoteSessionQo import CreateVoteSessionQo
from StellariaPact.dto import ProposalDto
from StellariaPact.dto.ProposalIntakeDto import ProposalIntakeDto
from StellariaPact.models.Proposal import Proposal
from StellariaPact.models.ProposalIntake import ProposalIntake
from StellariaPact.models.UserVote import UserVote
from StellariaPact.models.VoteSession import VoteSession
from StellariaPact.share import DiscordUtils
from StellariaPact.share.enums import IntakeStatus, ProposalStatus, VoteDuration, VoteSessionType
from StellariaPact.share.UnitOfWork import UnitOfWork

from .IntakeUI import IntakeUI
from .views.IntakeEmbedBuilder import IntakeEmbedBuilder
from .views.IntakeReviewView import IntakeReviewView

if TYPE_CHECKING:
    from StellariaPact.cogs.Intake.dto.IntakeSubmissionDto import IntakeSubmissionDto
    from StellariaPact.share.StellariaPactBot import StellariaPactBot
    from StellariaPact.share.UnitOfWork import UnitOfWork

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 0.8


class IntakeLogic:
    """处理提案预审（Intake）核心业务逻辑。

    职责：业务规则校验、数据库操作、流程编排。
    Discord UI 操作委托给 IntakeUI，内容格式化委托给 IntakeEmbedBuilder。
    """

    def __init__(self, bot: "StellariaPactBot"):
        self.bot = bot
        self._draft_cache: dict[int, tuple[float, "IntakeSubmissionDto"]] = {}

    # -------------------------
    # 草稿管理
    # -------------------------

    def save_draft(self, user_id: int, dto: "IntakeSubmissionDto"):
        self._draft_cache[user_id] = (time.time(), dto)

    def get_draft(self, user_id: int) -> "IntakeSubmissionDto | None":
        if user_id not in self._draft_cache:
            return None
        timestamp, dto = self._draft_cache[user_id]
        if time.time() - timestamp <= 1800:
            return dto
        del self._draft_cache[user_id]
        return None

    def clear_draft(self, user_id: int):
        self._draft_cache.pop(user_id, None)

    # -------------------------
    # 提交限制检查
    # -------------------------

    async def check_submission_limit(self, guild_id: int) -> tuple[bool, str]:
        """检查当前讨论中的提案是否达到上限。"""
        async with UnitOfWork(self.bot.db_handler) as uow:
            discussion_proposals = await uow.proposal.get_proposals_by_status(
                ProposalStatus.DISCUSSION
            )
            if len(discussion_proposals) >= 3:
                discussion_links = "\n".join(
                    f"- https://discord.com/channels/{guild_id}/{proposal.discussion_thread_id}"
                    for proposal in discussion_proposals
                )
                return False, (
                    "当前已有 3 个或更多提案正在讨论中，暂不允许提交新草案。"
                    "请等待现有议案结案。\n\n"
                    f"正在讨论中的提案：\n{discussion_links}"
                )
        return True, ""

    # -------------------------
    # 草案提交
    # -------------------------

    async def process_submit_intake(self, dto: "IntakeSubmissionDto") -> ProposalIntakeDto:
        allowed, message = await self.check_submission_limit(dto.guild_id)
        if not allowed:
            raise PermissionError(message)

        # 创建数据库记录
        created_intake = await self._create_intake_record(dto)
        intake_dto = ProposalIntakeDto.model_validate(created_intake)

        # 在审核论坛创建帖子
        forum = await self._require_forum_channel("intake_review")
        thread = await IntakeUI.create_review_thread(
            self.bot, forum, intake_dto, IntakeReviewView(self.bot, intake_dto)
        )

        # 回写审核帖子 ID
        return await self._backfill_review_thread_id(intake_dto.id, thread.id)

    async def _create_intake_record(
        self, dto: "IntakeSubmissionDto"
    ) -> ProposalIntake:
        """创建草案数据库记录（含重试）。"""
        for attempt in range(MAX_RETRIES):
            try:
                async with UnitOfWork(self.bot.db_handler) as uow:
                    new_intake = ProposalIntake(
                        guild_id=dto.guild_id,
                        author_id=dto.author_id,
                        title=dto.title,
                        reason=dto.reason,
                        motion=dto.motion,
                        implementation=dto.implementation,
                        executor=dto.executor,
                        status=IntakeStatus.PENDING_REVIEW,
                        required_votes=20,
                    )
                    created = await uow.intake.create_intake(new_intake)
                    await uow.commit()
                    return created
            except Exception as e:
                if "database is locked" in str(e).lower() and attempt < MAX_RETRIES - 1:
                    logger.warning(f"草案提交遇到数据库锁，正在重试 ({attempt + 1}/{MAX_RETRIES})")
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                raise
        raise RuntimeError("草案提交失败：未能创建草案记录。")

    async def _backfill_review_thread_id(
        self, intake_id: int, thread_id: int
    ) -> ProposalIntakeDto:
        """回写审核帖 ID 到草案记录（含重试）。"""
        for attempt in range(MAX_RETRIES):
            try:
                async with UnitOfWork(self.bot.db_handler) as uow:
                    intake = await uow.intake.get_intake_by_id(intake_id, for_update=True)
                    if not intake:
                        raise ValueError(f"草案不存在，ID={intake_id}")
                    intake.review_thread_id = thread_id
                    await uow.intake.update_intake(intake)
                    dto = ProposalIntakeDto.model_validate(intake)
                    await uow.commit()
                    return dto
            except Exception as e:
                if "database is locked" in str(e).lower() and attempt < MAX_RETRIES - 1:
                    logger.warning(
                        f"回写审核帖ID遇到数据库锁，正在重试 ({attempt + 1}/{MAX_RETRIES})"
                    )
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                raise
        raise RuntimeError("草案提交失败：未能回写审核帖子ID。")

    # -------------------------
    # 草案审核
    # -------------------------

    async def approve_intake(
        self, thread_id: int, reviewer_id: int, review_comment: str
    ) -> ProposalIntakeDto:
        async with UnitOfWork(self.bot.db_handler) as uow:
            intake = await uow.intake.mark_reviewed(
                thread_id, reviewer_id, review_comment,
                IntakeStatus.SUPPORT_COLLECTING,
                expected_current_status=[IntakeStatus.PENDING_REVIEW],
            )
            intake_dto = ProposalIntakeDto.model_validate(intake)

        # 在公示频道发送支持票面板
        channel = await self._require_text_channel("objection_publicity")
        vote_msg = await IntakeUI.create_support_message(self.bot, channel, intake_dto)

        # 回写投票消息 ID 并创建投票会话
        async with UnitOfWork(self.bot.db_handler) as uow:
            intake = await uow.intake.get_intake_by_id(intake_dto.id, for_update=True)
            if not intake:
                raise ValueError("在创建投票会话时找不到草案。")
            intake.voting_message_id = vote_msg.id
            await uow.intake.update_intake(intake)
            intake_dto = ProposalIntakeDto.model_validate(intake)

            if not intake.review_thread_id:
                raise ValueError("草案缺少审核帖子ID，无法创建投票会话。")

            now = datetime.now(timezone.utc)
            vote_qo = CreateVoteSessionQo(
                guild_id=vote_msg.guild.id if vote_msg.guild else 0,
                thread_id=intake.review_thread_id,
                context_message_id=vote_msg.id,
                intake_id=intake.id,
                session_type=VoteSessionType.INTAKE_SUPPORT,
                end_time=now + timedelta(days=3),
            )
            await uow.vote_session.create_vote_session(vote_qo)

        # 更新审核帖
        await IntakeUI.update_review_thread(
            self.bot, intake_dto, view=None, notify_proposer=True
        )
        return intake_dto

    async def reject_intake(
        self, thread_id: int, reviewer_id: int, review_comment: str
    ) -> ProposalIntakeDto:
        async with UnitOfWork(self.bot.db_handler) as uow:
            intake = await uow.intake.mark_reviewed(
                thread_id, reviewer_id, review_comment,
                IntakeStatus.REJECTED,
                expected_current_status=[
                    IntakeStatus.PENDING_REVIEW,
                    IntakeStatus.MODIFICATION_REQUIRED,
                ],
            )
            intake_dto = ProposalIntakeDto.model_validate(intake)

        await IntakeUI.update_review_thread(
            self.bot, intake_dto,
            view=IntakeReviewView(self.bot, intake_dto),
            notify_proposer=True,
        )
        return intake_dto

    async def edit_intake(
        self, intake_id: int, dto: "IntakeSubmissionDto"
    ) -> ProposalIntakeDto:
        async with UnitOfWork(self.bot.db_handler) as uow:
            intake = await uow.intake.get_intake_by_id(intake_id)
            if not intake:
                raise ValueError("未找到对应的草案。")

            intake.title = dto.title
            intake.reason = dto.reason
            intake.motion = dto.motion
            intake.implementation = dto.implementation
            intake.executor = dto.executor

            if intake.status == IntakeStatus.MODIFICATION_REQUIRED:
                intake.status = IntakeStatus.PENDING_REVIEW

            await uow.intake.update_intake(intake)
            intake_dto = ProposalIntakeDto.model_validate(intake)
            await uow.commit()

        # 更新审核帖
        await IntakeUI.update_review_thread(
            self.bot, intake_dto,
            view=IntakeReviewView(self.bot, intake_dto),
        )

        # 发送修改通知
        if intake_dto.review_thread_id:
            thread = await DiscordUtils.fetch_thread(self.bot, intake_dto.review_thread_id)
            if isinstance(thread, discord.Thread):
                embed = IntakeEmbedBuilder.build_modification_notice(intake_dto)
                await thread.send(embed=embed)

        return intake_dto

    async def request_modification_intake(
        self, thread_id: int, reviewer_id: int, review_comment: str
    ) -> ProposalIntakeDto:
        async with UnitOfWork(self.bot.db_handler) as uow:
            intake = await uow.intake.mark_reviewed(
                thread_id, reviewer_id, review_comment,
                IntakeStatus.MODIFICATION_REQUIRED,
            )
            intake_dto = ProposalIntakeDto.model_validate(intake)

        await IntakeUI.update_review_thread(
            self.bot, intake_dto,
            view=IntakeReviewView(self.bot, intake_dto),
            notify_proposer=True,
        )
        return intake_dto

    # -------------------------
    # 支持票收集
    # -------------------------

    async def process_support_toggle(self, interaction: discord.Interaction) -> tuple[str, int]:
        assert interaction.message is not None
        message_id = interaction.message.id
        user_id = interaction.user.id

        async with UnitOfWork(self.bot.db_handler) as uow:
            result = await self.handle_support_toggle(uow, user_id, message_id)

        if result.intake is not None:
            channels_config = self.bot.config.get("channels", {})
            await IntakeUI.update_support_message(
                self.bot, channels_config.get("objection_publicity"),
                result.intake, result.count,
            )

        if not result.need_promote or result.intake_id is None:
            action, count = result.action, result.count
        else:
            promoted, latest_count = await self.voting_threshold_reached(result.intake_id)
            action, count = (
                ("promoted", latest_count)
                if promoted
                else ("already_processed", latest_count)
            )

        msg = {
            "supported": f"✅ 收到支持！当前已收集到 **{count}** 张支持票",
            "withdrawn": f"⎌ 已撤回支持。当前剩余 **{count}** 张支持票",
            "promoted": f"🎉 收到支持！该草案已达到 **{count}** 票支持，已开启讨论贴",
            "already_processed": f"👌 阶段已修改，当前总票数为 **{count}** 票",
        }.get(action, "操作成功")

        await interaction.followup.send(msg, ephemeral=True)
        return action, count

    async def handle_support_toggle(
        self, uow: "UnitOfWork", user_id: int, message_id: int
    ) -> SupportToggleDbResultDto:
        intake = await uow.intake.get_intake_by_voting_message_id(message_id, for_update=True)
        if not intake:
            raise ValueError("草案不存在。")

        assert intake.id is not None
        intake_id = intake.id

        if intake.status != IntakeStatus.SUPPORT_COLLECTING:
            current_votes = await self._count_support_votes(uow, intake_id)
            return SupportToggleDbResultDto(
                action="already_processed", count=current_votes or 0,
                intake=None, need_promote=False, intake_id=intake_id,
            )

        vote_session = await self._get_support_session(uow, intake_id)
        if not vote_session:
            raise ValueError("找不到关联的投票会话。")

        # 切换用户投票状态
        action = await self._toggle_user_vote(uow, vote_session.id, user_id)
        await uow.flush()

        current_votes = await self._count_session_votes(uow, vote_session.id)
        intake_dto = ProposalIntakeDto.model_validate(intake)

        if current_votes < intake.required_votes:
            return SupportToggleDbResultDto(
                action=action, count=current_votes, intake=intake_dto,
                need_promote=False, intake_id=intake_id,
            )

        if intake.status != IntakeStatus.SUPPORT_COLLECTING:
            return SupportToggleDbResultDto(
                action="already_processed", count=current_votes,
                intake=intake_dto, need_promote=False, intake_id=intake_id,
            )

        return SupportToggleDbResultDto(
            action=action, count=current_votes, intake=intake_dto,
            need_promote=True, intake_id=intake_id,
        )

    async def voting_threshold_reached(self, intake_id: int) -> tuple[bool, int]:
        async with UnitOfWork(self.bot.db_handler) as uow:
            intake = await uow.intake.get_intake_by_id(intake_id, for_update=True)
            if not intake:
                return False, 0

            vote_session = await self._get_support_session(uow, intake_id)
            if not vote_session:
                return False, 0

            latest_count = await self._count_session_votes(uow, vote_session.id)

            if (
                intake.status != IntakeStatus.SUPPORT_COLLECTING
                or latest_count < intake.required_votes
            ):
                return False, latest_count

            intake.status = IntakeStatus.APPROVED
            await uow.intake.update_intake(intake)
            await uow.commit()

        await self.handle_support_reached(intake_id)
        return True, latest_count

    async def handle_support_reached(self, intake_id: int) -> ProposalDto | None:
        # 获取草案数据
        async with UnitOfWork(self.bot.db_handler) as uow:
            intake = await uow.intake.get_intake_by_id(intake_id)
            if not intake:
                raise ValueError("草案不存在。")
            if intake.status != IntakeStatus.APPROVED:
                raise ValueError("草案状态不正确，无法立案。")
            intake_dto = ProposalIntakeDto.model_validate(intake)
            required_votes = intake.required_votes

        # 创建讨论帖
        forum = await self._require_forum_channel("discussion")
        thread = await IntakeUI.create_discussion_thread(self.bot, forum, intake_dto)

        # 写入 Proposal 表并关联
        proposal_content = IntakeEmbedBuilder.build_proposal_content(intake_dto)
        async with UnitOfWork(self.bot.db_handler) as uow:
            new_proposal = Proposal(
                discussion_thread_id=thread.id,
                proposer_id=intake_dto.author_id,
                title=intake_dto.title,
                content=proposal_content,
                status=ProposalStatus.DISCUSSION,
            )
            created_proposal = await uow.proposal.add_proposal(new_proposal)

            intake_to_update = await uow.intake.get_intake_by_id(intake_id)
            if intake_to_update:
                intake_to_update.discussion_thread_id = thread.id
                await uow.intake.update_intake(intake_to_update)
                updated_dto = ProposalIntakeDto.model_validate(intake_to_update)
            else:
                updated_dto = intake_dto

            proposal_dto = ProposalDto.model_validate(created_proposal)
            await uow.commit()

        # 派发事件创建投票面板
        self.bot.dispatch(
            "vote_session_created",
            proposal_dto=proposal_dto,
            options=[],
            duration_hours=VoteDuration.PROPOSAL_DEFAULT,
            anonymous=True,
            realtime=True,
            notify=True,
            create_in_voting_channel=True,
            notify_creation_role=False,
            thread=thread,
            intake_id=intake_id,
        )

        # 更新公示消息为成功状态
        channels_config = self.bot.config.get("channels", {})
        if updated_dto.voting_message_id:
            success_embed = IntakeEmbedBuilder.build_support_result_embed(
                updated_dto, success=True, thread_id=thread.id,
                current_votes=required_votes,
            )
            await IntakeUI.edit_result_message(
                self.bot, channels_config.get("objection_publicity"),
                updated_dto.voting_message_id, success_embed,
            )

        # 更新审核帖
        await IntakeUI.update_review_thread(self.bot, updated_dto, view=None)
        return proposal_dto

    async def close_expired_intake(self, intake_id: int):
        async with UnitOfWork(self.bot.db_handler) as uow:
            intake = await uow.intake.get_intake_by_id(intake_id)
            if not intake or intake.status != IntakeStatus.SUPPORT_COLLECTING:
                return

            vote_session = await self._get_support_session(uow, intake_id)
            current_votes = 0
            if vote_session:
                current_votes = await self._count_session_votes(uow, vote_session.id)

            intake.status = IntakeStatus.REJECTED
            await uow.intake.update_intake(intake)

            await uow.session.execute(
                update(VoteSession)
                .where(VoteSession.intake_id == intake_id)
                .where(VoteSession.session_type == VoteSessionType.INTAKE_SUPPORT)
                .values(status=0)
            )

            intake_dto = ProposalIntakeDto.model_validate(intake)
            voting_message_id = intake.voting_message_id
            fail_embed = None
            if voting_message_id:
                fail_embed = IntakeEmbedBuilder.build_support_result_embed(
                    intake_dto, success=False, current_votes=current_votes,
                )

        # Discord UI 更新（在事务外）
        await IntakeUI.update_review_thread(
            self.bot, intake_dto, view=None,
            extra_note="草案因 3 天内支持票不足已自动关闭。",
        )

        if voting_message_id and fail_embed:
            channels_config = self.bot.config.get("channels", {})
            await IntakeUI.edit_result_message(
                self.bot, channels_config.get("objection_publicity"),
                voting_message_id, fail_embed,
            )

    # -------------------------
    # 内部辅助
    # -------------------------

    async def _require_forum_channel(self, config_key: str) -> discord.ForumChannel:
        """从配置获取论坛频道，若未配置或类型错误则抛出异常。"""
        channels_config = self.bot.config.get("channels", {})
        channel_id = channels_config.get(config_key)
        if not channel_id:
            raise ValueError(f"配置中未找到 '{config_key}' 频道ID。")

        channel = await DiscordUtils.fetch_channel(self.bot, channel_id)
        if not isinstance(channel, discord.ForumChannel):
            raise TypeError(f"'{config_key}' 频道类型不正确（需要 ForumChannel）。")
        return channel

    async def _require_text_channel(self, config_key: str) -> discord.TextChannel:
        """从配置获取文本频道，若未配置或类型错误则抛出异常。"""
        channels_config = self.bot.config.get("channels", {})
        channel_id = channels_config.get(config_key)
        if not channel_id:
            raise ValueError(f"配置中未找到 '{config_key}' 频道ID。")

        channel = await DiscordUtils.fetch_channel(self.bot, channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise TypeError(f"'{config_key}' 频道类型不正确（需要 TextChannel）。")
        return channel

    async def _get_support_session(
        self, uow: "UnitOfWork", intake_id: int
    ) -> VoteSession | None:
        stmt = (
            select(VoteSession)
            .where(VoteSession.intake_id == intake_id)
            .where(VoteSession.session_type == VoteSessionType.INTAKE_SUPPORT)
            .where(VoteSession.status == 1)
        )
        result = await uow.session.execute(stmt)
        return result.scalars().one_or_none()

    @staticmethod
    async def _count_session_votes(uow: "UnitOfWork", session_id: int) -> int:
        count_stmt = select(func.count(UserVote.id)).where(
            UserVote.session_id == session_id
        )
        result = (await uow.session.execute(count_stmt)).scalar_one()
        return result or 0

    @staticmethod
    async def _count_support_votes(uow: "UnitOfWork", intake_id: int) -> int:
        count_stmt = (
            select(func.count(UserVote.id))
            .join(VoteSession, UserVote.session_id == VoteSession.id)
            .where(VoteSession.intake_id == intake_id)
            .where(VoteSession.session_type == VoteSessionType.INTAKE_SUPPORT)
        )
        result = (await uow.session.execute(count_stmt)).scalar_one()
        return result or 0

    @staticmethod
    async def _toggle_user_vote(
        uow: "UnitOfWork", session_id: int, user_id: int
    ) -> str:
        """切换用户投票状态，返回 'supported' 或 'withdrawn'。"""
        stmt = select(UserVote).where(
            UserVote.session_id == session_id,
            UserVote.user_id == user_id,
        )
        result = await uow.session.execute(stmt)
        existing = result.scalars().one_or_none()

        if existing:
            await uow.session.delete(existing)
            return "withdrawn"
        else:
            new_vote = UserVote(
                session_id=session_id, user_id=user_id, choice=1, choice_index=1,
            )
            uow.session.add(new_vote)
            return "supported"

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ui import Button, View

from StellariaPact.cogs.Intake.views.IntakeEditModal import IntakeEditModal
from StellariaPact.cogs.Intake.views.IntakeReviewModal import IntakeReviewModal
from StellariaPact.dto.ProposalIntakeDto import ProposalIntakeDto
from StellariaPact.share.auth.RoleGuard import RoleGuard
from StellariaPact.share.enums import IntakeStatus
from StellariaPact.share.UnitOfWork import UnitOfWork

if TYPE_CHECKING:
    from StellariaPact.share.StellariaPactBot import StellariaPactBot

# 按钮定义注册表: custom_id → (label, style, row, callback_name)
_BUTTON_DEFS: list[tuple[str, str, discord.ButtonStyle, int, str]] = [
    ("persistent:intake_approve", "✅ 批准", discord.ButtonStyle.success, 0, "approve"),
    ("persistent:intake_reject", "❌ 拒绝", discord.ButtonStyle.danger, 0, "reject"),
    ("persistent:intake_modify", "📝 要求修改", discord.ButtonStyle.secondary, 0, "modify"),
    ("persistent:intake_edit", "✏️ 修改提案", discord.ButtonStyle.primary, 1, "edit_proposal"),
]

# 每个状态显示哪些按钮
_STATUS_BUTTON_KEYS: dict[int | None, list[str]] = {
    None: ["persistent:intake_approve", "persistent:intake_reject",
           "persistent:intake_modify", "persistent:intake_edit"],
    int(IntakeStatus.PENDING_REVIEW): ["persistent:intake_approve", "persistent:intake_reject",
                                        "persistent:intake_modify", "persistent:intake_edit"],
    int(IntakeStatus.MODIFICATION_REQUIRED): ["persistent:intake_reject",
                                               "persistent:intake_edit"],
}


class IntakeReviewView(View):
    """审核草案和提供提案人修改入口的视图。"""

    def __init__(self, bot: "StellariaPactBot", intake_dto: "ProposalIntakeDto | None" = None):
        super().__init__(timeout=None)
        self.bot = bot

        status = int(intake_dto.status) if intake_dto else None
        button_keys = _STATUS_BUTTON_KEYS.get(status, [])
        callback_map = {btn[0]: btn[4] for btn in _BUTTON_DEFS}

        for custom_id, label, style, row, _callback_name in _BUTTON_DEFS:
            if custom_id not in button_keys:
                continue
            btn = Button(label=label, style=style, custom_id=custom_id, row=row)
            btn.callback = getattr(self, callback_map[custom_id])
            self.add_item(btn)

    async def _check_permissions(self, interaction: discord.Interaction) -> bool:
        if not RoleGuard.hasRoles(interaction, "stewards"):
            await interaction.response.send_message(
                "❌ 您没有权限执行此操作，需要 管理组 身份组。", ephemeral=True
            )
            return False
        return True

    async def _handle_review_action(self, interaction: discord.Interaction, action: str):
        if not await self._check_permissions(interaction):
            return
        modal = IntakeReviewModal(self.bot, action)
        await interaction.response.send_modal(modal)

    async def approve(self, interaction: discord.Interaction):
        await self._handle_review_action(interaction, "approved")

    async def reject(self, interaction: discord.Interaction):
        await self._handle_review_action(interaction, "rejected")

    async def modify(self, interaction: discord.Interaction):
        await self._handle_review_action(interaction, "modification_requested")

    async def edit_proposal(self, interaction: discord.Interaction):
        async with UnitOfWork(self.bot.db_handler) as uow:
            if not interaction.channel_id:
                return await interaction.response.send_message(
                    "❌ 无法获取帖子上下文。", ephemeral=True
                )

            intake = await uow.intake.get_intake_by_review_thread_id(interaction.channel_id)
            if not intake:
                return await interaction.response.send_message(
                    "❌ 找不到相关草案。", ephemeral=True
                )

            intake_dto = ProposalIntakeDto.model_validate(intake)
            if interaction.user.id != intake.author_id:
                return await interaction.response.send_message(
                    "❌ 只有提案人可以修改该提案。", ephemeral=True
                )

            if intake.status not in (
                IntakeStatus.PENDING_REVIEW,
                IntakeStatus.MODIFICATION_REQUIRED,
            ):
                return await interaction.response.send_message(
                    "❌ 提案已进入其他阶段，无法继续修改。", ephemeral=True
                )

            modal = IntakeEditModal(self.bot, intake_dto)
            await interaction.response.send_modal(modal)

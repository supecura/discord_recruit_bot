"""
Valorant募集管理Discord Bot
============================

機能:
- /募集 コマンドで参加者募集を作成(人数・ランク帯[ドロップダウン]・開始時刻・メモ・自分の現在のランクを指定)
  (人数=1の場合は、テキストトリガーの「@1」と同様に他の参加メンバーを選択できる)
- 「参加者募集 @2」のように本文に「@数字」を含む通常メッセージを送るだけでも募集ウィザードが起動する。
  (チャンネルの「募集設定を開く」ボタンから、本人にだけ見える設定画面を開いて入力する)
  「@数字」は「あと何人募集するか」を表し(5人パーティ前提)、すでに参加が決まっている他のメンバー
  (5 - 数字 - 自分 の人数)をサーバー内メンバーから選択し、各メンバーの現在のランクを個別に設定できる。
  定員は常に5人で、「@数字」の数だけ空き枠が残る
- 「参加」ボタン押下 → 現在のランクを選択(ドロップダウン)してから参加登録
- 「キャンセル」ボタンで参加取り消し
- 定員に達したら自動で満員表示・ボタン無効化、参加者全員のランク一覧をチャンネル通知+DM通知
- 開始時刻を HH:MM 形式で指定すると、指定分前に自動リマインド(チャンネル+DM)
- 募集主のみが手動で締切可能(締切時にリマインドも自動キャンセル)

必要なライブラリ:
    pip install -U discord.py python-dotenv

実行方法:
    1. .env ファイルに DISCORD_TOKEN=あなたのBotトークン を記載
    2. python bot.py
"""

import os
import re
import asyncio
import datetime
from dataclasses import dataclass, field
from typing import Optional, Any

import discord
from discord import app_commands
from discord.ext import commands

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenvが無くても環境変数が設定済みなら動作する

# ----------------------------------------------------------------------------
# 基本設定
# ----------------------------------------------------------------------------

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# アクティブな募集を保持する辞書 { message_id: Recruitment }
active_recruitments: dict[int, "Recruitment"] = {}

# ランク帯の選択肢(募集の対象ランク帯、ドロップダウン用)
RANK_CHOICES = [
    app_commands.Choice(name="現ブロンズまで", value="現ブロンズまで"),
    app_commands.Choice(name="現シルバーまで", value="現シルバーまで"),
    app_commands.Choice(name="現ゴールドまで", value="現ゴールドまで"),
    app_commands.Choice(name="現シルゴル", value="現シルゴル"),
    app_commands.Choice(name="ランク不問", value="ランク不問"),
]
DEFAULT_RANK_BAND = "ランク不問"
PARTY_SIZE = 5  # テキストトリガー募集のパーティ人数(「@数字」は「あと何人」を表す)

# 個人の現在のランク選択肢(参加登録時・募集主本人の入力用、選択必須・ダイヤ以上は除く)
RANK_VALUES = ["アイアン", "ブロンズ", "シルバー", "ゴールド", "プラチナ"]
PARTICIPANT_RANK_CHOICES = [app_commands.Choice(name=v, value=v) for v in RANK_VALUES]
RANK_INDEX = {rank: i for i, rank in enumerate(RANK_VALUES)}


def rank_matches_band(rank: str, band: str) -> bool:
    """個人のランクが、募集の対象ランク帯の条件を満たしているかを判定する。"""
    if not band or band == "ランク不問":
        return True
    if band == "現ブロンズまで":
        return RANK_INDEX.get(rank, 99) <= RANK_INDEX["ブロンズ"]
    if band == "現シルバーまで":
        return RANK_INDEX.get(rank, 99) <= RANK_INDEX["シルバー"]
    if band == "現ゴールドまで":
        return RANK_INDEX.get(rank, 99) <= RANK_INDEX["ゴールド"]
    if band == "現シルゴル":
        return rank in ("シルバー", "ゴールド")
    return True  # 未知のランク帯値は将来の拡張に備えて許容する

# 「参加者募集 @2」のような本文中の「@数字」に反応するための正規表現
# (?<![<＜]) : 直前が '<' や '＜' でないこと(通常のメンション "<@1234567890>" と誤判定しないため)
# [@＠] : 半角/全角どちらの @ にも反応
# [0-9\uFF10-\uFF19]{1,2} : 半角/全角どちらの数字にも反応(1〜2桁)
TEXT_TRIGGER_PATTERN = re.compile(r"(?<![<\uFF1C])[@\uFF20]([0-9\uFF10-\uFF19]{1,2})")
_ZEN_TO_HAN_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")

# ウィザード内のセレクトメニュー用の選択肢
BAND_OPTION_VALUES = [c.value for c in RANK_CHOICES]
OWN_RANK_OPTION_VALUES = RANK_VALUES  # 「未回答」は廃止(選択必須のため)


def parse_start_time(text: str) -> Optional[datetime.datetime]:
    """'21:00' や '21:00〜' のようなテキストから今日/明日のdatetimeを推定する。
    パースできない場合はNoneを返す(=リマインド設定なし)。
    """
    if not text:
        return None
    cleaned = text.strip().replace("〜", "").replace("時", ":").replace("分", "")
    t = None
    for fmt in ("%H:%M", "%H"):
        try:
            t = datetime.datetime.strptime(cleaned, fmt).time()
            break
        except ValueError:
            continue
    if t is None:
        digits = "".join(ch for ch in cleaned if ch.isdigit())
        if len(digits) == 4:
            try:
                t = datetime.time(int(digits[:2]), int(digits[2:]))
            except ValueError:
                return None
        else:
            return None

    now = datetime.datetime.now()
    candidate = datetime.datetime.combine(now.date(), t)
    if candidate < now - datetime.timedelta(minutes=5):
        candidate += datetime.timedelta(days=1)
    return candidate


# ----------------------------------------------------------------------------
# データクラス: 募集情報
# ----------------------------------------------------------------------------

@dataclass
class Recruitment:
    host_id: int
    host_name: str
    capacity: int
    rank: str
    start_time_text: str
    note: str
    channel_id: int
    guild_id: int = 0
    remind_minutes: int = 10
    message_id: int = 0
    participants: list[int] = field(default_factory=list)
    participant_ranks: dict[int, str] = field(default_factory=dict)
    closed: bool = False
    created_at: datetime.datetime = field(default_factory=datetime.datetime.now)
    start_dt: Optional[datetime.datetime] = None
    reminder_task: Optional[asyncio.Task] = field(default=None, repr=False)
    reminder_sent: bool = False
    view: Optional[Any] = field(default=None, repr=False)  # RecruitmentViewの参照(再アタッチ用)

    def is_full(self) -> bool:
        return len(self.participants) >= self.capacity

    def build_embed(self) -> discord.Embed:
        if self.closed:
            color = discord.Color.dark_gray()
            # 定員に届かないまま締め切られた場合は「流会」、満員後に締め切られた場合は「締切」
            status = "🔒 締切" if self.is_full() else "🔒 流会"
        elif self.is_full():
            color = discord.Color.red()
            status = "🈵 満員"
        else:
            color = discord.Color.green()
            status = "🟢 募集中"

        embed = discord.Embed(
            title=f"🎯 VALORANT 募集 [{status}]",
            color=color,
        )
        embed.add_field(name="募集主", value=f"<@{self.host_id}>", inline=True)
        embed.add_field(name="人数", value=f"{len(self.participants)} / {self.capacity}", inline=True)
        embed.add_field(name="ランク帯", value=self.rank or DEFAULT_RANK_BAND, inline=True)
        embed.add_field(name="開始時刻", value=self.start_time_text or "未定", inline=True)

        if self.start_dt and not self.closed:
            remind_text = f"開始{self.remind_minutes}分前に自動リマインドします"
        elif self.start_time_text:
            remind_text = "リマインドなし(HH:MM形式で指定すると設定されます)"
        else:
            remind_text = "-"
        embed.add_field(name="リマインド", value=remind_text, inline=True)

        if self.note:
            embed.add_field(name="メモ", value=self.note, inline=False)

        if self.participants:
            participant_lines = "\n".join(
                f"・<@{uid}> — {self.participant_ranks.get(uid, '未回答')}"
                for uid in self.participants
            )
        else:
            participant_lines = "まだ参加者がいません"
        embed.add_field(name="参加者一覧(現在のランク)", value=participant_lines, inline=False)

        embed.set_footer(text=f"募集作成: {self.created_at.strftime('%Y-%m-%d %H:%M')}")
        return embed


async def safe_dm(user_id: int, content: str):
    """DM送信に失敗しても(DM拒否設定など)処理を止めない。"""
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        if user.bot:
            return  # Bot(自分自身を含む)にはDMを送れない
        await user.send(content)
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        pass


async def refresh_main_message(r: "Recruitment"):
    """募集メッセージ本体(Embed・ボタン状態)を最新の状態に更新する。"""
    channel = bot.get_channel(r.channel_id)
    if channel is None:
        return
    try:
        message = await channel.fetch_message(r.message_id)
    except (discord.NotFound, discord.HTTPException):
        return

    if r.view is None:
        r.view = RecruitmentView(r)

    disabled = r.closed or r.is_full()
    for item in r.view.children:
        if isinstance(item, discord.ui.Button) and item.custom_id == "join":
            item.disabled = disabled

    try:
        await message.edit(embed=r.build_embed(), view=r.view)
    except discord.HTTPException:
        pass


async def announce_full(r: "Recruitment"):
    """定員に達した際、参加者全員の現在のランクをチャンネル+DMで通知する。"""
    roster_lines = [
        f"・<@{uid}> — {r.participant_ranks.get(uid, '未回答')}" for uid in r.participants
    ]
    roster_text = "\n".join(roster_lines)

    channel = bot.get_channel(r.channel_id)
    if channel:
        mentions = " ".join(f"<@{uid}>" for uid in r.participants)
        await channel.send(
            f"🈵 メンバーが揃いました!\n\n**参加メンバーの現在のランク**\n{roster_text}\n\n{mentions}"
        )

    dm_text = (
        f"🈵「{r.host_name}」さんのVALORANT募集が満員になりました。\n"
        f"開始時刻: {r.start_time_text or '未定'}\n\n"
        f"**参加メンバーの現在のランク**\n{roster_text}"
    )
    for uid in r.participants:
        await safe_dm(uid, dm_text)


async def schedule_reminder(recruitment: "Recruitment"):
    """開始時刻の remind_minutes 分前に通知を送るタスク。"""
    if not recruitment.start_dt:
        return

    remind_at = recruitment.start_dt - datetime.timedelta(minutes=recruitment.remind_minutes)
    wait_seconds = (remind_at - datetime.datetime.now()).total_seconds()
    if wait_seconds > 0:
        try:
            await asyncio.sleep(wait_seconds)
        except asyncio.CancelledError:
            return

    if recruitment.closed or recruitment.reminder_sent:
        return
    recruitment.reminder_sent = True

    channel = bot.get_channel(recruitment.channel_id)
    mentions = " ".join(f"<@{uid}>" for uid in recruitment.participants)
    if channel:
        await channel.send(
            f"⏰ もうすぐ開始です!(あと約{recruitment.remind_minutes}分) "
            f"開始時刻: {recruitment.start_time_text} {mentions}"
        )

    dm_text = (
        f"⏰ VALORANT募集のリマインドです。\n"
        f"募集主: {recruitment.host_name}\n"
        f"開始時刻: {recruitment.start_time_text}(あと約{recruitment.remind_minutes}分)\n"
        f"ランク帯: {recruitment.rank or DEFAULT_RANK_BAND}"
    )
    for uid in recruitment.participants:
        await safe_dm(uid, dm_text)


# ----------------------------------------------------------------------------
# UI: ランク選択(参加登録の第2ステップ、本人のみに見えるエフェメラル表示)
# ----------------------------------------------------------------------------

class RankSelectView(discord.ui.View):
    def __init__(self, recruitment: "Recruitment"):
        super().__init__(timeout=120)
        self.recruitment = recruitment

    @discord.ui.select(
        placeholder="現在のランクを選択してください",
        options=[discord.SelectOption(label=v, value=v) for v in RANK_VALUES],
    )
    async def rank_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        r = self.recruitment
        user_id = interaction.user.id

        # 選択している間に状況が変わっている可能性があるため再チェック
        if r.closed:
            await interaction.response.edit_message(content="この募集はすでに締め切られています。", view=None)
            return
        if user_id in r.participants:
            await interaction.response.edit_message(content="すでに参加しています。", view=None)
            return
        if r.is_full():
            await interaction.response.edit_message(content="満員のため参加できませんでした。", view=None)
            return

        chosen_rank = select.values[0]

        if not rank_matches_band(chosen_rank, r.rank):
            await interaction.response.edit_message(
                content=(
                    f"❌ 参加できませんでした。\n"
                    f"この募集の対象ランク帯は「{r.rank or DEFAULT_RANK_BAND}」ですが、"
                    f"選択されたランク「{chosen_rank}」は対象外のため、参加はキャンセルされました。"
                ),
                view=None,
            )
            return

        r.participants.append(user_id)
        r.participant_ranks[user_id] = chosen_rank

        await interaction.response.edit_message(
            content=f"✅ 参加登録しました。現在のランク: {chosen_rank}", view=None
        )

        await refresh_main_message(r)

        await safe_dm(
            user_id,
            f"✅ 「{r.host_name}」さんのVALORANT募集に参加登録しました(ランク: {chosen_rank})。\n"
            f"人数: {len(r.participants)}/{r.capacity}　開始時刻: {r.start_time_text or '未定'}\n"
            f"ランク帯: {r.rank or DEFAULT_RANK_BAND}",
        )

        if r.is_full():
            await announce_full(r)


# ----------------------------------------------------------------------------
# UI: 参加/キャンセル/締切ボタン(募集メッセージ本体)
# ----------------------------------------------------------------------------

class RecruitmentView(discord.ui.View):
    def __init__(self, recruitment: "Recruitment"):
        super().__init__(timeout=None)
        self.recruitment = recruitment

    @discord.ui.button(label="参加する", style=discord.ButtonStyle.success, custom_id="join")
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        r = self.recruitment
        if r.closed:
            await interaction.response.send_message("この募集は締め切られています。", ephemeral=True)
            return
        if interaction.user.id in r.participants:
            await interaction.response.send_message("すでに参加しています。", ephemeral=True)
            return
        if r.is_full():
            await interaction.response.send_message("満員のため参加できません。", ephemeral=True)
            return

        rank_view = RankSelectView(r)
        await interaction.response.send_message(
            "参加登録のため、現在のランクを選択してください:", view=rank_view, ephemeral=True
        )

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary, custom_id="cancel")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        r = self.recruitment
        if interaction.user.id not in r.participants:
            await interaction.response.send_message("参加していません。", ephemeral=True)
            return

        r.participants.remove(interaction.user.id)
        r.participant_ranks.pop(interaction.user.id, None)

        disabled = r.closed or r.is_full()
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.custom_id == "join":
                item.disabled = disabled

        embed = r.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="締切(募集主のみ)", style=discord.ButtonStyle.danger, custom_id="close")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        r = self.recruitment
        if interaction.user.id != r.host_id:
            await interaction.response.send_message("募集主のみが締め切れます。", ephemeral=True)
            return

        r.closed = True
        if r.reminder_task and not r.reminder_task.done():
            r.reminder_task.cancel()

        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        embed = r.build_embed()
        closed_text = "この募集を締め切りました。" if r.is_full() else "この募集は流会しました。"
        await interaction.response.edit_message(content=closed_text, embed=embed, view=self)


# ----------------------------------------------------------------------------
# UI: テキストトリガー(「@数字」検知)から始まる募集作成ウィザード
# ----------------------------------------------------------------------------

async def create_recruitment_and_post(
    interaction: discord.Interaction,
    *,
    capacity: int,
    rank_band: str,
    own_rank: str,
    start_time_text: str,
    note: str,
    remind_minutes: int,
    extra_participant_ranks: Optional[dict[int, str]] = None,
):
    """募集オブジェクトを作成し、募集メッセージを投稿する共通処理。
    /募集 コマンドとテキストトリガーウィザードの両方から呼び出される。

    extra_participant_ranks: 募集主が最初から追加した他の参加者 { ユーザーID: ランク } の辞書。
    (テキストトリガーや人数=1の/募集で、募集主が最初から参加メンバーとその個別ランクを入力するケースで使用)
    """
    start_dt = parse_start_time(start_time_text)

    recruitment = Recruitment(
        host_id=interaction.user.id,
        host_name=interaction.user.display_name,
        capacity=capacity,
        rank=rank_band,
        start_time_text=start_time_text,
        note=note,
        channel_id=interaction.channel_id,
        guild_id=interaction.guild_id or 0,
        remind_minutes=remind_minutes,
        start_dt=start_dt,
    )
    recruitment.participants.append(interaction.user.id)
    recruitment.participant_ranks[interaction.user.id] = own_rank

    for uid, rank in (extra_participant_ranks or {}).items():
        if uid != interaction.user.id and uid not in recruitment.participants:
            recruitment.participants.append(uid)
            recruitment.participant_ranks[uid] = rank

    view = RecruitmentView(recruitment)
    recruitment.view = view

    disabled = recruitment.is_full()
    for item in view.children:
        if isinstance(item, discord.ui.Button) and item.custom_id == "join":
            item.disabled = disabled

    embed = recruitment.build_embed()

    await interaction.response.send_message(embed=embed, view=view)
    sent_message = await interaction.original_response()

    recruitment.message_id = sent_message.id
    active_recruitments[sent_message.id] = recruitment

    if start_dt:
        recruitment.reminder_task = asyncio.create_task(schedule_reminder(recruitment))

    if recruitment.is_full():
        await announce_full(recruitment)

    return recruitment


class RecruitDetailsModal(discord.ui.Modal, title="募集の詳細を入力"):
    """ウィザードの最終ステップ: 開始時刻・メモ・リマインド分前を入力するモーダル。"""

    def __init__(self, session: "SetupSessionView"):
        super().__init__()
        self.session = session

        self.start_time_input = discord.ui.TextInput(
            label="開始時刻(例: 21:00、任意)",
            required=False,
            max_length=20,
        )
        self.note_input = discord.ui.TextInput(
            label="メモ(任意)",
            required=False,
            max_length=200,
            style=discord.TextStyle.paragraph,
        )
        self.remind_input = discord.ui.TextInput(
            label="リマインド分前(数字、未入力で10分)",
            required=False,
            max_length=3,
            default="10",
        )
        self.add_item(self.start_time_input)
        self.add_item(self.note_input)
        self.add_item(self.remind_input)

    async def on_submit(self, interaction: discord.Interaction):
        remind_str = self.remind_input.value.strip()
        remind_minutes = int(remind_str) if remind_str.isdigit() else 10
        remind_minutes = max(1, min(180, remind_minutes))

        # 選択順を保ったまま { ユーザーID: ランク } を構築(MemberRankAssignViewで全員分入力必須のため通常はここで欠落しない)
        extra_ranks = {
            uid: self.session.extra_member_ranks.get(uid, RANK_VALUES[0])
            for uid in self.session.extra_member_ids
        }

        # テキストトリガー募集の定員は常にパーティ人数(5人)。「@数字」の数だけ空き枠が残る
        final_capacity = self.session.capacity

        await create_recruitment_and_post(
            interaction,
            capacity=final_capacity,
            rank_band=self.session.rank_band,
            own_rank=self.session.own_rank,
            start_time_text=self.start_time_input.value.strip(),
            note=self.note_input.value.strip(),
            remind_minutes=remind_minutes,
            extra_participant_ranks=extra_ranks,
        )

        # 設定画面(本人にだけ見えるメッセージ)と、チャンネル上の起動用メッセージを後片付けする
        if self.session.launcher is not None:
            self.session.launcher.completed = True
        if self.session.wizard_interaction is not None:
            try:
                await self.session.wizard_interaction.edit_original_response(
                    content="✅ 募集を作成しました。", view=None
                )
            except discord.HTTPException:
                pass
        if self.session.launcher_message is not None:
            try:
                await self.session.launcher_message.edit(
                    content="✅ 募集を作成しました(下のメッセージをご確認ください)。", view=None
                )
            except discord.HTTPException:
                pass


class SlashRecruitSession:
    """/募集 コマンドで人数=1が指定された場合に、他の参加メンバー選択〜個別ランク設定の間、
    コマンドで既に入力済みの値を保持しておくための簡易セッション。"""

    def __init__(
        self,
        author_id: int,
        rank_band: str,
        own_rank: str,
        start_time_text: str,
        note: str,
        remind_minutes: int,
    ):
        self.author_id = author_id
        self.rank_band = rank_band
        self.own_rank = own_rank
        self.start_time_text = start_time_text
        self.note = note
        self.remind_minutes = remind_minutes
        self.extra_member_ids: list[int] = []
        self.extra_member_names: dict[int, str] = {}
        self.extra_member_ranks: dict[int, str] = {}


class SlashMemberRankAssignView(discord.ui.View):
    """/募集(人数=1)で選択した各メンバーごとに現在のランクを個別設定する画面。"""

    def __init__(self, session: SlashRecruitSession):
        super().__init__(timeout=300)
        self.session = session

        for member_id in session.extra_member_ids:
            name = session.extra_member_names.get(member_id, f"ID:{member_id}")
            select = discord.ui.Select(
                placeholder=f"{name} の現在のランクを選択(必須)",
                options=[discord.SelectOption(label=v, value=v) for v in OWN_RANK_OPTION_VALUES],
            )

            def make_callback(uid: int, display_name: str, sel: discord.ui.Select = select):
                async def callback(interaction: discord.Interaction):
                    if interaction.user.id != self.session.author_id:
                        await interaction.response.send_message(
                            "この募集設定はあなたのものではありません。", ephemeral=True
                        )
                        return
                    chosen_rank = sel.values[0]
                    if not rank_matches_band(chosen_rank, self.session.rank_band):
                        self.session.extra_member_ranks.pop(uid, None)
                        await interaction.response.send_message(
                            f"❌ {display_name} さんのランク「{chosen_rank}」は、"
                            f"この募集の対象ランク帯「{self.session.rank_band}」の条件を満たしていないため設定できません。"
                            f"別のランクを選び直すか、対象ランク帯を見直してください。",
                            ephemeral=True,
                        )
                        return
                    self.session.extra_member_ranks[uid] = chosen_rank
                    await interaction.response.defer()  # 成功時は通知を出さない
                return callback

            select.callback = make_callback(member_id, name)
            self.add_item(select)

        finish_button = discord.ui.Button(label="この内容で募集を作成", style=discord.ButtonStyle.primary)

        async def finish_callback(interaction: discord.Interaction):
            if interaction.user.id != self.session.author_id:
                await interaction.response.send_message("この募集設定はあなたのものではありません。", ephemeral=True)
                return
            missing = [
                self.session.extra_member_names.get(uid, f"ID:{uid}")
                for uid in self.session.extra_member_ids
                if uid not in self.session.extra_member_ranks
            ]
            if missing:
                await interaction.response.send_message(
                    f"以下のメンバーの現在のランクを選択してください: {'、'.join(missing)}",
                    ephemeral=True,
                )
                return

            # 「自分 + 選択したメンバー + あと1人」を常に確保する(即座に満員にはしない)
            final_capacity = 1 + len(self.session.extra_member_ids) + 1
            await create_recruitment_and_post(
                interaction,
                capacity=final_capacity,
                rank_band=self.session.rank_band,
                own_rank=self.session.own_rank,
                start_time_text=self.session.start_time_text,
                note=self.session.note,
                remind_minutes=self.session.remind_minutes,
                extra_participant_ranks=dict(self.session.extra_member_ranks),
            )

        finish_button.callback = finish_callback
        self.add_item(finish_button)


class SlashMemberSelectView(discord.ui.View):
    """/募集 コマンドで人数=1が指定された場合に表示する、他の参加メンバー選択画面。"""

    def __init__(self, session: SlashRecruitSession):
        super().__init__(timeout=300)
        self.session = session

        member_select = discord.ui.UserSelect(
            placeholder="他の参加メンバーを選択(任意、最大4人・サーバー内のメンバーのみ)",
            min_values=0,
            max_values=4,
        )

        async def on_member_select(interaction: discord.Interaction):
            if interaction.user.id != self.session.author_id:
                await interaction.response.send_message("この募集設定はあなたのものではありません。", ephemeral=True)
                return
            selected = [m for m in member_select.values if m.id != self.session.author_id and not m.bot]
            self.session.extra_member_ids = [m.id for m in selected]
            self.session.extra_member_names = {m.id: m.display_name for m in selected}
            self.session.extra_member_ranks = {
                uid: rank for uid, rank in self.session.extra_member_ranks.items()
                if uid in self.session.extra_member_ids
            }
            if any(m.bot for m in member_select.values):
                await interaction.response.send_message(
                    "⚠️ Botは参加メンバーに選べないため除外しました。選び直してください。", ephemeral=True
                )
            else:
                await interaction.response.defer()  # 成功時は通知を出さない

        member_select.callback = on_member_select
        self.add_item(member_select)

        proceed_button = discord.ui.Button(
            label="このまま作成(メンバー未選択でもOK)", style=discord.ButtonStyle.primary
        )

        async def proceed_callback(interaction: discord.Interaction):
            if interaction.user.id != self.session.author_id:
                await interaction.response.send_message("この募集設定はあなたのものではありません。", ephemeral=True)
                return
            if self.session.extra_member_ids:
                rank_view = SlashMemberRankAssignView(self.session)
                await interaction.response.edit_message(
                    content="選択したメンバーの現在のランクを設定してください(全員分の選択が必要です)。",
                    view=rank_view,
                )
            else:
                # 誰も選択しなければ「自分+あと1人」で作成
                await create_recruitment_and_post(
                    interaction,
                    capacity=2,
                    rank_band=self.session.rank_band,
                    own_rank=self.session.own_rank,
                    start_time_text=self.session.start_time_text,
                    note=self.session.note,
                    remind_minutes=self.session.remind_minutes,
                    extra_participant_ranks={},
                )

        proceed_button.callback = proceed_callback
        self.add_item(proceed_button)


class MemberRankAssignView(discord.ui.View):
    """選択した各メンバーごとに現在のランクを個別設定する画面。"""

    def __init__(self, session: "SetupSessionView"):
        super().__init__(timeout=300)
        self.session = session

        for member_id in session.extra_member_ids:
            name = session.extra_member_names.get(member_id, f"ID:{member_id}")
            select = discord.ui.Select(
                placeholder=f"{name} の現在のランクを選択(必須)",
                options=[discord.SelectOption(label=v, value=v) for v in OWN_RANK_OPTION_VALUES],
            )

            def make_callback(uid: int, display_name: str, sel: discord.ui.Select = select):
                async def callback(interaction: discord.Interaction):
                    if interaction.user.id != self.session.author_id:
                        await interaction.response.send_message(
                            "この募集設定はあなたのものではありません。", ephemeral=True
                        )
                        return
                    chosen_rank = sel.values[0]
                    if not rank_matches_band(chosen_rank, self.session.rank_band):
                        self.session.extra_member_ranks.pop(uid, None)
                        await interaction.response.send_message(
                            f"❌ {display_name} さんのランク「{chosen_rank}」は、"
                            f"この募集の対象ランク帯「{self.session.rank_band}」の条件を満たしていないため設定できません。"
                            f"別のランクを選び直すか、対象ランク帯を見直してください。",
                            ephemeral=True,
                        )
                        return
                    self.session.extra_member_ranks[uid] = chosen_rank
                    await interaction.response.defer()  # 成功時は通知を出さない
                return callback

            select.callback = make_callback(member_id, name)
            self.add_item(select)

        next_button = discord.ui.Button(label="次へ(開始時刻・メモを入力)", style=discord.ButtonStyle.primary)

        async def next_callback(interaction: discord.Interaction):
            if interaction.user.id != self.session.author_id:
                await interaction.response.send_message("この募集設定はあなたのものではありません。", ephemeral=True)
                return
            missing = [
                self.session.extra_member_names.get(uid, f"ID:{uid}")
                for uid in self.session.extra_member_ids
                if uid not in self.session.extra_member_ranks
            ]
            if missing:
                await interaction.response.send_message(
                    f"以下のメンバーの現在のランクを選択してください: {'、'.join(missing)}",
                    ephemeral=True,
                )
                return
            await interaction.response.send_modal(RecruitDetailsModal(self.session))

        next_button.callback = next_callback
        self.add_item(next_button)


class SetupSessionView(discord.ui.View):
    """テキストトリガーで起動するウィザードの1画面目: ランク帯 / 自分のランク / すでに決まっている他の参加メンバーを選ぶ。"""

    def __init__(self, author_id: int, needed: int):
        super().__init__(timeout=300)
        self.author_id = author_id
        self.needed = needed  # 「@数字」= あと何人募集するか
        self.capacity = PARTY_SIZE
        # 募集主以外で、すでに参加が決まっているメンバー数(5 - 募集人数 - 募集主本人)
        self.other_count = max(0, PARTY_SIZE - needed - 1)
        self.rank_band = DEFAULT_RANK_BAND
        self.own_rank: Optional[str] = None
        self.launcher: Optional["WizardLauncherView"] = None  # チャンネル上の起動用ボタンView
        self.launcher_message: Optional[discord.Message] = None  # チャンネル上の起動用メッセージ
        self.wizard_interaction: Optional[discord.Interaction] = None  # 本人にだけ見える設定画面のinteraction
        self.extra_member_ids: list[int] = []
        self.extra_member_names: dict[int, str] = {}
        self.extra_member_ranks: dict[int, str] = {}

        band_select = discord.ui.Select(
            placeholder="対象ランク帯を選択(任意、未選択なら「ランク不問」)",
            options=[discord.SelectOption(label=v, value=v) for v in BAND_OPTION_VALUES],
            custom_id="wizard_band_select",
        )
        band_select.callback = self._on_band_select
        self.add_item(band_select)

        own_rank_select = discord.ui.Select(
            placeholder="自分の現在のランクを選択(必須)",
            options=[discord.SelectOption(label=v, value=v) for v in OWN_RANK_OPTION_VALUES],
            custom_id="wizard_own_rank_select",
        )
        own_rank_select.callback = self._on_own_rank_select
        self.add_item(own_rank_select)

        # すでに参加が決まっている他のメンバー(5 - 募集人数 - 自分)を、サーバー内メンバーから選択する
        if self.other_count > 0:
            member_select = discord.ui.UserSelect(
                placeholder=f"他の参加メンバーを{self.other_count}人選択(必須・サーバー内のメンバーのみ)",
                min_values=self.other_count,
                max_values=self.other_count,
                custom_id="wizard_member_select",
            )

            async def _on_member_select(interaction: discord.Interaction):
                if not self._is_author(interaction):
                    await interaction.response.send_message(
                        "この募集設定はあなたのものではありません。", ephemeral=True
                    )
                    return
                selected = [m for m in member_select.values if m.id != self.author_id and not m.bot]
                self.extra_member_ids = [m.id for m in selected]
                self.extra_member_names = {m.id: m.display_name for m in selected}
                # 選択し直した場合に備え、対象外になったメンバーのランクは破棄する
                self.extra_member_ranks = {
                    uid: rank for uid, rank in self.extra_member_ranks.items() if uid in self.extra_member_ids
                }
                if any(m.bot for m in member_select.values):
                    await interaction.response.send_message(
                        "⚠️ Botは参加メンバーに選べないため除外しました。選び直してください。", ephemeral=True
                    )
                else:
                    await interaction.response.defer()  # 成功時は通知を出さない

            member_select.callback = _on_member_select
            self.add_item(member_select)

    def _is_author(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    async def _on_band_select(self, interaction: discord.Interaction):
        if not self._is_author(interaction):
            await interaction.response.send_message("この募集設定はあなたのものではありません。", ephemeral=True)
            return
        self.rank_band = interaction.data["values"][0]
        await interaction.response.defer()  # 成功時は通知を出さない

    async def _on_own_rank_select(self, interaction: discord.Interaction):
        if not self._is_author(interaction):
            await interaction.response.send_message("この募集設定はあなたのものではありません。", ephemeral=True)
            return
        self.own_rank = interaction.data["values"][0]
        await interaction.response.defer()  # 成功時は通知を出さない

    @discord.ui.button(label="次へ", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_author(interaction):
            await interaction.response.send_message("この募集設定はあなたのものではありません。", ephemeral=True)
            return

        if self.own_rank is None:
            await interaction.response.send_message(
                "「自分の現在のランク」を選択してから「次へ」を押してください。", ephemeral=True
            )
            return

        if self.other_count > 0 and len(self.extra_member_ids) != self.other_count:
            await interaction.response.send_message(
                f"他の参加メンバーを{self.other_count}人選択してから「次へ」を押してください"
                f"(自分を除く{self.other_count}人、現在{len(self.extra_member_ids)}人選択中)。",
                ephemeral=True,
            )
            return

        if self.other_count > 0:
            rank_view = MemberRankAssignView(self)
            await interaction.response.edit_message(
                content="選択したメンバーの現在のランクを設定してください(全員分の選択が必要です)。",
                view=rank_view,
            )
        else:
            await interaction.response.send_modal(RecruitDetailsModal(self))


class WizardLauncherView(discord.ui.View):
    """チャンネルに表示する起動用メッセージのボタン。
    押した本人にだけ見える(エフェメラルな)設定画面を開くので、入力内容は他の人には見えない。"""

    def __init__(self, author_id: int, needed: int):
        super().__init__(timeout=300)
        self.author_id = author_id
        self.needed = needed
        self.completed = False
        self.message: Optional[discord.Message] = None

    @discord.ui.button(label="募集設定を開く", style=discord.ButtonStyle.primary)
    async def open_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "募集主が募集内容を設定中です。しばらくお待ちください。", ephemeral=True
            )
            return

        session = SetupSessionView(author_id=self.author_id, needed=self.needed)
        session.launcher = self
        session.launcher_message = self.message
        session.wizard_interaction = interaction

        intro = (
            f"🎯 募集を作成します(あと{self.needed}人募集・{PARTY_SIZE}人パーティ)。\n"
            f"この画面はあなたにだけ表示されています。\n"
            f"「自分の現在のランク」は必須です。ランク帯は任意で選んでください。\n"
        )
        if session.other_count > 0:
            intro += (
                f"すでに参加が決まっている他のメンバー{session.other_count}人を選択し、"
                f"「次へ」を押してください(次の画面で各メンバーのランクを設定します)。"
            )
        else:
            intro += "「次へ」を押してください。"
        await interaction.response.send_message(intro, view=session, ephemeral=True)

    async def on_timeout(self):
        if self.completed or self.message is None:
            return
        try:
            await self.message.edit(
                content="⌛ 募集設定の有効期限が切れました。もう一度「参加者募集 @数字」と送ってください。",
                view=None,
            )
        except discord.HTTPException:
            pass


@bot.event
async def on_message(message: discord.Message):
    # Bot自身や他Botのメッセージ、DMは無視
    if message.author.bot or message.guild is None:
        return

    match = TEXT_TRIGGER_PATTERN.search(message.content)
    if match:
        needed = int(match.group(1).translate(_ZEN_TO_HAN_DIGITS))
        if needed < 1 or needed > PARTY_SIZE - 1:
            await message.reply(
                f"募集人数(@の後の数字)は1〜{PARTY_SIZE - 1}で指定してください。例: 「参加者募集 @2」(あと2人)",
                mention_author=False,
            )
        else:
            launcher = WizardLauncherView(author_id=message.author.id, needed=needed)
            launcher.message = await message.reply(
                f"⏳ 募集主({message.author.display_name} さん)が募集内容を設定中です(あと{needed}人募集)。\n"
                f"募集主は下のボタンから設定画面を開いてください(設定画面は本人にだけ表示されます)。",
                view=launcher,
                mention_author=False,
            )

    # プレフィックスコマンド(現状未使用)も引き続き処理できるようにしておく
    await bot.process_commands(message)


# ----------------------------------------------------------------------------
# スラッシュコマンド: /募集
# ----------------------------------------------------------------------------

@bot.tree.command(name="募集", description="VALORANTのメンバー募集を作成します")
@app_commands.describe(
    人数="募集する合計人数(自分を含む、例: 5)",
    自分の現在のランク="あなた自身の現在のランク(ドロップダウンから選択、必須)",
    ランク帯="募集の対象ランク帯(ドロップダウンから選択、任意)",
    開始時刻="例: 21:00 (HH:MM形式にするとリマインド通知が有効になります)",
    メモ="モード・目的などの補足(任意)",
    リマインド分前="開始何分前に通知するか(デフォルト10分、開始時刻がHH:MM形式の場合のみ有効)",
)
@app_commands.choices(ランク帯=RANK_CHOICES, 自分の現在のランク=PARTICIPANT_RANK_CHOICES)
async def recruit(
    interaction: discord.Interaction,
    人数: int,
    自分の現在のランク: app_commands.Choice[str],
    ランク帯: app_commands.Choice[str] = None,
    開始時刻: str = "",
    メモ: str = "",
    リマインド分前: int = 10,
):
    if 人数 < 1 or 人数 > 10:
        await interaction.response.send_message("人数は1〜10で指定してください。", ephemeral=True)
        return
    if リマインド分前 < 1 or リマインド分前 > 180:
        await interaction.response.send_message("リマインド分前は1〜180の範囲で指定してください。", ephemeral=True)
        return

    rank_value = ランク帯.value if ランク帯 else DEFAULT_RANK_BAND
    own_rank_value = 自分の現在のランク.value

    if 人数 == 1:
        # 人数=1の場合は「@1」のテキストトリガーと同様、他の参加メンバーを選択できるようにする
        session = SlashRecruitSession(
            author_id=interaction.user.id,
            rank_band=rank_value,
            own_rank=own_rank_value,
            start_time_text=開始時刻,
            note=メモ,
            remind_minutes=リマインド分前,
        )
        view = SlashMemberSelectView(session)
        await interaction.response.send_message(
            "人数=1が指定されたため、他の参加メンバーを選ぶことができます(任意、最大4人・サーバー内のメンバーのみ)。\n"
            "選ばずに「このまま作成」を押すと、募集主のみで「あと1人募集中」の状態になります。",
            view=view,
            ephemeral=True,
        )
        return

    await create_recruitment_and_post(
        interaction,
        capacity=人数,
        rank_band=rank_value,
        own_rank=own_rank_value,
        start_time_text=開始時刻,
        note=メモ,
        remind_minutes=リマインド分前,
    )


@bot.tree.command(name="募集一覧", description="現在進行中の募集を一覧表示します")
async def recruit_list(interaction: discord.Interaction):
    open_recruitments = [r for r in active_recruitments.values() if not r.closed]
    if not open_recruitments:
        await interaction.response.send_message("現在募集中のものはありません。", ephemeral=True)
        return

    lines = []
    for r in open_recruitments:
        status = "満員" if r.is_full() else "募集中"
        lines.append(
            f"・<#{r.channel_id}> 募集主: <@{r.host_id}> "
            f"({len(r.participants)}/{r.capacity}) [{status}] 開始: {r.start_time_text or '未定'}"
        )

    embed = discord.Embed(
        title="📋 募集一覧",
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ----------------------------------------------------------------------------
# 起動処理
# ----------------------------------------------------------------------------

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"ログイン完了: {bot.user} (ID: {bot.user.id})")


def main():
    if not TOKEN:
        raise RuntimeError(
            "環境変数 DISCORD_TOKEN が設定されていません。"
            ".env ファイルまたは環境変数にBotトークンを設定してください。"
        )
    bot.run(TOKEN)


if __name__ == "__main__":
    main()

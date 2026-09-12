from __future__ import annotations

import asyncio
import os
import random
import math
import base64

from collections import deque
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs

import discord
import yt_dlp

from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


# ============================================================
# 설정
# ============================================================

load_dotenv()

# ============================================================
# YouTube 쿠키 설정
# Railway 환경변수 YT_COOKIES_B64 → /tmp/youtube_cookies.txt
# ============================================================
YT_COOKIES_B64 = os.getenv("YT_COOKIES_B64")
YT_COOKIES_FILE = "/tmp/youtube_cookies.txt"

if YT_COOKIES_B64:
    try:
        with open(YT_COOKIES_FILE, "wb") as f:
            f.write(base64.b64decode(YT_COOKIES_B64))

        print("[YOUTUBE] 쿠키 파일 로드 완료")

    except Exception as e:
        print(f"[YOUTUBE] 쿠키 파일 생성 실패: {e}")
        YT_COOKIES_FILE = None
else:
    YT_COOKIES_FILE = None
    print("[YOUTUBE] 쿠키 없음")

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID_RAW = os.getenv("GUILD_ID")
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")
OWNER_ID_RAW = os.getenv("OWNER_ID")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN이 .env에 없습니다.")

if not GUILD_ID_RAW:
    raise RuntimeError("GUILD_ID가 .env에 없습니다.")

try:
    GUILD_ID = int(GUILD_ID_RAW)
except ValueError:
    raise RuntimeError("GUILD_ID는 숫자여야 합니다.")

if not OWNER_ID_RAW:
    raise RuntimeError("OWNER_ID가 .env에 없습니다.")

try:
    OWNER_ID = int(OWNER_ID_RAW)
except ValueError:
    raise RuntimeError("OWNER_ID는 숫자여야 합니다.")


GUILD = discord.Object(id=GUILD_ID)


# ============================================================
# 재생목록 설정
# ============================================================

MAX_PLAYLIST_TRACKS = 100


# ============================================================
# 데이터
# ============================================================

@dataclass
class Track:
    title: str
    webpage_url: str
    duration: int | None
    requester: str
    requester_id: int
    channel_id: int


# ============================================================
# yt-dlp
# ============================================================

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "ignoreerrors": True,

    # YouTube 쿠키
    "cookiefile": YT_COOKIES_FILE,

    # YouTube 클라이언트 설정
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web"],
        }
    },

    # YouTube EJS
    "js_runtimes": {
        "node": {},
    },
}



# ============================================================
# URL 검사
# ============================================================

def is_url(value: str) -> bool:
    try:
        parsed = urlparse(value)

        return (
            parsed.scheme in ("http", "https")
            and bool(parsed.netloc)
        )

    except Exception:
        return False


def is_youtube_playlist_url(value: str) -> bool:
    """
    YouTube 재생목록 URL인지 확인한다.

    예:
    https://www.youtube.com/playlist?list=...
    https://www.youtube.com/watch?v=xxx&list=...
    """

    try:
        parsed = urlparse(value)

        hostname = (
            parsed.hostname or ""
        ).lower()

        if (
            hostname == "youtu.be"
            or hostname.endswith("youtube.com")
            or hostname.endswith("youtube-nocookie.com")
        ):

            query = parse_qs(parsed.query)

            return bool(query.get("list"))

    except Exception:
        pass

    return False


def is_youtube_shorts_url(value: str) -> bool:
    """
    YouTube Shorts URL인지 확인한다.
    """

    try:
        parsed = urlparse(value)

        hostname = (
            parsed.hostname or ""
        ).lower()

        if not (
            hostname == "youtu.be"
            or hostname.endswith("youtube.com")
            or hostname.endswith("youtube-nocookie.com")
        ):
            return False

        path = parsed.path.lower().rstrip("/")

        return (
            path.startswith("/shorts/")
            or "/shorts/" in path
        )

    except Exception:
        return False


def get_webpage_url(info: dict) -> str | None:
    return (
        info.get("webpage_url")
        or info.get("original_url")
        or info.get("url")
    )


# ============================================================
# yt-dlp 단일 영상 추출
# ============================================================

def extract_info_sync(query: str) -> dict:
    """
    검색 또는 단일 영상 URL에서 정보를 가져온다.

    이 함수는 blocking이므로 별도 thread에서 실행한다.
    """

    target = (
        query
        if is_url(query)
        else f"ytsearch1:{query}"
    )

    with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:

        info = ydl.extract_info(
            target,
            download=False,
        )

        if not info:
            raise RuntimeError(
                "YouTube에서 결과를 가져오지 못했습니다."
            )

        if "entries" in info:

            entries = info["entries"]

            if not entries:
                raise RuntimeError(
                    "검색 결과가 없습니다."
                )

            info = entries[0]

        return info


async def extract_info(query: str) -> dict:

    loop = asyncio.get_running_loop()

    return await loop.run_in_executor(
        None,
        extract_info_sync,
        query,
    )


# ============================================================
# YouTube 검색
# ============================================================

def search_youtube_sync(
    query: str,
    limit: int = 5,
) -> list[dict]:
    """
    YouTube에서 검색 결과를 여러 개 가져온다.
    """

    options = YTDL_OPTIONS.copy()

    options["noplaylist"] = True

    target = f"ytsearch{limit}:{query}"

    with yt_dlp.YoutubeDL(options) as ydl:

        info = ydl.extract_info(
            target,
            download=False,
        )

        if not info:
            raise RuntimeError(
                "YouTube에서 검색 결과를 가져오지 못했습니다."
            )

        entries = info.get("entries") or []

        results = []

        for entry in entries:

            if not entry:
                continue

            webpage_url = get_webpage_url(entry)

            if not webpage_url:
                continue

            # 검색 결과에서도 Shorts 제외
            if is_youtube_shorts_url(webpage_url):
                continue

            results.append(entry)

        return results


async def search_youtube(
    query: str,
    limit: int = 5,
) -> list[dict]:

    loop = asyncio.get_running_loop()

    return await loop.run_in_executor(
        None,
        search_youtube_sync,
        query,
        limit,
    )

# ============================================================
# YouTube 재생목록
# ============================================================

PLAYLIST_MAX_TRACKS = 100


def extract_playlist_sync(
    url: str,
    limit: int = PLAYLIST_MAX_TRACKS,
) -> list[dict]:

    options = YTDL_OPTIONS.copy()

    # 재생목록 전체를 가져오기 위해 False
    options["noplaylist"] = False

    # 문제가 있는 영상은 건너뛰기
    options["ignoreerrors"] = True

    # 최대 100곡
    options["playlistend"] = limit

    # 실제 음원 다운로드/추출은 하지 않음
    options["extract_flat"] = True

    with yt_dlp.YoutubeDL(options) as ydl:

        info = ydl.extract_info(
            url,
            download=False,
        )

        if not info:
            raise RuntimeError(
                "YouTube 재생목록을 가져오지 못했습니다."
            )

        entries = info.get("entries") or []

        results = []

        for entry in entries:

            # 삭제 / 비공개 / 정책상 접근 불가 영상
            if not entry:
                continue

            # Shorts 제외
            webpage_url = (
                entry.get("webpage_url")
                or entry.get("url")
            )

            if not webpage_url:
                continue

            parsed = urlparse(webpage_url)

            # YouTube Shorts 제외
            if "/shorts/" in parsed.path.lower():
                continue

            title = (
                entry.get("title")
                or "알 수 없는 제목"
            )

            duration = entry.get("duration")

            results.append({
                "title": title,
                "webpage_url": webpage_url,
                "duration": duration,
            })

            if len(results) >= limit:
                break

        return results


async def extract_playlist(
    url: str,
    limit: int = PLAYLIST_MAX_TRACKS,
) -> list[dict]:

    loop = asyncio.get_running_loop()

    return await loop.run_in_executor(
        None,
        extract_playlist_sync,
        url,
        limit,
    )


# ============================================================
# 음악 컨트롤 버튼
# ============================================================

class MusicControlView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    # --------------------------------------------------------
    # 일시정지
    # --------------------------------------------------------

    @discord.ui.button(
        label="일시정지",
        emoji="⏸️",
        style=discord.ButtonStyle.secondary,
    )
    async def pause_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        guild = get_guild(interaction)
        player = bot.get_player(guild.id)

        if player.pause():

            await interaction.response.send_message(
                "⏸️ 음악을 일시정지했습니다.",
                ephemeral=True,
            )

        else:

            await interaction.response.send_message(
                "❌ 현재 재생 중인 음악이 없습니다.",
                ephemeral=True,
            )

    # --------------------------------------------------------
    # 다시재생
    # --------------------------------------------------------

    @discord.ui.button(
        label="다시재생",
        emoji="▶️",
        style=discord.ButtonStyle.success,
    )
    async def resume_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        guild = get_guild(interaction)
        player = bot.get_player(guild.id)

        if player.resume():

            await interaction.response.send_message(
                "▶️ 음악을 다시 재생합니다.",
                ephemeral=True,
            )

        else:

            await interaction.response.send_message(
                "❌ 일시정지된 음악이 없습니다.",
                ephemeral=True,
            )

    # --------------------------------------------------------
    # 스킵
    # --------------------------------------------------------

    @discord.ui.button(
        label="스킵",
        emoji="⏭️",
        style=discord.ButtonStyle.primary,
    )
    async def skip_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        guild = get_guild(interaction)
        player = bot.get_player(guild.id)

        if not player.current:

            await interaction.response.send_message(
                "❌ 현재 재생 중인 음악이 없습니다.",
                ephemeral=True,
            )

            return

        if player.skip():

            await interaction.response.send_message(
                "⏭️ 현재 음악을 건너뜁니다.",
                ephemeral=True,
            )

        else:

            await interaction.response.send_message(
                "❌ 현재 음악을 스킵할 수 없습니다.",
                ephemeral=True,
            )

    # --------------------------------------------------------
    # 셔플
    # --------------------------------------------------------

    @discord.ui.button(
        label="셔플",
        emoji="🔀",
        style=discord.ButtonStyle.secondary,
    )
    async def shuffle_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        guild = get_guild(interaction)
        player = bot.get_player(guild.id)

        if len(player.queue) < 2:

            await interaction.response.send_message(
                "❌ 셔플하려면 대기열에 최소 2곡이 필요합니다.",
                ephemeral=True,
            )

            return

        queue = list(player.queue)

        random.shuffle(queue)

        player.queue.clear()
        player.queue.extend(queue)

        await interaction.response.send_message(
            "🔀 대기열을 무작위로 섞었습니다.",
            ephemeral=True,
        )

    # --------------------------------------------------------
    # 정지
    # --------------------------------------------------------

    @discord.ui.button(
        label="정지",
        emoji="⏹️",
        style=discord.ButtonStyle.danger,
    )
    async def stop_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        guild = get_guild(interaction)
        player = bot.get_player(guild.id)

        if not player.voice:

            await interaction.response.send_message(
                "❌ 현재 음성 채널에 연결되어 있지 않습니다.",
                ephemeral=True,
            )

            return

        await player.stop()

        await interaction.response.send_message(
            "⏹️ 음악을 정지하고 대기열을 비웠습니다.",
            ephemeral=True,
        )


# ============================================================
# 검색 결과 View
# ============================================================

class SearchResultView(discord.ui.View):

    def __init__(
        self,
        interaction: discord.Interaction,
        results: list[dict],
    ):
        super().__init__(timeout=60)

        self.original_user_id = interaction.user.id
        self.results = results
        self.selected = False

        for index, result in enumerate(results):

            button = discord.ui.Button(
                label=str(index + 1),
                style=discord.ButtonStyle.primary,
                custom_id=f"youtube_search_{index}",
            )

            button.callback = self.make_callback(index)

            self.add_item(button)

    def make_callback(self, index: int):

        async def callback(
            interaction: discord.Interaction,
        ):

            if interaction.user.id != self.original_user_id:

                await interaction.response.send_message(
                    "❌ 이 검색 결과는 검색한 사람만 선택할 수 있습니다.",
                    ephemeral=True,
                )

                return

            if self.selected:

                await interaction.response.send_message(
                    "❌ 이미 선택된 검색 결과입니다.",
                    ephemeral=True,
                )

                return

            self.selected = True

            result = self.results[index]

            for child in self.children:
                child.disabled = True

            await interaction.response.edit_message(
                content=(
                    f"⏳ **{result.get('title', '알 수 없는 제목')}**\n"
                    f"선택했습니다. 음악을 대기열에 추가합니다."
                ),
                view=self,
            )

            try:

                await add_selected_track(
                    interaction,
                    result,
                )

            except Exception as e:

                print(
                    f"[SEARCH SELECT ERROR] "
                    f"{type(e).__name__}: {e}"
                )

                await interaction.followup.send(
                    f"❌ 음악을 추가할 수 없습니다.\n"
                    f"`{e}`",
                    ephemeral=True,
                )

        return callback

    async def on_timeout(self):

        for child in self.children:
            child.disabled = True


# ============================================================
# 검색 결과 선택 후 음악 추가
# ============================================================

async def add_selected_track(
    interaction: discord.Interaction,
    info: dict,
):

    guild = get_guild(interaction)
    channel = get_user_voice_channel(interaction)

    player = bot.get_player(guild.id)

    title = (
        info.get("title")
        or "알 수 없는 제목"
    )

    webpage_url = get_webpage_url(info)

    if not webpage_url:
        raise RuntimeError(
            "선택한 영상의 URL을 가져오지 못했습니다."
        )

    if is_youtube_shorts_url(webpage_url):
        raise RuntimeError(
            "YouTube Shorts는 재생할 수 없습니다."
        )

    duration = info.get("duration")

    track = Track(
        title=title,
        webpage_url=webpage_url,
        duration=duration,
        requester=interaction.user.display_name,
        requester_id=interaction.user.id,
        channel_id=interaction.channel_id,
    )

    # 음성 채널 연결
    await player.connect(channel)

    was_empty = (
        player.current is None
        and len(player.queue) == 0
    )

    player.add(track)
    player.start_worker()

    if was_empty:

        await interaction.followup.edit_message(
            interaction.message.id,
            content=(
                f"🎵 **{title}**\n"
                f"재생을 시작합니다."
            ),
            view=None,
        )

    else:

        position = len(player.queue)

        await interaction.followup.edit_message(
            interaction.message.id,
            content=(
                f"🎵 **{title}**\n"
                f"대기열에 추가했습니다.\n"
                f"현재 대기 순번: **{position}**"
            ),
            view=None,
        )


# ============================================================
# Player
# ============================================================

class GuildPlayer:

    def __init__(
        self,
        bot: commands.Bot,
        guild_id: int,
    ):

        self.bot = bot
        self.guild_id = guild_id

        self.queue: deque[Track] = deque()

        self.voice: discord.VoiceClient | None = None
        self.current: Track | None = None

        self.volume: float = 0.6

        self.worker_task: asyncio.Task | None = None

        self.repeat_mode: str = "off"

        # 스킵 투표
        self.skip_votes: set[int] = set()

        # 재생 위치
        self.playback_started_at: float | None = None
        self.paused_at: float | None = None
        self.paused_duration: float = 0.0

        self._stop_requested = False

        # 현재곡 임베드
        self.current_message: discord.Message | None = None
        self.progress_task: asyncio.Task | None = None

    # --------------------------------------------------------
    # 음성 채널
    # --------------------------------------------------------

    async def connect(
        self,
        channel: discord.VoiceChannel,
    ):

        if self.voice and self.voice.is_connected():

            if self.voice.channel.id == channel.id:
                return self.voice

            raise RuntimeError(
                f"봇은 이미 다른 음성 채널 "
                f"({self.voice.channel.name})에 있습니다."
            )

        self.voice = await channel.connect()

        return self.voice

    # --------------------------------------------------------
    # Queue
    # --------------------------------------------------------

    def add(
        self,
        track: Track,
    ):
        self.queue.append(track)

    def add_many(
        self,
        tracks: list[Track],
    ):
        self.queue.extend(tracks)

    def clear_queue(self):
        self.queue.clear()

    def clear_skip_votes(self):
        self.skip_votes.clear()

    def set_repeat_mode(
        self,
        mode: str,
    ):

        if mode not in (
            "off",
            "one",
            "all",
        ):

            raise ValueError(
                f"잘못된 반복 모드입니다: {mode}"
            )

        self.repeat_mode = mode

    # --------------------------------------------------------
    # 재생 위치
    # --------------------------------------------------------

    def get_playback_position(
        self,
    ) -> float:

        if not self.current:
            return 0.0

        if self.playback_started_at is None:
            return 0.0

        now = asyncio.get_running_loop().time()

        if self.paused_at is not None:
            now = self.paused_at

        position = (
            now
            - self.playback_started_at
            - self.paused_duration
        )

        return max(
            0.0,
            position,
        )

    # --------------------------------------------------------
    # 진행률
    # --------------------------------------------------------

    def get_progress_percent(
        self,
    ) -> float:

        if not self.current:
            return 0.0

        if not self.current.duration:
            return 0.0

        position = self.get_playback_position()

        return min(
            100.0,
            (
                position
                / self.current.duration
            ) * 100,
        )

    def make_progress_bar(
        self,
        length: int = 20,
    ) -> str:

        percent = self.get_progress_percent()

        position = int(
            (length - 1)
            * percent
            / 100
        )

        bar = []

        for index in range(length):

            if index == position:
                bar.append("🔵")

            else:
                bar.append("▬")

        return "".join(bar)

    @staticmethod
    def format_time(
        seconds: float | int | None,
    ) -> str:

        if seconds is None:
            return "0:00"

        seconds = max(
            0,
            int(seconds),
        )

        hours = seconds // 3600
        minutes = (
            seconds % 3600
        ) // 60
        secs = seconds % 60

        if hours > 0:

            return (
                f"{hours}:"
                f"{minutes:02d}:"
                f"{secs:02d}"
            )

        return (
            f"{minutes}:"
            f"{secs:02d}"
        )

    # --------------------------------------------------------
    # 현재곡 Embed
    # --------------------------------------------------------

    def create_now_playing_embed(
        self,
    ) -> discord.Embed:

        if not self.current:

            return discord.Embed(
                title="🎵 현재 재생 중",
                description=(
                    "현재 재생 중인 음악이 없습니다."
                ),
                color=discord.Color.blurple(),
            )

        track = self.current

        position = (
            self.get_playback_position()
        )

        percent = (
            self.get_progress_percent()
        )

        progress_bar = (
            self.make_progress_bar()
        )

        current_time = (
            self.format_time(position)
        )

        total_time = (
            self.format_time(track.duration)
        )

        # 상태
        if (
            self.voice
            and self.voice.is_paused()
        ):

            status = "⏸️ 일시정지됨"
            color = discord.Color.gold()

        elif (
            self.voice
            and self.voice.is_playing()
        ):

            status = "▶️ 재생 중"
            color = discord.Color.blurple()

        else:

            status = "⏹️ 정지됨"
            color = discord.Color.red()

        embed = discord.Embed(
            title="🎵 현재 재생 중",
            description=(
                f"**[{track.title}]"
                f"({track.webpage_url})**"
            ),
            color=color,
        )

        embed.add_field(
            name="상태",
            value=status,
            inline=True,
        )

        embed.add_field(
            name="👤 신청자",
            value=f"`{track.requester}`",
            inline=True,
        )

        embed.add_field(
            name="🔊 볼륨",
            value=(
                f"`{int(self.volume * 100)}%`"
            ),
            inline=True,
        )

        embed.add_field(
            name="⏱️ 진행률",
            value=(
                f"{progress_bar}\n"
                f"`{current_time} / {total_time}` "
                f"**{percent:.0f}%**"
            ),
            inline=False,
        )

        # 다음 곡
        if self.queue:

            embed.add_field(
                name="📜 다음 곡",
                value=(
                    f"`{self.queue[0].title}`\n"
                    f"외 **{len(self.queue) - 1}곡**"
                    if len(self.queue) > 1
                    else (
                        f"`{self.queue[0].title}`"
                    )
                ),
                inline=False,
            )

        embed.set_footer(
            text="쁘띠영성 🎵"
        )

        return embed

    # --------------------------------------------------------
    # 현재곡 Embed 자동 갱신
    # --------------------------------------------------------

    def start_progress_updater(
        self,
        message: discord.Message,
    ):

        if (
            self.progress_task
            and not self.progress_task.done()
        ):

            self.progress_task.cancel()

        self.current_message = message

        self.progress_task = asyncio.create_task(
            self._progress_updater()
        )

    async def _progress_updater(self):

        try:

            while self.current:

                message = (
                    self.current_message
                )

                if not message:
                    break

                try:

                    await message.edit(
                        embed=(
                            self.create_now_playing_embed()
                        ),
                        view=MusicControlView(),
                    )

                except discord.NotFound:

                    break

                except discord.HTTPException as e:

                    print(
                        f"[PROGRESS UPDATE ERROR] "
                        f"{e}"
                    )

                await asyncio.sleep(2)

        except asyncio.CancelledError:

            pass

        finally:

            self.current_message = None

    # --------------------------------------------------------
    # Worker 시작
    # --------------------------------------------------------

    def start_worker(self):

        if (
            self.worker_task is None
            or self.worker_task.done()
        ):

            self._stop_requested = False

            self.worker_task = asyncio.create_task(
                self._worker()
            )

    # --------------------------------------------------------
    # Worker
    # --------------------------------------------------------

    async def _worker(self):

        while (
            self.queue
            and not self._stop_requested
        ):

            if (
                not self.voice
                or not self.voice.is_connected()
            ):
                break

            track = self.queue.popleft()

            self.current = track

            try:

                await self._play_track(
                    track
                )

            except asyncio.CancelledError:

                raise

            except Exception as e:

                print(
                    f"[PLAYER ERROR] "
                    f"{track.title}: "
                    f"{type(e).__name__}: {e}"
                )

            finally:

                if (
                    self.progress_task
                    and not self.progress_task.done()
                ):

                    self.progress_task.cancel()

                self.progress_task = None
                self.current_message = None

                self.current = None

                self.clear_skip_votes()

            # ------------------------------------------------
            # 반복
            # ------------------------------------------------

            if self._stop_requested:
                break

            if self.repeat_mode == "one":

                self.queue.appendleft(
                    track
                )

            elif self.repeat_mode == "all":

                self.queue.append(
                    track
                )

        self.current = None

        # ----------------------------------------------------
        # 대기열이 비면 자동 퇴장
        # ----------------------------------------------------

        if (
            not self.queue
            and not self._stop_requested
            and self.voice
            and self.voice.is_connected()
        ):

            print(
                "[PLAYER] 대기열이 비어 "
                "음성 채널에서 나갑니다."
            )

            await self.voice.disconnect()

            self.voice = None

    # --------------------------------------------------------
    # 실제 재생
    # --------------------------------------------------------

    async def _play_track(
        self,
        track: Track,
    ):

        print(
            f"[PLAY] {track.title}"
        )

        # ----------------------------------------------------
        # 중요:
        # 저장된 webpage_url은 영구 스트림 URL이 아니다.
        # 실제 재생 직전에 다시 추출한다.
        # ----------------------------------------------------

        info = await extract_info(
            track.webpage_url
        )

        stream_url = info.get("url")

        if not stream_url:

            raise RuntimeError(
                "오디오 스트림 URL을 가져오지 못했습니다."
            )

        if (
            not self.voice
            or not self.voice.is_connected()
        ):

            raise RuntimeError(
                "Discord 음성 채널 연결이 끊어졌습니다."
            )

        ffmpeg_before_options = (
            "-reconnect 1 "
            "-reconnect_streamed 1 "
            "-reconnect_delay_max 5"
        )

        source = discord.FFmpegPCMAudio(
            stream_url,
            executable=FFMPEG_PATH,
            before_options=(
                ffmpeg_before_options
            ),
            options="-vn",
        )

        volume_source = (
            discord.PCMVolumeTransformer(
                source,
                volume=self.volume,
            )
        )

        finished = asyncio.Event()

        playback_error: list[
            Exception | None
        ] = [None]

        def after_play(
            error: Exception | None,
        ):

            playback_error[0] = error

            self.bot.loop.call_soon_threadsafe(
                finished.set
            )

        self.playback_started_at = (
            asyncio.get_running_loop().time()
        )

        self.paused_at = None
        self.paused_duration = 0.0

        self.voice.play(
            volume_source,
            after=after_play,
        )

        # ----------------------------------------------------
        # 현재곡 Embed
        # ----------------------------------------------------

        try:

            channel = self.bot.get_channel(
                track.channel_id
            )

            if channel is None:

                channel = await self.bot.fetch_channel(
                    track.channel_id
                )

            if isinstance(
                channel,
                discord.TextChannel,
            ):

                message = await channel.send(
                    embed=(
                        self.create_now_playing_embed()
                    ),
                    view=MusicControlView(),
                )

                self.current_message = message

                self.start_progress_updater(
                    message
                )

        except Exception as e:

            print(
                f"[NOW PLAYING MESSAGE ERROR] "
                f"{type(e).__name__}: {e}"
            )

        await finished.wait()

        if playback_error[0]:

            raise playback_error[0]

    # --------------------------------------------------------
    # Pause
    # --------------------------------------------------------

    def pause(self) -> bool:

        if (
            self.voice
            and self.voice.is_playing()
        ):

            self.paused_at = (
                asyncio.get_running_loop().time()
            )

            self.voice.pause()

            return True

        return False

    # --------------------------------------------------------
    # Resume
    # --------------------------------------------------------

    def resume(self) -> bool:

        if (
            self.voice
            and self.voice.is_paused()
        ):

            if self.paused_at is not None:

                now = (
                    asyncio.get_running_loop().time()
                )

                self.paused_duration += (
                    now - self.paused_at
                )

                self.paused_at = None

            self.voice.resume()

            return True

        return False

    # --------------------------------------------------------
    # Skip
    # --------------------------------------------------------

    def skip(self) -> bool:

        if (
            self.voice
            and self.voice.is_playing()
        ):

            self.voice.stop()

            return True

        return False

    # --------------------------------------------------------
    # Stop
    # --------------------------------------------------------

    async def stop(self):

        self._stop_requested = True

        self.queue.clear()
        self.clear_skip_votes()

        if self.voice:

            if (
                self.voice.is_playing()
                or self.voice.is_paused()
            ):

                self.voice.stop()

            if self.voice.is_connected():

                await self.voice.disconnect()

        self.voice = None
        self.current = None

    # --------------------------------------------------------
    # Volume
    # --------------------------------------------------------

    def set_volume(
        self,
        percent: int,
    ):

        percent = max(
            0,
            min(percent, 100),
        )

        self.volume = percent / 100

        if (
            self.voice
            and self.voice.source
            and isinstance(
                self.voice.source,
                discord.PCMVolumeTransformer,
            )
        ):

            self.voice.source.volume = (
                self.volume
            )


# ============================================================
# Bot
# ============================================================

intents = discord.Intents.default()
intents.voice_states = True


class MusicBot(commands.Bot):

    def __init__(self):

        super().__init__(
            command_prefix="!",
            intents=intents,
        )

        self.players: dict[
            int,
            GuildPlayer,
        ] = {}

    async def setup_hook(self):

        print(
            "========== SETUP HOOK 실행 =========="
        )

        print(
            f"[COMMANDS] 대상 서버 ID: "
            f"{GUILD_ID}"
        )

        commands = self.tree.get_commands(
            guild=GUILD
        )

        print(
            f"[COMMANDS] 현재 Tree에 등록된 명령어: "
            f"{len(commands)}개"
        )

        for command in commands:

            print(
                f"  - /{command.name}"
            )

        synced = await self.tree.sync(
            guild=GUILD
        )

        print(
            f"[COMMANDS] Discord 동기화 완료: "
            f"{len(synced)}개"
        )

        for command in synced:

            print(
                f"  ✓ /{command.name}"
            )

    def get_player(
        self,
        guild_id: int,
    ) -> GuildPlayer:

        if guild_id not in self.players:

            self.players[guild_id] = GuildPlayer(
                self,
                guild_id,
            )

        return self.players[guild_id]


bot = MusicBot()


# ============================================================
# 공통 검사
# ============================================================

def get_guild(
    interaction: discord.Interaction,
) -> discord.Guild:

    if not interaction.guild:

        raise RuntimeError(
            "이 명령어는 서버에서만 사용할 수 있습니다."
        )

    return interaction.guild


def get_user_voice_channel(
    interaction: discord.Interaction,
) -> discord.VoiceChannel:

    if not interaction.user.voice:

        raise RuntimeError(
            "먼저 음성 채널에 들어가 주세요."
        )

    channel = interaction.user.voice.channel

    if not isinstance(
        channel,
        discord.VoiceChannel,
    ):

        raise RuntimeError(
            "일반 음성 채널에서 사용해 주세요."
        )

    return channel


# ============================================================
# Embed
# ============================================================

def create_music_embed(
    title: str,
    description: str | None = None,
    color: discord.Color = discord.Color.blurple(),
) -> discord.Embed:

    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
    )

    embed.set_footer(
        text="쁘띠영성"
    )

    return embed


# ============================================================
# Track 생성
# ============================================================

def make_track(
    info: dict,
    interaction: discord.Interaction,
) -> Track | None:

    title = (
        info.get("title")
        or "알 수 없는 제목"
    )

    webpage_url = get_webpage_url(info)

    if not webpage_url:
        return None

    # Shorts 제외
    if is_youtube_shorts_url(
        webpage_url
    ):
        return None

    duration = info.get("duration")

    return Track(
        title=title,
        webpage_url=webpage_url,
        duration=duration,
        requester=(
            interaction.user.display_name
        ),
        requester_id=interaction.user.id,
        channel_id=interaction.channel_id,
    )


# ============================================================
# /재생
# ============================================================

@app_commands.command(
    name="재생",
    description="YouTube 음악을 검색하거나 URL로 재생합니다.",
)
@app_commands.guilds(GUILD)
@app_commands.describe(
    검색어="YouTube URL 또는 음악 검색어",
)
async def 재생(
    interaction: discord.Interaction,
    검색어: str,
):

    await interaction.response.defer()

    try:

        guild = get_guild(interaction)
        channel = get_user_voice_channel(
            interaction
        )

        player = bot.get_player(
            guild.id
        )

        # ====================================================
        # URL
        # ====================================================

        if is_url(검색어):

            player = bot.get_player(guild.id)

            # --------------------------------------------------------
            # 재생목록인지 먼저 확인
            # --------------------------------------------------------

            is_playlist = (
                "list=" in 검색어
                or "playlist" in 검색어.lower()
            )

            # ========================================================
            # 재생목록
            # ========================================================

            if is_playlist:

                await interaction.followup.send(
                    "📋 재생목록을 불러오는 중입니다...\n"
                    "최대 100곡까지 확인합니다."
                )

                try:

                    playlist_tracks = await extract_playlist(
                        검색어,
                        limit=100,
                    )

                except Exception as e:

                    print(
                        f"[PLAYLIST ERROR] "
                        f"{type(e).__name__}: {e}"
                    )

                    await interaction.followup.send(
                        "❌ 재생목록을 불러오지 못했습니다.\n"
                        f"`{e}`"
                    )

                    return

                # ----------------------------------------------------
                # 정상적으로 가져온 곡이 하나도 없는 경우
                # ----------------------------------------------------

                if not playlist_tracks:

                    await interaction.followup.send(
                        "❌ 재생목록에서 재생 가능한 영상을 찾지 못했습니다.\n"
                        "삭제되었거나 비공개/정책상 재생할 수 없는 영상만 "
                        "포함되어 있을 수 있습니다."
                    )

                    return

                # ----------------------------------------------------
                # 음성 채널 연결
                # ----------------------------------------------------

                await player.connect(channel)

                added_count = 0

                was_empty = (
                    player.current is None
                    and len(player.queue) == 0
                )

                # ----------------------------------------------------
                # 재생목록 곡 추가
                # ----------------------------------------------------

                for info in playlist_tracks:

                    title = (
                        info.get("title")
                        or "알 수 없는 제목"
                    )

                    webpage_url = (
                        info.get("webpage_url")
                        or info.get("original_url")
                        or info.get("url")
                    )

                    if not webpage_url:
                        continue

                    duration = info.get("duration")

                    track = Track(
                        title=title,
                        webpage_url=webpage_url,
                        duration=duration,
                        requester=interaction.user.display_name,
                        requester_id=interaction.user.id,
                        channel_id=interaction.channel_id,
                    )

                    player.add(track)

                    added_count += 1

                # ----------------------------------------------------
                # Worker 시작
                # ----------------------------------------------------

                if added_count > 0:
                    player.start_worker()

                # ----------------------------------------------------
                # 결과 출력
                # ----------------------------------------------------

                skipped_count = (
                    len(playlist_tracks) - added_count
                )

                if was_empty:

                    await interaction.followup.send(
                        f"📋 **재생목록을 불러왔습니다.**\n\n"
                        f"🎵 추가된 곡: **{added_count}곡**\n"
                        f"▶️ 첫 번째 곡부터 재생을 시작합니다."
                    )

                else:

                    await interaction.followup.send(
                        f"📋 **재생목록을 대기열에 추가했습니다.**\n\n"
                        f"🎵 추가된 곡: **{added_count}곡**\n"
                        f"📜 현재 대기열: **{len(player.queue)}곡**"
                    )

                return

            # ========================================================
            # 일반 YouTube 영상
            # ========================================================

            info = await extract_info(검색어)

            # Shorts 제외
            webpage_url = (
                info.get("webpage_url")
                or info.get("original_url")
                or 검색어
            )

            if "/shorts/" in webpage_url.lower():

                await interaction.followup.send(
                    "❌ YouTube Shorts는 재생할 수 없습니다."
                )

                return

            await player.connect(channel)

            title = (
                info.get("title")
                or "알 수 없는 제목"
            )

            duration = info.get("duration")

            track = Track(
                title=title,
                webpage_url=webpage_url,
                duration=duration,
                requester=interaction.user.display_name,
                requester_id=interaction.user.id,
                channel_id=interaction.channel_id,
            )

            was_empty = (
                player.current is None
                and len(player.queue) == 0
            )

            player.add(track)
            player.start_worker()

            if was_empty:

                message = (
                    f"🎵 **{title}**\n"
                    f"재생을 시작합니다."
                )

            else:

                position = len(player.queue)

                message = (
                    f"🎵 **{title}**\n"
                    f"대기열에 추가했습니다.\n"
                    f"현재 대기 순번: **{position}**"
                )

            await interaction.followup.send(
                message
            )

            return


        # ====================================================
        # 검색어
        # ====================================================

        results = await search_youtube(
            검색어,
            limit=5,
        )

        if not results:

            await interaction.followup.send(
                "❌ 검색 결과가 없습니다."
            )

            return

        lines = [
            "🔎 **YouTube 검색 결과**",
            "",
            "원하는 음악의 번호를 선택해주세요.",
            "",
        ]

        for index, result in enumerate(
            results,
            start=1,
        ):

            title = (
                result.get("title")
                or "알 수 없는 제목"
            )

            channel_name = (
                result.get("channel")
                or result.get("uploader")
                or "알 수 없는 채널"
            )

            duration = result.get(
                "duration"
            )

            if duration:

                minutes = duration // 60
                seconds = duration % 60

                duration_text = (
                    f"{minutes}:{seconds:02d}"
                )

            else:

                duration_text = "알 수 없음"

            lines.append(
                f"**{index}.** {title}\n"
                f"   🎤 {channel_name} "
                f"· ⏱️ {duration_text}"
            )

        view = SearchResultView(
            interaction,
            results,
        )

        await interaction.followup.send(
            "\n".join(lines),
            view=view,
        )

    except Exception as e:

        print(
            f"[COMMAND ERROR] /재생: "
            f"{type(e).__name__}: {e}"
        )

        await interaction.followup.send(
            f"❌ 재생할 수 없습니다.\n"
            f"`{e}`"
        )


# ============================================================
# /일시정지
# ============================================================

@app_commands.command(
    name="일시정지",
    description="현재 음악을 일시정지합니다.",
)
@app_commands.guilds(GUILD)
async def 일시정지(
    interaction: discord.Interaction,
):

    guild = get_guild(interaction)

    player = bot.get_player(
        guild.id
    )

    if player.pause():

        await interaction.response.send_message(
            "⏸️ 음악을 일시정지했습니다."
        )

    else:

        await interaction.response.send_message(
            "❌ 현재 재생 중인 음악이 없습니다."
        )


# ============================================================
# /다시재생
# ============================================================

@app_commands.command(
    name="다시재생",
    description="일시정지된 음악을 다시 재생합니다.",
)
@app_commands.guilds(GUILD)
async def 다시재생(
    interaction: discord.Interaction,
):

    guild = get_guild(interaction)

    player = bot.get_player(
        guild.id
    )

    if player.resume():

        await interaction.response.send_message(
            "▶️ 음악을 다시 재생합니다."
        )

    else:

        await interaction.response.send_message(
            "❌ 일시정지된 음악이 없습니다."
        )


# ============================================================
# /스킵
# ============================================================

@app_commands.command(
    name="스킵",
    description="현재 음악을 건너뜁니다. 일반 사용자는 투표가 필요합니다.",
)
@app_commands.guilds(GUILD)
async def 스킵(
    interaction: discord.Interaction,
):

    guild = get_guild(interaction)

    channel = get_user_voice_channel(
        interaction
    )

    player = bot.get_player(
        guild.id
    )

    # --------------------------------------------------------
    # 현재 재생 확인
    # --------------------------------------------------------

    if not player.current:

        await interaction.response.send_message(
            "❌ 현재 재생 중인 음악이 없습니다."
        )

        return

    # --------------------------------------------------------
    # 봇 연결 확인
    # --------------------------------------------------------

    if (
        not player.voice
        or not player.voice.is_connected()
    ):

        await interaction.response.send_message(
            "❌ 봇이 음성 채널에 연결되어 있지 않습니다."
        )

        return

    if (
        player.voice.channel.id
        != channel.id
    ):

        await interaction.response.send_message(
            "❌ 봇과 같은 음성 채널에 있어야 합니다."
        )

        return

    # --------------------------------------------------------
    # 실제 사용자 수
    # --------------------------------------------------------

    members = [
        member
        for member in channel.members
        if not member.bot
    ]

    member_count = len(members)

    if member_count == 0:

        await interaction.response.send_message(
            "❌ 음성 채널에 사용자가 없습니다."
        )

        return

    # --------------------------------------------------------
    # 관리자 / 신청자 / Owner
    # --------------------------------------------------------

    is_admin = (
        interaction.user
        .guild_permissions
        .administrator
    )

    is_requester = (
        player.current.requester_id
        == interaction.user.id
    )

    is_owner = (
        interaction.user.id
        == OWNER_ID
    )

    # --------------------------------------------------------
    # 즉시 스킵
    # --------------------------------------------------------

    if (
        is_admin
        or is_requester
        or is_owner
    ):

        player.clear_skip_votes()

        if player.skip():

            if (
                is_admin
                and is_requester
            ):

                reason = (
                    "관리자이자 현재 곡 신청자"
                )

            elif is_admin:

                reason = "관리자"

            elif is_owner:

                reason = "봇 소유자"

            else:

                reason = "현재 곡 신청자"

            await interaction.response.send_message(
                f"⏭️ **{reason}**에 의해 "
                f"현재 음악을 건너뜁니다."
            )

        else:

            await interaction.response.send_message(
                "❌ 현재 음악을 건너뛸 수 없습니다."
            )

        return

    # --------------------------------------------------------
    # 일반 사용자 투표
    # --------------------------------------------------------

    user_id = interaction.user.id

    if user_id in player.skip_votes:

        await interaction.response.send_message(
            "❌ 이미 스킵 투표에 참여했습니다.",
            ephemeral=True,
        )

        return

    player.skip_votes.add(
        user_id
    )

    required_votes = math.ceil(
        member_count / 2
    )

    current_votes = len(
        player.skip_votes
    )

    # --------------------------------------------------------
    # 과반수
    # --------------------------------------------------------

    if current_votes >= required_votes:

        player.clear_skip_votes()

        if player.skip():

            await interaction.response.send_message(
                f"⏭️ 스킵 투표가 완료되었습니다.\n"
                f"🗳️ **{current_votes} / {required_votes}표**\n"
                f"현재 음악을 건너뜁니다."
            )

        else:

            await interaction.response.send_message(
                "❌ 현재 음악을 건너뛸 수 없습니다."
            )

        return

    # --------------------------------------------------------
    # 투표 진행
    # --------------------------------------------------------

    await interaction.response.send_message(
        f"⏭️ **스킵 투표**\n"
        f"🎵 **{player.current.title}**\n\n"
        f"🗳️ **{current_votes} / "
        f"{required_votes}표**\n"
        f"과반수의 동의가 필요합니다."
    )


# ============================================================
# /정지
# ============================================================

@app_commands.command(
    name="정지",
    description="음악을 정지하고 대기열을 비운 뒤 퇴장합니다.",
)
@app_commands.guilds(GUILD)
async def 정지(
    interaction: discord.Interaction,
):

    guild = get_guild(interaction)

    player = bot.get_player(
        guild.id
    )

    if not player.voice:

        await interaction.response.send_message(
            "❌ 현재 음성 채널에 연결되어 있지 않습니다."
        )

        return

    await player.stop()

    await interaction.response.send_message(
        "⏹️ 음악을 정지하고 대기열을 비웠습니다."
    )


# ============================================================
# /대기열
# ============================================================

@app_commands.command(
    name="대기열",
    description="현재 음악 대기열을 확인합니다.",
)
@app_commands.guilds(GUILD)
async def 대기열(
    interaction: discord.Interaction,
):

    guild = get_guild(interaction)

    player = bot.get_player(
        guild.id
    )

    lines = []

    repeat_names = {
        "off": "▶️ 꺼짐",
        "one": "🔁 한곡 반복",
        "all": "🔂 전체 반복",
    }

    repeat_mode = repeat_names.get(
        player.repeat_mode,
        "▶️ 꺼짐",
    )

    lines.append(
        f"🔄 **반복 모드:** "
        f"{repeat_mode}"
    )

    # --------------------------------------------------------
    # 현재 재생
    # --------------------------------------------------------

    if player.current:

        lines.append(
            f"\n🎵 **현재 재생**\n"
            f"**{player.current.title}**"
        )

    # --------------------------------------------------------
    # 대기열
    # --------------------------------------------------------

    if player.queue:

        lines.append(
            "\n📜 **대기열**"
        )

        for index, track in enumerate(
            player.queue,
            start=1,
        ):

            lines.append(
                f"`{index}.` {track.title}"
            )

    # --------------------------------------------------------
    # 없음
    # --------------------------------------------------------

    if (
        not player.current
        and not player.queue
    ):

        await interaction.response.send_message(
            "📭 현재 재생 중인 음악과 대기열이 없습니다."
        )

        return

    await interaction.response.send_message(
        "\n".join(lines)
    )


# ============================================================
# /대기열삭제
# ============================================================

@app_commands.command(
    name="대기열삭제",
    description="대기열에서 원하는 음악을 삭제합니다.",
)
@app_commands.guilds(GUILD)
async def 대기열삭제(
    interaction: discord.Interaction,
    번호: app_commands.Range[int, 1, 100],
):

    guild = get_guild(interaction)

    player = bot.get_player(
        guild.id
    )

    if not player.queue:

        await interaction.response.send_message(
            "📭 현재 대기열에 음악이 없습니다."
        )

        return

    queue = list(
        player.queue
    )

    if 번호 > len(queue):

        await interaction.response.send_message(
            f"❌ 대기열에는 현재 "
            f"{len(queue)}곡만 있습니다."
        )

        return

    removed = queue.pop(
        번호 - 1
    )

    player.queue.clear()
    player.queue.extend(queue)

    await interaction.response.send_message(
        f"🗑️ 대기열에서 "
        f"**{removed.title}**을(를) 삭제했습니다."
    )


# ============================================================
# /대기열초기화
# ============================================================

@app_commands.command(
    name="대기열초기화",
    description="현재 대기열의 모든 음악을 삭제합니다.",
)
@app_commands.guilds(GUILD)
async def 대기열초기화(
    interaction: discord.Interaction,
):

    guild = get_guild(interaction)

    player = bot.get_player(
        guild.id
    )

    if not player.queue:

        await interaction.response.send_message(
            "📭 현재 대기열이 이미 비어 있습니다."
        )

        return

    count = len(
        player.queue
    )

    player.queue.clear()

    await interaction.response.send_message(
        f"🗑️ 대기열에서 "
        f"**{count}곡**을 모두 삭제했습니다."
    )


# ============================================================
# /셔플
# ============================================================

@app_commands.command(
    name="셔플",
    description="현재 음악 대기열을 무작위로 섞습니다.",
)
@app_commands.guilds(GUILD)
async def 셔플(
    interaction: discord.Interaction,
):

    guild = get_guild(interaction)

    player = bot.get_player(
        guild.id
    )

    if len(player.queue) < 2:

        await interaction.response.send_message(
            "❌ 셔플하려면 대기열에 최소 2곡이 필요합니다."
        )

        return

    queue = list(
        player.queue
    )

    random.shuffle(queue)

    player.queue.clear()
    player.queue.extend(queue)

    await interaction.response.send_message(
        "🔀 대기열을 무작위로 섞었습니다."
    )


# ============================================================
# /볼륨
# ============================================================

@app_commands.command(
    name="볼륨",
    description="음악 볼륨을 변경합니다. 0~100.",
)
@app_commands.guilds(GUILD)
@app_commands.describe(
    크기="볼륨을 0~100 사이로 입력하세요.",
)
async def 볼륨(
    interaction: discord.Interaction,
    크기: app_commands.Range[int, 0, 100],
):

    guild = get_guild(interaction)

    player = bot.get_player(
        guild.id
    )

    player.set_volume(
        크기
    )

    await interaction.response.send_message(
        f"🔊 볼륨을 **{크기}%**로 설정했습니다."
    )


# ============================================================
# /반복
# ============================================================

@app_commands.command(
    name="반복",
    description="반복 재생 모드를 설정합니다.",
)
@app_commands.guilds(GUILD)
@app_commands.describe(
    모드="반복 재생 모드를 선택합니다.",
)
@app_commands.choices(
    모드=[
        app_commands.Choice(
            name="꺼짐",
            value="off",
        ),
        app_commands.Choice(
            name="한곡",
            value="one",
        ),
        app_commands.Choice(
            name="전체",
            value="all",
        ),
    ]
)
async def 반복(
    interaction: discord.Interaction,
    모드: app_commands.Choice[str],
):

    guild = get_guild(interaction)

    player = bot.get_player(
        guild.id
    )

    player.set_repeat_mode(
        모드.value
    )

    mode_names = {
        "off": "꺼짐",
        "one": "한곡 반복",
        "all": "전체 반복",
    }

    await interaction.response.send_message(
        f"🔁 반복 모드가 "
        f"**{mode_names[모드.value]}**로 설정되었습니다."
    )


# ============================================================
# /도움말
# ============================================================

@app_commands.command(
    name="도움말",
    description="음악 봇의 사용 가능한 명령어를 확인합니다.",
)
@app_commands.guilds(GUILD)
async def 도움말(
    interaction: discord.Interaction,
):

    embed = discord.Embed(
        title="🎵 쁘띠영성 음악봇",
        description=(
            "사용 가능한 음악 명령어입니다.\n"
            "원하는 명령어를 `/`로 입력해서 사용할 수 있습니다."
        ),
        color=discord.Color.blue(),
    )

    embed.add_field(
        name="🎵 재생",
        value=(
            "`/재생 검색어`\n"
            "YouTube URL 또는 검색어로 음악을 재생합니다.\n\n"
            "YouTube 재생목록 URL을 입력하면 "
            f"최대 **{MAX_PLAYLIST_TRACKS}곡**을 추가합니다.\n"
            "Shorts는 자동으로 제외됩니다."
        ),
        inline=False,
    )

    embed.add_field(
        name="⏯️ 재생 제어",
        value=(
            "`/일시정지` — 현재 음악을 일시정지합니다.\n"
            "`/다시재생` — 일시정지된 음악을 다시 재생합니다.\n"
            "`/스킵` — 현재 음악을 건너뜁니다.\n"
            "`/정지` — 음악을 정지하고 대기열을 비운 뒤 퇴장합니다."
        ),
        inline=False,
    )

    embed.add_field(
        name="📜 대기열",
        value=(
            "`/대기열` — 현재 음악과 대기열을 확인합니다.\n"
            "`/대기열삭제 번호` — 원하는 대기열 음악을 삭제합니다.\n"
            "`/대기열초기화` — 대기열의 모든 음악을 삭제합니다.\n"
            "`/셔플` — 대기열을 무작위로 섞습니다."
        ),
        inline=False,
    )

    embed.add_field(
        name="🎚️ 기타",
        value=(
            "`/볼륨 크기` — 볼륨을 0~100%로 설정합니다.\n"
            "`/반복 모드` — 반복 재생 모드를 설정합니다."
        ),
        inline=False,
    )

    embed.set_footer(
        text="음성 채널에 아무도 없으면 봇이 자동으로 퇴장합니다."
    )

    await interaction.response.send_message(
        embed=embed
    )


# ============================================================
# 명령어 오류
# ============================================================

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):

    print(
        f"[APP COMMAND ERROR] "
        f"{type(error).__name__}: {error}"
    )

    message = (
        "❌ 명령어 실행 중 오류가 발생했습니다."
    )

    if isinstance(
        error,
        app_commands.CommandInvokeError,
    ):

        original = error.original

        if isinstance(
            original,
            RuntimeError,
        ):

            message = f"❌ {original}"

    try:

        if interaction.response.is_done():

            await interaction.followup.send(
                message,
                ephemeral=True,
            )

        else:

            await interaction.response.send_message(
                message,
                ephemeral=True,
            )

    except Exception:
        pass


# ============================================================
# 음성 채널 상태 감지
# ============================================================

@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):

    # 음성 채널을 나간 경우에만 확인
    if before.channel is None:
        return

    # 봇 자신의 이동/퇴장은 무시
    if member.bot:
        return

    guild = before.channel.guild

    player = bot.get_player(
        guild.id
    )

    # 봇이 연결되어 있지 않으면 무시
    if not player.voice:
        return

    if not player.voice.is_connected():
        return

    # 현재 봇이 있는 채널이 아니면 무시
    if (
        player.voice.channel.id
        != before.channel.id
    ):
        return

    # 사람만 확인
    human_members = [
        member
        for member in before.channel.members
        if not member.bot
    ]

    # 사람이 한 명도 없으면 자동 퇴장
    if not human_members:

        print(
            f"[PLAYER] 음성 채널에 사람이 없어 "
            f"자동으로 퇴장합니다. "
            f"Guild: {guild.id}"
        )

        await player.stop()


# ============================================================
# 실행
# ============================================================

if __name__ == "__main__":

    bot.tree.add_command(
        재생,
        guild=GUILD,
    )

    bot.tree.add_command(
        일시정지,
        guild=GUILD,
    )

    bot.tree.add_command(
        다시재생,
        guild=GUILD,
    )

    bot.tree.add_command(
        스킵,
        guild=GUILD,
    )

    bot.tree.add_command(
        정지,
        guild=GUILD,
    )

    bot.tree.add_command(
        대기열,
        guild=GUILD,
    )

    bot.tree.add_command(
        볼륨,
        guild=GUILD,
    )

    bot.tree.add_command(
        대기열삭제,
        guild=GUILD,
    )

    bot.tree.add_command(
        대기열초기화,
        guild=GUILD,
    )

    bot.tree.add_command(
        셔플,
        guild=GUILD,
    )

    bot.tree.add_command(
        반복,
        guild=GUILD,
    )

    bot.tree.add_command(
        도움말,
        guild=GUILD,
    )

    bot.run(TOKEN)

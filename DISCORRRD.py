import discord
from discord.ext import commands
import yt_dlp
import asyncio
import json
import os

# --- 1. 기초 설정 ---
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!', intents=intents)

CONFIG_FILE = 'config.json'
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"music_channels": {}}

def save_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4)

config_data = load_config()
players = {} 

YDL_OPTIONS = {'format': 'bestaudio', 'noplaylist': 'True'}
FFMPEG_PATH = r'C:\ffmpeg\bin\ffmpeg.exe' # 실제 경로로 수정 필수
FFMPEG_OPTIONS = {'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5', 'options': '-vn'}

class PlayerState:
    def __init__(self):
        self.queue = []
        self.now_playing_msg = None
        self.current_song = None
        self.is_waiting = False

    async def cleanup(self):
        if self.now_playing_msg:
            try: await self.now_playing_msg.delete()
            except: pass
            self.now_playing_msg = None
        self.queue = []
        self.current_song = None
        self.is_waiting = False

# --- 2. 버튼 UI 클래스 ---
class MusicControlView(discord.ui.View):
    def __init__(self, voice_client, guild_id):
        super().__init__(timeout=None)
        self.vc = voice_client
        self.guild_id = guild_id

    @discord.ui.button(label="재생/일시정지", style=discord.ButtonStyle.blurple, emoji="⏯️")
    async def toggle(self, interaction, button):
        if self.vc.is_playing():
            self.vc.pause()
            await interaction.response.send_message("⏸️ 내 시간이 멈췄어...", ephemeral=True)
        elif self.vc.is_paused():
            self.vc.resume()
            await interaction.response.send_message("▶️ 내 시간이 다시 흘러!", ephemeral=True)

    @discord.ui.button(label="스킵", style=discord.ButtonStyle.secondary, emoji="⏭️")
    async def skip(self, interaction, button):
        if not self.vc.is_playing() and not self.vc.is_paused():
            return await interaction.response.send_message("❌ 노래를 먼저...", ephemeral=True)
        self.vc.stop()
        await interaction.response.send_message("⏭️ 다음 노래를 부를게요!", ephemeral=True)

    @discord.ui.button(label="대기열", style=discord.ButtonStyle.success, emoji="📜")
    async def show_q(self, interaction, button):
        state = players.get(self.guild_id)
        if not state or not state.queue:
            return await interaction.response.send_message("더 부를게 없어요!", ephemeral=True)
        txt = "\n".join([f"{i+1}. {s['title']} (신청: {s['requester']})" for i, s in enumerate(state.queue[:10])])
        await interaction.response.send_message(f"**📜 대기열:**\n{txt}", ephemeral=True)

    @discord.ui.button(label="종료", style=discord.ButtonStyle.danger, emoji="⏹️")
    async def stop(self, interaction, button):
        if self.guild_id in players: await players[self.guild_id].cleanup()
        if self.vc.is_connected(): await self.vc.disconnect()
        await interaction.response.send_message("⏹️ 퇴근이다! 퇴근!", ephemeral=True)

# --- 3. UI 업데이트 로직 ---
async def update_player_ui(message, song_data, voice):
    state = players[message.guild.id]
    state.current_song = song_data
    state.is_waiting = False
    
    is_last = len(state.queue) == 0
    
    embed = discord.Embed(
        title="🎵 현재 재생 중", 
        description=f"**[{song_data['title']}]({song_data['web_url']})**",
        color=discord.Color.gold() if is_last else discord.Color.green()
    )
    
    if is_last:
        embed.description += "\n\n⚠️ **저 이 노래만 부르면 퇴근이에요!**"
    else:
        embed.description += f"\n\n💿 제가 부를게 **{len(state.queue)}곡** 남았어요!"

    embed.add_field(name="신청자", value=song_data.get('requester', '알 수 없음'), inline=True)
    embed.set_image(url=song_data.get('thumbnail'))
    
    view = MusicControlView(voice, message.guild.id)

    if state.now_playing_msg:
        try: await state.now_playing_msg.edit(embed=embed, view=view)
        except: state.now_playing_msg = await message.channel.send(embed=embed, view=view)
    else:
        state.now_playing_msg = await message.channel.send(embed=embed, view=view)

# --- 4. 재생 및 퇴장 로직 ---
def play_next(message, guild_id):
    voice = discord.utils.get(bot.voice_clients, guild=message.guild)
    if not voice: return

    state = players.get(guild_id)
    if state and state.queue:
        next_song = state.queue.pop(0)
        source = discord.FFmpegPCMAudio(next_song['url'], executable=FFMPEG_PATH, **FFMPEG_OPTIONS)
        voice.play(source, after=lambda e: play_next(message, guild_id))
        asyncio.run_coroutine_threadsafe(update_player_ui(message, next_song, voice), bot.loop)
    else:
        async def go_idle():
            if state:
                state.is_waiting = True
                if state.now_playing_msg:
                    idle_embed = discord.Embed(
                        description="⏳ 이제 부를게 없는데... 5분만 기다려 줄게요!\n새 곡을 입력하면 다시 시작합니다!",
                        color=discord.Color.light_grey()
                    )
                    try: await state.now_playing_msg.edit(embed=idle_embed, view=None)
                    except: pass

            await asyncio.sleep(300)
            cur_vc = discord.utils.get(bot.voice_clients, guild=message.guild)
            if cur_vc and not cur_vc.is_playing() and (not state or not state.queue):
                if guild_id in players: await players[guild_id].cleanup()
                await cur_vc.disconnect()

        asyncio.run_coroutine_threadsafe(go_idle(), bot.loop)

# --- 5. 이벤트 섹션 (무인 퇴장 안내 포함) ---
@bot.event
async def on_voice_state_update(member, before, after):
    vc = member.guild.voice_client
    if not vc: return

    if len([m for m in vc.channel.members if not m.bot]) == 0:
        await asyncio.sleep(2)
        if len([m for m in vc.channel.members if not m.bot]) == 0:
            gid = member.guild.id
            state = players.get(gid)
            if state and state.now_playing_msg:
                exit_embed = discord.Embed(
                    description="뭐야! 왜 다 나가는데! 나도 걍 퇴근한다 수고!",
                    color=discord.Color.dark_grey()
                )
                try: await state.now_playing_msg.edit(embed=exit_embed, view=None)
                except: pass
            if gid in players: await players[gid].cleanup()
            await vc.disconnect()

@bot.event
async def on_message(message):
    if message.author == bot.user or not message.guild: return
    guild_id_str = str(message.guild.id)
    music_channel_id = config_data["music_channels"].get(guild_id_str)

    if message.content.startswith('!'):
        await bot.process_commands(message)
        return

    if music_channel_id and message.channel.id == music_channel_id:
        content = message.content.strip()
        if not content: return
        await message.delete()

        if message.author.voice:
            gid = message.guild.id
            if gid not in players: players[gid] = PlayerState()
            state = players[gid]
            voice = discord.utils.get(bot.voice_clients, guild=message.guild)
            if not voice: voice = await message.author.voice.channel.connect()

            search_query = content if content.startswith('http') else f"ytsearch:{content}"
            with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
                try:
                    info = ydl.extract_info(search_query, download=False)
                    if 'entries' in info: info = info['entries'][0]
                    song_data = {'url': info['url'], 'title': info['title'], 'thumbnail': info.get('thumbnail'), 'web_url': info.get('webpage_url'), 'requester': message.author.display_name}
                except: return

            if voice.is_playing() or voice.is_paused():
                state.queue.append(song_data)
                await message.channel.send(f"✅ **{song_data['title']}** 추가 (신청: {message.author.display_name})", delete_after=3)
                if state.current_song: await update_player_ui(message, state.current_song, voice)
            else:
                source = discord.FFmpegPCMAudio(song_data['url'], executable=FFMPEG_PATH, **FFMPEG_OPTIONS)
                voice.play(source, after=lambda e: play_next(message, gid))
                await update_player_ui(message, song_data, voice)

# --- 6. 명령어 섹션 (삭제 명령어 복구됨) ---
@bot.command(name="삭제")
async def remove_song(ctx, index: int):
    gid = ctx.guild.id
    if gid in players and players[gid].queue:
        if 0 < index <= len(players[gid].queue):
            removed = players[gid].queue.pop(index - 1)
            await ctx.send(f"🗑️ **{removed['title']}** 노래를 제외했어요!", delete_after=3)
            # 삭제 후 UI 갱신 (다시 마지막 곡이 될 수 있으므로)
            voice = ctx.guild.voice_client
            if voice and players[gid].current_song:
                await update_player_ui(ctx, players[gid].current_song, voice)
        else: await ctx.send("❌ 엥? 번호 잘못 적은듯?", delete_after=3)
    else: await ctx.send("❌ 삭제할 노래가 없어요!", delete_after=3)
    await ctx.message.delete()

@bot.command(name="설정")
@commands.has_permissions(administrator=True)
async def set_channel(ctx):
    config_data["music_channels"][str(ctx.guild.id)] = ctx.channel.id
    save_config(config_data)
    await ctx.send(f"✅ {ctx.channel.mention} 이제부터 여기가 음악 채널이다!")

bot.run("TOKEN")
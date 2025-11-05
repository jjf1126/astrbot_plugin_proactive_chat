# 文件名: main.py (位于 data/plugins/astrbot_plugin_proactive_chat/ 目录下)
# 版本: 0.9.9 (社区优化版)

# 导入标准库
import asyncio
import json
import random
import time
import traceback
import zoneinfo
from datetime import datetime

# v0.9.8 修复 (持久化会话): 导入 aiofiles 及其异步os模块，彻底解决事件循环阻塞问题
import aiofiles
import aiofiles.os as aio_os

# 导入第三方库
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# 导入 AstrBot 的核心 API 和组件
import astrbot.api.star as star
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter

# v0.9.9 优化 (API 适配): 导入官方定义的消息对象，以使用 add_message_pair
from astrbot.core.agent.message import AssistantMessageSegment, UserMessageSegment
from astrbot.core.message.components import Plain, Record
from astrbot.core.message.message_event_result import MessageChain

# --- 插件主类 ---


# v0.9.9 优化 (框架适应性): 遵循插件开发规范，使用位置参数注册插件
@star.register(
    "astrbot_plugin_proactive_chat",
    "DBJD-CR & Gemini-2.5-Pro",
    "一个让Bot能够发起主动消息的插件，拥有上下文感知、持久化会话、动态情绪、免打扰时段和健壮的TTS集成。",
    "0.9.9",
)
class Main(star.Star):
    """
    插件的主类，继承自 astrbot.api.star.Star。
    负责插件的生命周期管理、事件监听和核心逻辑执行。
    """

    def __init__(self, context: star.Context, config) -> None:
        """
        插件的构造函数。
        当 AstrBot 加载插件时被调用。
        """
        super().__init__(context)

        self.config = config
        self.scheduler = None
        self.timezone = None

        # v0.9.8 修复 (持久化会话): 使用 StarTools 获取插件专属数据目录，确保数据隔离
        self.data_dir = star.StarTools.get_data_dir("astrbot_plugin_proactive_chat")
        # v0.9.9 优化 (代码质量): 使用 pathlib 的 / 操作符拼接路径，更现代化、更具可读性
        self.session_data_file = self.data_dir / "session_data.json"

        # v0.9.8 修复 (持久化会话 / 并发数据竞争): 在 __init__ (同步) 中，只声明变量，不创建任何异步对象
        self.data_lock = None  # 初始化异步锁，用于保护对 session_data 的并发读写
        self.session_data = {}  # 初始化会话数据字典

        logger.info("[主动消息] 插件实例已创建。")

    # --- v0.9.8 修复 (持久化会话 / 并发数据竞争): 将数据操作分为“带锁的外部接口”和“无锁的内部实现” ---

    async def _load_data_internal(self):
        """
        从文件中加载会话数据（异步无锁内部实现）。
        这是一个底层函数，必须在持有 data_lock 的情况下被调用。
        """
        if await aio_os.path.exists(self.session_data_file):
            try:
                async with aiofiles.open(
                    self.session_data_file, encoding="utf-8"
                ) as f:
                    content = await f.read()
                    # v0.9.9 优化 (性能): 将同步的 json.loads 操作放入独立线程，避免阻塞事件循环
                    loop = asyncio.get_running_loop()
                    self.session_data = await loop.run_in_executor(
                        None, json.loads, content
                    )
            # v0.9.8 修复 (精细化异常捕获): 捕获更具体的异常，提高代码健壮性
            except (OSError, json.JSONDecodeError) as e:
                logger.error(f"[主动消息] 加载会话数据失败: {e}，将使用空数据启动。")
                self.session_data = {}
        else:
            self.session_data = {}

    async def _save_data_internal(self):
        """
        将会话数据保存到文件（异步无锁内部实现）。
        这是一个底层函数，必须在持有 data_lock 的情况下被调用。
        它使用异步IO，避免在保存文件时阻塞 AstrBot 的主事件循环。
        """
        try:
            await aio_os.makedirs(self.data_dir, exist_ok=True)
            async with aiofiles.open(
                self.session_data_file, "w", encoding="utf-8"
            ) as f:
                # 使用 indent=4 和 ensure_ascii=False 来保证 JSON 文件的可读性 (v0.9.7 继承)
                # v0.9.9 优化 (性能): 将同步的 json.dumps 操作放入独立线程
                loop = asyncio.get_running_loop()
                content_to_write = await loop.run_in_executor(
                    None,
                    lambda: json.dumps(self.session_data, indent=4, ensure_ascii=False),
                )
                await f.write(content_to_write)
        except OSError as e:
            logger.error(f"[主动消息] 保存会话数据失败: {e}")

    async def initialize(self):
        """
        插件的异步初始化函数。
        在 AstrBot 的主事件循环准备好后被调用。
        """
        # v0.9.8 修复 (持久化会话 / 并发数据竞争): 在 initialize (异步) 中，才真正创建异步对象
        self.data_lock = asyncio.Lock()

        # 加载初始数据
        async with self.data_lock:
            await self._load_data_internal()
        logger.info("[主动消息] 已成功从文件加载会话数据。")

        try:
            # 从 AstrBot 主配置中获取时区设置 (v0.9.7 继承)
            self.timezone = zoneinfo.ZoneInfo(self.context.get_config().get("timezone"))
        # v0.9.9 修复 (Issue #5): 新增 ValueError 捕获，处理用户未配置或配置了无效时区格式的情况
        except (zoneinfo.ZoneInfoNotFoundError, TypeError, KeyError, ValueError):
            self.timezone = None

        # 创建一个独立的、属于本插件的异步调度器实例 (v0.9.7 继承)
        self.scheduler = AsyncIOScheduler(timezone=self.timezone)
        self.scheduler.start()

        # 从持久化数据中恢复因重启而中断的定时任务 (v0.9.7 继承)
        await self._init_jobs_from_data()
        logger.info("[主动消息] 调度器已初始化。")

    async def _init_jobs_from_data(self):
        """从已加载的 session_data 中恢复定时任务。"""
        # 修复：从新的 "basic_settings" 分组中读取核心配置 (v0.9.7 继承)
        basic_conf = self.config.get("basic_settings", {})
        target_user_id = str(basic_conf.get("target_user_id", "")).strip()
        if not target_user_id:
            return

        restored_count = 0
        for session_id, session_info in self.session_data.items():
            # v0.9.8 修复 (持久化会话): 修复了“脸盲症”，正确识别私聊消息类型
            # 私聊的 message_type 可能是 "FriendMessage" 或 "private"，需要同时检查
            is_private_chat = (
                "friendmessage" in session_id.lower() or "private" in session_id.lower()
            )

            if f":{target_user_id}" in session_id and is_private_chat:
                next_trigger = session_info.get("next_trigger_time")
                # 如果任务的预定执行时间还没到，就重新安排它 (v0.9.7 继承)
                if next_trigger and time.time() < next_trigger:
                    try:
                        run_date = datetime.fromtimestamp(next_trigger)
                        self.scheduler.add_job(
                            self.check_and_chat,
                            "date",
                            run_date=run_date,
                            args=[session_id],
                            id=session_id,
                            replace_existing=True,
                            misfire_grace_time=60,  # 允许任务在错过触发时间后 60 秒内依然执行 (v0.9.7 继承)
                        )
                        logger.info(
                            f"[主动消息] 已成功从文件恢复任务: {session_id}, 执行时间: {run_date}"
                        )
                        restored_count += 1
                    except Exception as e:
                        logger.error(
                            f"[主动消息] 添加恢复任务 '{session_id}' 到调度器时失败: {e}"
                        )

        if restored_count > 0:
            logger.info(f"[主动消息] 共恢复 {restored_count} 个定时任务。")
        else:
            logger.info("[主动消息] 检查完成，没有需要恢复的定时任务。")

    # v0.9.8 修复 (持久化会话): 创建一个“原子”操作函数，负责调度并立即保存状态
    async def _schedule_next_chat_and_save(self, session_id: str):
        """
        安排下一次主动聊天并立即将状态持久化到文件。
        这是一个“原子”操作，确保任何调度决策都能在重启后幸存。
        """
        # v0.9.8 修复 (并发数据竞争): 使用异步锁保护所有对 session_data 的写操作
        async with self.data_lock:
            # 修复：从分组后的配置中正确读取参数 (v0.9.7 继承)
            schedule_conf = self.config.get("schedule_settings", {})
            min_interval = int(schedule_conf.get("min_interval_minutes", 30)) * 60
            max_interval = max(
                min_interval, int(schedule_conf.get("max_interval_minutes", 900)) * 60
            )
            random_interval = random.randint(min_interval, max_interval)
            next_trigger_time = time.time() + random_interval
            run_date = datetime.fromtimestamp(next_trigger_time)

            # 1. 在内存中安排下一个任务
            self.scheduler.add_job(
                self.check_and_chat,
                "date",
                run_date=run_date,
                args=[session_id],
                id=session_id,
                replace_existing=True,
                misfire_grace_time=60,
            )

            # 2. 更新内存中的会话数据
            self.session_data.setdefault(session_id, {})["next_trigger_time"] = (
                next_trigger_time
            )
            # 修复：优化日志输出，使其更直观 (v0.9.7 继承)
            logger.info(
                f"[主动消息] 已为会话 {session_id} 安排下一次主动聊天，时间：{run_date.strftime('%Y-%m-%d %H:%M:%S')}。"
            )

            # 3. 立刻将更新后的会话数据写入文件，确保持久化
            await self._save_data_internal()

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=999)
    async def on_private_message(self, event: AstrMessageEvent):
        """
        监听所有私聊消息。
        这是我们重置计时器和计数器的入口。
        """
        # 修复：从新的 "basic_settings" 分组中读取核心配置 (v0.9.7 继承)
        basic_conf = self.config.get("basic_settings", {})
        if not basic_conf.get("enable", False):
            return
        target_user_id = str(basic_conf.get("target_user_id", "")).strip()
        if not target_user_id or event.get_sender_id() != target_user_id:
            return

        session_id = event.unified_msg_origin

        # 当目标用户回复时，重置未回复计数器和最后消息时间 (v0.9.7 逻辑)
        # v0.9.8 修复 (并发数据竞争): 此处不再需要自己加锁，因为 schedule_and_save 内部会处理
        self.session_data.setdefault(session_id, {})["unanswered_count"] = 0
        self.session_data[session_id]["last_msg_time"] = time.time()

        # 调用“原子”的调度与保存函数，确保数据一致性
        await self._schedule_next_chat_and_save(session_id)
        logger.info(f"[主动消息] 用户已回复。会话 {session_id} 的未回复计数已重置。")

    async def check_and_chat(self, session_id: str):
        """
        由定时任务触发的核心函数。
        负责检查条件、调用 LLM 并发送消息。
        """
        # v0.9.8 修复 (并发数据竞争): 在任务开始时，只需要读取一次数据，无需加锁
        session_info = self.session_data.get(session_id, {})
        unanswered_count = session_info.get("unanswered_count", 0)

        try:
            basic_conf = self.config.get("basic_settings", {})
            schedule_conf = self.config.get("schedule_settings", {})

            # 检查插件是否启用和是否处于免打扰时段 (v0.9.7 继承)
            if not basic_conf.get("enable", False) or is_quiet_time(
                schedule_conf.get("quiet_hours", "1-7"), self.timezone
            ):
                logger.info("[主动消息] 插件被禁用或当前为免打扰时段，跳过并重新调度。")
                await self._schedule_next_chat_and_save(session_id)
                return

            # v0.9.8 新功能: 未回复次数上限
            max_unanswered = schedule_conf.get("max_unanswered_times", 3)
            if max_unanswered > 0 and unanswered_count >= max_unanswered:
                logger.info(
                    f"[主动消息] 未回复次数 ({unanswered_count}) 已达到上限 ({max_unanswered})，暂停主动消息。"
                )
                return

            logger.info(
                f"[主动消息] 开始生成 Prompt，当前未回复次数: {unanswered_count}。"
            )

            # 获取当前会话使用的 LLM Provider (v0.9.7 继承)
            provider = self.context.get_using_provider(umo=session_id)
            if not provider:
                logger.warning("[主动消息] 未找到 LLM Provider，放弃并重新调度。")
                await self._schedule_next_chat_and_save(session_id)
                return

            # --- 核心：加载人格和历史 ---
            pure_history_messages, original_system_prompt = [], ""
            conv_id = None
            try:
                # v0.9.8 修复 (上下文感知): 如果没有会话，则主动创建一个
                conv_id = (
                    await self.context.conversation_manager.get_curr_conversation_id(
                        session_id
                    )
                )
                if not conv_id:
                    logger.warning(
                        f"[主动消息] 会话 {session_id} 尚未建立对话，正在为其创建新对话..."
                    )
                    conv_id = await self.context.conversation_manager.new_conversation(
                        session_id
                    )
                    logger.info(f"[主动消息] 新对话创建成功，ID: {conv_id}")

                conversation = await self.context.conversation_manager.get_conversation(
                    session_id, conv_id
                )
                if conversation:
                    if conversation.history and isinstance(conversation.history, str):
                        try:
                            # v0.9.9 优化 (性能): 将同步的 json.loads 操作放入独立线程
                            loop = asyncio.get_running_loop()
                            pure_history_messages = await loop.run_in_executor(
                                None, json.loads, conversation.history
                            )
                            # 新增日志: 打印加载到的上下文条数 (issue#2)
                            logger.info(
                                f"[主动消息] 成功加载上下文: 共 {len(pure_history_messages)} 条历史消息。"
                            )
                        except json.JSONDecodeError:
                            logger.warning("[主动消息] 解析历史记录JSON失败。")
                    if conversation.persona_id:
                        persona = await self.context.persona_manager.get_persona(
                            conversation.persona_id
                        )
                        if persona:
                            original_system_prompt = persona.system_prompt
                            # v0.9.8 修复 (上下文感知): persona 对象的“名字”是 persona_id (issue#2)
                            logger.info(
                                f"[主动消息] 成功加载人格: '{persona.persona_id}' (会话专属)"
                            )

                if not original_system_prompt:
                    default_persona_v3 = (
                        await self.context.persona_manager.get_default_persona_v3(
                            umo=session_id
                        )
                    )
                    if default_persona_v3:
                        original_system_prompt = default_persona_v3["prompt"]
                        # 新增日志: 打印加载的默认人格名称 (issue#2)
                        logger.info(
                            f"[主动消息] 成功加载人格: '{default_persona_v3['name']}' (全局默认)"
                        )
            except Exception as e:
                logger.warning(f"[主动消息] 获取上下文失败: {e}")

            if not original_system_prompt:
                logger.error("[主动消息] 关键错误：无法加载任何人格设定，放弃。")
                return

            # --- 核心：构造最终的 Prompt ---
            prompt_conf = self.config.get("prompt_settings", {})
            motivation_template = prompt_conf.get("proactive_prompt", "")
            # v0.9.8 新功能: 注入当前时间占位符 {{current_time}} (issue#2)
            now_str = datetime.now(self.timezone).strftime("%Y年%m月%d日 %H:%M")
            final_user_simulation_prompt = motivation_template.replace(
                "{{unanswered_count}}", str(unanswered_count)
            ).replace("{{current_time}}", now_str)

            # v0.9.8 更新: 优化日志输出
            logger.info("[主动消息] 已生成包含动机和时间的 Prompt。")

            # --- 核心：以正确的“三分离”架构调用 LLM --- (v0.9.7 继承)
            llm_response_obj = await provider.text_chat(
                prompt=final_user_simulation_prompt,
                contexts=pure_history_messages,
                system_prompt=original_system_prompt,
            )

            if llm_response_obj and llm_response_obj.completion_text:
                response_text = llm_response_obj.completion_text.strip()
                logger.info(f"[主动消息] LLM 已生成文本: '{response_text}'。")

                # --- 核心：使用正确的 API 和健壮的逻辑发送消息 ---
                is_tts_sent = False
                tts_conf = self.config.get("tts_settings", {})
                # v0.9.8 新功能: 增加了插件级的TTS总开关 (issue#2)
                if tts_conf.get("enable_tts", True):
                    try:
                        logger.info("[主动消息] 尝试进行手动 TTS。")
                        tts_provider = self.context.get_using_tts_provider(
                            umo=session_id
                        )
                        # 修复：确保 tts_provider 不是列表 (v0.9.7 继承)
                        if isinstance(tts_provider, list):
                            tts_provider = tts_provider[0] if tts_provider else None
                        if tts_provider:
                            audio_path = await tts_provider.get_audio(response_text)
                            if audio_path:
                                # 使用 MessageChain 封装语音组件 (v0.9.7 继承)
                                await self.context.send_message(
                                    session_id, MessageChain([Record(file=audio_path)])
                                )
                                is_tts_sent = True
                                await asyncio.sleep(0.5)
                    # v0.9.8 修复 (精细化异常捕获): 捕获所有 TTS 相关的异常，但不会让程序崩溃
                    except Exception as e:
                        logger.error(f"[主动消息] 手动 TTS 流程发生异常: {e}")

                # 无论 TTS 是否成功，都根据配置决定是否发送原文 (v0.9.7 继承)
                if not is_tts_sent or tts_conf.get("always_send_text", True):
                    await self.context.send_message(
                        session_id, MessageChain([Plain(text=response_text)])
                    )

                # --- 核心: 解决记忆黑洞 ---
                # v0.9.9 优化 (API 适配): 使用官方提供的 add_message_pair API，代码更简洁、更健壮
                try:
                    if conv_id:
                        # 1. 创建官方定义的消息对象
                        user_msg_obj = UserMessageSegment(
                            content=final_user_simulation_prompt
                        )
                        assistant_msg_obj = AssistantMessageSegment(
                            content=response_text
                        )

                        # 2. 调用全新的“官方套装”
                        await self.context.conversation_manager.add_message_pair(
                            cid=conv_id,
                            user_message=user_msg_obj,
                            assistant_message=assistant_msg_obj,
                        )
                        logger.info(
                            "[主动消息] 已成功将本次主动对话存档至对话历史 (使用 add_message_pair)。"
                        )
                except Exception as e:
                    logger.error(
                        f"[主动消息] 使用 add_message_pair 存档对话历史失败: {e}"
                    )

                # 任务成功，更新计数器，并安排下一次任务
                self.session_data.setdefault(session_id, {})["unanswered_count"] = (
                    unanswered_count + 1
                )
                await self._schedule_next_chat_and_save(session_id)
                logger.info(
                    f"[主动消息] 任务成功，未回复次数更新为: {unanswered_count + 1}。"
                )
            else:
                logger.warning("[主动消息] LLM 调用失败或返回空内容，重新调度。")
                await self._schedule_next_chat_and_save(session_id)
        # v0.9.8 修复 (精细化异常捕获): 使用更精确的异常捕获
        except Exception as e:
            logger.error(
                f"[主动消息] check_and_chat 任务发生致命错误: {e}\n{traceback.format_exc()}"
            )
            try:
                # 即使发生致命错误，也尝试重新调度，避免任务链中断 (v0.9.7 继承)
                await self._schedule_next_chat_and_save(session_id)
            except Exception as se:
                logger.error(f"[主动消息] 在错误处理中重新调度失败: {se}")

    async def terminate(self):
        """
        插件被卸载或停用时调用的清理函数。
        """
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown()
        # v0.9.8 修复 (持久化会话 / 并发数据竞争): 使用带锁的异步方法保存数据
        if self.data_lock:
            async with self.data_lock:
                await self._save_data_internal()
        logger.info("主动消息插件已终止。")


def is_quiet_time(quiet_hours_str: str, tz: zoneinfo.ZoneInfo) -> bool:
    """检查当前时间是否处于免打扰时段。"""
    try:
        start_str, end_str = quiet_hours_str.split("-")
        start_hour, end_hour = int(start_str), int(end_str)
        now = datetime.now(tz) if tz else datetime.now()
        # 处理跨天的情况 (例如 23-7) (v0.9.7 继承)
        if start_hour <= end_hour:
            return start_hour <= now.hour < end_hour
        else:
            return now.hour >= start_hour or now.hour < end_hour
    # v0.9.8 修复 (精细化异常捕获): 捕获可能发生的多种异常
    except (ValueError, TypeError):
        return False

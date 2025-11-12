# 文件名: main.py (位于 data/plugins/astrbot_plugin_proactive_chat/ 目录下)
# 版本: 0.9.97 (代码质量重构版)

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
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Plain, Record
from astrbot.core.message.message_event_result import MessageChain

# --- 插件主类 ---


# v0.9.95 优化: 将插件主类命名为更具描述性的 ProactiveChatPlugin
class ProactiveChatPlugin(star.Star):
    """
    插件的主类，继承自 astrbot.api.star.Star。
    负责插件的生命周期管理、事件监听和核心逻辑执行。
    """

    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        """
        插件的构造函数。
        当 AstrBot 加载插件时被调用。
        config (AstrBotConfig): 插件专属的配置对象，由 AstrBot 框架通过依赖注入自动传入。
        """
        super().__init__(context)

        # v0.9.95 优化: 增加 config 参数的类型注解
        self.config: AstrBotConfig = config
        self.scheduler = None
        self.timezone = None

        # v0.9.8 修复 (持久化会话): 使用 StarTools 获取插件专属数据目录，确保数据隔离
        self.data_dir = star.StarTools.get_data_dir("astrbot_plugin_proactive_chat")
        # v0.9.9 优化 (代码质量): 使用 pathlib 的 / 操作符拼接路径，更现代化、更具可读性
        self.session_data_file = self.data_dir / "session_data.json"

        # v0.9.8 修复 (持久化会话 / 并发数据竞争): 在 __init__ (同步) 中，只声明变量，不创建任何异步对象
        self.data_lock = None  # 初始化异步锁，用于保护对 session_data 的并发读写
        self.session_data = {}  # 初始化会话数据字典

        # v0.9.95 优化: 在初始化时一次性读取所有配置，集中管理
        self.basic_conf = {}
        self.schedule_conf = {}
        self.prompt_conf = {}
        self.tts_conf = {}
        self.target_user_id = ""

        logger.info("[主动消息] 插件实例已创建。")

    # --- 数据持久化核心函数 ---

    async def _load_data_internal(self):
        """
        从文件中加载会话数据（异步无锁内部实现）。
        这是一个底层函数，必须在持有 data_lock 的情况下被调用。
        """
        if await aio_os.path.exists(self.session_data_file):
            try:
                async with aiofiles.open(self.session_data_file, encoding="utf-8") as f:
                    content = await f.read()
                    # v0.9.9 优化 (性能): 使用 asyncio.to_thread 替代 loop.run_in_executor
                    self.session_data = await asyncio.to_thread(json.loads, content)
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
                # v0.9.9 优化 (性能): 使用 asyncio.to_thread 替代 loop.run_in_executor
                content_to_write = await asyncio.to_thread(
                    json.dumps, self.session_data, indent=4, ensure_ascii=False
                )
                await f.write(content_to_write)
        except OSError as e:
            logger.error(f"[主动消息] 保存会话数据失败: {e}")

    # --- 插件生命周期函数 ---

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

        # v0.9.95 优化: 在初始化时一次性读取所有配置项
        self.basic_conf = self.config.get("basic_settings", {})
        self.schedule_conf = self.config.get("schedule_settings", {})
        self.prompt_conf = self.config.get("prompt_settings", {})
        self.tts_conf = self.config.get("tts_settings", {})
        self.target_user_id = str(self.basic_conf.get("target_user_id", "")).strip()

        try:
            # 从 AstrBot 主配置中获取时区设置 (v0.9.7 继承)
            self.timezone = zoneinfo.ZoneInfo(self.context.get_config().get("timezone"))
        # v0.9.9 修复 (Issue #5): 新增 ValueError 捕获，处理用户未配置或配置了无效时区格式的情况
        except (zoneinfo.ZoneInfoNotFoundError, TypeError, KeyError, ValueError) as e:
            # v0.9.95 优化: 增加时区回退警告，明确告知用户当前行为
            logger.warning(
                f"[主动消息] 时区配置无效或未配置 ({e})，将使用服务器系统时区作为备用。"
            )
            self.timezone = None

        # 创建一个独立的、属于本插件的异步调度器实例 (v0.9.7 继承)
        self.scheduler = AsyncIOScheduler(timezone=self.timezone)
        self.scheduler.start()

        # 从持久化数据中恢复因重启而中断的定时任务 (v0.9.7 继承)
        await self._init_jobs_from_data()
        logger.info("[主动消息] 调度器已初始化。")

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

    # --- 核心调度逻辑 ---

    async def _init_jobs_from_data(self):
        """从已加载的 session_data 中恢复定时任务。"""
        # v0.9.95 优化: 直接使用实例属性 self.target_user_id
        if not self.target_user_id:
            return

        restored_count = 0
        for session_id, session_info in self.session_data.items():
            # v0.9.8 修复 (持久化会话): 修复了“脸盲症”，正确识别私聊消息类型
            is_private_chat = (
                "friendmessage" in session_id.lower() or "private" in session_id.lower()
            )

            if f":{self.target_user_id}" in session_id and is_private_chat:
                next_trigger = session_info.get("next_trigger_time")
                # 如果任务的预定执行时间还没到，就重新安排它 (v0.9.7 继承)
                if next_trigger and time.time() < next_trigger:
                    try:
                        run_date = datetime.fromtimestamp(
                            next_trigger, tz=self.timezone
                        )
                        self.scheduler.add_job(
                            self.check_and_chat,
                            "date",
                            run_date=run_date,
                            args=[session_id],
                            id=session_id,
                            replace_existing=True,
                            misfire_grace_time=60,
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
    async def _schedule_next_chat_and_save(
        self, session_id: str, reset_counter: bool = False
    ):
        """
        安排下一次主动聊天并立即将状态持久化到文件。
        这是一个“原子”操作，确保任何调度决策都能在重启后幸存。
        v0.9.96 更新: 新增 reset_counter 参数，用于原子化地重置未回复计数。
        """
        # v0.9.8 修复 (并发数据竞争): 使用异步锁保护所有对 session_data 的读写操作
        async with self.data_lock:
            # v0.9.96 修复 (数据竞争): 在锁内执行计数器重置操作
            if reset_counter:
                self.session_data.setdefault(session_id, {})["unanswered_count"] = 0
                logger.info(
                    f"[主动消息] 用户已回复。会话 {session_id} 的未回复计数已重置。"
                )

            # v0.9.95 优化: 直接使用实例属性 self.schedule_conf
            min_interval = int(self.schedule_conf.get("min_interval_minutes", 30)) * 60
            max_interval = max(
                min_interval,
                int(self.schedule_conf.get("max_interval_minutes", 900)) * 60,
            )
            random_interval = random.randint(min_interval, max_interval)
            next_trigger_time = time.time() + random_interval
            run_date = datetime.fromtimestamp(next_trigger_time, tz=self.timezone)

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

    # --- 事件监听 ---

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=999)
    async def on_private_message(self, event: AstrMessageEvent):
        """
        监听用户回复，重置计时器和计数器。
        """
        # v0.9.97 修复: 增加对空消息的过滤，防止因“对方正在输入”等状态事件而错误重置计数器
        if not event.message_str or not event.message_str.strip():
            return

        # v0.9.95 优化: 直接使用实例属性
        if not self.basic_conf.get("enable", False):
            return
        if not self.target_user_id or event.get_sender_id() != self.target_user_id:
            return

        session_id = event.unified_msg_origin
        
        # v0.9.96 修复 (数据竞争): 不再直接修改共享状态，而是调用一个统一的、带锁的函数来处理
        # 将重置计数器的意图，通过参数传递给带锁的函数
        await self._schedule_next_chat_and_save(session_id, reset_counter=True)

    # --- v0.9.95 优化: `check_and_chat` 函数重构 ---

    async def _is_chat_allowed(self, session_id: str) -> bool:
        """检查是否允许进行主动聊天（条件检查）。"""
        # v0.9.96 修复 (数据竞争): 此函数不再直接读取 unanswered_count，
        # 相关的逻辑被移入 check_and_chat 的原子操作块中。
        if not self.basic_conf.get("enable", False) or is_quiet_time(
            self.schedule_conf.get("quiet_hours", "1-7"), self.timezone
        ):
            logger.info("[主动消息] 插件被禁用或当前为免打扰时段，跳过并重新调度。")
            return False

        return True

    async def _prepare_llm_request(self, session_id: str) -> dict | None:
        """准备 LLM 请求所需的上下文、人格和最终 Prompt。"""
        pure_history_messages = []
        original_system_prompt = ""
        conv_id = None

        try:
            conv_id = await self.context.conversation_manager.get_curr_conversation_id(
                session_id
            )
            # v0.9.95 修复: 移除“创建空对话”的兜底机制，从根源上解决“None对话”问题
            if not conv_id:
                logger.warning(
                    f"[主动消息] 无法找到会话 {session_id} 的当前对话ID，可能是新会话，跳过本次任务。"
                )
                return None

            conversation = await self.context.conversation_manager.get_conversation(
                session_id, conv_id
            )
            if (
                conversation
                and conversation.history
                and isinstance(conversation.history, str)
            ):
                try:
                    # v0.9.9 优化 (性能): 将同步的 json.loads 操作放入独立线程
                    pure_history_messages = await asyncio.to_thread(
                        json.loads, conversation.history
                    )
                except json.JSONDecodeError:
                    logger.warning("[主动消息] 解析历史记录JSON失败。")

            if conversation and conversation.persona_id:
                persona = await self.context.persona_manager.get_persona(
                    conversation.persona_id
                )
                if persona:
                    original_system_prompt = persona.system_prompt

            if not original_system_prompt:
                default_persona_v3 = (
                    await self.context.persona_manager.get_default_persona_v3(
                        umo=session_id
                    )
                )
                if default_persona_v3:
                    original_system_prompt = default_persona_v3["prompt"]

        except Exception as e:
            logger.warning(f"[主动消息] 获取上下文或人格失败: {e}")
            return None  # 获取失败则中止任务

        if not original_system_prompt:
            logger.error("[主动消息] 关键错误：无法加载任何人格设定，放弃。")
            return None

        logger.info(
            f"[主动消息] 成功加载上下文: 共 {len(pure_history_messages)} 条历史消息。"
        )
        logger.info(
            f"[主动消息] 成功加载人格: '{conversation.persona_id if conversation and conversation.persona_id else 'default'}'"
        )

        return {
            "conv_id": conv_id,
            "history": pure_history_messages,
            "system_prompt": original_system_prompt,
        }

    async def _send_proactive_message(self, session_id: str, text: str):
        """负责处理 TTS 和文本消息的发送逻辑。"""
        is_tts_sent = False
        if self.tts_conf.get("enable_tts", True):
            try:
                logger.info("[主动消息] 尝试进行手动 TTS。")
                tts_provider = self.context.get_using_tts_provider(umo=session_id)
                if tts_provider:
                    audio_path = await tts_provider.get_audio(text)
                    if audio_path:
                        await self.context.send_message(
                            session_id, MessageChain([Record(file=audio_path)])
                        )
                        is_tts_sent = True
                        await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"[主动消息] 手动 TTS 流程发生异常: {e}")

        if not is_tts_sent or self.tts_conf.get("always_send_text", True):
            await self.context.send_message(
                session_id, MessageChain([Plain(text=text)])
            )

    async def _finalize_and_reschedule(
        self,
        session_id: str,
        conv_id: str,
        user_prompt: str,
        assistant_response: str,
        unanswered_count: int,
    ):
        """
        负责更新会话历史、更新计数器并重新调度下一个任务。
        这是一个“原子”操作，确保所有写操作都在一个锁内完成。
        v0.9.96 更新: 新增 unanswered_count 参数，以在原子操作中正确更新计数值。
        """
        async with self.data_lock:
            # 1. 存档记忆
            try:
                user_msg_obj = UserMessageSegment(content=user_prompt)
                assistant_msg_obj = AssistantMessageSegment(content=assistant_response)
                await self.context.conversation_manager.add_message_pair(
                    cid=conv_id,
                    user_message=user_msg_obj,
                    assistant_message=assistant_msg_obj,
                )
                logger.info(
                    "[主动消息] 已成功将本次主动对话存档至对话历史 (使用 add_message_pair)。"
                )
            except Exception as e:
                logger.error(f"[主动消息] 使用 add_message_pair 存档对话历史失败: {e}")

            # 2. 更新计数器
            # v0.9.96 修复 (数据竞争): 在锁内安全地读取和写入 unanswered_count
            # 注意：我们直接使用传入的 unanswered_count，这是本次任务开始时的值，确保逻辑一致性。
            self.session_data.setdefault(session_id, {})["unanswered_count"] = (
                unanswered_count + 1
            )
            logger.info(
                f"[主动消息] 任务成功，未回复次数更新为: {unanswered_count + 1}。"
            )

            # 3. 重新调度 (将 _schedule_next_chat_and_save 的核心逻辑内联于此，确保原子性)
            min_interval = int(self.schedule_conf.get("min_interval_minutes", 30)) * 60
            max_interval = max(
                min_interval,
                int(self.schedule_conf.get("max_interval_minutes", 900)) * 60,
            )
            random_interval = random.randint(min_interval, max_interval)
            next_trigger_time = time.time() + random_interval
            run_date = datetime.fromtimestamp(next_trigger_time, tz=self.timezone)

            self.scheduler.add_job(
                self.check_and_chat,
                "date",
                run_date=run_date,
                args=[session_id],
                id=session_id,
                replace_existing=True,
                misfire_grace_time=60,
            )

            self.session_data.setdefault(session_id, {})["next_trigger_time"] = (
                next_trigger_time
            )
            logger.info(
                f"[主动消息] 已为会话 {session_id} 安排下一次主动聊天，时间：{run_date.strftime('%Y-%m-%d %H:%M:%S')}。"
            )

            # 4. 保存所有状态
            await self._save_data_internal()

    async def check_and_chat(self, session_id: str):
        """
        由定时任务触发的核心函数（车间主任）。
        负责调度各个子函数，完成一次完整的主动消息流程。
        """
        try:
            # --- 步骤1：前置条件检查 (无锁部分) ---
            if not await self._is_chat_allowed(session_id):
                return

            # --- 步骤2：检查未回复次数上限 (原子读操作) ---
            async with self.data_lock:
                unanswered_count = self.session_data.get(session_id, {}).get(
                    "unanswered_count", 0
                )
                max_unanswered = self.schedule_conf.get("max_unanswered_times", 3)
                if max_unanswered > 0 and unanswered_count >= max_unanswered:
                    logger.info(
                        f"[主动消息] 未回复次数 ({unanswered_count}) 已达到上限 ({max_unanswered})，暂停主动消息。"
                    )
                    return

            logger.info(
                f"[主动消息] 开始生成 Prompt，当前未回复次数: {unanswered_count}。"
            )

            # --- 步骤3：准备 LLM 请求 (无锁部分) ---
            request_package = await self._prepare_llm_request(session_id)
            if not request_package:
                await self._schedule_next_chat_and_save(session_id)
                return

            conv_id = request_package["conv_id"]
            pure_history_messages = request_package["history"]
            original_system_prompt = request_package["system_prompt"]

            # --- 步骤4：调用 LLM 生成回复 (无锁部分) ---
            provider = self.context.get_using_provider(umo=session_id)
            if not provider:
                logger.warning("[主动消息] 未找到 LLM Provider，放弃并重新调度。")
                await self._schedule_next_chat_and_save(session_id)
                return

            motivation_template = self.prompt_conf.get("proactive_prompt", "")
            now_str = datetime.now(self.timezone).strftime("%Y年%m月%d日 %H:%M")
            final_user_simulation_prompt = motivation_template.replace(
                "{{unanswered_count}}", str(unanswered_count)
            ).replace("{{current_time}}", now_str)

            logger.info("[主动消息] 已生成包含动机和时间的 Prompt。")

            # --- 核心：以正确的“三分离”架构调用 LLM --- (v0.9.7 继承)
            llm_response_obj = await provider.text_chat(
                prompt=final_user_simulation_prompt,
                contexts=pure_history_messages,
                system_prompt=original_system_prompt,
            )

            # --- 步骤5 & 6：发送消息、收尾工作 ---
            if llm_response_obj and llm_response_obj.completion_text:
                response_text = llm_response_obj.completion_text.strip()
                logger.info(f"[主动消息] LLM 已生成文本: '{response_text}'。")

                await self._send_proactive_message(session_id, response_text)

                # v0.9.96 修复: 调用新的、原子化的收尾函数
                await self._finalize_and_reschedule(
                    session_id,
                    conv_id,
                    final_user_simulation_prompt,
                    response_text,
                    unanswered_count,
                )
            else:
                logger.warning("[主动消息] LLM 调用失败或返回空内容，重新调度。")
                await self._schedule_next_chat_and_save(session_id)

        except Exception as e:
            logger.error(
                f"[主动消息] check_and_chat 任务发生未捕获的致命错误: {e}\n{traceback.format_exc()}"
            )
            try:
                # 即使发生致命错误，也尝试重新调度，避免任务链中断 (v0.9.7 继承)
                await self._schedule_next_chat_and_save(session_id)
            except Exception as se:
                logger.error(f"[主动消息] 在错误处理中重新调度失败: {se}")


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

import sys,threading,os,time
from dotenv import load_dotenv
from pathlib import Path
load_dotenv(override=True)
from multi_agent_platform.team_protocols import consume_lead_inbox
WORKDIR = Path.cwd()
from plan_and_coordination.error_recovery import DEFAULT_MAX_TOKENS, RecoveryState
from tool_use import TOOLS
from hooks import trigger_hooks
from load_skill import SYSTEM as SKILLS_SYSTEM
from llm import call_llm_with_recovery, CALL_RETRY, CALL_FINISH
from plan_and_coordination.prompt import update_context,get_system_prompt
from memory_management.context_compact import snip_compact,micro_compact,tool_result_budget,estimate_size,CONTEXT_LIMIT,compact_history
import token_usage
from concurrency.background_tasks import should_run_background,start_background_task,execute_tool,collect_background_results
from concurrency.cron_scheduler import has_cron_queue,consume_cron_queue

if sys.platform == "win32":
    os.environ.setdefault("PYTHONUTF8", "1")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.stdin = open(
        sys.stdin.fileno(), mode="r", encoding="utf-8", errors="replace", buffering=1
    )
try:
    import readline
    # macOS 的 libedit 在处理中文输入时有退格问题，这四行修复它
    readline.parse_and_bind("set bind-tty-special-chars off")
    readline.parse_and_bind("set input-meta on")
    readline.parse_and_bind("set output-meta on")
    readline.parse_and_bind("set convert-meta off")
except ImportError:
    pass

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# s07: 拼接 load_skill 构建的 skills SYSTEM，让主 agent 感知可用技能
SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Before starting any multi-step task, use todo_write to plan your steps. "
    "Update status as you go."
    "For complex sub-problems, use the task tool to spawn a subagent.\n\n"
    f"{SKILLS_SYSTEM}"
)
# 最大无操作提示todolist
rounds_since_todo = 0
"""
:message  消息队列
:description agent loop循环
"""
from memory_management.memory import load_memories, extract_memories,consolidate_memories
def agent_loop(messages: list,context:dict):
    """主循环 — 使用组装的系统提示，而不是硬编码的 SYSTEM."""
    system = get_system_prompt(context)
    global rounds_since_todo
    # global SYSTEM
    # s09: 根据最近对话加载相关记忆
    memories_content = load_memories(messages)
    # s09: 获取构建的记忆索引
    # SYSTEM += build_system()
    # 设置默认最大token
    max_tokens = DEFAULT_MAX_TOKENS
    state = RecoveryState()
    while True:
        # s09: 保存压缩前快照以准确提取内存
        pre_compress = [m if isinstance(m, dict) else {
            "role": m.get("role",""),
            "content": str(m.get("content",""))
        } for m in messages]
        # s08 先进行最大文件落盘 -> 掐头去尾保留中间的数据替换成占位符 ->  压缩
        # L3
        messages[:] = tool_result_budget(messages)
        # L1
        messages[:] = snip_compact(messages)
        # L2
        messages[:] = micro_compact(messages)
        # s05 如果无操作循环进行了3次，更新模型的todolist，注入提醒
        if rounds_since_todo >= 3 and messages:
            messages.append({
                "role": "user",
                "content": "<reminder>Update your todos.</reminder>"
            })
            rounds_since_todo = 0
        # s08 L4执行，文件落盘，调用LLM返回总结
        if estimate_size(messages) > CONTEXT_LIMIT:
            print("[L4:->auto compact]")
            messages[:] = compact_history(messages)
        
        request_messages = messages
        # 注入最近消息的相关记忆到最后一条数据
        memory_turn = None
        for i in range(len(messages) - 1, -1, -1):
            if isinstance(messages[i].get("content"), str):
                memory_turn = i
                break
        if memories_content and memory_turn is not None:
            print("[memories] 加载最近记忆")
            request_messages = messages.copy()
            request_messages[memory_turn] = {
                **messages[memory_turn],
                "content": memories_content + "\n\n" + messages[memory_turn]["content"],
            }
        
        # 注入定时器任务
        fired = consume_cron_queue()
        for job in fired:
            messages.append({
                "role": "user",
                "content": f"[Scheduled] {job.prompt}"
            })
            print(f"  \033[35m[inject cron] {job.prompt[:50]}\033[0m")

        # s11: LLM 调用及报错处理已抽到 llm.call_llm_with_recovery
        status, response, max_tokens = call_llm_with_recovery(
            messages,
            request_messages,
            system=system,
            tools=TOOLS,
            state=state,
            max_tokens=max_tokens,
        )
        if status == CALL_RETRY:
            continue
        if status == CALL_FINISH:
            return
        # 将 assistant 的回复追加到历史，供下次迭代使用
        messages.append({"role": "assistant", "content": response.content})
        # 如果 LLM 没有调用任何工具，说明它已经给出了最终答案，循环结束
        if response.stop_reason != "tool_use":
            # s09: 从压缩前快照提取以保持完整精度，写入新的记忆
            extract_memories(pre_compress)
            # s09：整理记忆
            consolidate_memories()
            force = trigger_hooks("Stop", messages)   # ← 退出之前
            if force:
                # hook returned a message → inject it and continue
                messages.append({"role": "user", "content": force})
                continue
            return
        # 模型调用一次后累加
        rounds_since_todo += 1
        # 遍历 LLM 返回的所有 block，执行 tool_use 类型的调用
        results = []
        for block in response.content:
            if block.type != "tool_use": continue
            print(f"\033[33m> block.name {block.name}\033[0m")
            # 执行L4落盘
            if block.name == "compact":
                messages[:] = compact_history(messages)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": "[Compacted. Conversation history has been summarized.]"
                })
                messages.append({
                    "role": "user",
                    "content": results
                })
                break  # 结束当前回合，用压缩的上下文重新开始
            # s04 调用工具前调用hook拦截【工具权限判断】
            blocked = trigger_hooks("PreToolUse", block)
            # 将工具执行结果作为 user 消息追加回历史，LLM 可据此继续推理
            if blocked:
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(blocked)
                })
                continue
            # 判断是否需要在后台执行，不需要则直接调用工具
            if should_run_background(block.name, block.input):
                bg_id = start_background_task(block)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": f"[Background task {bg_id} started] "
                        f"Command: {block.input.get('command', '')}. "
                        f"Result will be available when complete."}
                )
            else:
                output = execute_tool(block)
                print(str(output)[:300])
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output}
                )
                # 	工具执行后调用
                trigger_hooks("PostToolUse", block, output)  # s04: post hook
            # s05: 当调用 todo_write 时重置提醒计数器
            if block.name == "todo_write": rounds_since_todo = 0
        else:
            # 正常路径：没有调用压缩，在一条用户消息中注入工具结果和后台通知
            user_content = list(results)
            bg_notifications = collect_background_results()
            if bg_notifications:
                for notif in bg_notifications:
                    user_content.append({"type": "text", "text": notif})
                print(f"  \033[32m[inject] {len(bg_notifications)} background "
                    f"notification(s)\033[0m")
            messages.append({"role": "user", "content": user_content})
        # Re-evaluate context and prompt after each tool round
        context = update_context(context, messages)
        system = get_system_prompt(context)



#------------------------scheduler---------------------------------------------------------
session_history: list = []
session_context = update_context({}, [])

def queue_processor_loop():
    """当代理空闲时自动发送已触发的 cron 任务."""
    global session_context
    while True:
        time.sleep(0.2)
        # 判断有无任务
        if not has_cron_queue():
            continue
        # 尝试非阻塞获取 agent_lock。如果获取失败（说明 agent 正在处理用户输入或其他任务），则跳过，等待下次。
        if not agent_lock.acquire(blocking=False):
            continue
        # 获取锁后，再次检查 cron_queue（双重检查，防止在获取锁的瞬间队列被清空）。
        try:
            if not has_cron_queue():
                continue
            print("\n  \033[35m[queue processor] 按计划完成工作\033[0m")
            run_agent_turn_locked()
        finally:
            agent_lock.release()
            
def run_agent_turn_locked(user_query: str | None = None):
    """ 运行一个代理回合。调用者必须持有 agent_lock """
    global session_context
    if user_query is not None:
        session_history.append({"role": "user", "content": user_query})
    agent_loop(session_history, session_context)
    session_context = update_context(session_context, session_history)
    print_latest_assistant_text(session_history)
    # 每次会话结束：统计本次消耗的 token 并重置计数，供下次会话重新累计
    token_usage.print_usage()
    token_usage.reset()
    print()
    
def print_latest_assistant_text(messages: list):
    print("--------------------final content--------------------------")
    """负责把 agent 的最终文本回复呈现给终端用户。"""
    if not messages:
        return
    msg = messages[-1]
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return
    content = msg.get("content", "")
    if isinstance(content, str):
        print(content)
        return
    for block in content:
        if getattr(block, "type", None) == "text":
            print(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            print(block.get("text", ""))
            
agent_lock = threading.Lock()

# ── Entry point ──────────────────────────────────────────
if __name__ == "__main__":
    print(":s19 work tree")
    print("输入问题，回车发送。输入 q 退出。\n")
    threading.Thread(target=queue_processor_loop, daemon=True).start()
    history = []
    context = update_context({},[])
    while True:
        try:
            query = input("\033[36ms19 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        # 退出agent Loop
        if query.strip().lower() in ("q", "exit", ""):
            break
        # 用户输入提交后、进入 LLM 前调用Hooks，输入验证，注入上下文
        trigger_hooks("UserPromptSubmit", query)   # ← 进入 LLM 之前
        history.append({"role": "user", "content": query})
        with agent_lock:
            run_agent_turn_locked(query)
            
        inbox = consume_lead_inbox(route_protocol=True)
        if inbox:
            inbox_text = "\n".join(
                f"From {m['from']} [{m.get('type', 'message')}]: "
                f"{m['content'][:200]}" for m in inbox)
            history.append({"role": "user",
                            "content": f"[Inbox]\n{inbox_text}"})
        print()
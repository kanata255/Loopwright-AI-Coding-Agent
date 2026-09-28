import os
import sys
from dotenv import load_dotenv
from pathlib import Path
load_dotenv(override=True)
WORKDIR = Path.cwd()
from error_recovery import DEFAULT_MAX_TOKENS, RecoveryState
from tool_use import TOOLS, TOOL_HANDLERS
from hooks import trigger_hooks
from load_skill import SYSTEM as SKILLS_SYSTEM
from llm import call_llm_with_recovery, CALL_OK, CALL_RETRY, CALL_FINISH
from prompt import update_context,get_system_prompt
from context_compact import snip_compact,micro_compact,tool_result_budget,estimate_size,CONTEXT_LIMIT,compact_history
import token_usage

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
from memory import load_memories,build_system,extract_memories,consolidate_memories
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
        pre_compress = [m if isinstance(m, dict) else {"role": m.get("role",""),
                                                       "content": str(m.get("content",""))} for m in messages]
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
            handler = TOOL_HANDLERS.get(block.name)
            try:
                output = handler(**block.input) if handler else f"Unknown: {block.name}"
            except Exception as e:
                output = f"Error: {e}"
            # 	工具执行后调用
            trigger_hooks("PostToolUse", block, output)  # s04: post hook
            # s05: 当调用 todo_write 时重置提醒计数器
            if block.name == "todo_write": rounds_since_todo = 0
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                }
            )
        else:
            # 正常路径：没有调用压缩
            messages.append({"role": "user", "content": results})
        # Re-evaluate context and prompt after each tool round
        context = update_context(context, messages)
        system = get_system_prompt(context)

# ── Entry point ──────────────────────────────────────────
if __name__ == "__main__":
    print("s12: task system")
    print("输入问题，回车发送。输入 q 退出。\n")
    history = []
    context = update_context({},[])
    while True:
        try:
            query = input("\033[36ms12 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        # 退出agent Loop
        if query.strip().lower() in ("q", "exit", ""):
            break
        # 用户输入提交后、进入 LLM 前调用Hooks，输入验证，注入上下文
        trigger_hooks("UserPromptSubmit", query)   # ← 进入 LLM 之前
        history.append({"role": "user", "content": query})
        agent_loop(history,context)
        context = update_context(context, history)
        # agent_loop 结束后，history[-1] 就是 assistant 的最后一条消息。
        # 遍历其 content block 列表，找到 type=="text" 的 block 并打印，这就是 LLM 的最终答案。
        # （中间打印的 print1/print2 是工具执行过程，不是最终答案。）
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if getattr(block, "type", None) == "text":
                    print(f"final====>{block.text}")
        # 每次会话结束：统计本次消耗的 token 并重置计数，供下次会话重新累计
        token_usage.print_usage()
        token_usage.reset()
        print()

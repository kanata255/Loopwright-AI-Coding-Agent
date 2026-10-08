
from pathlib import Path
WORKDIR = Path.cwd()

from tool_use import run_read,run_bash,run_write,run_edit,run_glob
from hooks import trigger_hooks
from llm import call_llm

# s06: subagent gets its own system prompt no task, no recursion
SUB_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Complete the task you were given, then return a concise summary. "
    "Do not delegate further."
)

# 相比与主Agent 少了todo_write 用于写todo_list工具
SUB_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
]
SUB_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}
# 子agent最大安全循环
SAFE_LIMIT = 30
from tool_use import extract_text
def spawn_subagent(description: str) -> str:
    """生成一个带有新消息list的子代理，只返回摘要."""
    print(f"\n\033[35m[Subagent spawned]\033[0m")
    messages = [{"role": "user", "content": description}]  # fresh context
    for _ in range(SAFE_LIMIT):  # safety limit
        response = call_llm(
            messages, "subagent", system=SUB_SYSTEM, tools=SUB_TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            break
        results = []
        for block in response.content:
            if block.type == "tool_use":
                # Issue 1: subagent also runs hooks (permissions apply)
                blocked = trigger_hooks("PreToolUse", block)
                if blocked:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": str(blocked)})
                    continue
                handler = SUB_HANDLERS.get(block.name)
                try:
                    output = handler(**block.input) if handler else f"Unknown: {block.name}"
                except Exception as e:
                    output = f"Error: {e}"
                trigger_hooks("PostToolUse", block, output)
                print(f"  \033[90m[sub] {block.name}: {str(output)[:100]}\033[0m")
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": output})
        messages.append({"role": "user", "content": results})
    # agent循环结束，此时可能有两种情况
    # 1、 assistant 文本消息（最终答案）—— extract_text 能提取到内容
    # 2、 user 的 tool_result 消息（工具执行结果）—— 如果循环因安全限制 SAFE_LIMIT=30 被强制退出，最后一条就是工具结果，而不是最终答案
    # 闹到最后一条消息
    result = extract_text(messages[-1]["content"])
    # 如果最后一条数据不是最终答案，进入if判断
    # if not result: 是在处理「子 agent 没给出最终答案就被安全上限打断」的情况——它回退到历史里最近的一条 assistant 文本作为答案，再找不到就返回兜底提示，保证 spawn_subagent 永远有非空返回值。
    if not result:
        # 最后一条消息是 tool_result，从后往前找消息记录里的中间推理或最终答案
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                result = extract_text(msg["content"])
                if result:
                    break
        # 都没有结论
        if not result:
            result = "Subagent stopped after 30 turns without final answer."
    print(f"\033[35m[Subagent done]\033[0m")
    return result  # only summary, entire message history discarded
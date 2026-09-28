"""
update_context()  →  从真实状态派生 context（工具列表、工作目录、记忆内容）
        ↓
get_system_prompt(context)  →  缓存包装层，context 没变就复用
        ↓
assemble_system_prompt(context)  →  真正按 context 挑选并拼接 prompt 片段
        ↓
agent_loop 里每轮工具调用后重新走一遍上述流程
"""
import json
from pathlib import Path
WORKDIR = Path.cwd()
MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
from tool_use import TOOL_HANDLERS
from load_skill import list_skills

"""
用 topic 作为 key，把提示词拆成可独立管理的片段。
好处：每段职责单一，便于按需加载、按稳定顺序拼接（对 API 级 prompt cache 友好）。
"""
PROMPT_SECTIONS = {
    "identity": (
        "You are a coding agent. Act, don't explain. "
        "Before starting any multi-step task, use todo_write to plan your steps "
        "and update status as you go."
        "For complex sub-problems, use the task tool to spawn a subagent."
    ),
    "tools": f"Available tools: {', '.join(TOOL_HANDLERS.keys())}.",
    "workspace": f"Working directory: {WORKDIR}",
    "skills": f"Skills available:\n{list_skills()}\nUse load_skill to get full details when needed.",
    "memory": "Relevant memories are injected below when available.",
}
def assemble_system_prompt(context: dict) -> str:
    """根据当前情况选择并加入提示部分."""
    sections = []
    # 必须加载的几项
    sections.append(PROMPT_SECTIONS["identity"])
    sections.append(PROMPT_SECTIONS["tools"])
    sections.append(PROMPT_SECTIONS["workspace"])
    # 拼接技能目录（无技能时为空字符串，跳过即可）
    if list_skills():
        sections.append(PROMPT_SECTIONS["skills"])
    # 拼接记忆
    memories = context.get("memories", "")
    if memories:
        sections.append(f"{PROMPT_SECTIONS["workspace"]}:\n{memories}")
    return "\n\n".join(sections)


_last_context_key = None
_last_prompt = None
def get_system_prompt(context: dict) -> str:
    """缓存包装器——只有在上下文变化时才重新组装。
		使用 json.dumps 进行确定性序列化，而不是 Python 的 hash()，
		因为 hash() 有进程随机化且在嵌套字典/列表上会失败。
		这个缓存只是在同一进程内避免重复的字符串组装。
		真正的 Claude 代码还通过稳定的部分顺序和 SYSTEM_PROMPT_DYNAMIC_BOUNDARY 来保护 API 级别的提示缓存。
    """
    global _last_context_key, _last_prompt
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    if key == _last_context_key and _last_prompt:
        print("  \033[90m[cache hit] 命中缓存，prompt未发生变化\033[0m")
        return _last_prompt
    # 重新组装
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)
    loaded = ["identity", "tools", "workspace"]
    if list_skills():
        loaded.append("skills")
    if context.get("memories"):
        loaded.append("memory")
    print(f"  \033[32m[assembled] sections: {', '.join(loaded)}\033[0m")
    return _last_prompt


def update_context(context: dict, messages: list) -> dict:
    """从实际情况中获取上下文：有哪些工具，是否存在记忆文件."""
    memories = ""
    # 判断记忆文件还存在不
    if MEMORY_INDEX.exists():
        content = MEMORY_INDEX.read_text().strip()
        if content:
            memories = content
    return {
        "enabled_tools": list(TOOL_HANDLERS.keys()),
        "workspace": str(WORKDIR),
        "memories": memories,
    }

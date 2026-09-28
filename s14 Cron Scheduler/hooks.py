from pathlib import Path
from typing import Literal, TypeAlias
WORKDIR = Path.cwd()


Event: TypeAlias = Literal["UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]

HOOKS = {
    "UserPromptSubmit": [],
    "PreToolUse": [],
    "PostToolUse": [],
    "Stop": [],
}

# 注册 Hooks
def register_hook(event: Event, callback):
    HOOKS[event].append(callback)

# 通用的钩子分发函数，传入对应环节的参数，找到HOOKS里面对应的hook list
def trigger_hooks(event: Event, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:   # 返回值 != None 意味着 hook 说"停"
            return result
    return None

# 权限拦截 Bash命令
# 直接拒绝清单 [递归强制删除根目录、权限、关机、重启、格式化文件系统、磁盘裸写/克隆]
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]
# [ 删除文件、重定向覆盖/etc文件、给所有用户完全权限 、删除]
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777",'del']
def _is_file_edit(command: str) -> bool:
    """判断 bash 命令是否会写文件：输出重定向(> / >>) 或 sed -i / tee。"""
    if "sed -i" in command or "tee " in command:
        return True
    for op in (">>", ">"):
        idx = command.find(op)
        if idx != -1 and not command[idx + len(op):].strip().startswith("&"):
            return True
    return False

def permission_hook(block):
    """PreToolUse: s03的check_permission移动到hooks里面进行调用，避免agentLoop逐渐臃肿."""
    if block.name == "bash":
        for pattern in DENY_LIST:
            if pattern in block.input.get("command", ""):
                print(f"\n\033[31m⛔ Blocked: '{pattern}'\033[0m")
                # 直接拒接
                return "Permission denied by deny list"
        for kw in DESTRUCTIVE:
            if kw in block.input.get("command", ""):
                print(f"\n\033[33m⚠  Potentially destructive command\033[0m")
                print(f"   Tool: {block.name}({block.input})")
                choice = input("   Allow? [y/N] ").strip().lower()
                if choice not in ("y", "yes"):
                    return "Permission denied by user"
        if _is_file_edit(block.input.get("command", "")):
            print(f"\n\033[33m⚠  Bash命令意图更改文件\033[0m")
            print(f"   Tool: {block.name}({block.input})")
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"
    if block.name in ("write_file", "edit_file"):
        path = block.input.get("path", "")
        if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
            print(f"\n\033[33m⚠  Writing outside workspace\033[0m")
            print(f"   Tool: {block.name}({block.input})")
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"
    return None


def context_inject_hook(query: str) -> str | None:
    """把当前工作目录的信息注入到每个 prompt 里."""
    print(f"\033[90m[HOOK] UserPromptSubmit: 当前工作目录 {WORKDIR}\033[0m")
    return None   # return None = no modification, let prompt through


def log_hook(block):
    """PreToolUse: log every tool call."""
    args_preview = str(list(block.input.values())[:2])[:60]
    print(f"\033[90m[HOOK] {block.name}({args_preview})\033[0m")
    return None
def large_output_hook(block, output):
    """PostToolUse: warn on large output."""
    if len(str(output)) > 100000:
        print(f"\033[33m[HOOK] ⚠ Large output from {block.name}: {len(str(output))} chars\033[0m")
    return None

# Stop hook:当循环即将结束时打印总结
def summary_hook(messages: list):
    tool_count = sum(1 for m in messages
                     for b in (m.get("content") if isinstance(m.get("content"), list) else [])
                     if isinstance(b, dict) and b.get("type") == "tool_result")
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None

# 进行注册
register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)

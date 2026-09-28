"""
tool_use.py — 工具定义与执行模块
包含所有 LLM 可调用的工具及其执行函数。
s02: Tool Use — 在 s01 基础上新增 4 个工具 + 分发映射。
运行: python s02_tool_use/code.py
需要: pip install anthropic python-dotenv + .env 中配置 ANTHROPIC_API_KEY
本文件 = s01 的全部代码 + 以下新增:
  + run_read / run_write / run_edit / run_glob 四个工具实现
  + TOOL_HANDLERS 分发映射（替代 s01 中硬编码的 run_bash 调用）
  + safe_path 路径安全校验
循环本身（agent_loop）与 s01 完全一致。
"""

import ast
import json
import os
import subprocess
from pathlib import Path

WORKDIR = Path.cwd()


# ═══════════════════════════════════════════════════════════
#  NEW in s02: 4 个新工具
# ═══════════════════════════════════════════════════════════
def safe_path(p: str) -> Path:
    """校验读文件路径是否在工作区内，越界时交互式征求用户授权。"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        print(f"\n⚠  read_file 越界：{path}")
        choice = input("   允许读取工作区之外的文件吗? [y/N] ").strip().lower()
        if choice not in ("y", "yes"):
            raise ValueError(f"Permission denied: {p}")
    return path


def run_bash(command: str) -> str:
    """执行 shell 命令并返回输出。"""
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(
            command,
            shell=True,
            cwd=os.getcwd(),
            capture_output=True,
            timeout=120,
        )
        out = _decode((r.stdout or b"") + (r.stderr or b"")).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


def _decode(data: bytes) -> str:
    """按 utf-8 → gbk 顺序解码子进程输出，兜底 replace 避免抛 UnicodeDecodeError。"""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def run_read(path, limit=None):
    """读取文件内容，可选按行数限制。"""
    lines = safe_path(path).read_text().splitlines()
    if limit:
        lines = lines[:limit]
    return "\n".join(lines)


def run_write(path, content):
    """将内容写入指定路径的文件，返回写入字节数。"""
    # 写操作不走 safe_path 的 workspace 限制：越界写已由 permission-已继承进hooks.py 的闸门 2+3 把关，
    # 用户批准后允许落到 workspace 外。这里直接按给定路径解析并写入。
    Path(path).resolve().write_text(content)
    return f"Wrote {len(content)} bytes to {path}"


def run_edit(path, old_text, new_text):
    """将文件中的旧文本替换为新文本（仅替换首次出现），并写回。"""
    # 与 run_write 同理：写操作不受 workspace 限制（闸门已把关）。
    target = Path(path).resolve()
    text = target.read_text()
    if old_text not in text:
        return "Error: text not found"
    target.write_text(text.replace(old_text, new_text, 1))
    return f"Edited {path}"


def run_glob(pattern):
    """按 glob 模式在工作区内查找文件，返回匹配路径列表。"""
    import glob as g
    
    return "\n".join(g.glob(pattern, root_dir=WORKDIR))


# todo_write 工具，接收一个带状态的列表，保存在当前进程内存中，同时在终端显示进度：
CURRENT_TODOS: list[dict] = []


def run_todo_write(todos: list) -> str:
    """将任务列表保存到内存并在终端打印带状态图标的进度。"""
    global CURRENT_TODOS
    CURRENT_TODOS = todos
    
    lines = ["\n## Current Tasks"]
    for t in CURRENT_TODOS:
        icon = {"pending": " ", "in_progress": "▸", "completed": "✓"}[t["status"]]
        lines.append(f"  [{icon}] {t['content']}")
    print("\n".join(lines))
    return f"Updated {len(CURRENT_TODOS)} tasks"

def extract_text(content) -> str:
    """从消息内容块中提取文本."""
    """  extract_text 只收集 type == "text" 的 block。而 tool_result 消息的 block 类型是 "tool_result"，不是 "text"，所以会返回空字符串 """
    if not isinstance(content, list):
        return str(content)
    return "\n".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")

def _normalize_todos(todos):
    """校验并规范化 todos 输入：支持字符串(JSON/字面量)或列表，返回 (todos, 错误信息)。"""
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                return None, "Error: todos must be a list or JSON array string"
    if not isinstance(todos, list):
        return None, "Error: todos must be a list"
    for i, t in enumerate(todos):
        if not isinstance(t, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "content" not in t or "status" not in t:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        if t["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] has invalid status '{t['status']}'"
    return todos, None


# task tool
from task_system import create_task, list_tasks, get_task, claim_task, complete_task


def run_create_task(subject: str, description: str = "",
                    blockedBy: list[str] | None = None) -> str:
    # 创建任务
    try:
        task = create_task(subject, description, blockedBy)
    except ValueError as e:
        return f"Error: {e}"
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
    # 作为 tool_result 返回给模型
    return f"Created {task.id}: {task.subject}{deps}"


def run_list_tasks() -> str:
    # 拿到时间排序的任务list
    tasks = list_tasks()
    if not tasks:
        return "No tasks. Use create_task to add some."
    lines = []
    for t in tasks:
        icon = {
            "pending": "○",
            "in_progress": "●",
            "completed": "✓"
        }.get(t.status, "?")  # 兜底未知状态
        # 获取任务依赖项
        deps = f" (blockedBy: {', '.join(t.blockedBy)})" if t.blockedBy else ""
        # 获取任务owner
        owner = f" [{t.owner}]" if t.owner else ""
        # 注入list 作为tool_result返回结果
        lines.append(f"  {icon} {t.id}: {t.subject} "
                    f"[{t.status}]{owner}{deps}")
    return "\n".join(lines)


def run_get_task(task_id: str) -> str:
    """
    :param task_id:
    :return: Task
    """
    try:
        return get_task(task_id)
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"


def run_claim_task(task_id: str) -> str:
    # 单 agent 场景的简化。多 agent 时这里应该由调用方传 owner。
    return claim_task(task_id, owner="agent")


def run_complete_task(task_id: str) -> str:
    # 传入id完成任务
    return complete_task(task_id)


from subagent import spawn_subagent
from load_skill import load_skill

TOOL_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
    "todo_write": run_todo_write,
    "task": spawn_subagent,
    "load_skill": load_skill,
    # task_system
    "create_task": run_create_task,
    "list_tasks": run_list_tasks,
    "get_task": run_get_task,
    "claim_task": run_claim_task,
    "complete_task": run_complete_task,
}

# ── Tool definitions ──────────────────────────────────────
TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read file contents.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace exact text in a file once.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "glob",
        "description": "Find files matching a glob pattern.",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
    },
    # s05: 新增一条
    {
        "name": "todo_write", "description": "Create and manage a task list ...",
        "input_schema": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {
                                "type":  "string",
                                "enum": ["pending", "in_progress", "completed"]
                            },
                        },
                    },
                },
            },
        },
    },
    # s05 subagent 子agent
    {
        "name": "task",
        "description": "Launch a subagent to handle a complex subtask. Returns only the final conclusion.",
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string"}
            },
            "required": ["description"]
        },
    },
    # s07 skills
    {
        "name": "load_skill",
        "description": "Load the full content of a skill by name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"}
            },
            "required": ["name"]
        }
    },
    # s08 压缩上下文
    {
        "name": "compact",
        "description": "Summarize earlier conversation to free context space.",
        "input_schema": {
            "type": "object",
            "properties": {
                "focus": {"type": "string"}
            }
        }
    },
    # s12 task system
    {
        "name": "create_task",
        "description": "Create a new task with optional blockedBy dependencies.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "description": {"type": "string"},
                "blockedBy": {
                    "type": "array",
                    "items": {"type": "string"}
                }
            },
            "required": ["subject"]
        }
    },
    {
        "name": "list_tasks",
        "description": "List all tasks with status, owner, and dependencies.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "get_task",
        "description": "Get full details of a specific task by ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"}
            },
            "required": ["task_id"]
        }
    },
    {
        "name": "claim_task",
        "description": "Claim a pending task. Sets owner, changes status to in_progress.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"}
            },
            "required": ["task_id"]
        }
    },
    {
        "name": "complete_task", "description": "Complete an in-progress task. Reports unblocked downstream tasks.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"]
        }
    },
]

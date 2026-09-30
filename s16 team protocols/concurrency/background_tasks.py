import  threading
from tool_use import execute_tool

_bg_counter = 0
# 后台任务
background_tasks: dict[str, dict] = {}   # bg_id → {tool_use_id, command, status}
# 后台任务执行的实际输出字符串
background_results: dict[str, str] = {}   # bg_id → output
# 作用：保护 background_tasks 和 background_results 的并发访问。
background_lock = threading.Lock()
def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    """
    :param tool_name 工具名称
    :param tool_input 执行命令类型
    :return bool true：缓慢执行
    启发式判断一个工具调用是否可能耗时超过 30 秒，只对bash生效
    """
    if tool_name != "bash":
        return False
    cmd = tool_input.get("command", "").lower()
    # 需要后台执行的一些关键字
    slow_keywords = ["install", "build", "test", "deploy", "compile",
                     "docker build", "pip install", "npm install",
                     "cargo build", "pytest", "make"]
    return any(kw in cmd for kw in slow_keywords)


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    """最终决定是否将某个工具调用放入后台执行。."""
    # 如果模型就直接返回需要后台运行的字段，直接返回True，否则在进行启发式回退
    if tool_input.get("run_in_background"):
        return True
    return is_slow_operation(tool_name, tool_input)

def start_background_task(block) -> str:
    """在守护线程中运行工具。返回后台任务ID。"""
    global _bg_counter
    _bg_counter += 1
    bg_id = f"bg_{_bg_counter:04d}"
    cmd = block.input.get("command", block.name)
    def worker():
        result = execute_tool(block)
        # execute_tool 内部已捕获异常，失败时返回 "Error: ..." 或 "Unknown tool: ..."
        failed = (
		        result.startswith("Error:")
		        or result.startswith("Unknown tool:")
        )
        status = "failed" if failed else "completed"
        with background_lock:
            background_tasks[bg_id]["status"] = status
            background_results[bg_id] = result
    with background_lock:   # 在线程启动前，先加锁，注册任务，状态设置为执行中
        background_tasks[bg_id] = {
            "tool_use_id": block.id,
            "command": cmd,
            "status": "running",
        }
    thread = threading.Thread(target=worker, daemon=True)  # 创建并启动线程，daemon=True 意味着主程序退出时后台线程会被强制终止
    thread.start()
    print(f"  \033[33m[background task] 已派发任务 {bg_id}: {cmd[:40]}\033[0m")
    return bg_id

def collect_background_results() -> list[str]:
    """收集所有已完成的后台任务，转换为 <task_notification> 格式的字符串列表，并从字典中移除，避免重复通知。"""
    with background_lock:
        ready_ids = [bid for bid, task in background_tasks.items() # 找出已经完成的任务
                     if task["status"] == "completed"]
    notifications = []
    for bg_id in ready_ids:
        with background_lock:
            task = background_tasks.pop(bg_id)             # 弹出已经完成的任务
            output = background_results.pop(bg_id, "")     # pop 出结果字符串（默认空串）。
        summary = output[:200] if len(output) > 200 else output
        notifications.append(
            f"<task_notification>\n"
            f"  <task_id>{bg_id}</task_id>\n"
            f"  <status>completed</status>\n"
            f"  <command>{task['command']}</command>\n"
            f"  <summary>{summary}</summary>\n"
            f"</task_notification>")
        print(f"  \033[32m[background done] {bg_id}: "
              f"{task['command'][:40]} ({len(output)} chars)\033[0m")
    return notifications
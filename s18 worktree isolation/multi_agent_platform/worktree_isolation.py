from pathlib import Path
WORKDIR = Path.cwd()
import  subprocess, json, time,re
from multi_agent_platform.task_system import load_task,save_task
WORKTREES_DIR = WORKDIR / ".worktrees"
WORKTREES_DIR.mkdir(exist_ok=True)
VALID_WT_NAME = re.compile(r'^[A-Za-z0-9._-]{1,64}$')
def validate_worktree_name(name: str) -> str | None:
    """
    :description:名称安全校验
    如果无效则返回错误信息，如果有效则返回 None。
    """
    if not name:
        return "Worktree name cannot be empty"
    if name == "." or name == "..":
        return f"'{name}' is not a valid worktree name"
    if not VALID_WT_NAME.match(name):
        return (f"Invalid worktree name '{name}': "
                "only letters, digits, dots, underscores, dashes (1-64 chars)")
    return None

def run_git(args: list[str]) -> tuple[bool, str]:
    """运行 git 命令。返回 (ok, output)."""
    try:
        r = subprocess.run(["git"] + args, cwd=WORKDIR,capture_output=True, text=True, timeout=30)
        out = (r.stdout + r.stderr).strip()
        out = out[:5000] if out else "(no output)"
        return r.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, "Error: git timeout"

def log_event(event_type: str, worktree_name: str, task_id: str = ""):
    """最佳日志."""
    event = {"type": event_type, "worktree": worktree_name,"task_id": task_id, "ts": time.time()}
    events_file = WORKTREES_DIR / "events.jsonl"
    with open(events_file, "a") as f:
        f.write(json.dumps(event) + "\n")
        
def create_worktree(name: str, task_id: str = "") -> str:
    """ 创建 worktree + 绑定任务."""
    err = validate_worktree_name(name)
    if err:
        return f"Error: {err}"
    # 防止重复创建
    path = WORKTREES_DIR / name
    if path.exists():
        return f"Worktree '{name}' already exists at {path}"
    ok, result = run_git(["worktree", "add", str(path), "-b", f"wt/{name}", "HEAD"])
    if not ok:
        return f"Git error: {result}"
    if task_id:
        bind_task_to_worktree(task_id, name)
    # 记录事件
    log_event("create", name, task_id)
    print(f"  \033[33m[worktree] created: {name} at {path}\033[0m")
    return f"Worktree '{name}' created at {path}"
def bind_task_to_worktree(task_id: str, worktree_name: str):
    """将工作树字段写入任务。保持状态为待处理以便自动认领."""
    task = load_task(task_id)
    task.worktree = worktree_name
    save_task(task)
    print(f"  \033[33m[bind] {task.subject} �� worktree:{worktree_name}\033[0m")
def _count_worktree_changes(path: Path) -> tuple[int, int]:
    """统计工作树中的未提交文件和提交."""
    try:
        r1 = subprocess.run(["git", "status", "--porcelain"],
                            cwd=path, capture_output=True, text=True, timeout=10)
        files = len([l for l in r1.stdout.strip().splitlines() if l.strip()])
        r2 = subprocess.run(["git", "log", "@{push}..HEAD", "--oneline"],
                            cwd=path, capture_output=True, text=True, timeout=10)
        commits = len([l for l in r2.stdout.strip().splitlines() if l.strip()])
        return files, commits
    except Exception:
        return -1, -1
def remove_worktree(name: str, discard_changes: bool = False) -> str:
    
    """删除工作树。如果有未提交的更改则拒绝，除非使用 discard_changes"""
    # 1、安全性校验、存在性校验
    err = validate_worktree_name(name)
    if err:
        return err
    path = WORKTREES_DIR / name
    if not path.exists():
        return f"Worktree '{name}' not found"
    # 2、安全性检查（只有明确 discard_changes=False（默认）且工作树干净时，才允许删除
    if not discard_changes:
        files, commits = _count_worktree_changes(path)
        if files < 0:
            # "无法确认状态，请用 discard_changes=true 强制删除"
            return (f"Cannot verify worktree '{name}' status. "
                    "Use discard_changes=true to force removal.")
        if files > 0 or commits > 0:
            #  f"有 {files} 个未提交文件和 {commits} 个未推送提交，..."
            return (f"Worktree '{name}' has {files} uncommitted file(s) "
                    f"and {commits} unpushed commit(s). "
                    "Use discard_changes=true to force removal, "
                    "or keep_worktree to preserve for review.")
    ok1, _ = run_git(["worktree", "remove", str(path), "--force"])
    if not ok1:
        return f"Failed to remove worktree directory for '{name}'"
    run_git(["branch", "-D", f"wt/{name}"])
    log_event("remove", name)
    print(f"  \033[33m[worktree] removed: {name}\033[0m")
    return f"Worktree '{name}' removed"
def keep_worktree(name: str) -> str:
    """保留工作树以供手动审查。保留分支."""
    err = validate_worktree_name(name)
    if err:
        return err
    log_event("keep", name)
    print(f"  \033[36m[worktree] kept: {name}\033[0m")
    return f"Worktree '{name}' kept for review (branch: wt/{name})"

def run_create_worktree(name: str, task_id: str = "") -> str:
    return create_worktree(name, task_id)
def run_remove_worktree(name: str, discard_changes: bool = False) -> str:
    return remove_worktree(name, discard_changes)
def run_keep_worktree(name: str) -> str:
    return keep_worktree(name)

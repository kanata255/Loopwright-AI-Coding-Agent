"""
�־û�����ϵͳ
"""
from pathlib import Path
WORKDIR = Path.cwd()
from dataclasses import dataclass, asdict
import  json, time, random

TASKS_DIR = WORKDIR / ".tasks"
TASKS_DIR.mkdir(exist_ok=True)

@dataclass
class Task:
    id: str                 # 任务唯一标识
    subject: str            # 任务标题
    description: str        # 详细描述
    status: str             # pending | in_progress | completed  未完成 | 进行中 | 完成
    owner: str | None       # 认领者
    blockedBy: list[str]    #   依赖列表 A.blockedBy = [B] B执行完毕之后才能执行A
    
def _task_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.json"

""" asdict 双向序列化：asdict(Task) → dict → JSON；读回来用 Task(**dict) 还原。 """
def save_task(task: Task):
    """ 序列化任务并写入文件 """
    _task_path(task.id).write_text(json.dumps(asdict(task), indent=2))
def load_task(task_id: str) -> Task:
    """ 加载序列化文件 """
    return Task(**json.loads(_task_path(task_id).read_text()))
def list_tasks() -> list[Task]:
    # 按照创建时间进行排序，文件名排序=== 创建时间排序
    return [Task(**json.loads(p.read_text()))
            for p in sorted(TASKS_DIR.glob("task_*.json"))]
def get_task(task_id: str) -> str:
    """ 直接返回格式化 JSON 文本，模型容易解析. """
    task = load_task(task_id)
    return json.dumps(asdict(task), indent=2)

def _load_all_tasks() -> dict[str, Task]:
    """加载全部任务为 {id: Task}，损坏/缺失文件静默跳过。"""
    tasks = {}
    for p in TASKS_DIR.glob("task_*.json"):
        try:
            t = Task(**json.loads(p.read_text()))
            tasks[t.id] = t
        except (json.JSONDecodeError, TypeError, KeyError):
            continue
    return tasks


def detect_cycle(task_id: str, extra: Task | None = None) -> list[str] | None:
    """沿 blockedBy 边做 DFS，检测从 task_id 出发是否成环。

    返回闭合环上的 id 列表（如 ['A', 'B', 'A']），无环返回 None。
    extra 用于把尚未落盘的新任务纳入图参与检测。
    """
    tasks = _load_all_tasks()
    if extra is not None:
        tasks[extra.id] = extra
    visiting: list[str] = []
    visited: set[str] = set()

    def dfs(cur: str) -> list[str] | None:
        if cur in visiting:
            i = visiting.index(cur)
            return visiting[i:] + [cur]
        if cur in visited or cur not in tasks:
            return None
        visiting.append(cur)
        for dep in tasks[cur].blockedBy:
            cycle = dfs(dep)
            if cycle:
                return cycle
        visiting.pop()
        visited.add(cur)
        return None

    return dfs(task_id)


def create_task(subject: str, description: str = "",blockedBy: list[str] | None = None) -> Task:
    """
    :description 创建并保存一个任务到文件
    :param subject: 任务标题
    :param description: 详细描述
    :param blockedBy: 依赖项
    :return: Task
    """
    task = Task(
        id=f"task_{int(time.time())}_{random.randint(0, 9999):04d}",
        subject=subject,
        description=description,
        status="pending",
        owner=None,
        blockedBy=blockedBy or [],
    )
    cycle = detect_cycle(task.id, extra=task)
    if cycle:
        raise ValueError(f"circular dependency: {' -> '.join(cycle)}")
    save_task(task)
    return task

def can_start(task_id: str) -> bool:
    """ 检查所有被阻塞的依赖是否已完成。缺失的依赖项将被视为阻塞 """
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        # 判断依赖任务文件是否还存在
        if not _task_path(dep_id).exists():
            return False
        # 文件存在，判断task的状态是否完成，未完成返回fasle
        if load_task(dep_id).status != "completed":
            return False
    return True

def claim_task(task_id: str, owner: str = "agent") -> str:
    # 任务认领
    task = load_task(task_id)
    # 状态检查：只有 pending 能认领。已在 in_progress 或 completed 的任务会被拒绝，防止重复认领。
    if task.status != "pending":
        return f"Task {task_id} is {task.status}, cannot claim"
    # 依赖项判断
    if not can_start(task_id):
        cycle = detect_cycle(task_id)
        if cycle:
            return f"Error: circular dependency detected: {' -> '.join(cycle)}"
        deps = [d for d in task.blockedBy
                if not _task_path(d).exists() or load_task(d).status != "completed"]
        return f"Blocked by: {deps}"
    task.owner = owner
    task.status = "in_progress"
    # 保存更新进文件
    save_task(task)
    print(f"  \033[36m[claim] {task.subject} -> 进行中： (owner: {owner})\033[0m")
    return f"Claimed {task.id} ({task.subject})"

def complete_task(task_id: str) -> str:
    # 完成任务方法
    task = load_task(task_id)
    # 只有 in_progress 能完成。没认领就完成、或重复完成都会被拒。
    if task.status != "in_progress":
        return f"Task {task_id} is {task.status}, cannot complete"
    task.status = "completed"
    save_task(task)
    """
        扫描任务进行解锁
        1、扫描所有任务
        2、判断是否还未开工 status === pending
        3、判断依赖项不为空的，依赖项为空不需要进行解锁
        4、依赖项都完成了的
    """
    unblocked = [t.subject for t in list_tasks()
                 if t.status == "pending" and t.blockedBy and can_start(t.id)]
    print(f"  \033[32m[complete] {task.subject} ?\033[0m")
    # 拼接解解锁任务进消息，让模型知道接下来可以做什么
    msg = f"Completed {task.id} ({task.subject})"
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
        print(f"  \033[33m[解锁任务] {', '.join(unblocked)}\033[0m")
    return msg



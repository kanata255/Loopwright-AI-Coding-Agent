import  json, random, threading
from pathlib import Path
WORKDIR = Path.cwd()
from datetime import datetime
DURABLE_PATH = WORKDIR / ".scheduled_tasks.json"
from dataclasses import dataclass, asdict
import time

@dataclass
class CronJob:
    id: str
    cron: str        # "0 9 * * *"  5字段 cron 表达式（分 时 日 月 周
    prompt: str      # 任务触发时要注入给 agent 的消息文本
    recurring: bool  # 是否为重复任务。True 表示每次匹配都触发；False 表示只触发一次，触发后从调度表中移除。
    durable: bool    # 是否持久化。True 会写入 .scheduled_tasks.json，重启后自动加载；False 仅存在于内存，重启丢失。
    
scheduled_jobs: dict[str, CronJob] = {}
cron_queue: list[CronJob] = []
cron_lock = threading.RLock()
_last_fired: dict[str, str] = {}  # job_id → "YYYY-MM-DD HH:MM"

def _cron_field_matches(field: str, value: int) -> bool:
    """将单个 cron 字段与一个值匹配."""
    if field == "*":
        return True
    if field.startswith("*/"):
        step = int(field[2:])
        return step > 0 and value % step == 0
    if "," in field:
        return any(_cron_field_matches(f.strip(), value)
                for f in field.split(","))
    if "-" in field:
        lo, hi = field.split("-", 1)
        return int(lo) <= value <= int(hi)
    return value == int(field)

def cron_matches(cron_expr: str, dt: datetime) -> bool:
    """ 检查一个5字段的cron表达式是否与给定的日期时间匹配。标准cron语义：当日和周都被限制时，它们使用OR """
    # 字段顺序：分、时、日、月、周
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    dow_val = (dt.weekday() + 1) % 7  # dow_val 转换：Python 中 Monday=0，cron 中 Sunday=0，所以 (weekday+1)%7 将周一转为 1，周日转为 0
    # 分钟
    m = _cron_field_matches(minute, dt.minute)
    # 小时
    h = _cron_field_matches(hour, dt.hour)
    # 日
    dom_ok = _cron_field_matches(dom, dt.day)
    # 月
    month_ok = _cron_field_matches(month, dt.month)
    # 周
    dow_ok = _cron_field_matches(dow, dow_val)
    # Minute, hour, month must all match
    if not (m and h and month_ok):
        return False
    # DOM and DOW: if both constrained, either matching is enough (OR)
    dom_unconstrained = dom == "*"
    dow_unconstrained = dow == "*"
    if dom_unconstrained and dow_unconstrained:
        return True
    if dom_unconstrained:
        return dow_ok
    if dow_unconstrained:
        return dom_ok
    return dom_ok or dow_ok


def _validate_cron_field(field: str, lo: int, hi: int) -> str | None:
    """ 验证单个 cron 字段值是否在 [lo, hi] 范围内 """
    if field == "*":
        return None
    if field.startswith("*/"):
        step_str = field[2:]
        if not step_str.isdigit():
            return f"Invalid step: {field}"
        step = int(step_str)
        if step <= 0:
            return f"Step must be > 0: {field}"
        return None
    if "," in field:
        for part in field.split(","):
            err = _validate_cron_field(part.strip(), lo, hi)
            if err: return err
        return None
    if "-" in field:
        parts = field.split("-", 1)
        if not parts[0].isdigit() or not parts[1].isdigit():
            return f"Invalid range: {field}"
        a, b = int(parts[0]), int(parts[1])
        if a < lo or a > hi or b < lo or b > hi:
            return f"Range {field} out of bounds [{lo}-{hi}]"
        if a > b:
            return f"Range start > end: {field}"
        return None
    if not field.isdigit():
        return f"Invalid field: {field}"
    val = int(field)
    if val < lo or val > hi:
        return f"Value {val} out of bounds [{lo}-{hi}]"
    return None


def validate_cron(cron_expr: str) -> str | None:
    """验证 cron 表达式是否合法，返回错误信息或 None"""
    # 没有5段直接报错
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return f"Expected 5 fields, got {len(fields)}"
    # 字段边界：分 0-59，时 0-23，日 1-31，月 1-12，周 0-6。
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    names = ["minute", "hour", "day-of-month", "month", "day-of-week"]
    for i, (field, (lo, hi), name) in enumerate(zip(fields, bounds, names)):
        err = _validate_cron_field(field, lo, hi)
        if err:
            return f"{name}: {err}"
    return None


def save_durable_jobs():
    """将持久化的作业保存到 .scheduled_tasks.json."""
    print("scheduled_jobs-------------------------")
    with cron_lock:
        print(f"scheduled_jobs:{scheduled_jobs}")
        durable = [asdict(j) for j in scheduled_jobs.values() if j.durable]
        DURABLE_PATH.write_text(json.dumps(durable, indent=2))
    
def load_durable_jobs():
    """在启动时从磁盘加载持久化作业."""
    if not DURABLE_PATH.exists():
        return
    try:
        jobs = json.loads(DURABLE_PATH.read_text())
        for j in jobs:
            # 将json创建成CronJob类型
            job = CronJob(**j)
            # 判断类型的时间是否合法
            err = validate_cron(job.cron)
            if err:
                print(f"  \033[31m[cron] skipping invalid job {job.id}: {err}\033[0m")
                continue
            
            # 载入内存
            scheduled_jobs[job.id] = job
        valid = [j for j in jobs if j["id"] in scheduled_jobs]
        if valid:
            print(f"  \033[35m[cron] loaded {len(valid)} durable job(s) 已经加载{len(valid)}个项目\033[0m")
    except Exception:
        pass
    
def schedule_job(cron: str, prompt: str, recurring: bool = True,
            durable: bool = True) -> CronJob | str:
    """
    :description 注册一个新的定时任务
    :param cron 时间匹配字符串
    :param prompt 任务触发时要注入给 agent 的消息文本
    :param recurring  是否为重复任务。True 表示每次匹配都触发；False 表示只触发一次，触发后从调度表中移除。
    :param durable    是否持久化。True 会写入 .scheduled_tasks.json，重启后自动加载；False 仅存在于内存，重启丢失。
    :return  CronJob 或错误字符串."""
    # 验证 cron 是否合法
    err = validate_cron(cron)
    if err:
        return err
    # 创建CronJob类，生成随机ID
    job = CronJob(
        id=f"cron_{random.randint(0, 999999):06d}",
        cron=cron, prompt=prompt,
        recurring=recurring, durable=durable,
    )
    # 加锁 加入全局字典，防止其他地方在读取
    with cron_lock:
        scheduled_jobs[job.id] = job
    # 是否需要持久化存储
    if durable:
        save_durable_jobs()
    print(f"  \033[35m[cron register] {job.id} '{cron}' → {prompt[:40]}\033[0m")
    return job

def cancel_job(job_id: str) -> str:
    """取消一个定时任务."""
    # 加锁取消
    with cron_lock:
        job = scheduled_jobs.pop(job_id, None)
        _last_fired.pop(job_id,None)
    if not job:
        return f"Job {job_id} not found"
    # 是否需要持久化存储，如果是需要持久化的内容，则需要进行全量覆写，把当前字典里面的内容重新写入磁盘中
    if job.durable:
        save_durable_jobs()
    print(f"  \033[31m[cron cancel] {job_id}\033[0m")
    return f"Cancelled {job_id}"

def cron_scheduler_loop():
    """独立守护线程：每1秒轮询一次，触发匹配的任务。单个任务错误会被捕获，以防一个错误的任务导致整个调度线程崩溃"""
    while True:
        time.sleep(1)
        now = datetime.now()
        # 日期感知标记防止每日作业在第2天跳过
        # 生成 minute_marker（精确到分钟，如 "2025-01-01 09:30"），用于防止同一分钟内重复触发。需要一个机制来记录：“这个任务在这一分钟已经触发过了，不要再触发”，一分钟内只触发一次
        minute_marker = now.strftime("%Y-%m-%d %H:%M")
        with cron_lock:
            # 读出内存中的定时任务
            for job in list(scheduled_jobs.values()):
                try:
                    # 匹配日期时间
                    if cron_matches(job.cron, now):
                        if _last_fired.get(job.id) != minute_marker:
                            # 将 job 追加到 cron_queue（等待 agent 消费）。
                            cron_queue.append(job)
                            # 更新时间
                            # 更新时间
                            _last_fired[job.id] = minute_marker
                            print(f"  \033[35m[cron fire] {job.id} → "
                                f"{job.prompt[:40]}\033[0m")
                        # 如果不是重复任务，则需要在内存中删除，再判断是否持久化，再磁盘上进行删除
                        if not job.recurring:
                            scheduled_jobs.pop(job.id, None)
                            if job.durable:
                                save_durable_jobs()
                except Exception as e:
                    print(f"  \033[31m[cron error] {job.id}: {e}\033[0m")
                    
def consume_cron_queue() -> list[CronJob]:
    """从 cron_queue 消耗已触发的作业（由 agent_loop 调用）. 消费内存中的定时任务"""
    with cron_lock:
        fired = list(cron_queue)
        cron_queue.clear()
    return fired
def has_cron_queue() -> bool:
    """加锁检查队列是否非空."""
    with cron_lock:
        return bool(cron_queue)
    
# 在启动时加载持久化任务，然后启动调度线程
load_durable_jobs()
# 创建守护线程，每秒检查 cron 匹配，向队列投递任务
threading.Thread(target=cron_scheduler_loop, daemon=True).start()
print("  \033[35m[cron] scheduler thread started\033[0m")


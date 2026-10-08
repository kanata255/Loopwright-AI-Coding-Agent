import json,time

IDLE_POLL_INTERVAL = 5   # seconds
IDLE_TIMEOUT = 60         # seconds
from multi_agent_platform.task_system import claim_task,TASKS_DIR,can_start
from multi_agent_platform.agent_team import BUS

def scan_unclaimed_tasks() -> list[dict]:
    """查找所有依赖项已完成的待处理、未拥有的任务."""
    unclaimed = []
    for f in sorted(TASKS_DIR.glob("task_*.json")):
        task = json.loads(f.read_text())
        if (task.get("status") == "pending"
                and not task.get("owner")
                and can_start(task["id"])):
            unclaimed.append(task)
    return unclaimed


def idle_poll(agent_name: str, messages: list,name: str, role: str) -> str:
    """轮询 60 秒。返回 'work'、'shutdown' 或 'timeout'."""
    for _ in range(IDLE_TIMEOUT // IDLE_POLL_INTERVAL):
        time.sleep(IDLE_POLL_INTERVAL)
        # 先看收件箱
        inbox = BUS.read_inbox(agent_name)
        if inbox:
            for msg in inbox:
                # 收到 shutdown_request → 回执 + 返回 "shutdown"
                if msg.get("type") == "shutdown_request":
                    req_id = msg.get("metadata", {}).get("request_id", "")
                    BUS.send(name, "lead", "Shutting down gracefully.","shutdown_response",{"request_id": req_id, "approve": True})
                    print(f"  \033[35m[protocol] {name} approved shutdown "f"in idle ({req_id})\033[0m")
                    return "shutdown"
            # 不是关机消息，就当普通消息处理
            messages.append({"role": "user",
                "content": "<inbox>" + json.dumps(inbox) + "</inbox>"})
            print(f"  \033[36m[idle] {name} found inbox messages\033[0m")
            return "work"
        # 没有消息，再看任务板
        unclaimed = scan_unclaimed_tasks()
        if unclaimed:
            task = unclaimed[0]
            result = claim_task(task["id"], agent_name)
            if "Claimed" in result:
                messages.append({"role": "user",
                    "content": f"<auto-claimed>Task {task['id']}: "f"{task['subject']}</auto-claimed>"})
                print(f"  \033[32m[idle] {name} auto-claimed: "f"{task['subject']}\033[0m")
                return "work"
            print(f"  \033[33m[idle] {name} claim failed: " f"{result}\033[0m")
    print(f"  \033[31m[idle] {name} timeout ({IDLE_TIMEOUT}s)\033[0m")
    # 上层收到 "timeout" 就让队友退出（下班）。
    return "timeout"

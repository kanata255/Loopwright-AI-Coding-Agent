"""
一句话总结：s16 的 team protocols 是一套用 request_id 做关联、用 ProtocolState 做状态机、用文件消息总线做传输的轻量级异步请求-响应协议，
覆盖了 shutdown 和 plan_approval 两种交互；核心正确性由 match_response 的类型校验 + 幂等保证，
核心易用性由 consume_lead_inbox 统一入口和队友 idle loop 保证；

问题 / 可改进点
位置	                                问题	                                                                    建议
new_request_id	                    随机 6 位，可能碰撞	                                                    用 uuid4 或计数器+时间戳
pending_requests	                永不清理，内存泄漏	                                                        加 TTL / 定期清理终态请求
match_response	                    不校验 sender 是否等于 target	                                            加 msg.from == state.target 校验
handle_inbox_message idle 分支	    plan_approval_response 注入 messages 后不 break，队友不会立即重新跑 LLM	    协议消息处理后设置 wake = True 并 break 到 LLM
submit_plan	                        软约束，不阻塞队友执行	                                                    加 awaiting_approval 状态 + 工具门控
review_plan	                        不校验 state.type == "plan_approval"	                                    加类型断言
request_plan	                    无 request_id，与 review_plan 不关联	                                    可考虑也创建 ProtocolState（反向请求）
队友异常	                            except Exception: break 静默死亡	                                        上报 error 给 Lead
轮询	                                time.sleep(1) 忙等	                                                    用 threading.Event / queue.Queue
shutdown 响应	                    硬编码 approve=True	                                                    允许队友拒绝并带理由
"""
from anthropic import Anthropic
import os
from multi_agent_platform.autonomous_agents import idle_poll
from multi_agent_platform.task_system import claim_task,list_tasks,complete_task
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
from dataclasses import dataclass, field
import time,random,json,threading
active_teammates: dict[str, bool] = {}
from multi_agent_platform.agent_team import BUS

@dataclass
class ProtocolState:
    request_id: str
    type: str       # "shutdown" | "plan_approval"
    sender: str
    target: str
    status: str     # pending | approved | rejected
    payload: str    # 承载业务数据（plan 文本 / shutdown 原因）。
    created_at: float = field(default_factory=time.time)
    
# 是一个全局内存字典，保存所有 in-flight（在途）请求
pending_requests: dict[str, ProtocolState] = {}

# request_id 生成
def new_request_id() -> str:
    return f"req_{random.randint(0, 999999):06d}"

def match_response(response_type: str, request_id: str, approve: bool):
    """
    类型校验 + 幂等性，是分布式协议里非常关键的两点。很多简易实现只做 request_id 匹配就完事，会导致「响应类型错配」和「重复响应覆盖状态」两类 bug。

    通过 request_id 将响应与原始请求相关联。验证 response_type 是否与请求类型匹配.
    """
    state = pending_requests.get(request_id)
    if not state:
        print(f"  \033[31m[protocol] unknown request_id: {request_id}\033[0m")
        return
    # 验证响应类型是否与请求类型匹配
    if state.type == "shutdown" and response_type != "shutdown_response":
        print(f"  \033[31m[protocol] type mismatch: expected shutdown_response, "f"got {response_type}\033[0m")
        return
    if state.type == "plan_approval" and response_type != "plan_approval_response":
        print(f"  \033[31m[protocol] type mismatch: expected plan_approval_response, "f"got {response_type}\033[0m")
        return
    if state.status != "pending":
        print(f"  \033[33m[protocol] {request_id} already {state.status}, "f"ignoring duplicate\033[0m")
        return
    state.status = "approved" if approve else "rejected"
    icon = "✓" if approve else "✗"
    color = "32" if approve else "31"
    print(f"  \033[{color}m[protocol] {state.type} {icon} "f"({request_id}: {state.status})\033[0m")
    

def consume_lead_inbox(route_protocol: bool = True) -> list[dict]:
    """
    Lead 侧
    读取 Lead 的收件箱。路由协议响应，
    返回所有消息。由 run_check_inbox() 和主循环调用，
    以避免消息在没有协议路由的情况下被消耗.
    Lead 侧用「类型后缀 _response + 有 request_id」作为路由条件，统一进 match_response。这是一个约定优于配置的简化路由策
    """
    msgs = BUS.read_inbox("lead")
    if not msgs:
        return []
    if route_protocol:
        for msg in msgs:
            meta = msg.get("metadata", {})
            req_id = meta.get("request_id", "")
            msg_type = msg.get("type", "")
            if req_id and msg_type.endswith("_response"):
                approve = meta.get("approve", False)
                match_response(msg_type, req_id, approve)
    return msgs

def run_spawn_teammate(name: str, role: str, prompt: str) -> str:
    """
        :param name: 创建名称
        :param role: 创建角色的作用
        :param prompt: prompt
    """
    return spawn_teammate_thread(name, role, prompt)

# ── Teammate Thread (s16: idle loop + dispatch) ──
def spawn_teammate_thread(name: str, role: str, prompt: str) -> str:
    if name in active_teammates:
        return f"Teammate '{name}' already exists"
    system = (f"You are '{name}', a {role}. "
              f"Use tools to complete tasks. "
              f"You can list and claim tasks from the board. "
              f"Check inbox for protocol messages.")
    def handle_inbox_message(name: str, msg: dict, messages: list):
        """按类型分发传入的协议消息."""
        msg_type = msg.get("type", "message")
        meta = msg.get("metadata", {})
        req_id = meta.get("request_id", "")
        if msg_type == "shutdown_request":
            BUS.send(name, "lead", "Shutting down gracefully.","shutdown_response",{"request_id": req_id, "approve": True})
            print(f"  \033[35m[protocol] {name} approved shutdown "f"({req_id})\033[0m")
            return True
        if msg_type == "plan_approval_response":
            approve = meta.get("approve", False)
            if approve:
                messages.append({"role": "user",
                    "content": "[Plan approved] Proceed with the task."})
            else:
                messages.append({"role": "user",
                    "content": f"[Plan rejected] Feedback: {msg['content']}"})
        return False
    def run():
        from tool_use import run_bash, run_read, run_write
        messages = [{"role": "user", "content": prompt}]
        sub_tools = [
            {"name": "bash", "description": "Run a shell command.",
             "input_schema": {"type": "object",
                              "properties": {"command": {"type": "string"}},
                              "required": ["command"]}},
            {"name": "read_file", "description": "Read file.",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"}},
                              "required": ["path"]}},
            {"name": "write_file", "description": "Write file.",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["path", "content"]}},
            {"name": "send_message",
             "description": "Send message to another agent.",
             "input_schema": {"type": "object",
                              "properties": {"to": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["to", "content"]}},
            {"name": "submit_plan",
             "description": "Submit a plan for Lead approval.",
             "input_schema": {"type": "object",
                              "properties": {"plan": {"type": "string"}},
                              "required": ["plan"]}},
            # s17 new: teammates can list, claim, and complete tasks
            {"name": "list_tasks",
             "description": "List all tasks on the board.",
             "input_schema": {"type": "object", "properties": {},
                              "required": []}},
            {"name": "claim_task",
             "description": "Claim a pending task.",
             "input_schema": {"type": "object",
                              "properties": {"task_id": {"type": "string"}},
                              "required": ["task_id"]}},
            {"name": "complete_task",
             "description": "Mark an in-progress task as completed.",
             "input_schema": {"type": "object",
                              "properties": {"task_id": {"type": "string"}},
                              "required": ["task_id"]}},
        ]
        def _run_list_tasks():
            tasks = list_tasks()
            if not tasks:
                return "No tasks."
            return "\n".join(
                f"  {t.id}: {t.subject} [{t.status}]"
                for t in tasks)
        def _run_claim_task(task_id: str):
            return claim_task(task_id, owner=name)
        def _run_complete_task(task_id: str):
            return complete_task(task_id)
        sub_handlers = {
            "bash": run_bash, "read_file": run_read, "write_file": run_write,
            "send_message": lambda to, content: (BUS.send(name, to, content),"Sent")[1],
            "submit_plan": lambda plan: _teammate_submit_plan(name, plan),
            "list_tasks": _run_list_tasks,
            "claim_task": _run_claim_task,
            "complete_task": _run_complete_task,
        }
        while True:
            # ── 身份再注入（s17 新）──
            if len(messages) <= 3:
                messages.insert(0, {"role": "user",
                    "content": f"<identity>You are '{name}', role: {role}. "f"Continue your work.</identity>"})
            should_shutdown = False
            # ── WORK 阶段 ──
            for _ in range(10):  # 最多 10 个 LLM 回合
                inbox = BUS.read_inbox(name)
                for msg in inbox:
                    stopped = handle_inbox_message(name, msg, messages)
                    if stopped:
                        should_shutdown = True
                        break
                if should_shutdown:
                    break
                if inbox and not should_shutdown:
                    non_protocol = [m for m in inbox
                                    if m.get("type") == "message"]
                    if non_protocol:
                        messages.append({"role": "user",
                            "content": f"<inbox>{json.dumps(non_protocol)}</inbox>"})
                try:
                    response = client.messages.create(
                        model=MODEL, system=system, messages=messages[-20:],
                        tools=sub_tools, max_tokens=8000)
                except Exception:
                    break
                messages.append({"role": "assistant", "content": response.content})
                if response.stop_reason != "tool_use":
                    break  # 干完了，跳出 WORK 循环 → 进入 IDLE
                results = []
                for block in response.content:
                    if block.type == "tool_use":
                        handler = sub_handlers.get(block.name)
                        output = handler(**block.input) if handler else "Unknown"
                        results.append({"type": "tool_result",
                                        "tool_use_id": block.id,
                                        "content": str(output)})
                messages.append({"role": "user", "content": results})
            # 整个生命周期结束
            if should_shutdown:
                break
            idle_result = idle_poll(name, messages, name, role)
            if idle_result == "shutdown":
                break  # 关机 → 结束整个生命周期
            if idle_result == "timeout":
                break # 超时 → 也结束（下班）
            # 如果 idle_result == "work"，继续外层 while，回到 WORK 阶段
        
        # 总结
        summary = "Done."
        for msg in reversed(messages):
            if msg["role"] == "assistant" and isinstance(msg["content"], list):
                for b in msg["content"]:
                    if getattr(b, "type", None) == "text":
                        summary = b.text
                        break
                else:
                    continue
                break
        BUS.send(name, "lead", summary, "result")
        active_teammates.pop(name, None)
        print(f"  \033[32m[teammate] {name} finished\033[0m")
    active_teammates[name] = True
    threading.Thread(target=run, daemon=True).start()
    print(f"  \033[36m[teammate] {name} spawned as {role}\033[0m")
    return f"Teammate '{name}' spawned as {role} (autonomous)"

def _teammate_submit_plan(from_name: str, plan: str) -> str:
    """队友将计划提交给负责人审批。注意：这是协议级别的请求，而不是代码级别的门控。
    提交后，队友的线程会继续运行——它仍然可以调用 bashwrite 等等。
    真正的执行依赖于模型在采取行动之前等待审批响应。
    代码级别的工具门控则需要阻止队友的工具分发，直到审批到达。
    """
    req_id = new_request_id()
    pending_requests[req_id] = ProtocolState(
        request_id=req_id, type="plan_approval",
        sender=from_name, target="lead",
        status="pending", payload=plan)
    BUS.send(from_name, "lead", plan,
             "plan_approval_request",
             {"request_id": req_id})
    return f"Plan submitted ({req_id}). Waiting for approval..."




# ── tool (s16 new) ──
def run_request_shutdown(teammate: str) -> str:
    """发起（Lead）"""
    req_id = new_request_id()
    pending_requests[req_id] = ProtocolState(
        request_id=req_id, type="shutdown",
        sender="lead", target=teammate,
        status="pending", payload="")
    BUS.send("lead", teammate, "Please shut down gracefully.",
             "shutdown_request",
             {"request_id": req_id})
    print(f"  \033[35m[protocol] shutdown_request → {teammate} "
          f"({req_id})\033[0m")
    return f"Shutdown request sent to {teammate} (req: {req_id})"

def run_request_plan(teammate: str, task: str) -> str:
    """Lead asks a teammate to submit a plan for a task."""
    BUS.send("lead", teammate, f"Please submit a plan for: {task}",
             "message")
    return f"Asked {teammate} to submit a plan"

def run_review_plan(request_id: str, approve: bool, feedback: str = "") -> str:
    state = pending_requests.get(request_id)
    if not state:
        return f"Request {request_id} not found"
    if state.status != "pending":
        return f"Request {request_id} already {state.status}"
    state.status = "approved" if approve else "rejected"
    BUS.send("lead", state.sender, feedback or ("Approved" if approve else "Rejected"),
             "plan_approval_response",
             {"request_id": request_id, "approve": approve})
    icon = "✓" if approve else "✗"
    print(f"  \033[32m[protocol] plan {icon} ({request_id})\033[0m")
    return f"Plan {'approved' if approve else 'rejected'} ({request_id})"

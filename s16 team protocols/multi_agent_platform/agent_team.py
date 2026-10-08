# 使用简单的文件追加和删除。
# 真实的 CC 使用 proper-lockfile 来保证并发写入的安全性。

import json,time,threading
from pathlib import Path
import os
from anthropic import Anthropic
from llm import call_llm_once

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

WORKDIR = Path.cwd()

# 存储介质：文件系统，每个 Agent 对应一个 .mailboxes/{agent_name}.jsonl 文件
# 格式：JSONL（每行一条 JSON 消息）
MAILBOX_DIR = WORKDIR / ".mailboxes"
MAILBOX_DIR.mkdir(exist_ok=True)


class MessageBus:
    """基于文件的消息总线。每个代理都有一个 .jsonl 收件箱。读取是破坏性的：
    read_text   unlink（消耗消息）。本次没有文件锁；
    真实 CC 使用 proper-lockfile
    潜在竞态：如果 send 在 read_text() 和 unlink() 之间写入，新消息会随文件被删掉
    """
    def send(self, from_agent: str, to_agent: str, content: str,
             msg_type: str = "message"):
        msg = {
            "from": from_agent,
            "to": to_agent,
            "content": content,
            "type": msg_type,
            "ts": time.time()
        }
        inbox = MAILBOX_DIR / f"{to_agent}.jsonl"
        # 追加写（"a" 模式）：无需读取旧内容，天然支持"多条消息累积"
        with open(inbox, "a") as f:
            f.write(json.dumps(msg) + "\n")
        print(f"  \033[33m[bus send] {from_agent} - {to_agent}: "
              f"{content[:50]}\033[0m")
    
    def read_inbox(self, agent: str) -> list[dict]:
        inbox = MAILBOX_DIR / f"{agent}.jsonl"
        if not inbox.exists():
            return []
        msgs = [json.loads(line) for line in inbox.read_text().splitlines()
                if line.strip()]
        inbox.unlink()  # 读后即删（unlink）：这是"消费"语义，一条消息只会被处理一次，避免重复注入
        return msgs
    
# 创建全局单例
BUS = MessageBus()
# 追踪生成的队友
active_teammates: dict[str, bool] = {}
def spawn_teammate_thread(name: str, role: str, prompt: str) -> str:
    """
        在后台线程中生成一个队友代理，每个队友最多10轮
    """
    # 如果已经创建了返回，防止重复创建
    if name in active_teammates:
        return f"Teammate '{name}' already exists"
    #  prompt 是 teammate 的全部"人格"：告诉它自己是谁、能干什么、如何汇报
    system = (f"You are '{name}', a {role}. "
              f"Use tools to complete tasks. "
              f"Send results via send_message to 'lead'.")
    def run():
        from tool_use import run_bash, run_read, run_write
        messages = [{"role": "user", "content": prompt}]
        """
            相比 Lead 的 14 个工具，teammate 只有 4 个：
            无 task 相关工具：teammate 不能自己创建/领取任务，任务分配权在 Lead
            无 cron 工具：不能定时
            无 spawn_teammate：teammate 不能再 spawn teammate（防止无限递归）
            独有 send_message：这是 teammate 的"嘴"，用于向 lead 汇报
        """
        sub_tools = [
            {"name": "bash", "description": "Run a shell command.",
             "input_schema": {"type": "object",
                              "properties": {"command": {"type": "string"}},
                              "required": ["command"]}},
            {"name": "read_file", "description": "Read file contents.",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"}},
                              "required": ["path"]}},
            {"name": "write_file", "description": "Write content to a file.",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["path", "content"]}},
            {"name": "send_message",
             "description": "Send a message to another agent.",
             "input_schema": {"type": "object",
                              "properties": {"to": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["to", "content"]}},
        ]
        
        sub_handlers = {
            "bash": run_bash,
            "read_file": run_read,
            "write_file": run_write,
            "send_message": lambda to, content: (BUS.send(name, to, content), "Sent")[1],
        }
        for _ in range(10):
            # 先读自己的信箱
            inbox = BUS.read_inbox(name)
            if inbox:
                messages.append({"role": "user",
                            "content": f"<inbox>{json.dumps(inbox)}</inbox>"})
            response = call_llm_once(
                messages=messages[-20:],
                system=system,
                tools=sub_tools,
                max_tokens=8000
            )
            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason != "tool_use":
                break
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    handler = sub_handlers.get(block.name)
                    try:
                        if handler:
                            output = handler(**block.input)
                        else:
                            output = f"Unknown tool: {block.name}"
                    except Exception as e:
                        output = f"Error: {e}"
                    results.append({"type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": str(output)})
            messages.append({"role": "user", "content": results})

        # 汇报机制，发送消息给Lead
        summary = "Done."
        # 获取执行的最终结果
        for msg in reversed(messages):
            if msg["role"] == "assistant" and isinstance(msg["content"], list):
                for b in msg["content"]:
                    if getattr(b, "type", None) == "text":
                        summary = b.text
                        break
                else:
                    continue
                break
        # 发送到lead
        BUS.send(name, "lead", summary, "result")
        active_teammates.pop(name, None)
        print(f"  \033[32m[teammate] {name} finished\033[0m")
    # 启动角色，启动线程
    active_teammates[name] = True
    threading.Thread(target=run, daemon=True).start()
    print(f"  \033[36m[teammate] {name} spawned as {role}\033[0m")
    return f"Teammate '{name}' spawned as {role}"

def run_spawn_teammate(name: str, role: str, prompt: str) -> str:
    """
        :param name: 创建名称
        :param role: 创建角色的作用
        :param prompt: prompt
    """
    return spawn_teammate_thread(name, role, prompt)

def run_send_message(to: str, content: str) -> str:
    """ Lead 主动发消息 """
    BUS.send("lead", to, content)
    return f"Sent to {to}"

def run_check_inbox() -> str:
    ""
    """
        :return 返回给lead的执行结果
        :description Lead 拉取回信
    """
    msgs = BUS.read_inbox("lead")
    if not msgs:
        return "(inbox empty)"
    lines = []
    for m in msgs:
        lines.append(f"  [{m['from']}] {m['content'][:200]}")
    # ：作为 tool_result 直接回灌给模型
    return "\n".join(lines)
"""
双路径 read_inbox("lead") 冲突：主循环被动注入和 check_inbox 工具共享同一个破坏性信箱，谁先跑谁拿到，另一个就空。
send_message 无存在性校验：可以向已退出的 teammate 发消息，写入的文件永远不会被读。
teammate 无 read_inbox 工具但有实际读信箱的行为：它靠的是循环头自动读，而不是主动工具调用——这是"被动接收"模型，与 Lead 的"主动 check_inbox"模型不对称。真实 CC 里 teammate 也是主动 idle 等待的，语义更一致。

###  Lead 侧：spawn → send → check 的时序
text
用户: "帮我把项目拆成两个任务并行做"
   ↓
__main__: history.append(user msg)
   ↓
agent_loop(history, context)
   ↓
LLM 决定: 调 spawn_teammate × 2
   ↓
for block in response.content:
    execute_tool(block) → run_spawn_teammate → spawn_teammate_thread
      ├─ active_teammates["alice"] = True
      ├─ threading.Thread(target=run).start()   ← 后台跑了
      └─ return "Teammate 'alice' spawned"
   ↓
结果回灌 → LLM 可能继续调 send_message 分配具体任务
   ↓
for block: run_send_message("alice", "写接口文档")
   ↓
BUS.send("lead", "alice", "写接口文档") → .mailboxes/alice.jsonl
   ↓
（此时 alice 线程运行中，下一轮循环读到信箱）


### Teammate 侧：接收 → 执行 → 汇报
alice 线程: run()
   ├─ messages = [{"role":"user","content":prompt}]
   ├─ 第 1 轮: BUS.read_inbox("alice") → 读到 "写接口文档"
   │        ↓ 作为 <inbox> 注入
   │     LLM 输出 tool_use: write_file / bash
   │        ↓ 执行 → 结果回灌
   ├─ 第 2 轮: BUS.read_inbox("alice") → 空
   │     LLM 输出无 tool_use → break
   └─ BUS.send("alice", "lead", summary, "result")
   └─ active_teammates.pop("alice")


###  Lead 侧：接收汇报
用户下一次提问前（或提问后）:
    inbox = BUS.read_inbox("lead")
    若 alice 已发 result → 读到
    history.append({"role":"user", "content":"[Inbox]\nFrom alice: ..."})
    下次 agent_loop 时 LLM 看到

"""
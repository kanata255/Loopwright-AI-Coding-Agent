"""
s15 的队友能干活了，但协调是松散的：Lead 发消息，队友回复，没有结构化的协议。两个场景暴露了问题：

关机：Lead 想让 Alice 关机。直接杀线程，Alice 写了一半的文件留在磁盘上。需要握手：Lead 发请求，Alice 确认收尾后关机。

计划审批：Bob 想重构认证模块，属于高风险操作。应该先让 Lead 看 Bob 的计划，审批通过后再动手。

这两个场景结构完全一样：一方发请求，另一方给回复，请求和回复通过同一个 ID 关联。有状态机追踪：pending → approved / rejected。

┌─────────────────────────────────────────────────────────────────┐
│  Shutdown 协议（Lead 发起，Teammate 响应）                        │
│                                                                  │
│  Lead                          Teammate                          │
│  run_request_shutdown()                                          │
│    ├ new_request_id()                                            │
│    ├ pending_requests[req] = ProtocolState(shutdown, pending)    │
│    └ BUS.send(shutdown_request, {request_id}) ──────────────→    │
│                                   handle_inbox_message()         │
│                                   ├ BUS.send(shutdown_response)  │
│                                   └ return True → 退出循环        │
│  consume_lead_inbox() ←──────── shutdown_response                │
│    └ match_response() → status = approved                        │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  Plan Approval 协议（Teammate 发起，Lead 审批）                   │
│                                                                  │
│  Teammate                      Lead                              │
│  submit_plan()                                                   │
│    ├ new_request_id()                                            │
│    ├ pending_requests[req] = ProtocolState(plan_approval,        │
│    │                                        pending)              │
│    └ BUS.send(plan_approval_request, {request_id}) ──────────→   │
│                                   consume_lead_inbox()           │
│                                   (只是读取，不 match，因为       │
│                                    这是 request 不是 response)    │
│                                   LLM 看到 request → 决定         │
│                                   run_review_plan(req, approve)  │
│                                     ├ status = approved/rejected │
│                                     └ BUS.send(plan_approval_    │
│                                          response) ──────────→   │
│  idle loop 读 inbox ←────────── plan_approval_response           │
│    └ handle_inbox_message → 注入 messages                        │
└─────────────────────────────────────────────────────────────────┘

"""
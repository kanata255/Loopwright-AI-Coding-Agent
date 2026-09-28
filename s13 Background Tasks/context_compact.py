"""
压缩上下文
 每轮 loop 开始
    │
    ├─ L3 tool_result_budget  只看最后一条消息的大结果落盘
    ├─ L1 snip_compact        消息条数 >50 → 掐头去尾
    ├─ L2 micro_compact       旧 tool_result 压成占位符
    │
    ├─ L4 判断 estimate_size > 100000 ? ──是──> compact_history（整体总结）
    │                            │否
    ▼                            ▼
    调用 LLM ──报 prompt_too_long──> L5 reactive_compact（重试1次）

tool_use 和对应的 tool_result 必须同时存在或同时删除。如果你把中间剪掉，只留下 tool_result 而丢了它对应的 tool_use（或反之），API 会直接报错



------------------------------------------测试结果----------------------------------------------------------------------------
有上下文压碎
==========================================
Token 用量统计
==========================================
LLM 调用次数 : 8
输入 tokens  : 10068
输出 tokens  : 1983
缓存读取 tokens : 13312
总计 tokens  : 12051
==========================================

无压缩
==========================================
Token 用量统计
==========================================
LLM 调用次数 : 7
输入 tokens  : 20379
输出 tokens  : 1929
缓存读取 tokens : 64512
总计 tokens  : 22308
==========================================
------------------------------------------测试结果----------------------------------------------------------------------------
// 第 1 条：assistant 发出工具调用请求
	{
	  "role": "assistant",
	  "content": [
	    {"type": "text", "text": "我来读一下这个文件"},
	    {"type": "tool_use", "id": "toolu_abc", "name": "read_file", "input": {...}}
	  ]
	}
	
	// 第 2 条：user 返回工具执行结果
	{
	  "role": "user",
	  "content": [
	    {"type": "tool_result", "tool_use_id": "toolu_abc", "content": "文件内容..."}
	  ]
	}
"""
from pathlib import Path
import json, time
from llm import call_llm

WORKDIR = Path.cwd()
#
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
# 工具调用结果压缩文件目录
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"

CONTEXT_LIMIT = 100000
# 保留最近 3 条 tool_result 的完整内容
KEEP_RECENT = 3
# L3 最小罗盘长度
PERSIST_THRESHOLD = 15000
def estimate_size(msgs): return len(str(msgs))
def _block_type(block):
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)

# 判断这条消息是否调用了工具
def _message_has_tool_use(msg):
    if msg.get("role") != "assistant":   # 只可能是 assistant 发出 tool_use
        return False
    content = msg.get("content")
    if not isinstance(content, list):        # content 必须是块列表，如果它是普通字符串（纯文本消息），不可能含 tool_use，返回 False。
        return False
    return any(_block_type(block) == "tool_use" for block in content)  # 遍历所有块，只要有一个块的 type == "tool_use"，就返回 True。

# 判断这条消息是否返回了工具调用结果
def _is_tool_result_message(msg):
    if msg.get("role") != "user":     # tool_result 由 user 角色返回
        return False
    content = msg.get("content")
    if not isinstance(content, list):   # 遍历块，只要有一个是 dict 且 type == "tool_result"，返回 True。
        return False
    return any(isinstance(block, dict) and block.get("type") == "tool_result"
               for block in content)


#-------------------------------------------------------------------------------------------------------------------------
"""
:description
L1— 掐头去尾（snip_compact）
  - 条件：消息条数 len(messages) > 50
  - 动作：保留开头 3 条 + 末尾 47 条，中间替换成 [snipped N messages]
  - 保护：若头/尾切割点恰好拆开了一对 tool_use↔tool_result，会扩张边界保证成对保留

"""
# L1: snipCompact — 掐头去尾，只保留开头3条和末尾47条数据，中间的[snipped N messages] 占位
def snip_compact(messages, max_messages=50):
    if len(messages) <= max_messages: return messages
    print("【L1】 掐头去尾，中间的用占位符替代")
    keep_head, keep_tail = 3, max_messages - 3
    head_end, tail_start = keep_head, len(messages) - keep_tail
    # 头部保护边界，如果头部最后一条消息是请求调用工具，则需要保留后面的工具返回结果，头部保留就需要变长
    if head_end > 0 and _message_has_tool_use(messages[head_end - 1]):
        while head_end < len(messages) and _is_tool_result_message(messages[head_end]):   # 判断是否返回了工具调用结果，返回了就扩充头部保留长度+1
            head_end += 1
    if (0 < tail_start < len(messages)
		    and _is_tool_result_message(messages[tail_start])
            and _message_has_tool_use(messages[tail_start - 1])):  # 尾部保护边界,如果尾部第一条是消息返回并且前面一条是工具调用，则扩充尾部
        tail_start -= 1
    if head_end >= tail_start:
        return messages
    snipped = tail_start - head_end
    return messages[:head_end] + [{"role": "user", "content": f"[snipped {snipped} messages]"}] + messages[tail_start:]

#-------------------------------------------------------------------------------------------------------------------------
"""
:description: L2旧工具调用结果替换成占位符,最后三条调用结果保持不变，其他替换成压缩提示prompt
  L2 — 旧工具结果占位符（micro_compact）
  - 条件：历史里 tool_result 总数 > 3
  - 动作：除最近 3 条外，其余内容 >120 字符的 tool_result 替换成 [Earlier tool result compacted...]
  - 定位：管「历史里早就用过的旧结果」，只留最近的 3 条完整
"""
# 把所有 tool_result 块收集成一个列表，每个元素是三元组 (消息下标, 块下标, 块本身)
def collect_tool_results(messages):
    blocks = []
    for mi, msg in enumerate(messages):
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list): continue    # tool_result 和 content列表 才有可能是工具调用结果
        for bi, block in enumerate(msg["content"]):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                blocks.append((mi, bi, block))
    return blocks

def micro_compact(messages):
    tool_results = collect_tool_results(messages)
    if len(tool_results) <= KEEP_RECENT: return messages   # 如果总共只有 ≤3 个工具结果，全部保留，直接返回。
    print("【L2】 旧工具调用结果替换成占位符")
    for _, _, block in tool_results[:-KEEP_RECENT]:  # 只保留最近KEEP_RECENT条完整工具调用结果，其他的进入循环判断，如果内容的字符长度>120，就替换（阈值 120 非常小，意味着几乎任何实质性的工具结果都会被替换（一个文件名列表都轻松超过 120 字符）。）
        if len(block.get("content", "")) > 120:
            block["content"] = "[Earlier tool result compacted. Re-run if needed.]"   # 告诉模型"这里曾经有个工具结果，被压缩了
    return messages

#-------------------------------------------------------------------------------------------------------------------------

"""
:param messages：消息队列
:param max_bytes：最小压缩比特，默认200kb
:description: L3: toolResultBudget — 大结果落盘，用文件引用
 - 只看 messages[-1]（最后一条，最新的一批工具结果）
  - 条件：最后一条是 user 消息，且其中所有 tool_result 的字符总数 total > max_bytes(20000)
  - 动作：按体积从大到小排序，把单条 >15000 字符的搬走，写成 .task_outputs/tool-results/{tool_use_id}.txt，原位置替换成 <persisted-output> 标签（含 2000 字符预览），直到 total 降到 20000 以内
  - 定位：拦截「刚产生的大结果」，让最大的一次性内容不进上下文
"""
def persist_large_output(tool_use_id, output):
    if len(output) <= PERSIST_THRESHOLD: return output    # 不够大，原样返回
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)   # 确保目录存在
    path = TOOL_RESULTS_DIR / f"{tool_use_id}.txt"        # 用 tool_use_id 命名
    if not path.exists(): path.write_text(output)         # 写入文件
    return f"<persisted-output>\nFull output: {path}\nPreview:\n{output[:2000]}\n</persisted-output>"   # 结构化标签 <persisted-output>：让模型明确知道"这不是原始结果，而是外置引用"。
def tool_result_budget(messages, max_bytes=20000):
    """
    历史消息里的旧结果已经被 L2 处理过了（替换成占位符）。
    （模型刚发起一批工具调用，user 刚返回结果）。
    只处理最后一条，成本最低、针对性最强，不会误伤已压缩的历史。
    """
    last = messages[-1] if messages else None
    if not last or last.get("role") != "user" or not isinstance(last.get("content"), list): return messages   # messages 为空 → 直接返回 / 最后一条不是 user（工具结果一定以 user 返回）→ 不处理 / content 不是列表（纯文本消息）→ 不处理
    blocks = [(i, b) for i, b in enumerate(last["content"]) if isinstance(b, dict) and b.get("type") == "tool_result"]  # 获取最后一条消息里所有 tool_result 块
    total = sum(len(str(b.get("content", ""))) for _, b in blocks) # 计算所有content的总长度
    if total <= max_bytes: return messages
    print(f"【L3】 大文件落盘开始落盘")
    ranked = sorted(blocks, key=lambda p: len(str(p[1].get("content", ""))), reverse=True)  # 按体积从大到小排序
    # 优先搬走最大的，目标是用最少的落盘次数把总量降到预算内。
    for _, block in ranked:
        if total <= max_bytes: break  # 满足条件后直接返回
        content = str(block.get("content", ""))   # 取出正文，如果正文太小则跳过不需要压缩
        if len(content) <= PERSIST_THRESHOLD: continue
        tid = block.get("tool_use_id", "unknown")
        block["content"] = persist_large_output(tid, content)   # 将正文压缩并落盘，返回结构化标签，让LLM知道这个消息是被压缩过的
        total = sum(len(str(b.get("content", ""))) for _, b in blocks)  # 重新计算长度
    return messages

#-------------------------------------------------------------------------------------------------------------------------
"""
:description L4：让LLM生成一份可继续工作的摘要，然后用这一条摘要替换掉全部历史。写入文件
  L4 — 整体总结（compact_history）
  - 条件：estimate_size(messages) > CONTEXT_LIMIT(100000)（整段历史字符数）
  - 动作：先落盘 transcript_*.jsonl 存档，再调 LLM 生成摘要，用 [Compacted]\n\n摘要 一条消息替换掉全部历史
  - 定位：安全网，正常滚动压缩下不该触发
"""
# 写入文件，返回路径（存档保证原文不丢失）
def write_transcript(messages):
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)   # 构建目录确认在不在
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"  # 构建时间戳文件目录
    with path.open("w") as f:
        for msg in messages: f.write(json.dumps(msg, default=str) + "\n")
    return path
# LLM摘要核心
def summarize_history(messages):
    # 把整段历史序列化成 JSON 字符串，截断80000字符串长度
    conversation = json.dumps(messages, default=str)[:80000]
    # 精简 5 条保留清单 prompt
    prompt = ("Summarize this coding-agent conversation so work can continue.\n"
              "Preserve: 1. current goal, 2. key findings/decisions, 3. files read/changed, "
              "4. remaining work, 5. user constraints.\nBe compact but concrete.\n\n" + conversation)
    response = call_llm([{"role": "user", "content": prompt}], "context_compact", max_tokens=2000)
    return "\n".join(
        getattr(block, "text", "")
        for block in response.content
        if getattr(block, "type", None) == "text").strip() or "(empty summary)"   # 提取文本并兜底
# 编排入口
def compact_history(messages):
    transcript_path = write_transcript(messages)
    print(f"【L4】文件落盘地址: {transcript_path}]")
    # 让LLM生成摘要
    summary = summarize_history(messages)
    # [Compacted] 前缀：显式告诉模型"这是被压缩后的历史摘要，不是用户新说的话"。
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]

"""
:description 应急处理=> api报错时调用，返回 摘要 + 保留最后 5 条原文
:return 总结 + 最近5条信息
  L5 — 应急（reactive_compact）
  - 条件：LLM 调用抛 prompt_too_long / too many tokens 且重试次数 <1
  - 动作：落盘存档 + 生成摘要 + 保留最后 5 条原文，拼成 [Reactive compact]\n\n摘要 + 尾部5条
  - 定位：API 报错的最后兜底
"""
def reactive_compact(messages):
    transcript = write_transcript(messages)
    print(f"[transcript saved【L5应急】: {transcript}]")
    summary = summarize_history(messages)
    tail_start = max(0, len(messages) - 5)
    if (0 < tail_start < len(messages)
		    and _is_tool_result_message(messages[tail_start])
            and _message_has_tool_use(messages[tail_start - 1])):
        tail_start -= 1
    return [{"role": "user", "content": f"[Reactive compact]\n\n{summary}"}, *messages[tail_start:]]

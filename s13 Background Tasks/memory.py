"""
存储目录
适合保存内容：用户偏好、反复出现的反馈、项目背景、常用入口和排查线索 （用prompt约束保存的内容方向）
.memory/
  MEMORY.md              ← 索引（每条记忆一行摘要）
  user-preference-tabs.md    ← 单条记忆文件
  project-facts.md
  user-profile.md
  ...
┌─────────────────────────────────────────────────────────────┐
│  第 1 轮                                                     │
│                                                             │
│  history = [U1]                                             │
│     ↓                                                       │
│  agent_loop(history)                                        │
│     ├─ load_memories → ""（.memory 空）                     │
│     ├─ build_system → 无 memories_section                   │
│     ├─ while: 调 LLM → 回复 → append A1                     │
│     └─ 退出: extract_memories([U1])                         │
│              → LLM 抽出 M1                                   │
│              → write_memory_file(M1)                        │
│                                                             │
│  history = [U1, A1]                                         │
│  .memory = {M1}                                             │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│  第 2 轮                                                     │
│                                                             │
│  history = [U1, A1, U2]                                     │
│     ↓                                                       │
│  agent_loop(history)                                        │
│     ├─ load_memories → 选中 M1 → 内容注入 U2 前             │
│     ├─ build_system → 带 M1 索引                            │
│     ├─ while: 调 LLM（看到 M1 内容）→ 回复 → append A2      │
│     └─ 退出: extract_memories([U1, A1, U2])                 │
│              → prompt 里带 "已有 M1 的描述"                 │
│              → LLM 判断：M1 已覆盖 → 返回 []                │
│                                                             │
│  history = [U1, A1, U2, A2]                                 │
│  .memory = {M1}   ← 没变                                    │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│  第 N 轮（累积到 10 条记忆）                                 │
│                                                             │
│  agent_loop 退出时:                                          │
│     extract_memories(...) → 可能新增 1~2 条                  │
│     consolidate_memories() → 文件数 ≥ 10 → 触发合并          │
│        → 把所有记忆全文给 LLM                                │
│        → LLM 返回整理后的数组                                │
│        → 删除所有旧文件 → 写回新文件                         │
│                                                             │
│  .memory = {整理后的 3~5 条}                                 │
└─────────────────────────────────────────────────────────────┘

"""

from pathlib import Path
WORKDIR = Path.cwd()
# 存储地址
MEMORY_DIR = WORKDIR / ".memory"; MEMORY_DIR.mkdir(exist_ok=True)
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
"""
记忆类型
类型	        含义	                例子
user	    用户偏好	            "用户是中文母语者"
feedback	对 agent 的指导	    "不要自动 git commit"
project	    项目事实	            "后端用 FastAPI，端口 8000"
reference	外部指针	            "文档在 docs.internal/xxx"
"""

MEMORY_TYPES = ["user", "feedback", "project", "reference"]
import re,json,time
from dotenv import load_dotenv
from tool_use import extract_text
load_dotenv(override=True)
from llm import call_llm

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """
    :param text:
    :return:
    :description 把记忆文件拆成「元数据字典」和「正文」
    """
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, parts[2].strip()


#   遍历 .memory/ 下所有 .md 文件，为每个文件生成一行索引，重写 MEMORY.md。
def _rebuild_index():
    lines = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        # 排除索引文件
        if f.name == "MEMORY.md":
            continue
        raw = f.read_text()
        meta, body = _parse_frontmatter(raw)
        name = meta.get("name", f.stem)
        desc = meta.get("description", body.split("\n")[0][:80])
        lines.append(f"- [{name}]({f.name}) — {desc}")
    #  将记忆名称、记忆文件名称 - 记忆描述写入文件
    MEMORY_INDEX.write_text("\n".join(lines) + "\n" if lines else "")

def write_memory_file(name: str, mem_type: str, description: str, body: str):
    """
    :description 写入新记忆时自动重建索引
    :param name: 记忆名称
    :param mem_type: 记忆类型
    :param description: 描述
    :param body: 记忆主体内容
    :return: 返回文件路径
    """
    slug = name.lower().replace(" ", "-").replace("/", "-")
    filename = f"{slug}.md"
    filepath = MEMORY_DIR / filename
    filepath.write_text(
        f"---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n\n{body}\n"
    )
    # 新建文件后，重构记忆索引
    _rebuild_index()
    return filepath

def read_memory_index() -> str:
    """读取memory记忆索引"""
    if not MEMORY_INDEX.exists():
        return ""
    text = MEMORY_INDEX.read_text().strip()
    return text if text else ""

def read_memory_file(filename: str) -> str | None:
    """读索引内容（供 build_system() 塞进 system prompt）。空文件返回空串。"""
    path = MEMORY_DIR / filename
    if not path.exists():
        return None
    return path.read_text()

def list_memory_files() -> list[dict]:
    """
    :description 按文件名读完整内容
    :return 返回结构化列表，供后续 LLM 选择 / 抽取 / 合并使用。每条是 dict，含文件名、名字、描述、类型、正文。
    """
    result = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        raw = f.read_text()
        meta, body = _parse_frontmatter(raw)
        result.append({
            "filename": f.name,
            "name": meta.get("name", f.stem),
            "description": meta.get("description", ""),
            "type": meta.get("type", "user"),
            "body": body,
        })
    return result


def select_relevant_memories(messages: list, max_items: int = 5) -> list[str]:
    """
    这是"按需加载"的核心——不是把所有记忆都塞进上下文（会爆 token），而是先让 LLM 判断哪些跟当前对话相关
    """
    # 获取记忆结构化列表
    files = list_memory_files()
    if not files:
        return []
    # 收集最近的用户文本作为参考
    recent_texts = []
    # 获取最近3条用户消息
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    str(getattr(b, "text", "")) for b in content
                    if getattr(b, "type", None) == "text"
                )
            if isinstance(content, str):
                recent_texts.append(content)
            if len(recent_texts) >= 3:
                break
    # 将最近3条信息合并成字符串，并截断
    recent = " ".join(reversed(recent_texts))[:2000]
    if not recent.strip():
        return []
    # 把记忆做成目录给 LLM 看
    catalog_lines = []
    for i, f in enumerate(files):
        catalog_lines.append(f"{i}: {f['name']} — {f['description']}")
    catalog = "\n".join(catalog_lines)
    # 选择那些明显相关的记忆索引
    prompt = (
        "Given the recent conversation and the memory catalog below, "
        "select the indices of memories that are clearly relevant. "
        "Return ONLY a JSON array of integers, e.g. [0, 3]. "
        "If none are relevant, return [].\n\n"
        f"Recent conversation:\n{recent}\n\n"
        f"Memory catalog:\n{catalog}"
    )
    try:
        response = call_llm([{"role": "user", "content": prompt}], "memory.select", max_tokens=200)
        # 提取返回的text文本
        text = extract_text(response.content).strip()
        # 从响应中提取 JSON 数组
        match = re.search(r'\[.*?\]', text, re.DOTALL)
        if match:
            indices = json.loads(match.group())
            selected = []
            for idx in indices:
                if isinstance(idx, int) and 0 <= idx < len(files):
                    selected.append(files[idx]["filename"])
                    if len(selected) >= max_items:
                        break
            return selected
    except Exception:
        pass
    # Fallback: 名称描述的关键词匹配
    keywords = [w.lower() for w in recent.split() if len(w) > 3]
    selected = []
    for f in files:
        text = (f["name"] + " " + f["description"]).lower()
        if any(kw in text for kw in keywords):
            selected.append(f["filename"])
            if len(selected) >= max_items:
                break
    return selected

# 根据最近的消息对话，加载相关记忆
def load_memories(messages: list) -> str:
    """加载相关记忆内容以注入到上下文中."""
    # 选择出和历史消息最相近的记忆
    selected_files = select_relevant_memories(messages)
    if not selected_files:
        return ""
    parts = ["<relevant_memories>"]
    for filename in selected_files:
        # 加载对应文件
        content = read_memory_file(filename)
        if content:
            parts.append(content)
    parts.append("</relevant_memories>")
    return "\n\n".join(parts)


def extract_memories(messages: list):
    """这是记忆系统的"输入端"——每轮对话结束后，从对话里挖新记忆.
        - 只看最后 10 条消息（避免历史太长）
        - 每条转成 role: content 格式
        - 列表型 content 只取 text 块
    """
    # 收集最近的聊天内容
    dialogue_parts = []
    for msg in messages[-10:]:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                str(getattr(b, "text", "")) for b in content
                if getattr(b, "type", None) == "text"
            )
        if isinstance(content, str) and content.strip():
            dialogue_parts.append(f"{role}: {content}")
    dialogue = "\n".join(dialogue_parts)
    if not dialogue.strip():
        return
    # 检查现有记忆以避免重复
    existing = list_memory_files()
    existing_desc = "\n".join(f"- {m['name']}: {m['description']}" for m in existing) if existing else "(none)"
    """
        从这个对话中提取用户偏好、约束或项目事实。
        返回一个 JSON 数组。每个条目包括：{name, type, description, body}。
        - name：简短的 kebab-case 标识符（例如 'user-preference-tabs'）
        - type：'user'（用户偏好）、'feedback'（指导）、'project'（项目事实）、'reference'（外部指针）之一
        - description：用于索引查找的一行摘要
        - body：完整的详细信息，使用 Markdown 格式
        如果没有新信息或者已经被现有记忆覆盖，则返回 []
    """
    prompt = (
        "Extract user preferences, constraints, or project facts from this dialogue.\n"
        "Return a JSON array. Each item: {name, type, description, body}.\n"
        "- name: short kebab-case identifier (e.g. 'user-preference-tabs')\n"
        "- type: one of 'user' (user preference), 'feedback' (guidance), "
        "'project' (project fact), 'reference' (external pointer)\n"
        "- description: one-line summary for index lookup\n"
        "- body: full detail in markdown\n"
        "If nothing new or already covered by existing memories, return [].\n\n"
        f"Existing memories:\n{existing_desc}\n\n"
        f"Dialogue:\n{dialogue[:4000]}"
    )
    try:
        response = call_llm([{"role": "user", "content": prompt}], "memory.extract", max_tokens=800)
        text = extract_text(response.content).strip()
        # 从回应中提取 JSON 数组
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if not match:
            return
        items = json.loads(match.group())
        if not items:
            return
        count = 0
        for mem in items:
            name = mem.get("name", f"memory_{int(time.time())}")
            mem_type = mem.get("type", "user")
            desc = mem.get("description", "")
            body = mem.get("body", "")
            if desc and body:
                write_memory_file(name, mem_type, desc, body)
                count += 1
        if count:
            print(f"\n\033[33m[Memory: extracted {count} new memories]\033[0m")
    except Exception:
        pass

# 最多记忆条数
CONSOLIDATE_THRESHOLD = 5
def consolidate_memories():
    """合并重复/过时的记忆。当文件数量≥阈值时触发。"""
    files = list_memory_files()
    if len(files) < CONSOLIDATE_THRESHOLD:
        return
    catalog = "\n\n".join(
        f"## {f['filename']}\nname: {f['name']}\ndescription: {f['description']}\n{f['body']}"
        for f in files
    )
    """
    整合以下记忆文件。规则
    合并重复
    删除过时/矛盾
    总数 < 30
    用户偏好优先级最高（别丢）
    输出跟  {name, type, description, body}[] 一样的格式
    """
    prompt = (
        "Consolidate the following memory files. Rules:\n"
        "1. Merge duplicates into one\n"
        "2. Remove outdated/contradicted memories\n"
        f"3. Keep the total at most {CONSOLIDATE_THRESHOLD} memories\n"
        "4. Preserve important user preferences above all\n"
        "Return a JSON array. Each item: {name, type, description, body}.\n\n"
        f"{catalog[:16000]}"
    )
    try:
        response = call_llm([{"role": "user", "content": prompt}], "memory.consolidate", max_tokens=3000)
        text = extract_text(response.content).strip()
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if not match:
            return
        items = json.loads(match.group())
        # 兜底：模型输出再多也截断到约定上限
        items = items[:CONSOLIDATE_THRESHOLD]
        # 删除所有的老记忆
        for f in MEMORY_DIR.glob("*.md"):
            if f.name != "MEMORY.md":
                f.unlink()
        for mem in items:
            name = mem.get("name", f"memory_{int(time.time())}")
            mem_type = mem.get("type", "user")
            desc = mem.get("description", "")
            body = mem.get("body", "")
            if desc and body:
                # 写入更新后的记忆
                write_memory_file(name, mem_type, desc, body)
        print(f"\n\033[33m[Memory: consolidated {len(files)} → {len(items)} memories]\033[0m")
    except Exception:
        pass
    

# 构建SYSTEM注入agent_loop
def build_system() -> str:
    index = read_memory_index()
    memories_section = f"\n\nMemories available:\n{index}" if index else ""
    return (
        f"You are a coding agent at {WORKDIR}."
        f"{memories_section}\n"
        "Relevant memories are injected below. Respect user preferences from memory.\n"
        "When the user says 'remember' or expresses a clear preference, extract it as a memory."
    )

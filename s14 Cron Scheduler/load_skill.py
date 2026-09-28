"""
load_skill.py — 技能（Skill）加载模块

从 skills/ 目录扫描所有技能，构建技能注册表，并生成拼进主 agent SYSTEM 提示词的
技能目录（catalog）。主 agent 先通过 SYSTEM 感知到「有哪些技能可用」，再调用
load_skill 工具按需加载某个技能的完整内容。

目录约定：每个技能是 skills/ 下的一个子目录，内含一份 SKILL.md 清单文件，
SKILL.md 顶部为 YAML frontmatter（name / description），正文为技能具体说明。

被 code.py（拼接 SYSTEM）与 tool_use.py（注册 load_skill 工具处理器）共同引用。
"""

SKILL_REGISTRY: dict[str, dict] = {}
from pathlib import Path

import yaml

# 当前工作目录；skills 目录固定位于其下的 skills/ 子目录。
# 注意：依赖启动时的 cwd，而非本文件所在位置（见 _scan_skills 说明）。
WORKDIR = Path.cwd()
SKILLS_DIR = WORKDIR / "skills"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 SKILL.md 顶部的 YAML frontmatter。

    返回 (meta, body)：
        meta  — frontmatter 解析出的字典（如 {"name": ..., "description": ...}），
                无 frontmatter 或解析失败时为空字典 {}。
        body  — frontmatter 之后的正文，已去除首尾空白。
    """
    # 不以 "---" 开头说明没有 frontmatter，整段都当作正文
    if not text.startswith("---"):
        return {}, text
    # 按 "---" 切三段：空、frontmatter、正文。凑不满三段说明格式不完整
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        # safe_load 避免 yaml.load 的任意对象反序列化风险
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    return meta, parts[2].strip()


def _scan_skills():
    """扫描 skills/ 目录，把每个技能登记进 SKILL_REGISTRY。

    遍历 skills/ 下的每个子目录，读取其中的 SKILL.md：
        name  — 优先取 frontmatter 里的 name，缺失时回退为目录名。
        desc  — 优先取 frontmatter 里的 description，缺失时回退为正文首行（去 # 号）。
        content — SKILL.md 原始全文，供 load_skill 按需返回。
    """
    # skills/ 不存在（或 cwd 不对）时静默跳过，注册表保持为空
    if not SKILLS_DIR.exists():
        return
    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue
        manifest = d / "SKILL.md"
        if manifest.exists():
            raw = manifest.read_text()
            meta, body = _parse_frontmatter(raw)
            name = meta.get("name", d.name)
            # 无 description 时用正文第一行，去掉开头的 "#" 与空白
            desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
            SKILL_REGISTRY[name] = {"name": name, "description": desc, "content": raw}


_scan_skills()  # 模块导入时执行一次，注册表在后续所有调用前就绪


def list_skills() -> str:
    """把注册表中所有技能渲染成一份 markdown 目录，每个技能一行。"""
    return "\n".join(f"- **{s['name']}**: {s['description']}" for s in SKILL_REGISTRY.values())


def build_system() -> str:
    """生成要拼进主 agent SYSTEM 提示词的技能说明片段。

    包含：角色定位 + 可用技能目录 + 「用 load_skill 获取详情」的引导，
    让模型先建立「有哪些技能」的全局认知，再按需加载具体内容。
    """
    catalog = list_skills()
    return (
        f"You are a coding agent at {WORKDIR}. "
        f"Skills available:\n{catalog}\n"
        "Use load_skill to get full details when needed."
    )


def load_skill(name: str) -> str:
    """按名称返回某个技能的完整内容（SKILL.md 原文）。

    供 load_skill 工具处理器调用；名称不存在时返回提示而非抛异常，
    避免工具调用因 KeyError 中断 agent loop。
    """
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        return f"Skill not found: {name}"
    return skill["content"]


# 模块加载时即构建好 SYSTEM，供 code.py 导入拼接。
# 由于 Python 导入是同步的，code.py 拿到 SYSTEM 时此值必已就绪。
SYSTEM = build_system()
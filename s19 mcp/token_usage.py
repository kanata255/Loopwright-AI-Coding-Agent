"""全局 token 统计：累计主循环、子 agent、压缩摘要三处 LLM 调用的消耗。

Anthropic 的 response.usage 字段：
- input_tokens  / output_tokens         常规输入输出
- cache_read_input_tokens                命中缓存的输入（已含在 input_tokens 中）
- cache_creation_input_tokens            写入缓存的输入（已含在 input_tokens 中）
"""

_input = 0
_output = 0
_calls = 0
_cache_read = 0
_cache_write = 0


def record(response):
    """读取一次 LLM 响应的 usage 并累加。响应无 usage 时静默跳过。"""
    global _input, _output, _calls, _cache_read, _cache_write
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    _input += getattr(usage, "input_tokens", 0) or 0
    _output += getattr(usage, "output_tokens", 0) or 0
    _cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
    _cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0
    _calls += 1


def reset():
    """清空计数，供下一次会话重新累计。"""
    global _input, _output, _calls, _cache_read, _cache_write
    _input = _output = _calls = _cache_read = _cache_write = 0


def print_usage():
    print("\n" + "=" * 42)
    print("Token 用量统计")
    print("=" * 42)
    print(f"LLM 调用次数 : {_calls}")
    print(f"输入 tokens  : {_input}")
    print(f"输出 tokens  : {_output}")
    if _cache_read:
        print(f"缓存读取 tokens : {_cache_read}")
    if _cache_write:
        print(f"缓存写入 tokens : {_cache_write}")
    print(f"总计 tokens  : {_input + _output}")
    print("=" * 42)

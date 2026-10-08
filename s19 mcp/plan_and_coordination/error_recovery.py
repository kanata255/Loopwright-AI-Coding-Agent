import os, time, random
from pathlib import Path
WORKDIR = Path.cwd()
MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
PRIMARY_MODEL = os.environ["MODEL_ID"]
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL_ID")  # 备用模型
# 升级后的输出上限
ESCALATED_MAX_TOKENS = 64000
# 初始输出上限
DEFAULT_MAX_TOKENS = 8000
# max_tokens 续写次数上限
MAX_RECOVERY_RETRIES = 3
# 瞬态错误(429/529)重试上限
MAX_RETRIES = 10
# 指数退避基数(毫秒)
BASE_DELAY_MS = 500
# 连续 529 达到此值就切备用模型
MAX_CONSECUTIVE_529 = 3
CONTINUATION_PROMPT= "Output token limit hit. Resume directly — ..."

class RecoveryState:
    """Track recovery attempts across the loop."""
    def __init__(self):
        # 是否升级过最大token
        self.has_escalated = False
        self.recovery_count = 0
        self.consecutive_529 = 0
        # 是否执行过L5应急prompt压缩
        self.has_attempted_reactive_compact = False
        self.current_model = PRIMARY_MODEL
        
def retry_delay(attempt, retry_after=None):
    """带抖动的指数退避。Retry-After 优先."""
    if retry_after:
        return retry_after
    base = min(BASE_DELAY_MS * (2 ** attempt), 32000) / 1000
    jitter = random.uniform(0, base * 0.25)
    return base + jitter


"""
                 ┌──────────────────────────────┐
                 │  for attempt in range(10):   │
                 │     ┌────────────────────┐   │
                 │     │ try:               │   │
                 │     │   result = fn()    │   │
                 │     │   consecutive=0    │   │
                 │     │   return result ───┼───┼──► 函数结束，返回 result
                 │     │ except:            │   │
                 │     │   429? → continue ─┼───┼──► 回到 for 头，attempt+1
                 │     │   529? → continue ─┼───┼──► 回到 for 头，attempt+1
                 │     │   其它? → raise ───┼───┼──► 函数异常结束
                 │     └────────────────────┘   │
                 └──────────────────────────────┘
                              │
                     for 循环自然走完
                              │
                              ▼
        raise RuntimeError("Max retries exceeded") ──► 函数异常结束
"""
def with_retry(fn, state: RecoveryState):
    """
    瞬态错误重试包装器
    对于临时错误（429/529）使用指数退避。非临时错误会被重新抛出给外部处理器.
    """
    for attempt in range(MAX_RETRIES):
        try:
            result = fn()
            state.consecutive_529 = 0
            return result
        except Exception as e:
            name = type(e).__name__
            msg = str(e).lower()
            if "ratelimit" in name.lower() or "429" in msg:
                delay = retry_delay(attempt)
                print(f"  \033[33m[429 限流] 重试 {attempt+1}/{MAX_RETRIES},"
                      f" wait {delay:.1f}s\033[0m")
                time.sleep(delay)
                continue
            # 529过载
            if "overloaded" in name.lower() or "529" in msg or "overloaded" in msg:
                state.consecutive_529 += 1
                # 超过了上限，检查是否有备用模型，有就进行切换模型，没有就继续重试
                if state.consecutive_529 >= MAX_CONSECUTIVE_529:
                    # 如果有备用模型就启用备用模型
                    if FALLBACK_MODEL:
                        state.current_model = FALLBACK_MODEL
                        state.consecutive_529 = 0
                        print(f"  \033[31m[529 x{MAX_CONSECUTIVE_529}]"
                              f" switching to {FALLBACK_MODEL}\033[0m")
                    else:
                        state.consecutive_529 = 0
                        print(f"  \033[31m[529 x{MAX_CONSECUTIVE_529}]"
                              f" 没有配置备用模型，继续重试\033[0m")
                # 延时重试
                delay = retry_delay(attempt)
                print(f"  \033[33m[529 overloaded] retry {attempt+1}/{MAX_RETRIES},"
                      f" wait {delay:.1f}s\033[0m")
                time.sleep(delay)
                continue
            raise
    # 10次都没有进行return result，就抛出异常，被调用层捕获
    raise RuntimeError(f"超出 ({MAX_RETRIES}) 最大重试次数")
def is_prompt_too_long_error(e: Exception) -> bool:
    """检查 API 错误是否表示提示/上下文太长。"""
    msg = str(e).lower()
    return (("prompt" in msg and "long" in msg)
            or "prompt_is_too_long" in msg
            or "context_length_exceeded" in msg
            or "max_context_window" in msg)


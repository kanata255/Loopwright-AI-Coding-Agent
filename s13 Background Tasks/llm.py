"""统一的 LLM 调用入口。

- call_llm：把 client.messages.create 封装成简单调用，自动记录 token 用量，失败自动重试
- call_llm_with_recovery：主循环用，带瞬态重试 + 报错分类处理（prompt 过长 / max_tokens 续写）
"""

import os
import time
from anthropic import Anthropic, NOT_GIVEN
from error_recovery import (
    with_retry,
    is_prompt_too_long_error,
    DEFAULT_MAX_TOKENS,
    ESCALATED_MAX_TOKENS,
    MAX_RECOVERY_RETRIES,
    CONTINUATION_PROMPT,
)
import token_usage


client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]


def call_llm(messages, caller, *, system=NOT_GIVEN, tools=NOT_GIVEN, max_tokens=8000,
             retry_max=3, **extra):
    """调用 LLM，记录 token 用量并返回 response。失败时最多重试 retry_max 次。

    caller：标识本次调用来自哪个模块/函数，用于重试日志定位。
    """
    for attempt in range(retry_max + 1):
        try:
            response = client.messages.create(
                model=MODEL,
                messages=messages,
                system=system,
                tools=tools,
                max_tokens=max_tokens,
                **extra,
            )
            token_usage.record(response)
            return response
        except Exception:
            if attempt == retry_max:
                raise
            print(f"  \033[33m[call_llm] {caller} 调用失败正在重试"
                  f"[{attempt + 1}/{retry_max}]\033[0m")
            time.sleep(1)

# ── 主循环调用：瞬态重试 + 报错分类处理 ──────────────────────────
# 返回 status 三态：
CALL_OK = "ok"          # 拿到 response，继续走工具执行流程
CALL_RETRY = "retry"    # messages/state 已原地更新，回 loop 顶部重试
CALL_FINISH = "finish"  # 不可恢复，错误信息已 append 到 messages，loop 应 return


def call_llm_with_recovery(messages, request_messages, *, system, tools, state,
                           max_tokens):
    """调用 LLM 并处理报错。

    - messages：主对话历史，出错/续写时原地修改
    - request_messages：本次实际发送给 LLM 的消息（可能带记忆注入）
    - 返回 (status, response, max_tokens)；response 仅 status==CALL_OK 时非 None
    """
    try:
        # with_retry 处理瞬态错误（429/529）重试 + 备用模型切换
        response = with_retry(
            lambda mt=max_tokens, mdl=state.current_model:
            client.messages.create(
                model=mdl, system=system, messages=request_messages,
                tools=tools, max_tokens=mt),
            state)
    except Exception as e:
        # 不可重试的报错
        if is_prompt_too_long_error(e):
            # prompt 太长：L5 应急压缩一次后重试
            if not state.has_attempted_reactive_compact:
                from context_compact import reactive_compact  # 延迟导入，避免循环依赖
                messages[:] = reactive_compact(messages)
                state.has_attempted_reactive_compact = True
                return CALL_RETRY, None, max_tokens
            print("  \033[31m[unrecoverable] LLM报错prompt在进行L5级消息压缩后仍然很长\033[0m")
            messages.append({"role": "assistant", "content": [
                {"type": "text",
                 "text": "[Error] Context too large, cannot continue."}]})
            return CALL_FINISH, None, max_tokens
        name = type(e).__name__
        print(f"  \033[31m[unrecoverable] {name}: {str(e)[:100]}\033[0m")
        messages.append({"role": "assistant", "content": [
            {"type": "text", "text": f"[Error] {name}: {str(e)[:200]}"}]})
        return CALL_FINISH, None, max_tokens

    token_usage.record(response)

    if response.stop_reason == "max_tokens":
        # 阶段1：首次升级输出上限，不 append 截断内容
        if not state.has_escalated:
            max_tokens = ESCALATED_MAX_TOKENS
            state.has_escalated = True
            print(f"  \033[33m[max_tokens] 升级最大token量"
                  f" {DEFAULT_MAX_TOKENS} -> {ESCALATED_MAX_TOKENS}\033[0m")
            return CALL_RETRY, None, max_tokens
        # 阶段2：已升级过，追加截断输出 + 续写提示
        messages.append({"role": "assistant", "content": response.content})
        if state.recovery_count < MAX_RECOVERY_RETRIES:
            messages.append({"role": "user", "content": CONTINUATION_PROMPT})
            state.recovery_count += 1
            print(f"  \033[33m[max_tokens] 升级最大token，继续重试"
                  f" {state.recovery_count}/{MAX_RECOVERY_RETRIES}\033[0m")
            return CALL_RETRY, None, max_tokens
        print("  \033[31m[max_tokens] 恢复次数已达上限\033[0m")
        return CALL_FINISH, None, max_tokens

    return CALL_OK, response, max_tokens
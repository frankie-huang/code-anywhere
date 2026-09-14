#!/bin/bash
# =============================================================================
# src/lib/transcript.sh - Agent transcript 响应提取库
#
# 从 Claude / Codex 的 JSONL transcript 中提取会话答复内容。
# 原本住在 hooks/stop.sh，抽出为共享库，供 hook 流程与独立执行共用。
#
# 主要函数:
#   is_codex_transcript()        - 检测 transcript 是否为 Codex 格式
#   extract_claude_response()    - 从 Claude transcript 提取响应
#   extract_codex_response()     - 从 Codex transcript 提取响应
#   extract_response_from_file() - 单文件提取（格式自动分发 + 重试）
#   extract_response()           - 提取响应（带 subagents 回退）
#
# 两种使用方式:
#   1. source 本文件后调用上述函数（hook 流程，由 hook-router 预先加载依赖）
#   2. 直接执行：bash transcript.sh <transcript_path> [turn_id]
#      单次提取（不重试、不回退 subagents），结果 JSON 输出到 stdout，
#      供 Python 后端 subprocess 调用（退出码契约见文件末尾 CLI 入口：
#      0=有内容 / 1=硬失败 / 2=无可提取内容）。
#
# 依赖: core.sh（log）、json.sh（JSON_HAS_JQ / JSON_HAS_PYTHON3 / PYTHON3）
# =============================================================================

# 防重复加载：仅 sourced 模式守卫（declare -F 而非 type，避免误命中 PATH 中的
# 同名可执行文件；直接执行时若父 shell export -f 了同名函数，顶层 return 会报错）
if [ "${BASH_SOURCE[0]}" != "$0" ] && declare -F extract_response &> /dev/null; then
    return 0
fi

# cd+pwd 解析脚本目录（跨平台且兼容相对路径调用）：
# ${BASH_SOURCE[0]%/*} 在 `bash transcript.sh` 相对路径调用时不含斜杠，
# 会拼出 "transcript.sh/core.sh" 的错误路径
_transcript_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 引入依赖库（如果尚未引入；hook 流程已由 hook-router 加载，此处为独立执行兜底）
if ! declare -F log &> /dev/null; then
    source "${_transcript_lib_dir}/core.sh"
fi
if ! declare -F json_init &> /dev/null; then
    source "${_transcript_lib_dir}/json.sh"
    json_init
fi

# =============================================================================
# 从 Codex JSONL transcript 提取响应
# 支持两类格式：
#   1. codex exec --json stdout:
#   {"type":"thread.started","thread_id":"xxx"}
#   {"type":"item.completed","item":{"type":"agent_message","text":"..."}}
#   2. ~/.codex/sessions/.../rollout-*.jsonl:
#   {"type":"session_meta","payload":{"id":"xxx",...}}
#   {"type":"event_msg","payload":{"type":"agent_message","phase":"commentary","message":"..."}}
#   {"type":"event_msg","payload":{"type":"task_complete","turn_id":"...","last_agent_message":"..."}}
# 参数:
#   $1 - transcript 文件路径
#   $2 - turn_id (可选，Codex 持久化格式下用于定位目标 turn；空则取最后一个 task_complete 所在 turn)
# 返回: {"texts":["..."],"thinking":"","session_id":"xxx"} 或空
# =============================================================================
extract_codex_response() {
    local transcript_file="$1"
    local turn_id="$2"
    local result=""

    if [ ! -f "$transcript_file" ]; then
        return 1
    fi

    # 1. jq：仅处理 codex exec --json stdout 格式（持久化格式的 turn 分片和 reasoning 提取用 jq 难以维护）
    if [ "$JSON_HAS_JQ" = "true" ]; then
        result=$(jq -s '
(
    [.[] | select(.type == "thread.started") | .thread_id] |
    if length > 0 then .[0] else "" end
) as $sid |
if $sid == "" then null
else
[
    .[] | select(.type == "item.completed" and .item.type == "agent_message") |
    .item.text | gsub("^\\n+|\\n+$"; "") | select(length > 0)
] |
if length == 0 then null
else {texts: ., thinking: "", session_id: $sid}
end
end
' "$transcript_file" 2>/dev/null)
        if [ -n "$result" ] && [ "$result" != "null" ]; then
            echo "$result"
            return 0
        fi
    fi

    # 2. Python：处理所有格式（codex exec --json stdout + 持久化会话），jq 未命中或不可用时走此路径
    if [ "$JSON_HAS_PYTHON3" = "true" ]; then
        "$PYTHON3" -c "
import sys, json

path = sys.argv[1]
target_turn_id = sys.argv[2] if len(sys.argv) > 2 else ''

with open(path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

records = []
for line in lines:
    line = line.strip()
    if not line:
        continue
    try:
        records.append(json.loads(line))
    except Exception:
        continue

if not records:
    sys.exit(0)

def payload(record):
    p = record.get('payload')
    return p if isinstance(p, dict) else {}

def append_unique(items, text):
    text = (text or '').strip()
    if text and (not items or items[-1] != text):
        items.append(text)

def collect_text_from_content(content):
    parts = []
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get('type') in ('text', 'output_text'):
                parts.append(item.get('text') or '')
            elif isinstance(item.get('content'), str):
                parts.append(item.get('content'))
    return ''.join(parts)

# codex exec --json stdout format
if any(r.get('type') == 'thread.started' for r in records):
    session_id = ''
    texts = []
    for event in records:
        etype = event.get('type', '')
        if etype == 'thread.started':
            session_id = event.get('thread_id', '') or session_id
        elif etype == 'item.completed':
            item = event.get('item', {})
            if isinstance(item, dict) and item.get('type') == 'agent_message':
                append_unique(texts, item.get('text', ''))
    if texts:
        print(json.dumps({'texts': texts, 'thinking': '', 'session_id': session_id}, ensure_ascii=False))
    sys.exit(0)

# Codex persistent session format
session_id = ''
for r in records:
    if r.get('type') == 'session_meta':
        session_id = payload(r).get('id', '') or session_id
        break

if not target_turn_id:
    for r in reversed(records):
        p = payload(r)
        if p.get('type') == 'task_complete' and p.get('turn_id'):
            target_turn_id = p.get('turn_id')
            break

subset = records
if target_turn_id:
    start_idx = None
    end_idx = None
    for i, r in enumerate(records):
        p = payload(r)
        if p.get('turn_id') != target_turn_id:
            continue
        if start_idx is None and (
            (r.get('type') == 'event_msg' and p.get('type') == 'task_started') or
            r.get('type') == 'turn_context'
        ):
            start_idx = i
        if start_idx is not None and r.get('type') == 'event_msg' and p.get('type') in ('task_complete', 'turn_aborted'):
            end_idx = i
            break
    if start_idx is not None:
        subset = records[start_idx:(end_idx + 1 if end_idx is not None else len(records))]

texts = []
thinkings = []
fallback_final = ''

for r in subset:
    rtype = r.get('type')
    p = payload(r)

    if rtype == 'event_msg':
        ptype = p.get('type')
        if ptype == 'agent_message':
            msg = (p.get('message') or '').strip()
            if msg:
                append_unique(texts, msg)
        elif ptype == 'task_complete':
            fallback_final = p.get('last_agent_message') or fallback_final
        continue

    if rtype != 'response_item':
        continue

    ptype = p.get('type')
    if ptype == 'reasoning':
        summary = p.get('summary')
        if isinstance(summary, list):
            for item in summary:
                if isinstance(item, str):
                    append_unique(thinkings, item)
                elif isinstance(item, dict):
                    append_unique(thinkings, item.get('text') or item.get('summary') or item.get('content') or '')
        elif isinstance(summary, str):
            append_unique(thinkings, summary)
        content = p.get('content')
        if isinstance(content, str):
            append_unique(thinkings, content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    append_unique(thinkings, item.get('text') or item.get('content') or '')
        continue

    # Older/different Codex builds may only write response_item.message.
    if ptype == 'message' and p.get('role') in ('assistant', 'agent'):
        msg = collect_text_from_content(p.get('content')).strip()
        if msg:
            append_unique(texts, msg)
    elif ptype == 'agent_message':
        append_unique(texts, p.get('text') or p.get('message') or '')

append_unique(texts, fallback_final)

if not texts:
    sys.exit(0)

print(json.dumps({
    'texts': texts,
    'thinking': '\n\n'.join(t for t in thinkings if t).strip(),
    'session_id': session_id
}, ensure_ascii=False))
" "$transcript_file" "$turn_id" 2>/dev/null
        return $?
    fi
    return 1
}

# =============================================================================
# 检测 transcript 是否为 Codex 格式
# =============================================================================
is_codex_transcript() {
    local transcript_file="$1"
    [ -f "$transcript_file" ] || return 1
    local first_line
    first_line=$(head -1 "$transcript_file" 2>/dev/null)
    case "$first_line" in
        *'"type"'*'"thread.started"'*) return 0 ;;
        *'"type"'*'"session_meta"'*) return 0 ;;
        *) return 1 ;;
    esac
}

# =============================================================================
# 从 Claude transcript 提取响应
# 找到最近一条用户文本消息（排除仅含 tool_result 的 user 消息），
# 收集其后所有 assistant 消息中的 text 和 thinking 内容。
# texts 按 assistant 消息分组，每条 assistant 的文本合并为一个元素。
# 参数:
#   $1 - transcript 文件路径
# 返回: {"texts":["..."],"thinking":"...","session_id":"xxx"} 或空
# =============================================================================
extract_claude_response() {
    local transcript_file="$1"

    if [ ! -f "$transcript_file" ]; then
        return 1
    fi

    if [ "$JSON_HAS_JQ" = "true" ]; then
        jq -s '
# 找到最近一条含文本的 user 消息的索引
(
    [to_entries[] | select(
        .value.type == "user" and
        (
            (.value.message.content | type == "string" and length > 0) or
            (.value.message.content | type == "array" and (map(select(.type == "text")) | length > 0))
        )
    ) | .key] | if length > 0 then .[-1] else null end
) as $user_idx |
if $user_idx == null then
    null
else
    # 收集 user_idx 之后所有 assistant 消息
    [.[$user_idx + 1:] | .[] | select(.type == "assistant")] |
    {
        texts: [.[] | .message.content // [] | [.[] | select(.type == "text") | .text] | join("") | gsub("^\\n+|\\n+$"; "") | select(length > 0)],
        thinking: ([.[] | .message.content // [] | [.[] | select(.type == "thinking") | .thinking] | join("")] | join("\n\n") | gsub("^\\n+|\\n+$"; "")),
        session_id: ([.[] | .sessionId // empty] | if length > 0 then .[0] else "" end)
    } |
    if (.texts | length) == 0 then null else . end
end
' "$transcript_file" 2>/dev/null
    elif [ "$JSON_HAS_PYTHON3" = "true" ]; then
        "$PYTHON3" -c "
import sys, json

with open(sys.argv[1], 'r') as f:
    lines = f.readlines()

records = []
for line in lines:
    line = line.strip()
    if not line:
        continue
    try:
        records.append(json.loads(line))
    except:
        pass

# 找最近一条含文本的 user 消息索引
user_idx = None
for i in range(len(records) - 1, -1, -1):
    r = records[i]
    if r.get('type') != 'user':
        continue
    content = r.get('message', {}).get('content')
    if isinstance(content, str) and len(content) > 0:
        user_idx = i
        break
    if isinstance(content, list):
        has_text = any(item.get('type') == 'text' for item in content if isinstance(item, dict))
        if has_text:
            user_idx = i
            break

if user_idx is None:
    sys.exit(0)

# 收集 user_idx 之后所有 assistant 消息的 text 和 thinking
# texts 按 assistant 消息分组
stage_texts = []
thinkings = []
session_id = ''
for r in records[user_idx + 1:]:
    if r.get('type') != 'assistant':
        continue
    if not session_id:
        session_id = r.get('sessionId', '')
    content = r.get('message', {}).get('content', [])
    if not isinstance(content, list):
        continue
    msg_parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get('type') == 'text':
            msg_parts.append(item.get('text', ''))
        elif item.get('type') == 'thinking':
            thinkings.append(item.get('thinking', ''))
    msg_text = ''.join(msg_parts).strip()
    if msg_text:
        stage_texts.append(msg_text)

combined_thinking = '\n\n'.join(t for t in thinkings if t).strip()

if not stage_texts:
    sys.exit(0)

print(json.dumps({'texts': stage_texts, 'thinking': combined_thinking, 'session_id': session_id}))
" "$transcript_file" 2>/dev/null
    fi
}

# =============================================================================
# 从单个 transcript 文件中提取响应内容（分发 + 重试）
# 自动检测 Codex/Claude 格式，委托给对应的提取函数。
# 参数:
#   $1 - transcript 文件路径
#   $2 - turn_id (可选，Codex 持久化格式下用于定位目标 turn)
#   $3 - 最大重试次数 (可选，默认 5)
# 返回:
#   输出 JSON 字符串 {"texts":["...","..."],"thinking":"...","session_id":"..."}
#   退出码：0 = 提取到内容；2 = 正常执行但无内容；1 = 执行失败
#   （文件不存在、提取器进程异常退出）。既有调用方（extract_response /
#   Stop hook）只消费 stdout 不看退出码，本三态契约供 CLI 入口传播
# =============================================================================
extract_response_from_file() {
    local transcript_file="$1"
    local turn_id="$2"
    local max_retries="${3:-5}"
    local retry_count=0
    local result=""
    local extract_fn=""
    local retry_interval=0.1
    local final_status=2

    if [ ! -f "$transcript_file" ]; then
        return 1
    fi

    # 按格式选择提取函数、重试间隔和次数
    if is_codex_transcript "$transcript_file"; then
        log "Detected Codex transcript format"
        extract_fn="extract_codex_response"
        retry_interval=1  # Codex 持久化文件写入延迟比 Claude 大
    else
        extract_fn="extract_claude_response"
    fi

    while [ $retry_count -lt $max_retries ]; do
        result=$($extract_fn "$transcript_file" "$turn_id")
        local extract_status=$?
        if [ -n "$result" ] && [ "$result" != "null" ]; then
            echo "$result"
            return 0
        fi
        # 提取器进程本身失败（异常退出）与"正常无内容"区分：
        # 任一次异常即标记为 1，即使后续重试正常跑完但无内容——
        # 异常本身值得让调用方知道
        if [ $extract_status -ne 0 ]; then
            final_status=1
        fi

        retry_count=$((retry_count + 1))
        if [ $retry_count -lt $max_retries ]; then
            log "Retry $retry_count/$max_retries (interval=${retry_interval}s)"
            sleep $retry_interval
        fi
    done

    return $final_status
}

# =============================================================================
# 提取响应内容（带子代理回退）
# 参数:
#   $1 - 主 transcript 文件路径
#   $2 - turn_id (可选，Codex 持久化格式下用于定位目标 turn)
# 返回:
#   输出 JSON 字符串 {"texts":[...],"thinking":"...","session_id":"..."}
# 说明:
#   1. 先在主 transcript 文件中查找
#   2. 如果找不到，检查 subagents 目录，按修改时间倒序查找
# 注意: subagents 回退是为 Stop hook 落盘竞态设计的（后台发通知时主文件
#   可能尚未写入最终答复）。事后提取不应使用本函数的回退——主文件必有
#   内容，走到 subagent 分支只会取到子代理的输出——请直接调
#   extract_response_from_file。
# =============================================================================
extract_response() {
    local transcript_path="$1"
    local turn_id="$2"

    # 提前检查：路径为空直接返回
    if [ -z "$transcript_path" ]; then
        log "Transcript path is empty"
        return 1
    fi

    local result=""

    # 1. 先在主 transcript 文件中查找
    if [ -n "$transcript_path" ] && [ -f "$transcript_path" ]; then
        log "Searching in main transcript: $transcript_path"
        result=$(extract_response_from_file "$transcript_path" "$turn_id" 5)
        if [ -n "$result" ] && [ "$result" != "null" ]; then
            log "Found response in main transcript"
            echo "$result"
            return 0
        fi
    fi

    # 2. 主 transcript 找不到，尝试在 subagents 目录中查找
    local session_dir="${transcript_path%.jsonl}"
    local subagents_dir="$session_dir/subagents"

    if [ -d "$subagents_dir" ]; then
        log "Main transcript has no response, searching in subagents: $subagents_dir"

        # 按修改时间倒序遍历子代理文件（兼容 macOS + Linux）
        local subagent_files=()
        while IFS= read -r f; do
            [ -n "$f" ] && subagent_files+=("$f")
        done < <(ls -t "$subagents_dir"/*.jsonl 2>/dev/null)

        for subagent_file in "${subagent_files[@]}"; do
            if [ -f "$subagent_file" ]; then
                log "Searching in subagent: $(basename "$subagent_file")"
                result=$(extract_response_from_file "$subagent_file" "$turn_id" 3)
                if [ -n "$result" ] && [ "$result" != "null" ]; then
                    log "Found response in subagent: $(basename "$subagent_file")"
                    echo "$result"
                    return 0
                fi
            fi
        done
    fi

    log "No response found in transcript or subagents"
    return 1
}

# =============================================================================
# 直接执行入口（供 Python 后端 subprocess 调用）
# =============================================================================
# 用法: bash transcript.sh <transcript_path> [turn_id]
# 行为: 单次提取，不重试、不回退 subagents（事后调用主文件必有内容，
#       重试与子代理回退均为 Stop hook 的落盘竞态兜底）
# 退出码: 0 = 提取成功（JSON 输出到 stdout）
#         1 = 硬失败（文件缺失/不可读、解析器不可用、提取器执行失败）
#         2 = 无可提取内容（非故障，如目标轮次尚未落盘）
#       消费方（/copy 的 Python 侧）按码区分「空内容」与「读取故障」提示
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    _cli_transcript_path="${1:-}"
    _cli_turn_id="${2:-}"
    if [ -z "$_cli_transcript_path" ] || [ ! -f "$_cli_transcript_path" ] \
        || [ ! -r "$_cli_transcript_path" ]; then
        exit 1
    fi
    # jq 与 python3 均不可用属环境级故障，归 1（不能落到"无内容"的 2，
    # 否则会被误报成"轮次尚未完成"）
    if [ "$JSON_HAS_JQ" != "true" ] && [ "$JSON_HAS_PYTHON3" != "true" ]; then
        exit 1
    fi
    _cli_result=$(extract_response_from_file "$_cli_transcript_path" "$_cli_turn_id" 1)
    _cli_status=$?
    if [ -n "$_cli_result" ] && [ "$_cli_result" != "null" ]; then
        echo "$_cli_result"
        exit 0
    fi
    # 提取器三态退出码透传：2 = 无内容（非故障），1 = 执行失败
    exit "$_cli_status"
fi

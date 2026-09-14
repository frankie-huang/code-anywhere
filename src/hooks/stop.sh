#!/bin/bash
# =============================================================================
# src/hooks/stop.sh - Agent Stop 事件处理脚本
#
# 此脚本由 hook-router.sh 通过 source 调用，不直接执行
#
# 前置条件（由 hook-router.sh 完成）:
#   - $INPUT 变量包含从 stdin 读取的 JSON 数据
#   - 核心库、JSON 解析器、日志系统已初始化
#   - $PROJECT_ROOT, $SRC_DIR, $LIB_DIR 等路径变量已设置
#
# 适用场景:
#   - 主 Agent 完成响应
#   - 发送任务完成通知，包含 Agent 的最终响应内容
#
# 设计原则:
#   - 快速返回，不阻塞 Agent
#   - 通知发送在后台异步执行
#   - 任何错误不影响 Claude 正常退出
# =============================================================================

# 检查 stop_hook_active 标志，防止无限循环
STOP_HOOK_ACTIVE=$(json_get "$INPUT" "stop_hook_active")
if [ "$STOP_HOOK_ACTIVE" = "true" ]; then
    log "Stop hook already active, skipping"
    exit 0
fi

# transcript 提取函数（extract_response 等）位于共享库 lib/transcript.sh
source "$LIB_DIR/transcript.sh"

# =============================================================================
# 用 last_assistant_message 补全 response_json 的 texts 数组
# 参数:
#   $1 - response_json           extract_response 返回的 JSON（可为空）
#   $2 - last_assistant_message  Stop 事件提供的最终答复文本（可为空）
#   $3 - session_id              会话 ID（transcript 无数据时用于构造 fallback）
# 返回:
#   更新后的 response_json（stdout），补全失败时返回原值
# 说明:
#   Stop hook 后台执行时 transcript 可能尚未写入最终 assistant text，
#   但中间过程（thinking、早期 text）已落盘。本函数将 last_assistant_message
#   追加到 texts 末尾（如尚未包含）。transcript 无数据时构造最小 fallback。
# =============================================================================
supplement_last_message() {
    local response_json="$1"
    local last_msg="$2"
    local fallback_session_id="${3:-unknown}"

    if [ -z "$last_msg" ]; then
        echo "$response_json"
        return 0
    fi

    if [ -n "$response_json" ] && [ "$response_json" != "null" ]; then
        # transcript 有数据，追加最终答复（如 texts 末尾不是它）
        # 通过 stdin 管道 + 分隔符传入两个输入：
        #   - 避免 argv 传递长消息 (MAX_ARG_STRLEN=128KB)
        #   - 避免 local wrapper= + json_escape 方案，因为 json_escape 未覆盖所有控制字符，
        #     特殊字符会导致构造的 wrapper JSON 畸形而解析失败；分隔符方案原样传递无丢失
        local merged_json=""
        local SEP='__SUPPLEMENT_SEP_b7e2f4__'
        if [ "$JSON_HAS_JQ" = "true" ]; then
            merged_json=$(printf '%s\n%s\n%s' "$response_json" "$SEP" "$last_msg" \
                | jq -R -s -c --arg sep "$SEP" '
                    split($sep + "\n") as $raw_parts |
                    # jq split 无 maxsplit，需手动 re-join 以对齐 Python split(...,1)
                    (if ($raw_parts | length) > 2 then
                        [$raw_parts[0], ($raw_parts[1:] | join($sep + "\n"))]
                    else $raw_parts end) as $parts |
                    ($parts[0] | fromjson) as $resp |
                    $parts[1] as $raw |
                    # 幂等比较两侧同以全空白归一化：提取器口径随格式/解析器而异
                    # （claude-jq 只去换行、python 全空白 strip），与哪个提取器产出 texts
                    # 无关；归一化仅用于判空与去重，追加保留原文（$raw）
                    ($raw | gsub("^\\s+|\\s+$"; "")) as $msg |
                    # as 绑定不改变当前输入（仍是原始字符串），需切回 $resp 才能对 .texts 取值
                    $resp |
                    if $msg == "" then .
                    elif (.texts | length) == 0 or ((.texts[-1] | gsub("^\\s+|\\s+$"; "")) != $msg) then
                        .texts += [$raw]
                    else . end
                ' 2>/dev/null)
        elif [ "$JSON_HAS_PYTHON3" = "true" ]; then
            merged_json=$(printf '%s\n%s\n%s' "$response_json" "$SEP" "$last_msg" \
                | SEP="$SEP" "$PYTHON3" -c "
import sys, json, os
parts = sys.stdin.read().split(os.environ['SEP'] + chr(10), 1)
data = json.loads(parts[0])
raw = parts[1]
msg = raw.strip()
# 幂等比较两侧同 strip，与提取器口径无关（理由见 jq 分支注释）；归一化仅用于
# 判空（纯空白不追加，对齐提取器 select(length > 0)）与去重，追加保留原文（raw）
if msg and (not data.get('texts') or data['texts'][-1].strip() != msg):
    data.setdefault('texts', []).append(raw)
print(json.dumps(data, ensure_ascii=False))
" 2>/dev/null)
        fi
        if [ -n "$merged_json" ] && [ "$merged_json" != "null" ]; then
            log "Supplemented response with last_assistant_message"
            echo "$merged_json"
        else
            log "Supplement failed (empty parser result), falling back to original response"
            echo "$response_json"
        fi
    else
        # transcript 无数据，用 last_assistant_message 构造最小 response
        # 注：此路径几乎不可达（需 extract_response 彻底失败且 last_msg 非空），
        # json_escape 的控制字符风险在此无实际影响，无需走 stdin 管道
        local result="{\"texts\":[\"$(json_escape "$last_msg")\"],\"thinking\":\"\",\"session_id\":\"$fallback_session_id\"}"
        log "Constructed response from last_assistant_message (transcript unavailable)"
        echo "$result"
    fi
}

# =============================================================================
# 后台异步发送通知函数
#
# 调用链：
#   send_stop_notification_async()
#     ├─ extract_response(transcript_path)
#     │    └─ extract_response_from_file(transcript, 5 retries)
#     │         └─ (fallback) extract_response_from_file(subagent_file, 3 retries)
#     ├─ supplement_last_message(response_json, last_msg, session_id)
#     ├─ build_response_content(response_json, max_length)
#     └─ send_stop_notification(response_content, thinking)  —— 卡片构建与发送由平台承担
# =============================================================================
send_stop_notification_async() {
    # 捕获当前环境变量供后台使用
    local STOP_MESSAGE_MAX_LENGTH=$(get_config "STOP_MESSAGE_MAX_LENGTH" "10000")
    local STOP_THINKING_MAX_LENGTH=$(get_config "STOP_THINKING_MAX_LENGTH" "10000")
    local SESSION_ID=$(json_get "$INPUT" "session_id")
    local INPUT_TURN_ID=$(json_get "$INPUT" "turn_id")
    local TRANSCRIPT_PATH=$(json_get "$INPUT" "transcript_path")
    local LAST_ASSISTANT_MSG=$(json_get "$INPUT" "last_assistant_message")
    local PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(json_get "$INPUT" "cwd")}"
    local PROJECT_NAME=$(basename "${PROJECT_DIR:-$(pwd)}")
    local TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")

    # 检查是否有可用的发送渠道
    if ! im_channel_ready; then
        return 0
    fi

    # 前置解析 chat_id 并检查 mute 状态，muted 时直接跳过，避免白跑 transcript 提取
    local RESOLVED_CHAT_ID
    RESOLVED_CHAT_ID=$(get_chat_id "$SESSION_ID" "$PROJECT_DIR")
    if [ "$RESOLVED_CHAT_ID" = "$MUTED_SENTINEL" ]; then
        log "Session muted, skipping stop notification: $SESSION_ID"
        return 0
    fi

    log "Stop notification: extracting response from transcript"

    # 提取响应内容（texts 数组 + thinking）
    local THINKING=""
    local response_json=""
    # 无响应时的默认正文：渲染时落「任务已完成」默认元素
    # （与 build_response_content 对空输入的产出等价）
    local RESPONSE_CONTENT='{"texts":[],"truncated":false}'

    # 使用 extract_response 函数（支持子代理回退）
    response_json=$(extract_response "$TRANSCRIPT_PATH" "$INPUT_TURN_ID")

    # 用 Stop 事件提供的 last_assistant_message 补全竞态缺失的最终答复
    response_json=$(supplement_last_message "$response_json" "$LAST_ASSISTANT_MSG" "$SESSION_ID")

    # 有响应时：提取 thinking 与正文
    if [ -n "$response_json" ] && [ "$response_json" != "null" ]; then
        THINKING=$(json_get "$response_json" "thinking")
        RESPONSE_CONTENT=$(build_response_content "$response_json" "$STOP_MESSAGE_MAX_LENGTH")
    fi

    # 处理 thinking：STOP_THINKING_MAX_LENGTH=0 时跳过
    if [ "$STOP_THINKING_MAX_LENGTH" = "0" ]; then
        THINKING=""
        log "Thinking display disabled (STOP_THINKING_MAX_LENGTH=0)"
    elif [ -n "$THINKING" ] && [ ${#THINKING} -gt "$STOP_THINKING_MAX_LENGTH" ]; then
        THINKING="${THINKING:0:$STOP_THINKING_MAX_LENGTH}..."
        log "Thinking truncated to ${#THINKING} chars"
    fi

    # 发送完成通知
    send_stop_notification "$RESPONSE_CONTENT" "$THINKING" >/dev/null 2>&1
}

# 后台发送；单独 & 不够——宿主以 pipe 关闭（非 PID 退出）判断 hook 结束，
# 子进程继承 stdout/stderr 会导致 pipe 未关闭，需要 >/dev/null 2>&1 切断
# 注意: Stop hook 配置不要加 async: true，否则双层 async 可能导致此后台进程被提前终止
send_stop_notification_async >/dev/null 2>&1 &

# 立即返回，不阻塞 Agent
exit 0

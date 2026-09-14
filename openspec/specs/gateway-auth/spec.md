# gateway-auth Specification

## Purpose

定义飞书网关与 Callback 后端的双向认证机制，确保只有授权的后端才能与网关通信，防止未授权访问。
## Requirements
### Requirement: 飞书网关注册接口

飞书网关 MUST 提供 `POST /register` 接口，接收 Callback 后端的注册请求。

#### Scenario: 接收注册请求

- **GIVEN** 飞书网关正在运行
- **WHEN** 收到 POST `/register` 请求
- **AND** 请求 body 包含 `callback_url` 和 `owner_id`
- **THEN** 立即返回 `200` 状态码
- **AND** 返回 `{"status": "accepted", "message": "注册请求已接收，正在处理"}`

#### Scenario: 请求参数缺失

- **GIVEN** 飞书网关正在运行
- **WHEN** 收到 POST `/register` 请求
- **AND** 请求 body 缺少 `callback_url` 或 `owner_id`
- **THEN** 返回 `400` 状态码
- **AND** 返回 `{"error": "missing required fields: callback_url, owner_id"}`

#### Scenario: 异步处理注册

- **GIVEN** 飞书网关返回注册成功响应
- **WHEN** 后续查询映射表
- **THEN** 根据绑定状态决定：
  - 已绑定：直接调用 Callback 后端的 `/cb/register` 接口
  - 未绑定：向用户发送飞书授权卡片

### Requirement: 绑定关系存储

飞书网关 MUST 维护 `owner_id` 到 `callback_url` 和 `auth_token` 的映射关系。

#### Scenario: 创建新绑定

- **GIVEN** 用户授权新设备注册
- **WHEN** 创建绑定记录
- **THEN** 保存 `owner_id`、`callback_url`、`auth_token`、`updated_at`
- **AND** 持久化到 `runtime/bindings.json`

#### Scenario: 更新现有绑定

- **GIVEN** `owner_id` 已存在绑定
- **WHEN** 同一 `owner_id` 的新注册请求被授权
- **THEN** 覆盖原有的 `callback_url` 和 `auth_token`
- **AND** 更新 `updated_at` 时间戳

#### Scenario: 查询绑定

- **GIVEN** 绑定关系已创建
- **WHEN** 使用 `owner_id` 查询
- **THEN** 返回对应的 `callback_url` 和 `auth_token`
- **OR** 返回 `null`（如果不存在）

#### Scenario: 绑定持久化

- **GIVEN** 飞书网关重启
- **WHEN** 网关启动
- **THEN** 从 `runtime/bindings.json` 加载已有绑定

### Requirement: auth_token 生成

飞书网关 MUST 使用 HMAC-SHA256 算法生成 `auth_token`。

#### Scenario: 生成 auth_token

- **GIVEN** 需要为 `owner_id` 生成 auth_token
- **WHEN** 调用生成函数
- **THEN** 生成当前 Unix 时间戳
- **AND** 计算 `signature = HMAC-SHA256(FEISHU_VERIFICATION_TOKEN, owner_id + timestamp)`
- **AND** 返回 `base64url(timestamp) + "." + base64url(signature)`

#### Scenario: 验证 auth_token

- **GIVEN** 收到 `auth_token` 和 `owner_id`
- **WHEN** 验证 token 有效性
- **THEN** 解析 token 获取 timestamp 和 signature
- **AND** 重新计算 `expected_signature = HMAC-SHA256(FEISHU_VERIFICATION_TOKEN, owner_id + timestamp)`
- **AND** 使用恒定时间比较 signature 和 expected_signature
- **AND** 返回验证结果

### Requirement: Callback 后端注册回调接口

Callback 后端 MUST 提供 `POST /cb/register` 接口，接收网关的 auth_token。

#### Scenario: 接收 auth_token

- **GIVEN** Callback 后端正在运行
- **WHEN** 收到 POST `/cb/register` 请求
- **AND** 请求 body 包含 `owner_id` 和 `auth_token`
- **THEN** 验证 `owner_id` 与配置的 `FEISHU_OWNER_ID` 一致
- **AND** 存储 `auth_token` 到内存/文件
- **AND** 返回 `{"status": "ok", "message": "注册成功"}`

#### Scenario: owner_id 不匹配

- **GIVEN** Callback 后端正在运行
- **WHEN** 收到 POST `/cb/register` 请求
- **AND** 请求 body 中的 `owner_id` 与配置不一致
- **THEN** 返回 `403` 状态码
- **AND** 返回 `{"error": "owner_id mismatch"}`

### Requirement: 飞书授权卡片

飞书网关 MUST 向用户发送授权卡片，请求用户确认新设备注册。

#### Scenario: 发送授权卡片

- **GIVEN** 收到新的注册请求
- **AND** `owner_id` 未绑定任何 callback_url
- **WHEN** 处理注册请求
- **THEN** 向 `owner_id` 发送飞书卡片消息
- **AND** 卡片包含：来源 IP、Callback URL、允许按钮、拒绝按钮

#### Scenario: 用户允许注册

- **GIVEN** 用户点击授权卡片中的"允许"按钮
- **WHEN** 网关收到 `card.action.trigger` 事件
- **AND** `action.value.action` 为 `approve_register`
- **THEN** 调用 `action.value.callback_url` 指向的 `/cb/register` 接口
- **AND** 传递 `owner_id` 和生成的 `auth_token`
- **AND** 创建绑定记录
- **AND** 返回 `{"toast": {"type": "success", "content": "已授权绑定"}}`

#### Scenario: 用户拒绝注册

- **GIVEN** 用户点击授权卡片中的"拒绝"按钮
- **WHEN** 网关收到 `card.action.trigger` 事件
- **AND** `action.value.action` 为 `deny_register`
- **THEN** 不创建绑定记录
- **AND** 记录日志
- **AND** 返回 `{"toast": {"type": "info", "content": "已拒绝注册请求"}}`

### Requirement: 双向认证

飞书网关和 Callback 后端 MUST 在请求中携带 `auth_token` 进行身份验证。

#### Scenario: 网关转发消息携带 Token

- **GIVEN** 网关需要向 Callback 后端转发消息
- **AND** 存在有效的 `auth_token`
- **WHEN** 发送 HTTP 请求
- **THEN** 在请求头中添加 `X-Auth-Token: {auth_token}`

#### Scenario: 网关验证请求 Token

- **GIVEN** 网关收到 Callback 后端的请求
- **WHEN** 请求包含 `X-Auth-Token` 请求头
- **THEN** 解析 token 验证签名
- **AND** 验证 `owner_id` 与绑定关系一致
- **AND** 验证通过则处理请求
- **OR** 验证失败则返回 `401 Unauthorized`

#### Scenario: Callback 后端发送消息携带 Token

- **GIVEN** Callback 后端需要向网关发送消息
- **AND** 已存储有效的 `auth_token`
- **WHEN** 发送 HTTP 请求
- **THEN** 在请求头中添加 `X-Auth-Token: {auth_token}`

#### Scenario: Callback 后端验证请求 Token（可选）

- **GIVEN** Callback 后端收到网关的请求
- **WHEN** 请求包含 `X-Auth-Token` 请求头
- **THEN** 可选验证 token 与存储的一致
- **AND** 验证失败可返回 `401 Unauthorized`

### Requirement: WebSocket 隧道连接与认证

飞书网关 MUST 在现有 HTTP 端口上提供 WebSocket Upgrade 端点（`/ws/tunnel`），通过 register 消息统一认证。网关通过比对客户端携带的 auth_token 区分三种场景：
1. **续期模式**：有绑定 + token 匹配（同一终端重连），直接刷新 token
2. **换绑模式**：有绑定 + token 不匹配/缺失（新终端），需要飞书卡片授权
3. **首次注册模式**：无绑定，需要飞书卡片授权

#### Scenario: WS 握手建立

- **GIVEN** 飞书网关正在运行
- **WHEN** Callback 发送 HTTP GET 请求到 `/ws/tunnel?owner_id=ou_xxx`
- **AND** 请求包含 `Upgrade: websocket` 头
- **THEN** 网关返回 `101 Switching Protocols`
- **AND** 等待客户端发送 register 消息（30s 超时）

#### Scenario: 续期模式 - 同一终端重连（token 匹配）

- **GIVEN** 飞书网关已完成 WS 握手
- **AND** BindingStore 中已有 owner_id 的绑定记录
- **WHEN** 客户端发送 register 消息（含 auth_token 与绑定记录匹配）
- **THEN** 网关生成新 auth_token，暂存到 pending_auth_tokens
- **AND** 通过 WS 发送 `{"type":"auth_ok","auth_token":"new_xxx"}`
- **AND** 等待客户端发送 `{"type":"auth_ok_ack"}`
- **AND** 收到 auth_ok_ack 后更新 BindingStore 并注册到 WebSocketRegistry

#### Scenario: 换绑模式 - 新终端（token 不匹配）

- **GIVEN** 飞书网关已完成 WS 握手
- **AND** BindingStore 中已有 owner_id 的绑定记录
- **WHEN** 客户端发送 register 消息（auth_token 与绑定记录不匹配或缺失）
- **THEN** 连接进入 pending 状态
- **AND** 网关向 owner_id 发送飞书换绑授权卡片（展示旧终端 IP 和新终端 IP）

#### Scenario: 首次注册 - 无绑定进入 pending 状态

- **GIVEN** 飞书网关已完成 WS 握手
- **AND** BindingStore 中无 owner_id 的绑定记录
- **WHEN** 客户端发送 register 消息
- **THEN** 连接进入 pending 状态（不注册到 WebSocketRegistry）
- **AND** 网关向 owner_id 发送飞书授权卡片

#### Scenario: 授权通过（首次注册和换绑共用）

- **GIVEN** 有一个 pending 状态的 WS 连接（owner_id=ou_xxx）
- **WHEN** 飞书用户在授权卡片上点击"允许"
- **THEN** 网关生成 auth_token（HMAC-SHA256），暂存到 pending_auth_tokens 和 pending_binding_params
- **AND** 通过 WS 发送 `{"type":"auth_ok","auth_token":"xxx"}` 给 Callback
- **AND** 等待客户端发送 `{"type":"auth_ok_ack"}`
- **AND** 收到 auth_ok_ack 后更新 BindingStore，将连接从 pending 升级为已认证

#### Scenario: 授权拒绝

- **GIVEN** 有一个 pending 状态的 WS 连接（owner_id=ou_xxx）
- **WHEN** 飞书用户在授权卡片上点击"拒绝"
- **THEN** 通过 WS 发送 `{"type":"auth_error","message":"authorization denied"}`
- **AND** 直接关闭 socket（不使用 ws_close，因消息循环线程正在同一 socket 上 recv）
- **AND** 从 pending 中移除

#### Scenario: 授权超时

- **GIVEN** 有一个 pending 状态的 WS 连接
- **WHEN** 超过 10 分钟用户未操作授权卡片
- **THEN** 定期清理任务发送 `{"type":"auth_error","action":"stop","message":"authorization timeout"}`
- **AND** 关闭 WS 连接

#### Scenario: 连接替换

- **GIVEN** owner_id=ou_xxx 已在 WebSocketRegistry 注册（连接 A）
- **WHEN** 同一 owner_id 的新连接 B 认证成功
- **THEN** 向连接 A 发送 `{"type":"replaced","action":"stop"}` 消息
- **AND** 关闭连接 A
- **AND** 注册连接 B 到 WebSocketRegistry

#### Scenario: 用户解绑

- **GIVEN** owner_id 在 BindingStore 中有 WS 模式的绑定
- **WHEN** 用户在飞书端执行解绑操作
- **THEN** 删除 BindingStore 中的绑定记录
- **AND** 向该 owner_id 的 WS 连接发送 `{"type":"unbind","action":"stop","message":"user unbind"}`
- **AND** 客户端收到后清除本地 auth_token 并停止重连

#### Scenario: 连接断开时清理

- **GIVEN** owner_id 已在 WebSocketRegistry 注册
- **WHEN** WebSocket 连接断开（网络中断、客户端关闭、read timeout 超过 90s）
- **THEN** 从 WebSocketRegistry 移除该 owner_id 的映射

#### Scenario: pending 连接限制

- **GIVEN** 同一 owner_id 已有 5 个 pending 连接
- **WHEN** 第 6 个连接尝试进入 pending 状态
- **THEN** 拒绝新连接，返回 `{"type":"auth_error","message":"too many pending connections"}`

#### Scenario: 授权卡片冷却

- **GIVEN** 同一 owner_id 在 60 秒内已触发过授权卡片发送
- **WHEN** 新的 pending 连接尝试触发卡片发送
- **THEN** 拒绝新连接，返回 `{"type":"auth_error","message":"too many requests"}`

### Requirement: WebSocket 消息协议

飞书网关和 Callback 后端 MUST 使用约定的 JSON 消息格式通信。

#### Scenario: 注册消息格式（Callback → Gateway）

- **GIVEN** WS 握手完成后
- **WHEN** 客户端发送注册消息
- **THEN** 消息格式为：
  ```json
  {
    "type": "register",
    "owner_id": "ou_xxx",
    "auth_token": "xxx",
    "reply_in_thread": false,
    "claude_commands": ["opus"],
    "default_chat_dir": "~/work"
  }
  ```
- **AND** `auth_token` 为可选字段（已有 token 时携带，用于续期/换绑判断）
- **AND** `reply_in_thread`、`claude_commands`、`default_chat_dir` 为配置参数

#### Scenario: 注册成功消息格式

- **GIVEN** 续期/首次注册/换绑授权通过
- **WHEN** 网关发送注册成功通知
- **THEN** 消息格式为：
  ```json
  {
    "type": "auth_ok",
    "auth_token": "timestamp_b64.signature_b64"
  }
  ```

#### Scenario: 客户端确认消息格式

- **GIVEN** 客户端收到 auth_ok 并存储 auth_token
- **WHEN** 客户端发送确认
- **THEN** 消息格式为：
  ```json
  {
    "type": "auth_ok_ack"
  }
  ```
- **AND** 网关收到后才更新 BindingStore 并注册连接

#### Scenario: 注册失败消息格式

- **GIVEN** 授权被拒绝或超时
- **WHEN** 网关发送注册失败通知
- **THEN** 消息格式为：
  ```json
  {
    "type": "auth_error",
    "message": "authorization denied|authorization timeout|connection inactive"
  }
  ```
- **AND** 部分错误（如 authorization timeout、connection inactive）带有 `action: "stop"` 字段

#### Scenario: 连接被替换消息格式

- **GIVEN** 新连接认证成功，替换旧连接
- **WHEN** 网关通知旧连接被替换
- **THEN** 消息格式为：
  ```json
  {
    "type": "replaced",
    "action": "stop"
  }
  ```

#### Scenario: 用户解绑消息格式

- **GIVEN** 用户在飞书端执行解绑操作
- **WHEN** 网关通知客户端解绑
- **THEN** 消息格式为：
  ```json
  {
    "type": "unbind",
    "action": "stop",
    "message": "user unbind"
  }
  ```

#### Scenario: 请求消息格式

- **GIVEN** 网关需要通过 WS 转发请求到 Callback
- **WHEN** 构造请求消息
- **THEN** 消息格式为：
  ```json
  {
    "type": "request",
    "id": "uuid-for-matching",
    "method": "POST",
    "path": "/cb/decision",
    "headers": {},
    "body": {}
  }
  ```

#### Scenario: 响应消息格式

- **GIVEN** Callback 收到 WS 请求并处理完成
- **WHEN** 构造响应消息
- **THEN** 消息格式为：
  ```json
  {
    "type": "response",
    "id": "uuid-for-matching",
    "status": 200,
    "headers": {},
    "body": {}
  }
  ```
- **AND** `id` 与请求消息的 `id` 一致

#### Scenario: 错误消息格式

- **GIVEN** 请求处理过程中发生错误（超时、handler 异常等）
- **WHEN** 构造错误消息
- **THEN** 消息格式为：
  ```json
  {
    "type": "error",
    "id": "uuid-for-matching",
    "code": "timeout|handler_error|invalid_request",
    "message": "human-readable error description"
  }
  ```

#### Scenario: 心跳保活

- **GIVEN** WebSocket 连接已建立（已认证状态）
- **WHEN** 连接空闲超过心跳间隔（默认 30s）
- **THEN** Callback 客户端发送 WebSocket ping frame
- **AND** 网关响应 pong frame
- **AND** 如果网关连续 90s 未收到任何消息，断开连接

### Requirement: 双模式路由分发

飞书网关 MUST 支持 HTTP 和 WebSocket 双模式路由分发。

#### Scenario: 优先使用 WebSocket

- **GIVEN** owner_id 在 WebSocketRegistry 中有活跃连接（已认证状态）
- **WHEN** 网关需要转发请求到该 owner_id 的 Callback
- **THEN** 通过 WebSocket 连接发送请求消息
- **AND** 同步等待响应消息（超时 10s）

#### Scenario: WS 转发失败 fallback 到 HTTP

- **GIVEN** owner_id 在 WebSocketRegistry 中有活跃连接
- **AND** owner_id 在 BindingStore 中有 HTTP callback_url
- **WHEN** WS 转发失败（超时或连接异常）
- **THEN** 自动降级到 HTTP POST 方式转发

#### Scenario: 无 WS 连接 fallback 到 HTTP

- **GIVEN** owner_id 在 WebSocketRegistry 中无活跃连接
- **AND** owner_id 在 BindingStore 中有 HTTP callback_url
- **WHEN** 网关需要转发请求到该 owner_id 的 Callback
- **THEN** 使用现有 HTTP POST 方式转发

#### Scenario: 无可用路由

- **GIVEN** owner_id 既无 WS 连接也无 HTTP 绑定
- **WHEN** 网关需要转发请求
- **THEN** 返回错误 `{"error": "no available connection for owner_id"}`

### Requirement: Callback WebSocket 客户端

Callback 后端 MUST 支持 WebSocket 隧道客户端模式（当 `GATEWAY_URL` 配置为 `ws://` 或 `wss://` 格式时）。

#### Scenario: 启动 WS 客户端

- **GIVEN** `GATEWAY_URL` 配置为 `ws://` 或 `wss://` 格式
- **AND** 配置了 `FEISHU_OWNER_ID`
- **WHEN** Callback 后端启动
- **THEN** 启动 WS 客户端 daemon 线程
- **AND** 连接网关 WS 端点（URL 仅含 owner_id）
- **AND** 发送 register 消息（含 auth_token（如有）和配置参数）

#### Scenario: 收到 auth_ok 消息

- **GIVEN** WS 客户端已连接并发送了 register 消息
- **WHEN** 收到 `{"type":"auth_ok","auth_token":"xxx"}`
- **THEN** 将 auth_token 存储到本地 AuthTokenStore
- **AND** 发送 `{"type":"auth_ok_ack"}` 确认
- **AND** 连接升级为已认证状态，重置重连延迟

#### Scenario: 收到 auth_error 消息

- **GIVEN** WS 客户端已连接
- **WHEN** 收到 `{"type":"auth_error","message":"..."}`
- **THEN** 记录错误日志
- **AND** 等待退避时间后重新连接（重新触发注册流程）

#### Scenario: 断线重连

- **GIVEN** WS 客户端已连接（已认证状态）
- **WHEN** 连接断开（非 replaced）
- **THEN** 等待退避时间（初始 1s，指数增长，最大 60s）
- **AND** 退避时间加入随机 jitter（`delay * (0.5 + random() * 0.5)`）
- **AND** 重新连接网关（携带已存储的 auth_token）

#### Scenario: 计划维护重连

- **GIVEN** WS 客户端收到 `{"type":"shutdown"}` 消息
- **WHEN** 连接断开
- **THEN** 立即重连（无退避），因为网关即将重启完成

#### Scenario: 连接被替换

- **GIVEN** WS 客户端收到 `{"type":"replaced","action":"stop"}` 消息
- **WHEN** 连接断开
- **THEN** 永久停止客户端（running=False），不自动重连
- **AND** 记录警告日志
- **AND** 原因：换绑后旧 token 不匹配，重连会反复触发换绑卡片

#### Scenario: 用户解绑

- **GIVEN** WS 客户端收到 `{"type":"unbind","action":"stop"}` 消息
- **WHEN** 处理消息
- **THEN** 清除本地存储的 auth_token（调用 AuthTokenStore.delete）
- **AND** 永久停止客户端（running=False），不自动重连
- **AND** 记录信息日志

#### Scenario: 处理 WS 请求

- **GIVEN** WS 客户端已连接（已认证状态）
- **WHEN** 收到网关的请求消息
- **THEN** 在线程池中（max_workers=8）处理请求，不阻塞消息循环
- **AND** 通过 `urllib.request` 调用本地 HTTP handler（绕过系统代理）
- **AND** 使用发送锁保护 socket 写操作（防止多线程帧交错）
- **AND** 构造响应消息返回
- **AND** 处理异常时返回 error 消息

### Requirement: 优雅关闭

飞书网关 MUST 在关闭时通知所有 WebSocket 客户端。

#### Scenario: 网关计划关闭

- **GIVEN** 网关收到 SIGINT/SIGTERM 信号
- **WHEN** 开始关闭流程
- **THEN** 向所有活跃 WS 连接（含 pending）发送 `{"type":"shutdown"}` 消息
- **AND** 等待 1s 后关闭所有连接


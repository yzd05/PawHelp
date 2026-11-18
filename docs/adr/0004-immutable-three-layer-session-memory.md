# ADR 0004：不可变三层会话上下文、协议级工具配对裁剪与防篡改护栏

- **状态**：已采纳（Accepted）
- **日期**：2026-10-22
- **关联模块**：`app/context.py`、`app/workflow/memory.py`、`app/workflow/understanding.py`、`app/database/models.py`、`CONTEXT.md`

## Context（背景与问题）

长周期电商售后会话（常跨越十余轮多订单核对、物流轨迹追踪与退换货协商）在上下文管理上存在三类高频工程故障：

1. **滚动摘要（Rolling Summary）的不可逆污染**：每隔数轮重写一份全局总摘要时，模型极易把多笔历史订单号
   张冠李戴、改写金额数字或漏掉前置拒保条件；一旦某次覆盖摘要写错，后续所有轮次都会被永久污染且无法回退。
2. **滑窗截断破坏 Function Calling 协议**：按固定条数或 Token 粗暴截断历史时，若切掉 `AIMessage(tool_calls)`
   却保留对应的 `ToolMessage`（或反之），会直接触发 OpenAI 兼容接口 `400 Bad Request` 协议级报错。
3. **同步摘要阻塞首字响应（TTFT）与指代捏造**：在对话主链路同步等待摘要推理会明显拉高首包延迟；同时指代改写
   模型偶发凭空编造单号，或用历史旧单号覆盖用户本轮显式输入的新单号。

## Decision（核心决策）

### 1. 三层独立会话视图 + 不可变分段追加摘要（Append-Only Segments）

| 上下文层级 | 承载内容与压缩方式 | 一致性与性能契约 |
|---|---|---|
| Layer 1（近期原文层） | 最近若干轮完整 `Human / AI / Tool` 消息 | 原样保留细节，支撑当轮多步工具推理与精确引用 |
| Layer 2（中间半压层） | 较早轮次消息，用 `tool_observation` 压缩冗长工具 JSON | 确定性裁剪，不调模型、零幻觉，保留单号与核心状态 |
| Layer 3（早期分段摘要） | 按确定区间 `[from_msg_id, upto_msg_id]` 追加写入 `conversation_summaries` | **后台异步生成（不阻塞当轮 SSE 首字）**；`seq` 单调追加，历史段永不重写 |

- **悲观锁防重与失败不推锚点**：后台 `_summarize`（`app/workflow/memory.py:545-649`）使用
  `SELECT ... FOR UPDATE` 锁定会话行核对 `summary_upto_msg_id`；若模型超时、超长或数字校验失败，
  **绝不推进覆盖锚点**，后续轮次可从原边界无损重试（带 `_failed_anchors` 最大重试上限防死循环）。
- **背景与新段严格解耦**：向摘要模型传参时显式区分 `{"background_only": view.summary, "new_interval": new_batch}`，
  旧摘要仅用于理解代词指代，严禁模型将旧背景重复抄入新段或改写既有分段。
- **有界摘要投影**：当累积摘要段总长超出 `context_summary_max_tokens` 时，按“首段建档背景 + 最新若干段”
  组装注入投影，底层数据库分段记录保持原样不变。

### 2. 协议级完整工具配对裁剪（`_has_complete_tool_pairs`）

- `prepare_context`（`app/context.py:39-89`）在预算内寻找最早可保留起点时，强制要求候选切片同时满足：
  1. 起点消息必须是 `HumanMessage`，绝不从半轮助手回复或工具返回切入；
  2. 通过 `_has_complete_tool_pairs` 双向集合校验——切片内所有 `AIMessage.tool_calls` 的 `id` 集合必须与
     所有 `ToolMessage.tool_call_id` 集合完全相等，从根源杜绝悬空工具消息引发的 API 400 错误。

### 3. 中文数字等价白名单与单号防篡改硬护栏

- **摘要数字防捏造校验**：对模型产出的新摘要做 NFKC 归一化并提取全部 4~20 位数字串，结合
  `_chinese_numerals_to_strings`（将原文中的“一零零一”、“两千五”等中文数字折算为阿拉伯数字加入白名单，
  `app/workflow/memory.py:146-171, 580-583`）做严格子集校验；出现任何未知编号立即抛错丢弃该段摘要。
- **指代消解确定性护栏**（`app/workflow/understanding.py:232-280`）：
  - 改写问句若出现用户原话与历史均未提过的 4~20 位数字，直接丢弃改写回退原话；
  - 用户本轮显式写出订单号（`explicit_order_ids`）时，严禁被历史单号替换；
  - 同一句提及多笔订单后跟单数指代（“那个订单”）时，由 `explicit_order_ambiguity` 确定性拦截并弹出选单卡。

## Trade-offs & Rejected Alternatives（权衡与被否决方案）

- **否决“滚动重写单字段总摘要（Rolling Summary）”**：代码虽简短，但无法防止多轮累积幻觉、单号串改与并发写覆盖。
- **否决“跨会话全局用户记忆池”**：电商客服诉求强绑定当次会话交易上下文，跨会话检索旧聊天记录极易把历史退货单
  混入新咨询，故严格遵循 `CONTEXT.md` 单会话隔离边界。
- **近似 Token 计数取舍**：采用统一 `chars_per_token` 保守近似估算与 `bound_text` 二分截断，避免强耦合特定厂商
  本地 Tokenizer，换取多模型无缝切换与微秒级本地预算计算。

## 100+ QPS Production Evolution（百级并发生产演进）

当前异步摘要任务维护在单进程内存字典 `self.tasks` 中，进程关闭（`close()`）时取消未完成任务（因锚点未推进，
重启后可自动补做，仅浪费一次在途推理）。演进至 100+ QPS 多副本生产环境时：

1. **优雅停机排水与外置任务队列**：在应用关闭阶段预留 5~10 秒排水窗口等待在途摘要落库，并将摘要任务投递至
   Redis Streams / ARQ 异步工作队列，避免滚动发版打断在途 LLM 调用且解耦 Web 副本内存状态。
2. **热会话分层视图缓存**：以 `(conversation_id, latest_msg_id, summary_upto_msg_id)` 为不可变缓存键在 Redis
   缓存组装好的 `HistoryView`，降低长会话高频交互对 MySQL 历史表的重复读取压力。

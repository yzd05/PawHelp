# ADR 0002：确定性工作流前置路由与两阶段 LangGraph 图中断确认机制

- **状态**：已采纳（Accepted）
- **日期**：2026-10-22（2026-11-17 增补快慢模型超时分层与无状态扩容路径）
- **关联模块**：`app/workflow/graph.py`、`app/workflow/nodes.py`、`app/workflow/understanding.py`、`app/workflow/tool_agent.py`、`app/tools/registry.py`、`app/main.py`

## Context（背景与问题）

在电商售后、查单与退换货场景中，直接将知识检索与业务写操作全部交给纯 ReAct Function Calling 循环存在三类高危隐患：

1. **跳过检索与绕过门控幻觉**：把 `query_faq` 作为可选工具交给大模型时，模型常凭预训练记忆直接编造
   退货运费承担方或保修期，绕过相关性阈值与引用溯源核验。
2. **未授权副作用写入**：当用户情绪激动或诉求含糊时，纯自主 Agent 可能在未向用户核对单号、工单类别与
   问题描述的情况下直接落库建单，污染真实客诉工单流。
3. **重复查单拖慢首字延迟**：多轮对话已明确目标订单后，纯 Agent 循环仍常在首轮重复调用 `query_order`，
   白白增加一轮大模型推理与工具往返。
4. **工具循环失控风险**：缺乏受控步数预算与提示提醒计数时，模型在参数缺失场景下可能反复发起无效工具重试。

## Decision（核心决策）

### 1. 确定性工作流前置路由 + 下线 Agent 侧 `query_faq`

- **固定前置理解与路由**：由 `StateGraph(WorkflowState)` 编排 `resolve_reference → classify_intent → route`
  （`app/workflow/graph.py:414-454`）。路由仅依据结构化 `IntentDecision` 与确定性 `plan_route`
  （`app/workflow/understanding.py:291-340`）分发至 `knowledge / order_knowledge / business / select_order / complaint`，
  不在用户原话上做脆弱关键词猜测。
- **强制前置检索门控**：在 `app/workflow/graph.py:175` 显式将 `query_faq` 从 Agent 工具集中移除。
  所有知识与售后规则诉求强制走确定性节点 `retrieve → confidence_gate`（全库混合检索 + 售后多角度
  `_policy_queries` 扩写，不设脆弱分类硬过滤），未过门控直接拒答并给出`转人工 / 建工单`按钮。

### 2. 两阶段图中断（Two-Stage Graph Interrupts）与预取注入

| 中断阶段 | 触发条件与 SSE 事件 | 恢复契约与防错收益 |
|---|---|---|
| `select_order` | 缺订单号或多单指代歧义 → `order_selection` | 用户点选卡片后 `Command(resume=order_id)` 恢复；预取订单直接合成为首轮 `query_order` 消息对，省去 1 轮 LLM + 1 次查单 |
| `confirm_ticket` | Agent 提议建工单 → `ticket_confirmation` | 生成唯一 `proposal_id` 挂起；前端确认后核验 `user_id + conversation_id + message_id + proposal_id` 四元组匹配才真正落库 |

- **跨重启持久化与冷自愈**：在线应用挂载 `AsyncSqliteSaver` 检查点（测试环境用 `InMemorySaver` 隔离）；
  当检查点为空或上下文协议版本升级时，`_reconstruct_history_from_db`（`app/workflow/graph.py:493-503`）
  自动从 MySQL 权威消息表重建完整会话历史。

### 3. 快慢模型分离、步数上界与超时熔断预算

- **快慢分层**：指代消解、意图识别、问题拆分、查询改写等结构化轻节点统一绑定 `create_fast_chat_model`
  （独立短超时 `15s`、`max_retries=1`）；`classify_intent` 遇超时或连接异常直接快速降级，避免双层重试叠加成 6 次长等待。
- **工具循环硬护栏**：`ToolAgentNodes`（`app/workflow/tool_agent.py`）对模型推理步数与系统提醒分支统一计入
  `agent_steps` 硬上限，物流与售后外部工具通过 MCP 白名单策略与只读重试执行器（`app/tools/executor.py`）隔离调用。
- **整轮总挂钟上限**：`_run_observed_graph` 外层设置 `workflow_turn_timeout_seconds = 90s` 总超时，
  且观测计费通过 `asyncio.shield` 独立落库，防止上游抖动长期锁死单会话并发锁。

## Trade-offs & Rejected Alternatives（权衡与被否决方案）

- **否决“纯 ReAct 单循环全托管”**：牺牲了“一句话同时查实时物流又问退货运费规则”的自由跨路由编排
  （当前单选路由会优先处理其中一侧，或被充分性门控要求覆盖全部子问而保守拦截），换取知识 100% 过校准门控、
  写操作 100% 经人工确认的强安全边界。跨路由复合诉求留待前置轻量 Intent Decomposition 演进。
- **否决“自然语言回复‘确认’触发写操作”**：纯文本确认易被上下文裁剪或提示词注入伪造；绑定 `proposal_id`
  与检查点挂起态的结构化 `Command(resume)` 提供了协议级幂等与防重放保证。

## 100+ QPS Production Evolution（百级并发生产演进）

当前单机使用 `AsyncSqliteSaver`、进程内 `WeakValueDictionary` 会话锁以及保护 `BgeM3Embedder` / `BgeReranker`
的 `threading.Lock()`，无需额外中间件即可在宿主机直通单卡 GPU 跑通闭环。演进至 100+ QPS 多副本部署时：

1. **共享存储 Checkpointer**：通过现有的 `WorkflowChatService.set_checkpointer()` 注入接口
   （`app/workflow/graph.py:406`），零业务代码改动切换至 `AsyncPostgresSaver` 或 Redis Checkpointer，
   彻底消除多进程 `SQLITE_BUSY` 与跨节点中断恢复丢失。
2. **本地嵌入/重排模型服务化**：将约 `3.3 GB` 的 BGE-M3 与 Reranker 抽离至独立 HuggingFace TEI / Infinity
   推理服务并开启 Dynamic Batching，消除 `N × 3.3 GB` 显存冗余复制与进程内 `_inference_lock` 串行吞吐瓶颈。
3. **网关会话亲和与分布式租约**：API 网关按 `conversation_id` 一致性哈希转发，配合 Redis `SET NX PX` 短租约替代进程内锁。

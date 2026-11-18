# ADR 0001：以 MySQL 为唯一真相源、Milvus 2.6 为双路派生索引的四态最终一致架构

- **状态**：已采纳（Accepted）
- **日期**：2026-10-22（2026-11-17 增补墓碑态与毒丸单条隔离）
- **关联模块**：`app/database/models.py`、`app/repositories/knowledge.py`、`app/knowledge/vector_store.py`、`app/knowledge/vectorization.py`、`app/services/knowledge_admin.py`、`app/knowledge/search.py`

## Context（背景与问题）

电商客服知识库同时面临三类无法靠单一存储兼顾的工程诉求：

1. **双路混合检索**：既要召回口语化同义问法（依赖 1024 维 `BAAI/bge-m3` 稠密向量），
   又要精准命中 `MH-LP100`、`FT-40`、`E3` 等 SKU 型号与故障代码（依赖词面 BM25 稀疏检索）。
2. **关系型治理与事务关联**：知识块（`knowledge_chunks`）需要承载来源分类
   （`markdown / manual / flywheel / mined`）、同章节邻接链表（`prev_chunk_id / next_chunk_id`）
   及写入冲突审计记录，并与会话、审核队列共享 MySQL 事务边界。
3. **跨存储无分布式事务（No 2PC）**：MySQL 8.4 与 Milvus 2.6 之间不存在两阶段提交。
   若批量向量化中途超时、单条超长 UTF-8 坏块报错，或删除中途进程崩溃，极易引发
   “检索搜出已删幽灵向量”或“单条脏数据卡死整批同步”的一致性事故。

## Decision（核心决策）

### 1. 存储职责单向收敛（MySQL SoT + Milvus Derived Index）

- **MySQL 8.4 为唯一权威真相源（Source of Truth）**：所有知识正文、元数据、状态流转与检索水合
  （Hydration）一律以 MySQL 记录为准。
- **Milvus 2.6 为可重建派生索引**：仅存储字符串主键 `id`、1024 维 `dense_vector`、由内置中文分词
  `FunctionType.BM25` 自动派生的 `sparse_vector` 及最小标量元数据（`app/knowledge/vector_store.py:241-310`）。
- **检索水合强过滤防幽灵**：`HybridKnowledgeSearch` 从 Milvus 取回双路候选并完成服务端
  `RRFRanker` 融合后，回表 MySQL 时仅水合 `vectorize_status == "done"` 的有效行
  （`app/repositories/knowledge.py`）。即使极端崩溃在 Milvus 残留孤儿向量，在线问答也绝不泄露。

### 2. 无 2PC 下的 `pending / done / failed / deleting` 四态状态机

| 状态 | 语义与检索可见性 | 进入条件与恢复动作 |
|---|---|---|
| `pending` | 已落盘待建索引；检索不可见 | 新增、编辑或对账发现缺向量时写入；由 `vectorize_pending` 消费 |
| `done` | 双库对齐就绪；检索可见 | Milvus `upsert` 成功后提交，记录稳定 `vector_id = str(id)` |
| `failed` | 毒丸坏块隔离；检索不可见 | 单条超长/数据格式异常时自动降级隔离，不阻塞同批其余正常块 |
| `deleting` | 瞬态删除墓碑；立即隐身 | 删除或文档重建时先落墓碑，待 Milvus 删完向量后再物理删行 |

- **先墓碑后物理删除**：`delete_chunk` 与 `sync_materials`（`app/services/knowledge_admin.py:574-635`）
  先在 MySQL 提交 `deleting` 状态（立即从检索水合与 `/kb` 有效计数视图隐身），再执行 Milvus 删除，
  最后物理移除 MySQL 行，彻底消除“MySQL 已删而 Milvus 仍可搜出”的窗口。

### 3. 批内毒丸隔离与双向孤儿对账

- **毒丸单条降级隔离（Poison-Pill Isolation）**：`vectorize_pending`（`app/knowledge/vectorization.py:112-195`）
  遇到非瞬态数据错误（如超过 `MAX_TEXT_BYTES = 65535` UTF-8 字节上限）时，自动从批处理降级为逐条重试，
  仅将坏块标为 `failed` 并正常提交同批好块。
- **双向对账（Reconciliation）**：使用 `count(*)`（`consistency_level="Strong"`，规避 Compaction 前统计滞后）
  与 `list_all_ids` 双向核对：MySQL `done` 但 Milvus 缺失的行自动回退 `pending`；Milvus 孤儿向量与残留
  `deleting` 墓碑在对账时自动清理（`app/services/knowledge_admin.py:71-85`）。

## Trade-offs & Rejected Alternatives（权衡与被否决方案）

- **否决“单库 MySQL 全文索引 / pgvector 全包”**：MySQL 中文全文检索对口语同义问法召回弱；Milvus 2.6
  单表原生集成稠密向量 + BM25 稀疏向量与 RRF，省去额外维护 Elasticsearch 的运维成本。
- **否决“将向量推理塞入数据库长事务模拟强一致”**：本地 BGE-M3 推理与 gRPC 写入耗时数百毫秒，包入
  MySQL 事务会迅速打满连接池并锁死行记录；采用秒级最终一致与水合兜底更稳健。
- **分词粒度取舍**：Milvus 默认 `chinese` 分词器对连字符型号（`MH-LP100`）切分偏粗，在 `F_identifier`
  桶上依靠双路融合与 `BAAI/bge-reranker-v2-m3` 交叉重排兜底，不额外引入在线自定义词典负担。

## 100+ QPS Production Evolution（百级并发生产演进）

当前单实例通过 `_MILVUS_EXECUTOR = ThreadPoolExecutor(max_workers=1)`（`app/knowledge/vector_store.py:40`）
串行复用同步 `MilvusClient`，规避 `pymilvus` 跨线程 gRPC 隐患。演进至 100+ QPS 多副本生产环境时：

1. **读写执行器与进程物理分离**：将离线/后台批量向量化与对账拆至独立 Worker 进程与独立 `MilvusClient`，
   消除大批量 `upsert / query_iterator` 在单线程执行器上对实时检索造成的队头阻塞（HoL Blocking）。
2. **Transactional Outbox 流式同步**：高频运营写入改为在 MySQL 事务内落盘 Outbox 事件表，由后台消费者
   幂等异步投递至 Milvus 分布式集群，在线读路径改为多连接池并发查询。

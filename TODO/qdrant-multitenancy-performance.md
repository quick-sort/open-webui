# Qdrant Multitenancy 模式性能分析

## 场景：20 个 Knowledge，单个 Knowledge 文件数量多

## 物理 Collection 架构

Multitenancy 模式下，所有数据归入 5 个物理 collection，通过 `tenant_id` 做逻辑隔离：

| 物理 collection | 路由规则 | 说明 |
|---|---|---|
| `{prefix}_knowledge` | 不匹配其他规则的 name（即 knowledge UUID） | 所有 knowledge 的数据都在这一个 collection 里 |
| `{prefix}_files` | `file-` 开头 | 所有独立文件 |
| `{prefix}_memories` | `user-memory-` 开头 | 用户记忆 |
| `{prefix}_web-search` | `web-search-` 开头 | 网页搜索 |
| `{prefix}_hash-based` | 63位hex字符串 | YouTube/URL |

路由逻辑在 `_get_collection_and_tenant_id` 方法中：
```python
# qdrant_multitenancy.py
def _get_collection_and_tenant_id(self, collection_name: str) -> Tuple[str, str]:
    tenant_id = collection_name
    if collection_name.startswith('user-memory-'):
        return self.MEMORY_COLLECTION, tenant_id
    elif collection_name.startswith('file-'):
        return self.FILE_COLLECTION, tenant_id
    elif collection_name.startswith('web-search-'):
        return self.WEB_SEARCH_COLLECTION, tenant_id
    elif len(collection_name) == 63 and all(c in '0123456789abcdef' for c in collection_name):
        return self.HASH_BASED_COLLECTION, tenant_id
    else:
        return self.KNOWLEDGE_COLLECTION, tenant_id  # ← knowledge UUID 走这里
```

## 关键 HNSW 配置

```python
hnsw_config=models.HnswConfigDiff(
    payload_m=self.QDRANT_HNSW_M,      # 分区内 HNSW 图的连接数
    ef_construct=self.QDRANT_HNSW_EF_CONSTRUCT,
    on_disk=self.QDRANT_HNSW_ON_DISK,
    m=0,                                 # ← 全局 HNSW 索引被禁用
)
```

- `m=0`：不构建全局 HNSW 图，只依赖 tenant 分区内的子图
- `payload_m`：控制每个 tenant 分区内 HNSW 图的质量
- `tenant_id` 字段标记了 `is_tenant=True`，Qdrant 为每个 tenant 维护独立 HNSW 子图

## 检索流程

```python
# search 方法 - 按 tenant_id 过滤后做 ANN
query_filter=models.Filter(must=[_tenant_filter(tenant_id)])
# tenant_id = knowledge UUID
```

查询某个 knowledge 时：先按 `tenant_id` 定位到该 knowledge 的分区 → 在分区内的 HNSW 子图做 ANN 搜索。

## 性能结论

### 20 个 Knowledge 场景：性能没问题

- 20 个 tenant 是很小的数量，Qdrant 的 tenant 分区机制处理得很好
- 单个 knowledge 文件多 = 该分区的 HNSW 子图大，和普通模式下大 collection 一样的效果
- `payload_m` 和 `ef_construct` 控制分区内搜索质量，可按需调优

### 重复存储的影响

- `_knowledge` collection 中的数据量是正确的，不影响检索速度
- `_files` collection 中存了冗余的 `file-{id}` tenant 数据，在 Native FC 模式下完全不会被查询
- 冗余数据白白占用内存/磁盘，但不影响查询性能

### 与修改方案的关系

消除重复存储方案在 multitenancy 模式下效果更明显：
- 减少 `_files` 物理 collection 中的无用 point
- 降低 Qdrant 整体内存占用
- 检索侧不受影响（仍然查 `_knowledge` collection 的对应 tenant 分区）

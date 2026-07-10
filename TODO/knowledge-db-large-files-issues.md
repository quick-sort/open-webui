# Knowledge 数据库层面性能问题分析

## 场景：单个 Knowledge 有大量文件

## 问题列表

### 1. [高] `get_files_by_id` SELECT * 加载完整 `data` 列

```python
# knowledge.py
def get_files_by_id(self, knowledge_id, db):
    files = (
        db.query(File)  # ← SELECT * FROM file，包含 data 列
        .join(KnowledgeFile, File.id == KnowledgeFile.file_id)
        .filter(KnowledgeFile.knowledge_id == knowledge_id)
        .all()
    )
```

`File.data` 是 JSON 列，存了文件的全部文本内容（`{ "content": "...", "status": "..." }`）。
`db.query(File)` = `SELECT *`，每个文件的完整文本都被加载到内存。

被调用的地方：
- `get_file_metadatas_by_id` — 每次 add/remove/update 后返回响应，**只需要 id/hash/meta/timestamps，不需要 data**
- `reindex_knowledge_files` — 遍历所有 knowledge 的所有文件
- `export_knowledge_by_id` — 需要 content，合理
- RAG 全文模式 — 需要 content，合理

**影响**：500 个文件 × 100KB content = 每次操作后白白加载 50MB 到内存。

**修复**：`get_file_metadatas_by_id` 应直接查询所需列：
```python
def get_file_metadatas_by_id(self, knowledge_id, db):
    files = (
        db.query(File.id, File.hash, File.meta, File.created_at, File.updated_at)
        .join(KnowledgeFile, File.id == KnowledgeFile.file_id)
        .filter(KnowledgeFile.knowledge_id == knowledge_id)
        .all()
    )
    return [FileMetadataResponse(id=f.id, hash=f.hash, meta=f.meta,
            created_at=f.created_at, updated_at=f.updated_at) for f in files]
```

### 2. [中] `get_file_metadatas_by_id` 无分页返回全部

```python
def get_file_metadatas_by_id(self, knowledge_id, db):
    files = self.get_files_by_id(knowledge_id, db=db)  # ← 加载全部
    return [FileMetadataResponse(**file.model_dump()) for file in files]
```

在 7 处路由中被调用（add/remove/update/batch 等操作完成后返回响应）。
文件多时每次 API 响应都包含完整文件列表。

**影响**：前端每次操作后收到巨大 JSON 响应。

**修复**：操作类 API 返回操作结果即可，不需要返回完整文件列表。或改为分页。

### 3. [低] 全文模式 RAG 一次性加载所有文件内容

```python
# retrieval/utils.py
files = Knowledges.get_files_by_id(knowledge_base.id)
for file in files:
    documents.append(file.data.get('content', ''))
```

一次性把所有文件内容加载到内存拼接。

**影响**：当前使用 Native FC 向量检索模式不走这条路，但如果切换到全文模式会有问题。

### 4. [无问题] 表索引

`knowledge_file` 表索引完善：
- `ix_knowledge_file_knowledge_id` — 按 knowledge_id 查文件 ✓
- `ix_knowledge_file_file_id` — 按 file_id 查所属 knowledge ✓
- `ix_knowledge_file_user_id` — 按 user_id 查 ✓
- `uq_knowledge_file_knowledge_file` — 唯一约束防重复 ✓

JOIN 查询有索引覆盖，查询本身没有性能问题。

## 优先级

1. **`get_file_metadatas_by_id` 改为只查所需列** — 收益最大，改动最小
2. **操作类 API 不返回完整文件列表** — 减少响应体积
3. **全文模式流式加载** — 当前不影响，可后续优化

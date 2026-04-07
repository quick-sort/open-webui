# Knowledge 数据库表性能优化分析

场景：20 个 knowledge × 20,000 个文件 = 400,000 条 `knowledge_file` 关联记录 + 400,000 条 `file` 记录。

---

## 1. 缺少关键索引（最大问题）

当前 `knowledge_file` 表只有：
- 主键 `id`
- 唯一约束 `(knowledge_id, file_id)`

`file` 表完全没有 `__table_args__`，没有任何非主键索引。

**需要添加的索引：**

```python
# knowledge_file 表
class KnowledgeFile(Base):
    __table_args__ = (
        UniqueConstraint('knowledge_id', 'file_id', name='uq_knowledge_file_knowledge_file'),
        Index('ix_knowledge_file_knowledge_id', 'knowledge_id'),
        Index('ix_knowledge_file_file_id', 'file_id'),
        Index('ix_knowledge_file_user_id', 'user_id'),
    )

# file 表
class File(Base):
    __table_args__ = (
        Index('ix_file_user_id', 'user_id'),
        Index('ix_file_updated_at', 'updated_at'),
        Index('ix_file_created_at', 'created_at'),
        Index('ix_file_hash', 'hash'),
    )

# access_grant 表 — 也缺少查询用索引
class AccessGrant(Base):
    # 需要添加
    Index('ix_access_grant_resource', 'resource_type', 'resource_id'),
    Index('ix_access_grant_principal', 'principal_type', 'principal_id'),
```

虽然 `UniqueConstraint('knowledge_id', 'file_id')` 会隐式创建一个复合索引，但单独按 `file_id` 查询（如 `get_knowledges_by_file_id`）时无法利用该索引，因为 `file_id` 不是复合索引的前缀列。

## 2. `get_knowledge_bases` 全表加载问题

```python
def get_knowledge_bases(self, skip=0, limit=30, db=None):
    all_knowledge = db.query(Knowledge).order_by(Knowledge.updated_at.desc()).all()
```

这个方法接收 `skip` 和 `limit` 参数但完全没用，直接 `.all()` 加载全部 knowledge，然后在 Python 层遍历。当 knowledge 数量增长时这是 O(n) 的内存和时间开销。

## 3. `get_knowledge_bases_by_user_id` 的 N+1 问题

```python
def get_knowledge_bases_by_user_id(self, user_id, permission='write', db=None):
    knowledge_bases = self.get_knowledge_bases(db=db)  # 加载全部！
    return [kb for kb in knowledge_bases if ...]  # Python 层过滤
```

先加载所有 knowledge（含全部关联查询），再在 Python 中逐个检查权限。应该改为数据库层面过滤，类似 `search_knowledge_bases` 的做法。

## 4. `search_files_by_id` 中的 COUNT 性能

```python
total = query.count()  # 对 20,000 条记录做 COUNT
```

当单个 knowledge 有 20,000 个文件时，每次分页请求都要执行一次 `SELECT COUNT(*)` 全量扫描。优化方案：
- 在 `knowledge` 表增加 `file_count` 冗余字段，添加/删除文件时维护
- 或者使用 `COUNT` 估算（PostgreSQL 的 `reltuples`）

## 5. `get_files_by_id` 无分页

```python
def get_files_by_id(self, knowledge_id, db=None):
    files = db.query(File).join(KnowledgeFile, ...).filter(...).all()
```

20,000 个文件一次性全部加载到内存，没有分页。`get_file_metadatas_by_id` 也是同样的问题——先调用 `get_files_by_id` 加载全部文件（包括 `data` JSON 大字段），再转换为 metadata。

## 6. `file.data` JSON 大字段问题

`File` 表的 `data` 和 `meta` 都是 JSON 列。在列表查询中，即使不需要 `data` 的内容，SQLAlchemy 默认也会加载整个列。对于 RAG 场景，`data` 可能包含大量文本内容。

优化：查询列表时使用 `deferred` 加载或显式选择需要的列：

```python
from sqlalchemy.orm import defer
db.query(File).options(defer(File.data)).join(...)
```

## 7. `reset_knowledge_by_id` 批量删除

```python
db.query(KnowledgeFile).filter_by(knowledge_id=id).delete()
```

删除 20,000 条记录时，如果 `knowledge_id` 没有索引，这个操作会很慢。确保有独立索引。同时，级联删除对应的 `file` 记录也需要考虑事务大小。

## 8. `delete_all_knowledge` 逐个遍历

```python
knowledge_ids = [row[0] for row in db.query(Knowledge.id).all()]
for knowledge_id in knowledge_ids:
    AccessGrants.revoke_all_access('knowledge', knowledge_id, db=db)
```

逐个撤销权限，应该改为批量操作。

---

## 总结优先级

| 优先级 | 优化项 | 影响 |
|--------|--------|------|
| P0 | 给 `knowledge_file.file_id`、`file.user_id`、`file.updated_at` 加索引 | 所有 JOIN 和排序查询 |
| P0 | 给 `access_grant` 加 `(resource_type, resource_id)` 复合索引 | 权限查询 |
| P1 | 修复 `get_knowledge_bases` 使用分页而非全表加载 | 内存 + 响应时间 |
| P1 | 修复 `get_knowledge_bases_by_user_id` 改为 DB 层过滤 | 避免加载全部数据 |
| P1 | 列表查询时 defer `File.data` 大字段 | 减少内存和传输开销 |
| P2 | 增加 `knowledge.file_count` 冗余字段避免 COUNT | 分页查询性能 |
| P2 | `get_files_by_id` / `get_file_metadatas_by_id` 加分页 | 避免 OOM |

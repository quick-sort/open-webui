# 消除 Knowledge 文件在 Qdrant 中的重复存储

## 现状分析

### 实际用户操作流程

1. 用户先创建 Knowledge
2. 在 Knowledge 页面上传文件

### 前端流程 (`KnowledgeBase.svelte`)

```
uploadFileHandler
  → uploadFile(token, file, { knowledge_id: knowledge.id })   // 步骤1: 上传文件，metadata带了knowledge_id
  → addFileHandler(uploadedFile.id)                            // 步骤2: 将文件关联到knowledge
    → addFileToKnowledgeById(token, knowledgeId, fileId)       // 调用后端API
```

两步是**串行**的：先上传文件，再添加到 Knowledge。

### 后端流程

**步骤1: `POST /files/`** (`backend/open_webui/routers/files.py`)
- 接收到 `metadata = { knowledge_id: "xxx" }`，但只是存到 `file.meta.data` 中，未使用
- 调用 `process_uploaded_file` → `process_file(file_id=X, collection_name=None)`
- `collection_name=None` → 默认设为 `f'file-{file.id}'`
- 文件被 embedding 后存入 Qdrant 的 **`file-{file_id}` collection** ← 第一次存储

**步骤2: `POST /knowledge/{id}/file/add`** (`backend/open_webui/routers/knowledge.py`)
- `add_file_to_knowledge_by_id` 调用 `process_file(file_id=X, collection_name=knowledge_id)`
- `process_file` 进入 `elif form_data.collection_name` 分支：
  - 从 `file-{file.id}` collection 查询已有向量数据
  - **重新 embedding** 后存入 **`knowledge_id` collection** ← 第二次存储
- 然后在数据库中记录 knowledge ↔ file 关联关系

### 关键代码路径

```
# 步骤1 - 文件上传
files.py: upload_file
  → upload_file_handler (metadata含knowledge_id但未使用)
    → process_uploaded_file
      → process_file(file_id=X, collection_name=None)
        → collection_name = f'file-{file.id}'
        → save_docs_to_vector_db(collection_name='file-{id}', add=False)  ← 第一次

# 步骤2 - 添加到Knowledge
knowledge.py: add_file_to_knowledge_by_id
  → process_file(file_id=X, collection_name=knowledge_id)
    → 从 file-{id} collection 查询已有数据
    → save_docs_to_vector_db(collection_name=knowledge_id, add=True)  ← 第二次
```

### 资源浪费

- 每个文件的向量数据被存储两份（`file-{id}` + `knowledge_id`）
- 步骤2 重新计算 embedding（虽然数据一样）
- 文件数量多时，Qdrant 的内存和磁盘占用翻倍

### Knowledge 检索流程（Native Function Calling 模式）

实际使用的是 Native FC 方式，检索流程：

```
LLM 决定调用 query_knowledge_files(query="...", knowledge_ids=[...])
  → builtin.py: query_knowledge_files
    → 遍历 __model_knowledge__（或 knowledge_ids 参数）
    → type='collection' → collection_names.append(item_id)  # item_id = knowledge UUID
    → query_collection(collection_names=[knowledge_id], queries=[query], k=count)
      → 对 query 生成 embedding
      → 在 Qdrant 的 knowledge_id collection 中做 ANN 搜索
      → 返回 top-k 相关片段
    → 返回 JSON 给 LLM
```

关键代码 (`backend/open_webui/tools/builtin.py` ~line 2067):
```python
if item_type == 'collection':
    knowledge = Knowledges.get_knowledge_by_id(item_id)
    if knowledge and (权限检查...):
        collection_names.append(item_id)  # ← 直接用 knowledge_id 作为 collection name
```

在此模式下，`file-{id}` collection **完全没有被使用**。

### 默认 RAG 检索路径（备注）

`retrieval/utils.py` 中 `get_sources_from_items` 的 `type='collection'` 分支同理：
- 全文模式：从数据库读 `file.data.content`，不查 Qdrant
- 向量检索模式：`collection_names.append(item['id'])`，查 `knowledge_id` collection

---

## 修改方案

### 目标：查询速度优先，保持单 collection 检索

保留 `knowledge_id` collection（查询快，单 collection ANN 搜索），消除 `file-{id}` collection 的冗余创建。

### 核心思路

当文件是为 Knowledge 上传时，跳过 `file-{id}` collection 的创建，只在步骤2存入 `knowledge_id` collection。

### 具体修改

#### 更好的方案：修改步骤1直接存到 knowledge collection，步骤2只做数据库关联

**修改 `process_uploaded_file`** (`files.py`):
- 当 `file_metadata` 中有 `knowledge_id` 时，传 `collection_name=knowledge_id` 给 `process_file`
- 这样文件直接 embedding 到 `knowledge_id` collection，不创建 `file-{id}`

**修改 `add_file_to_knowledge_by_id`** (`knowledge.py`):
- 检查文件是否已经在 `knowledge_id` collection 中（通过 file_id filter 查询）
- 如果已存在，跳过 `process_file`，只做数据库关联
- 如果不存在（比如文件是先独立上传再添加到 knowledge 的），才调用 `process_file`

### 需要修改的文件和改动点

#### `backend/open_webui/routers/files.py`

`process_uploaded_file` 中 3 处 `process_file` 调用，增加 `collection_name` 参数：

```python
def _process_handler(db_session):
    # 从 file_metadata 获取 knowledge_id
    knowledge_id = file_metadata.get('knowledge_id') if file_metadata else None

    # ... 原有的 content_type 检测逻辑 ...

    # 所有 process_file 调用增加 collection_name:
    process_file(
        request,
        ProcessFileForm(
            file_id=file_item.id,
            content=...,  # 如果有的话
            collection_name=knowledge_id,  # 新增：有 knowledge_id 时直接存到 knowledge collection
        ),
        user=user,
        db=db_session,
    )
```

#### `backend/open_webui/routers/knowledge.py`

**`add_file_to_knowledge_by_id`**: 添加文件前检查是否已在 knowledge collection 中：

```python
def add_file_to_knowledge_by_id(...):
    # ... 权限检查 ...

    # 检查文件是否已经在 knowledge collection 中
    existing = VECTOR_DB_CLIENT.query(collection_name=id, filter={'file_id': form_data.file_id})
    if existing is None or len(existing.ids[0]) == 0:
        # 文件不在 knowledge collection 中，需要处理（兼容独立上传后添加的场景）
        process_file(
            request,
            ProcessFileForm(file_id=form_data.file_id, collection_name=id),
            user=user,
            db=db,
        )

    # 数据库关联
    Knowledges.add_file_to_knowledge_by_id(knowledge_id=id, file_id=form_data.file_id, user_id=user.id, db=db)
```

**`add_files_to_knowledge_batch`**: 同理，过滤掉已存在的文件。

#### `backend/open_webui/routers/retrieval.py`

`process_file` **不需要修改**。当前代码已有 fallback 逻辑：

```python
result = VECTOR_DB_CLIENT.query(collection_name=f'file-{file.id}', filter={'file_id': file.id})
if result is not None and len(result.ids[0]) > 0:
    docs = [...]  # 从 file-{id} collection 读
else:
    docs = [Document(page_content=file.data.get('content', ''), ...)]  # fallback 到数据库
```

当 `file-{id}` collection 不存在时，自动 fallback 到从数据库读内容。

### 检索侧：无需修改

- `builtin.py`: `collection_names.append(item_id)` — 不变
- `retrieval/utils.py`: `collection_names.append(item['id'])` — 不变
- 查询仍然是单个 `knowledge_id` collection，速度不受影响

### 总结

| 改动文件 | 改动内容 | 目的 |
|---------|---------|------|
| `files.py` | `process_uploaded_file` 传 `collection_name=knowledge_id` | 有 knowledge_id 时直接存到 knowledge collection |
| `knowledge.py` | `add_file_to_knowledge_by_id` 检查是否已存在再决定是否 process | 避免重复 embedding |
| `knowledge.py` | `add_files_to_knowledge_batch` 同上 | 批量添加同理 |
| `retrieval.py` | 不需要改 | fallback 逻辑已存在 |
| `builtin.py` | 不需要改 | 检索逻辑不变 |
| `retrieval/utils.py` | 不需要改 | 检索逻辑不变 |

### 兼容性

- 通过 Knowledge 页面上传：直接存 `knowledge_id` collection ✓
- 独立上传文件后添加到 Knowledge：先有 `file-{id}`，步骤2 检测不在 knowledge collection 中，正常复制 ✓
- 文件在聊天中独立引用：仍然查 `file-{id}` collection ✓（独立上传的文件不受影响）

### 迁移

已有数据无需迁移，`knowledge_id` collection 中的数据是完整的。可选择性清理冗余的 `file-{id}` collection（属于 knowledge 文件的那些）来释放空间。

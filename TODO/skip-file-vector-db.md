# 跳过 file-{file_id} 向量集合存储

## 背景

使用 Qdrant 作为向量数据库时，当文件上传到 Knowledge 后，会在 Qdrant 中产生两个 collection：
- `file-{file_id}`: 文件上传时自动创建
- `{knowledge_id}`: 添加到 Knowledge 时创建

文件内容在两个 collection 中重复存储，对于文件量大的用户造成存储空间浪费。

目标：添加环境变量 `SKIP_FILE_VECTOR_DB`，启用后文件上传时不再创建 `file-{file_id}` 向量集合。

---

## 问题分析

### 前端实际流程

用户操作：先创建 Knowledge → 再上传文件

前端代码 (`KnowledgeBase.svelte:302-332`):
1. 上传文件时传递 `metadata: { knowledge_id: knowledge.id }`
2. 调用 `POST /api/files/` 上传文件 → 后端存入 `file-{file_id}` collection
3. 文件上传完成后，**自动**调用 `POST /api/knowledge/{id}/file/add` → 后端从 `file-{file_id}` 复制到 `{knowledge_id}` collection

### 后端流程

1. **文件上传** (`POST /api/files/`):
   - 文件保存到存储系统
   - 调用 `process_file()` 不传 `collection_name`
   - Embedding 保存在 `file-{file_id}` collection

2. **添加到 Knowledge** (`POST /api/knowledge/{id}/file/add`):
   - 调用 `process_file(file_id, collection_name=knowledge_id)`
   - 从 `file-{file_id}` 读取 embedding → 写入 `{knowledge_id}` collection

### 检索流程（Native Function Call）

使用 native function call 时，检索流程：

1. **模型附加 Knowledge** (`middleware.py:2259-2298`):
   - `function_calling != 'native'` 时：将 knowledge 作为 files 注入 RAG
   - `function_calling == 'native'` 时：builtin tools 从 metadata 读取

2. **Native Function Call 检索** (`tools/builtin.py:1989-2166`):
   - `query_knowledge_files` 根据 `__model_knowledge__` 或 `knowledge_ids` 构建 collection_names
   - `type='collection'`（Knowledge Base）：使用 `knowledge_id` 作为 collection_name
   - `type='file'`（单独文件）：使用 `file-{file_id}` 作为 collection_name

### 使用 `file-{file_id}` collection 的所有场景

| 场景 | 代码位置 | 说明 |
|------|----------|------|
| 文件上传处理 | `retrieval.py:1551` | 默认存储到 `file-{file_id}` |
| 添加到 Knowledge | `retrieval.py:1582` | 从 `file-{file_id}` 读取内容 |
| 文件内容更新传播 | `files.py:554-570` | 从 `file-{file_id}` 重新添加到 Knowledge |
| 文件删除 | `files.py:779` | 删除 `file-{file_id}` collection |
| Native FC 检索单独文件 | `builtin.py:2072-2075` | 当 model 附加单独 file 时查询 |

### 现有 fallback 机制

`process_file()` 在 `form_data.collection_name` 存在时（知识库添加场景），会先尝试从 `file-{file_id}` 读取，如果不存在则 fallback 到 `file.data.get('content', '')`（retrieval.py:1582-1604）。

---

## 实现方案

### Step 1: 添加环境变量配置

**文件**: `backend/open_webui/config.py`（在 `BYPASS_EMBEDDING_AND_RETRIEVAL` 后，约2720行）

```python
SKIP_FILE_VECTOR_DB = PersistentConfig(
    'SKIP_FILE_VECTOR_DB',
    'rag.skip_file_vector_db',
    os.environ.get('SKIP_FILE_VECTOR_DB', 'False').lower() == 'true',
)
```

### Step 2: 修改 process_file() 跳过 file-{file_id} 存储

**文件**: `backend/open_webui/routers/retrieval.py`（约1701行）

在调用 `save_docs_to_vector_db` 前添加条件判断：

```python
should_skip = (
    request.app.state.config.SKIP_FILE_VECTOR_DB
    and collection_name == f'file-{file.id}'
)

if should_skip:
    # 只标记文件为完成状态，不存储到向量数据库
    with get_db() as session:
        Files.update_file_data_by_id(file.id, {'status': 'completed'}, db=session)
        Files.update_file_hash_by_id(file.id, hash, db=session)
    return {
        'status': True,
        'collection_name': None,
        'filename': file.filename,
        'content': text_content,
    }
else:
    # 原有 save_docs_to_vector_db 逻辑
    result = save_docs_to_vector_db(...)
```

### Step 3: 修改文件内容更新传播逻辑

**文件**: `backend/open_webui/routers/files.py`（约554-570行）

当前逻辑：更新 `file-{file_id}` 后，从该 collection 读取并更新所有 Knowledge collection。
修改：当 `SKIP_FILE_VECTOR_DB` 启用时，直接使用文件内容：

```python
file = Files.get_file_by_id(id, db=db)
content = file.data.get('content', '') if file else ''

for knowledge in knowledges:
    VECTOR_DB_CLIENT.delete(collection_name=knowledge.id, filter={'file_id': id})
    process_file(
        request,
        ProcessFileForm(file_id=id, collection_name=knowledge.id, content=content),
        ...
    )
```

### Step 4: 修改 Native Function Call 检索逻辑

**文件**: `backend/open_webui/tools/builtin.py`（约2071-2075行）

当 `SKIP_FILE_VECTOR_DB` 启用时，`file-{file_id}` 不存在，需要处理：

```python
elif item_type == 'file':
    file = Files.get_file_by_id(item_id)
    if file:
        from open_webui.retrieval.vector.factory import VECTOR_DB_CLIENT
        if VECTOR_DB_CLIENT.has_collection(f'file-{item_id}'):
            collection_names.append(f'file-{item_id}')
        # collection 不存在时跳过（SKIP_FILE_VECTOR_DB 模式下）
```

### Step 5（可选）: 暴露配置到 RAG API

**文件**: `backend/open_webui/routers/retrieval.py`（约445行）

```python
'SKIP_FILE_VECTOR_DB': request.app.state.config.SKIP_FILE_VECTOR_DB,
```

---

## 无需修改的代码

以下代码路径已有 fallback 机制，无需修改：

- `knowledge.py:662-667` — 添加到 Knowledge：`process_file()` 已有从 `file.data.get('content')` 的 fallback
- `knowledge.py:320-332` — Reindex：同样走 `process_file()` fallback
- `knowledge.py:831-839` — 删除文件：已检查 `has_collection` 后再删除
- `files.py:777-779` — 文件删除：已 try/except 包裹 `delete_collection`

---

## 验证方案

1. **默认行为不变**: `SKIP_FILE_VECTOR_DB=false`（默认），行为与现在一致
2. **启用跳过**:
   - 上传文件后 Qdrant 中不存在 `file-{file_id}` collection
   - 添加文件到 Knowledge 后 `{knowledge_id}` collection 中有正确数据
3. **Native FC 检索**: Model 附加 Knowledge 后可正常检索（使用 knowledge_id collection）
4. **内容更新**: 修改文件内容后，Knowledge 中的 embeddings 正确更新
5. **文件删除**: 删除文件时正确清理 Knowledge 中的 embeddings
6. **多 Knowledge**: 文件可添加到多个 Knowledge，不受影响

---

## 关键文件

| 文件 | 修改内容 |
|------|----------|
| `backend/open_webui/config.py` | 添加 `SKIP_FILE_VECTOR_DB` 配置 |
| `backend/open_webui/routers/retrieval.py` | 跳过 `file-{file_id}` 存储逻辑 |
| `backend/open_webui/routers/files.py` | 内容更新时直接使用文件内容传播 |
| `backend/open_webui/tools/builtin.py` | Native FC 检索时检查 collection 是否存在 |

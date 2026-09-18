# 邮件归档整理服务（FastAPI）

批量提交邮件头信息（`message_id`、`in_reply_to`、`references`、发送时间、主题），
服务将其归并为**稳定排序的会话树**，并给出**重复 ID、缺失引用、引用环**三类诊断。
批次请求与归并结果均持久化到 SQLite，可随时查询。

## 一条命令启动

需要 Docker 20+ 与 Docker Compose v2：

```bash
docker compose up --build
```

启动后服务监听 **8000** 端口：

| 地址 | 说明 |
| --- | --- |
| <http://localhost:8000> | 服务根 |
| <http://localhost:8000/docs> | Swagger UI（可直接发请求调试） |
| <http://localhost:8000/health> | 健康检查，返回 `{"status":"ok"}` |
| <http://localhost:8000/openapi.json> | OpenAPI 描述 |

首次启动会自动在卷内安装环境就绪后的建表全部完成（`app.main` lifespan 中执行幂等 DDL）；
数据库存放在命名卷 `mailarchive-data` 中，容器重建数据不丢。

## 必要配置

均通过环境变量注入，均有默认值：

| 变量 | 默认值（本地）/ Compose 值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | `data/mailarchive.db` / `/data/mailarchive.db` | SQLite 数据库文件路径，父目录不存在会自动创建，表自动建立 |

端口通过 Compose 的 `8000:8000` 映射发布；如需换宿主机端口，例如改成本地 9000：

```yaml
ports:
  - "9000:8000"
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/batches` | 提交一批邮件，返回会话树与全部诊断（HTTP 201） |
| `GET` | `/batches?limit=50&offset=0` | 批次列表，按提交时间倒序 |
| `GET` | `/batches/{batch_id}` | 取该批次的归档结果快照（与提交响应完全一致） |
| `GET` | `/messages/{message_id}` | 查询单封邮件及其最终父节点 |
| `GET` | `/threads/{message_id}` | 查询该邮件所属完整会话树；环邮件返回 **409** |
| `GET` | `/health` | 健康检查 |

未知 ID：批次 / 邮件查询返回 **404**；请求体不合法返回 **422**。

### 提交示例

```bash
curl -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -d '{
    "messages": [
      {"message_id": "A", "in_reply_to": null, "references": [],
       "sent_at": "2026-09-18T08:00:00Z", "subject": "例会通知"},
      {"message_id": "B", "in_reply_to": "A", "references": ["A"],
       "sent_at": "2026-09-18T09:00:00Z", "subject": "Re: 例会通知"},
      {"message_id": "C", "in_reply_to": "B", "references": ["A", "B"],
       "sent_at": "2026-09-18T10:00:00Z", "subject": "Re: 例会通知"},
      {"message_id": "D", "in_reply_to": "lost-123", "references": ["ghost-9"],
       "sent_at": "2026-09-18T11:00:00Z", "subject": "断链邮件"},
      {"message_id": "X", "in_reply_to": "Y", "references": ["Y"],
       "sent_at": "2026-09-18T12:00:00Z", "subject": "环一"},
      {"message_id": "Y", "in_reply_to": "X", "references": ["X"],
       "sent_at": "2026-09-18T13:00:00Z", "subject": "环二"}
    ]
  }'
```

### 响应结构

```jsonc
{
  "batch_id": "…",                 // 批次主键，查询接口据此取快照
  "submitted_at": "…UTC ISO8601",
  "totals": {                      // 计数：提交数 / 新唯一数 / 历史重复 / 环内无效 / 树数 …
    "submitted": 6, "unique_new": 6, "archived_duplicates": 0,
    "invalid": 2, "missing_reference_messages": 2, "cycles": 1, "trees": 2
  },
  "threads": [                     // 稳定排序的会话树（见下）
    {
      "message_id": "A",
      "parent_id": null,          // 根节点
      "in_reply_to": null,
      "references": [],
      "sent_at": "2026-09-18T08:00:00+00:00",
      "subject": "例会通知",
      "children": [ { "message_id": "B", "parent_id": "A", "children": [ … ] } ]
    }
  ],
  "duplicates": [                  // 重复 message_id（本批重复 + 历史归档重复）
    {"message_id": "Q", "archived": true,  "first_submitted_seq": null, "occurrences": [2]}
  ],
  "missing_references": [          // 每封邮件引用中、已知集合里不存在的去重 ID
    {"message_id": "D", "missing": ["ghost-9", "lost-123"]}
  ],
  "cycles": [                      // 引用环：整环成员（环序，环内最小 ID 起头）
    {"index": 1, "members": ["X", "Y"], "invalid_messages": ["X", "Y"]}
  ],
  "invalid_messages": [            // 被判 invalid 的邮件明细及原因
    {"message_id": "X", "reason": "reference_cycle", "cycle_index": 1, "…": "…"}
  ]
}
```

## 归并规则

- **父节点确定**
  1. 优先采用有效的 `in_reply_to`：指向已知邮件（本批或历史已归档）即采用；
  2. 否则从右向左扫描 `references`，取其中**最后一个已知项**；
  3. 都命不中（含 `in_reply_to`/`references` 指向不存在的 ID）即为断链邮件，**自成根节点**。
- **引用环**：候选父子关系构成功能图（每封新邮件至多一个候选父）。
  检测到环时，**环上所有邮件整体标记为 `invalid`，绝不部分挂接**（不会下沉成根、
  不会挂到其它树）；随后引用了该环的邮件会回到 `references` 中重新寻找其它有效父节点，
  重复检测直至无环。自引用（`in_reply_to` 指向自身）视为单节点环。
- **重复 ID**：同批次重复出现时仅采用首次出现的内容参与归并；若 ID 已在历史批次归档，
  本次不重复落库。两种情况都会在 `duplicates` 中标注。
- **稳定排序**：根之间、同一父节点的子节点之间统一按
  `(sent_at UTC, message_id)` 升序，先序输出，结果全序确定、可复现。
- **跨批次**：历史邮件是合法的父节点；历史父指针不可变，因此引用环只可能完全位于
  同一批新邮件内部。
- **时间**：`sent_at` 无时区时按 UTC 处理，有偏移时换算为 UTC 存储与排序。

## 持久化

SQLite（WAL 模式），表在服务启动时自动创建：

- `batches`：批次元数据与计数；
- `messages`：每封唯一邮件、最终 `parent_id`、是否 `is_invalid`；
- `batch_duplicates` / `batch_missing_refs` / `batch_cycles`：诊断明细；
- `batch_results`：完整结果 JSON 快照，`GET /batches/{id}` 原样返回。

写操作在进程内串行化（线程锁 + 单连接写事务），保证并发提交下的归并一致性。

## 本地开发（不使用 Docker）

需要 Python 3.11+：

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
# 地址：http://localhost:8000  （DATABASE_PATH 默认 data/mailarchive.db）
```

运行测试：

```bash
pip install pytest
python -m pytest -q
```

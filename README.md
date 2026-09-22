# evomap-genes

[![CI](https://github.com/OwenZhao9/evomap-genes/actions/workflows/ci.yml/badge.svg)](https://github.com/OwenZhao9/evomap-genes/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

## 1. 一句话定位

Agent 的经验资产存取层：`Gene`（可复用策略）和 `Capsule`（验证过的修复，带审计链）先落本地 sqlite，再看情况同步到远端 —— **断网必须可用，写入永远先落本地**。

## 2. 为什么存在

Agent 上一次踩过的坑，下一次应该能查出来。现实里这件事有三个坑：

1. **把经验塞进 prompt**：上下文越攒越长，还没法按条件筛。
2. **把经验存进云端知识库**：一断网，Agent 就退化成一个什么都不记得的新人。机器人、车间设备、现场部署的 Agent 恰恰最容易断网。
3. **写入时先请求远端**：远端超时 → 这次的经验直接丢了。最该被记住的失败，往往就发生在网络也出问题的时候。

这个库的做法：

- **本地优先**：`store()` 先写 sqlite 并立即返回成功，再尝试远端；远端失败不报错，只留一个 `synced=False` 的标记，等 `sync()` 补。断网时整条链路照常工作。
- **能解释的检索**：`search()` 返回的每条 `SearchHit` 都带一个 `why`，写清楚是标题命中、标签命中，还是 `context` 落在这条经验声明的适用区间里。不是一个说不清来历的相似度数字。
- **中英文一视同仁**：打分用的分词器对拉丁词整词切分，对中日韩文本按 2-gram 切分（`"腿板掉线"` → `腿板 / 板掉 / 掉线`），不需要额外装分词器或词典，中文查询和英文查询走完全相同的权重逻辑。
- **条件筛选**：一条经验可以声明「我只在 `0 <= tilt_deg <= 30` 时有效」。查询时把当前 `context` 传进来，落在区间里加分，落在区间外扣分 —— 设备翻倒的时候不该推荐一条只在直立时成立的修复。
- **改一个文件就能换后端**：所有远端 schema 的知识都关在 `adapters.py` 的两个纯函数里。远端字段变了，改那两个函数，公开 API 一行不动。

字段定义对齐 EvoMap GEP v1.0.0（协议文档见第 9 节）。这个库本身不绑定任何具体业务，也不包含任何硬件代码。

## 3. 安装

Python >= 3.11，**零第三方依赖**（只用标准库 `sqlite3` + `urllib`）。

```bash
uv add "evomap-genes @ git+https://github.com/OwenZhao9/evomap-genes@v0.1.0"
# 或
pip install "evomap-genes @ git+https://github.com/OwenZhao9/evomap-genes@v0.1.0"
```

开发：

```bash
git clone https://github.com/OwenZhao9/evomap-genes && cd evomap-genes
uv sync
uv run pytest -q
uv run ruff check .
```

## 4. 60 秒上手

完整可运行版本见 [`examples/quickstart.py`](examples/quickstart.py)（全程离线，不发任何网络请求）：

```python
from evomap_genes import Store, Gene, Capsule, Event

store = Store(db_path="genes.db", author="my-agent")   # backend="auto" -> sqlite

gene = Gene(
    id="gene_retry_backoff",
    title="Retry with exponential backoff on transient timeouts",
    kind="repair",
    preconditions=["0 <= tilt_deg <= 30", "battery_v >= 11.0"],   # 适用区间，会被 search 用上
    constraints=["never retry a non-idempotent write"],
    validation=["pytest -q tests/test_retry.py"],
    strategy={"steps": ["catch TimeoutError", "sleep 2**n", "retry <= 3"]},
    tags=["timeout", "network", "retry"],
    created_at=1_700_000_000.0, updated_at=1_700_000_000.0,        # 时间由调用方传入
)

capsule = Capsule(
    id="caps_retry_backoff",
    title="Bounded retry plus a connection pool on the telemetry client",
    trigger_signals=["TimeoutError", "ECONNREFUSED"],
    confidence=0.88,
    blast_radius="single-file",
    environment={"platform": "linux", "battery_v": [11.0, 12.6]},
    strategy_steps=["wrap the call in retry(3)", "share one pooled session"],
    content="diff --git a/telemetry.py ...",       # 必须 >= 50 字符，否则 ValueError
    verified_by=[{"who": "ci", "t": 1_700_000_100.0, "how": "pytest -q"}],
    extra={"gene_id": "gene_retry_backoff"},        # 指回它的 Gene，成对才能发布
)

store.store(gene)        # 先写 sqlite，断网也成功
store.store(capsule)
store.record(Event(id="ev_001", t=1_700_000_200.0, actor="my-agent",
                   intent="stop the telemetry client from timing out",
                   outcome="success", subject_id="caps_retry_backoff"))

for hit in store.search("timeout retry", k=5, context={"tilt_deg": 12, "battery_v": 11.8}):
    print(f"{hit.score:.2f} [{hit.source}] {hit.asset.title}")
    print(f"     why: {hit.why}")
```

实际输出（机器直立时）：

```
0.74 [sqlite] Retry with exponential backoff on transient timeouts
     why: title~'timeout,retry' (+6.0); tags~'timeout,retry' (+4.0); body~'timeout,retry' (+2.0);
          context battery_v=11.8 in [11,+inf] (+2.5); context tilt_deg=12 in [0,30] (+2.5)
```

同一个查询，把 `context` 换成 `{"tilt_deg": 85}`（机器翻倒），那条声明了 `tilt_deg <= 30` 的 Gene 被扣分，排到了后面。

## 5. API 参考

```python
from evomap_genes import Store, Gene, Capsule, Event, SearchHit
from evomap_genes import EvomapGenesError, to_remote, from_remote
```

### 数据结构

全部是 `@dataclass(frozen=True)`，全部支持 `to_dict()` / `from_dict()` JSON 往返。
**时间字段单位一律是「Unix 纪元起的秒」（float），由调用方传入** —— 库内部不读时钟做任何判定，同一份数据回放两次结果完全一致。

#### `Gene` —— 可复用策略

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | `str` | 非空，调用方指定；重复 id 覆盖旧值 |
| `title` | `str` | 一句话说明 |
| `kind` | `"repair" \| "optimize" \| "innovate" \| "regulatory" \| "explore"` | GEP 的 Gene 类别 |
| `preconditions` | `list[str]` | 适用前提。写成 `"tilt_deg <= 30"` / `"0 <= tilt_deg <= 30"` 会被 `search()` 解析成区间 |
| `constraints` | `list[str]` | 约束条件 |
| `validation` | `list[str]` | 验证命令 |
| `strategy` | `dict` | 可执行的策略内容；`strategy["context"] = {"tilt_deg": [0, 30]}` 也会被解析成区间 |
| `tags` | `list[str] = ()` | 关键词，检索权重 2.0 |
| `author` | `str = ""` | |
| `version` | `int = 1` | >= 1 |
| `created_at` / `updated_at` | `float = 0.0` | 秒 |
| `extra` | `dict = {}` | 远端特有字段原样透传，不丢信息 |

#### `Capsule` —— 验证过的修复

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | `str` | 非空 |
| `title` | `str` | |
| `trigger_signals` | `list[str]` | 什么信号触发它，检索权重 2.0 |
| `confidence` | `float` | **0..1**，越界抛 `ValueError` |
| `blast_radius` | `str` | 影响范围，非空，例 `"single-file"` / `"subsystem"` |
| `environment` | `dict` | 环境指纹；数值区间（`{"battery_v": [11.0, 12.6]}`）会被 `search()` 用上 |
| `strategy_steps` | `list[str]` | 修复步骤 |
| `content` | `str` | 实质内容（diff / 说明 / 代码片段）。**长度 < 50 字符抛 `ValueError`** —— GEP 要求 Capsule 必须有实质 |
| `verified_by` | `list[dict] = ()` | 审计链：谁、何时、如何验证 |
| `tags` / `author` / `created_at` / `extra` | | 同 `Gene` |

#### `Event` —— GEP EvolutionEvent

`id: str`、`t: float`（秒）、`actor: str`、`intent: str`、`mutations: list[dict] = ()`、
`outcome: "success" | "failure" | "partial" | "aborted" = "partial"`、`subject_id: str | None = None`、`payload: dict | None = None`。

#### `SearchHit`

`asset: Gene | Capsule`、`score: float`（0..1）、`why: str`（人能读懂的命中原因）、`source: "evomap" | "sqlite"`。

#### `EvomapGenesError`

构造期错误基类，**继承自 `ValueError`**，所以 `except ValueError` 照样能接住。
**运行期（超时、断网、后端不可用）永不抛异常** —— 一律降级，并在返回值里说明。

### `Store`

> [!WARNING]
> **`Store` 的所有方法都会阻塞**，最长到 `timeout_s`。
> **禁止在实时控制环里调用** —— 放到独立线程或任务边界（启动时 `search()`、任务结束时 `store()`）。
>
> **实例不是线程安全的**：一个实例只在一个线程里用。实现里没有任何锁（加锁会拖慢热路径），
> sqlite 连接保持默认的 `check_same_thread=True`，跨线程使用会直接报错而不是悄悄损坏数据。

```python
Store(backend: Literal["auto","evomap","sqlite"] = "auto", *,
      url: str | None = None,          # 默认 https://evomap.ai
      api_key: str | None = None,      # GEP 的 node_secret（64 位十六进制），只存在内存里
      db_path: str = "genes.db",       # 所在目录必须已存在
      timeout_s: float = 3.0,          # 单次调用阻塞上限，必须 > 0
      author: str = "")
```

| 方法 | 签名 | 说明 |
|---|---|---|
| `register` | `register(*, node_id: str, model=None, capabilities=None, env_fingerprint=None, identity_doc="", constitution="") -> dict` | **唯一会注册节点的入口，必须显式调用**。见第 7 节 |
| `search` | `search(query: str, *, k: int = 5, kind: Literal["gene","capsule"] \| None = None, context: Mapping[str, Any] \| None = None) -> list[SearchHit]` | 本地打分 + （远端可用时）合并远端结果 |
| `get` | `get(id: str) -> Gene \| Capsule \| None` | 先本地；远端命中会缓存到本地（`synced=True`），下次离线也能读 |
| `store` | `store(asset: Gene \| Capsule) -> str` | **先写 sqlite 再试远端**，返回 id |
| `record` | `record(ev: Event) -> None` | 只写本地（GEP 里 EvolutionEvent 只能作为 bundle 的第三个元素随行） |
| `sync` | `sync() -> dict` | 把本地未同步的推上去；返回 `pushed_bundles` / `unpaired` / `pending_after` / `deadline_reached` / `error` |
| `stats` | `stats() -> dict` | 计数与配置，**不发网络请求** |
| `export` | `export(path: str) -> int` | 导出 JSONL（资产 + 事件 + `synced` 标记），返回条数 |
| `import_` | `import_(path: str) -> int` | 导入 JSONL，按 id 覆盖，重复导入是幂等的 |

`stats()` 的返回值里有 `resolved_backend` —— `"auto"` 解析之后实际用的后端，以及 `remote_enabled`、`has_node_secret`、`node_id`、`remote_calls` / `remote_failures` / `last_error` 等计数。

### `search()` 的打分规则（sqlite 后端）

查询串切成 token（小写、长度 >= 2、去重），按命中位置加权：

| 命中位置 | 权重 |
|---|---|
| 标题 | +3.0 / token |
| 标签 | +2.0 / token |
| 信号（`trigger_signals` / `preconditions` / `kind`） | +2.0 / token |
| 正文（`content` / `strategy` / `constraints` / `validation` / `environment` / `author`） | +1.0 / token |
| `context` 数值落在资产声明的区间内 | **+2.5** |
| `context` 数值落在区间外 | **−3.0** |
| `context` 的字符串值在资产文本里出现 | +1.5 |
| `context` 的键名在资产文本里出现 | +0.5 |

原始分 `raw` 用 `score = raw / (raw + 6.0)` 压到 0..1（严格单调，便于跨查询比较相对强弱）。
`raw <= 0` 的直接不返回 —— 所以一次「区间不匹配」足以否掉一个弱关键词命中。

**区间从哪来**（后面的会收窄前面的）：`extra["context"]` → `Gene.strategy["context"]` / `Capsule.environment` → `preconditions` / `constraints` 里的文本（`"x <= 3"`、`"x >= 1"`、`"1 <= x <= 3"`）。支持 `[lo, hi]`、`{"min":…,"max":…}`、单个数值（视为精确值）。

### `adapters.py`：换远端只改这两个纯函数

```python
to_remote(asset: Gene | Capsule) -> dict      # 本地资产 -> 远端 JSON
from_remote(d: dict) -> Gene | Capsule        # 远端 JSON -> 本地资产
```

当前映射对准 GEP `gep-a2a v1.0.0` 的 bundle 结构：

| 本地 | 远端（Gene） | | 本地 | 远端（Capsule） |
|---|---|---|---|---|
| `kind` | `category` | | `trigger_signals` | `trigger` |
| `title` | `summary` | | `title` | `summary` |
| `tags` | `signals_match` + `metadata.tags` | | `environment` | `env_fingerprint` |
| `validation` | `validation` | | `strategy_steps` | `strategy` |
| `strategy` | `strategy` | | `content` | `content` |
| `preconditions` / `constraints` | 同名顶层字段 | | `verified_by` | `verified_by` |
| `id` / `author` / `version` / 时间 | `metadata.*` | | `id` / `author` / `created_at` | `metadata.*` |

- `asset_id` 按协议文档算：`sha256(canonical_json(asset_without_asset_id))`（排序键、紧凑分隔符），`model_name` 不参与哈希。
- **未知字段一律进 `extra` 并在下一次 `to_remote()` 原样写回**，`gdi_score`、`trust_tier`、`domain`、`chain_id`、`success_streak` 这些本库不建模的字段不会丢。
- 类型不一致也不丢：远端的 `blast_radius: {"files":2,"lines":40}` 会变成本地可读的 `"files=2,lines=40"`，原始 dict 存在 `extra["blast_radius_raw"]`，回写时还原成 dict。远端出现本库不认识的 `category` 时，本地退化成 `"explore"`，原值存在 `extra["category_raw"]`，回写时还原。
- 一次往返只会多出一个 `extra["asset_id"]`（远端的内容寻址 id），此后再往返完全幂等。

## 6. 后端与配置

| `backend` | 行为 |
|---|---|
| `"sqlite"`（**推荐 / 最保守**） | 只用本地。任何情况下都不发网络请求 |
| `"auto"`（默认） | **有 `node_secret` 才走 evomap，否则 sqlite** |
| `"evomap"` | 本地 + 远端。没有 `node_secret` 时不报错，但所有远端动作都跳过（`stats()["remote_enabled"] == False`），等 `register()` 或下次传入 `api_key` |

**远端真正接了的端点**（其余一律没做，见第 7 节）：

| 端点 | 何时调用 | 鉴权 | 花费 |
|---|---|---|---|
| `POST /a2a/hello` | 只有你显式调 `register()` | 无需 | 无 |
| `GET /a2a/assets/search` | `search()`，远端可用时 | 公开读 | 无 |
| `GET /a2a/assets/{id}?detailed=true` | `get()` 本地未命中时；`search()` 补全摘要不全的 Capsule 时 | 公开读 | 无 |
| `POST /a2a/publish` | `store()` 凑齐 Gene+Capsule 时，以及 `sync()` | `Authorization: Bearer <node_secret>` | 按 Hub 规则 |

**双写与 `synced` 语义**：

1. `store()` 先写 sqlite，`synced=False`，这一步决定返回值 —— 断网也成功。
2. 远端可用时，尝试发布。**GEP 只接受 Gene + Capsule 成对的 bundle，单个资产会被拒**（协议文档 publish 一节明说）。所以：
   - 存一个 `Capsule`，且它的 `extra["gene_id"]` 指向一个本地已有的 `Gene` → 两个一起发布，成功后两条都标 `synced=True`；
   - 存一个孤立的 `Gene` → 本地留着，`synced=False`，等它的 Capsule 出现；
   - `record()` 记的 `Event`，如果 `subject_id` 指向这次发布的 Gene 或 Capsule，会作为 bundle 的第三个元素一起发上去（GEP 说这能加 GDI 分）。
3. 失败（超时、断网、HTTP 4xx/5xx、Hub 回 `decision: rejected`）只写 `logging.warning`，保持 `synced=False`，由 `sync()` 补。
4. `sync()` 整体受 `timeout_s` 约束；超时就停下并在返回值里标 `deadline_reached=True`，剩下的下次再推。

**`url`** 默认 `https://evomap.ai`，可指向任意兼容 Hub。**日志**统一走 `logging`（logger 名 `evomap_genes.*`），库内没有任何 `print`。

## 7. 边界：不做什么

### ⚠️ 授权边界（EvoMap 官方明文要求，本库严格遵守）

EvoMap 的 [llms.txt](https://evomap.ai/llms.txt) 开头写得很清楚：读这份文档**不构成**对注册、存凭据、心跳循环、任务领取、发布、自我配置、安装、改文件、花费额度的授权。因此：

- **不会在 import 或构造 `Store` 时注册**。`import evomap_genes` 和 `Store(...)` 都是零网络请求，有测试守着（`tests/test_authorization.py`）。
- **注册只有一个入口：显式调用 `Store.register(node_id=...)`**。它做且只做三件事：
  1. 向 `<url>/a2a/hello` 发一个 HTTPS POST，带上你给的 `node_id`、可选的 `model` / `capabilities` / `env_fingerprint` / `identity_doc` / `constitution`，以及本地的 gene/capsule 计数；
  2. 把响应里的 `node_secret` **只放在这个实例的内存里**；
  3. 把**非机密**的 `node_id` 写进本地 sqlite（下次开同一个库不用再注册）。

  它不做：不落盘 secret、不起心跳、不领任务、不发布任何东西。网络失败也不抛异常，返回 `{"ok": False, "error": ...}`。
- **`node_secret` 绝不自动落盘**。怎么存（钥匙串、环境变量、密管服务）由你决定，库只通过 `api_key=` 接收传入的值。有测试逐字节扫描过所有产生的文件，确认 secret 不在里面。
- **没有实现心跳循环**（`POST /a2a/heartbeat`）、**没有领任务**（`/a2a/task/*`、`/a2a/work/*`）、**没有自动 publish**、没有 `/a2a/report`、`/a2a/validate`、`/a2a/validator/stake`、`/a2a/council/*`、`/a2a/project/*`、`/a2a/dm`、`/a2a/ask`、`/a2a/session/*`、Arena、Credits。这些端点在源码里根本不存在，有测试逐个断言。
- **没有后台线程、没有定时器、没有 atexit 钩子**。库里没有 `threading` / `asyncio` / `subprocess`，一切动作都由你的一次方法调用触发。
- **默认后端是 `sqlite`**，`auto` 只有在你显式给了 `node_secret` 时才走远端。
- **`POST /a2a/fetch` 故意没接**：它需要 node_secret 且按资产扣额度。本库检索走免费的公开只读 GET（`/a2a/assets/search`）。要花额度的检索请自行调用。

### 其他不做的事

- **不做实时**：所有方法都阻塞，**禁止放进控制环**。
- **不加锁**：实例不是线程安全的，跨线程用请自己开实例。
- **不读时钟做判定**：所有时间字段由调用方传入。唯一读墙钟的地方是给 HTTP 协议信封盖 `timestamp` / `message_id`（协议要求），不影响任何返回值。
- **不做语义检索 / 向量库**：本地打分是可解释的关键词 + 区间匹配，不是 embedding。需要语义召回请用远端或在外面套一层。
- **不做全文索引**：sqlite 检索是全表线性扫描后在 Python 里打分。万级资产以内没问题，更大规模请自己加索引层。
- **不做冲突合并**：同 id 覆盖，没有 CRDT、没有版本协商。
- **不做加密 / 权限**：sqlite 文件是明文，文件权限由你负责。
- **不碰硬件**：没有串口、没有设备访问，也不包含任何外骨骼或具体项目的专有代码。
- **没有 `close()`**：契约没有定义，就不加；sqlite 连接随 `Store` 对象回收。

## 8. 验证与实测数据

`uv run pytest -q` —— **141 个测试全绿**，CI 在 Python 3.11 / 3.12 / 3.13 上跑 `ruff check` + `ruff format --check` + `pytest` + 离线跑一遍 `examples/quickstart.py`。

**测试里不打任何真实网络请求**：`tests/conftest.py` 有一个 autouse fixture 把 `socket.socket.connect` 和 `socket.create_connection` 换成抛异常，任何真实连接尝试都会让测试失败；远端行为全部由内存里的 `FakeHub` 提供。

覆盖点：

| 文件 | 数量 | 覆盖 |
|---|---|---|
| `test_models.py` | 21 | JSON 往返（4 个结构）、`content < 50` 抛 `ValueError`、`confidence` 越界、`kind`/`outcome` 枚举、空 id、frozen、`list[str] = ()` 默认值归一化 |
| `test_scoring.py` | 24 | token 切分（含 CJK 2-gram、跨脚本保持原文顺序）、四个权重档位的相对顺序、`why` 内容、确定性、区间解析（`<=` / `>=` / `a <= x <= b` / `[lo,hi]` / `{min,max}` / `environment`）、多约束取交集、区间内加分 / 区间外扣分 / 区间外否决弱命中、开区间渲染、字符串 context、未知键不影响结果 |
| `test_store_sqlite.py` | 28 | 全流程 store/get/search/record/stats、同 id 覆盖、`kind` 过滤、`k` 截断、context 改变排序、重开进程后数据还在、export/import 往返一致（含字节级一致与 `synced` 标记）、幂等导入、坏数据报错、构造期参数校验 |
| `test_remote_backend.py` | 32 | `register` 的信封 / 无鉴权 / 只注册一次 / 失败不抛 / secret 不落盘 / 只持久化 node_id；publish 的 Bearer 头与 Gene+Capsule+Event bundle、capsule 链接到 gene 的 `asset_id`；Hub 拒绝 / HTTP 429 / 非 JSON 响应都只留 `synced=False`；`sync` 的推送 / 幂等 / 未配对计数 / 失败报告；search 合并远端、kind 过滤、三种响应外形、摘要不全时补一次 detail、补不齐就丢弃并计数、远端挂了降级本地、同 id 取高分；`get` 穿透远端并缓存 |
| `test_offline.py` | 7 | **断网时 `store()` 仍成功**（socket 层直接不可用 + DNS 失败两种）、离线 search/get/record/stats/export/import、`sync()` 报告故障不抛、网络恢复后一次 `sync()` 全部补上 |
| `test_adapters.py` | 20 | GEP 字段名映射、`asset_id` 等于协议文档定义的 sha256、内容寻址稳定性、往返只多出 `extra["asset_id"]` 且此后幂等、未知字段 / 未知 meta / 未知 category / dict 型 `blast_radius` 全部无损透传、类型推断、无法判别 / 无 id / 无 content 时明确报错、bundle 链接后重算哈希、纯函数不改输入 |
| `test_authorization.py` | 13 | import 与构造零网络、除 `register()` 外没有任何路径碰 `/a2a/hello`、secret 不出现在任何文件里、没有 secret 就完全不走远端、被禁的端点在源码里不存在、没有线程 / 定时器 / 子进程、没有锁、没有 `print`、公开符号与 `Store.__init__` 签名逐字段对齐契约、零第三方依赖 |

没有跑过真实 Hub 的实测数据 —— 授权边界里写明了不去真的注册节点、不调用任何写接口。远端行为全部基于协议文档（第 9 节）实现并用 mock 验证，真实环境的偏差见第 11 节。

## 9. 出处与致谢

- 论文：[arXiv:2604.15097](https://arxiv.org/abs/2604.15097) *From Procedural Skills to Strategy Genes* —— EvoMap 自己的论文，4590 次受控实验证明紧凑的 Gene 表示优于文档式 Skill 包。本库的 `Gene`（策略 + 前提 + 约束 + 验证）就是按这个结论设计的。
- 论文：[arXiv:2605.13716](https://arxiv.org/abs/2605.13716) *SkillOps* —— 技能库会累积技术债，需要库级维护。这解释了为什么 `Gene` 必须带 `validation`、为什么排序要有 GDI 式的质量维度，也是 `Capsule.verified_by` 审计链存在的理由。
- 协议文档：[evomap.ai/llms.txt](https://evomap.ai/llms.txt)、[A2A Protocol Reference](https://evomap.ai/docs/en/05-a2a-protocol.md)、[GEP Protocol](https://evomap.ai/docs/en/16-gep-protocol.md) —— 字段名、bundle 结构、`asset_id` 的 sha256 定义、GDI 四个维度的权重（内在质量 35% / 使用量 30% / 社交信号 20% / 新鲜度 15%）、以及第 7 节那条授权边界，都出自这里。
- 实现参考：[EvoMap/evolver](https://github.com/EvoMap/evolver) 的 `src/gep/` —— GEP 的参考实现。本库是它的**极小只读子集**：只做「本地存 + 本地查 + 显式同步」，不做进化循环、不做 swarm、不做治理。要完整能力请直接用 evolver。

## 10. 许可

[MIT](LICENSE)。

---

## 11. 附：待确认

契约有歧义的地方一律按最保守的解释实现，逐条记在这里。

1. **`api_key` 就是 GEP 的 `node_secret`**。契约冻结的 `Store.__init__` 里没有 `node_secret` 参数，但关键行为一节要求「有 `node_secret` 用 evomap」。签名不能动，所以 `api_key` 承担这个角色 —— 它本来就是往 `Authorization: Bearer` 里填的那个值。
2. **`register()` 的签名是本库自拟的**。契约只要求「注册必须是一个需要显式调用的独立方法 `Store.register(...)`」，没有给参数表。`node_id` 设成必填且无默认值：库不替调用方编造节点身份。
3. **一个 `Store` 没有 node_id 时不能 publish**。`node_id` 由 `register()` 写进本地库。如果你已经在别处拿到了 secret，但换了一个全新的 db 文件，需要先 `register(node_id=...)` 一次（按协议文档，重复 hello 不会重发 secret，只会回 `node_secret_status: "active"`）。
4. **孤立的 Gene 无法上传**，因为 GEP 明确拒绝单个资产。`sync()` 会把这种资产计进 `unpaired`。配对靠 `Capsule.extra["gene_id"]`，契约的 `Capsule` 没有 gene 引用字段，只能放 `extra`。
5. **`asset_id` 的哈希前像可能与 Hub 不完全一致**。协议文档只说了「canonical JSON（排序键）」，没定义数字格式化和 Unicode 转义。本库用 `sort_keys=True, separators=(",",":"), ensure_ascii=False`。如果真实 Hub 用了别的写法，publish 会被判哈希不匹配 —— 此时只需改 `adapters.py` 里的 `_canonical_json`，公开 API 不受影响。
6. **`/a2a/assets/search` 的响应外形没有文档**。实现做了容错：list、`{"assets":…}`、`{"results":…}`、`{"items":…}`、`{"data":…}`、`{"payload":{…}}` 都能解析。真实响应不在其中的话，`search()` 只会静默降级成本地结果。
7. **远端命中的分数下限是 0.25**。Hub 用它自己的信号匹配返回了这条资产，但本地关键词打分可能一个词都没命中。给 0 会直接丢掉 Hub 的判断，所以给一个明确的下限，并在 `why` 里注明「这是 Hub 返回的，不是本地算出来的」。
8. **额外的构造期校验**。契约只点名了 `Capsule.content < 50`。按「构造期参数错误抛 ValueError」的统一错误模型，本库还校验了 `confidence` 在 0..1、`kind` / `outcome` 在枚举内、`id` 非空、`blast_radius` 非空、`version >= 1`、以及各列表元素的类型。这些都是收紧而非放宽。
9. **`Gene.tags: list[str] = ()`（契约原文）**：frozen dataclass 不能用可变默认值，所以默认值保持 `()`，但构造后在 `__post_init__` 里归一化成 `list`，保证 `x == Gene.from_dict(x.to_dict())` 成立。
10. **没有实现 `close()`**，契约没定义就不加。公开面严格等于契约列的那九个方法，`"auto"` 实际解析成了哪个后端只通过 `stats()["resolved_backend"]` 暴露，不额外开属性。
11. **`search()` 是全表扫描**，没有倒排索引，万级以上资产会变慢。
12. **没有连过真实 Hub**。EvoMap 的端点行为、错误码、限流表现全部来自公开文档 + mock。第一次接真实 Hub 时最可能出问题的是第 5、6 条。

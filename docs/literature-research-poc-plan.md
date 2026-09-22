# 科研辅助 PoC 规划与实施合同

> 状态：生效；G0、P1、P2A 已完成；P2B–P5 尚未完成，科研辅助 PoC 尚未整体跑通
>
> 当前基线：`teaching-refactor@071c6b0`
>
> 生效日期：2026-09-20
>
> 暂定题目：**面向科研辅助的证据可追溯多文献检索问答系统设计与实现**
>
> 方向来源：据用户转述并确认，教师已明确本阶段只需跑通技术链路、证明技术可行；知网合作、学校算力、正式部署等属于后续学校工程化事项。
>
> 本文地位：本项目科研辅助 PoC 的唯一范围、架构、顺序与验收合同。

## 0. 文档权威与防偏移规则

本规划取代 [development-direction.md](development-direction.md) 中“以多智能体调度策略为毕设核心”的旧方向，也取代 [audit-verification.md](audit-verification.md) 中对该旧方向的认可。旧文档仅保留为历史审计材料，不再决定毕设范围或实现优先级。

本文使用的依据严格分层：

| 层级 | 已知内容 | 不得外推 |
|---|---|---|
| 教师截图中的书面信息 | 教师给出五个 AI 辅助教学候选方向，要求结合现有基础准备初步思路；其中方向（三）为“辅助学术研究” | 截图没有指定 arXiv、知网、系统架构、功能清单或评测方法 |
| 用户转述并确认的教师口头要求 | 当前只需证明技术链路可行；数据库合作、算力和后续缺陷由学校继续解决 | 不等于教师已经认可本文的每一项技术设计 |
| 用户本次项目决策 | 题目尚未备案，先按科研辅助 PoC 写规划并以文档约束后续实现 | 只授权规划与方向替换，不代表功能已经实现或效果已经验证 |
| 本文设计决策 | arXiv 优先、页码级证据、复用现有工作区、M1–M10 及非目标 | 属于项目方案，不得在汇报或论文中写成教师原话 |

发生冲突时，按以下顺序判断：

1. 用户转述并确认的教师要求；
2. 本规划及其后续带日期的决策记录；
3. 当前代码、测试和真实运行结果；
4. 其他历史路线图、README、设想或演示文案。

以下约束是强制性的：

- 任何新增功能必须直接服务第 1 节的唯一技术闭环，或被明确放入“后续候选”；不能因“以后可能有用”进入首版。
- 偏离本文范围、接口、数据语义或阶段顺序前，必须先修改本文并记录原因，再修改代码。
- 每个实现任务和提交都必须注明对应的本文节号与验收门；无法对应时不得实施。
- 测试通过只证明相应实现行为，不得自动升级为检索有效、回答正确、教学有效或可生产部署的证据。
- 缓存结果、模拟结果、真实在线结果必须明确区分，不得在演示或论文中混写。
- 失败状态必须保留并展示；不得用空数组、静默 fallback 或补跑结果掩盖失败。

## 1. 唯一目标与允许主张

### 1.1 唯一技术闭环

本项目只验证以下链路能否完整运行：

```text
研究问题
  -> 生成并确认检索式
  -> arXiv 公开文献检索
  -> 结构化元数据、去重与筛选
  -> 加入项目研究库
  -> 获取开放 PDF 或由用户附加 PDF
  -> 按页解析、分块和向量索引
  -> 在选定的多篇论文中检索证据
  -> LLM 基于证据进行综合回答
  -> 每个主要结论回链到论文、页码和原文
```

首版成功的定义不是“回答看起来合理”，而是上述每一步都有可观察状态，且最终引用能够回到不可变文档中的真实位置。

### 1.2 完成后允许作出的主张

- 在固定演示范围内，公开文献检索、项目入库、全文解析、知识检索和证据问答能够端到端协作。
- 文献源和模型调用通过明确接口隔离，未来可以在不改写核心证据链的前提下增加新的适配器。
- 系统能够对所选文献产生带可核验来源的回答，并在缺少证据时返回明确的不足状态。

### 1.3 禁止作出的主张

- 已完成或已验证知网、万方、Web of Science 等商业数据库接入。
- 已解决商业数据库授权、版权、机构认证或大规模全文获取问题。
- 已证明可支撑学校级并发、海量数据、正式安全合规或生产部署。
- 已证明提高学习效果、科研能力、写作水平、满意度或教学质量。
- 少量 arXiv 结果能够代表某领域的完整研究现状。
- 仅凭接口设计即可保证未来商业数据库“一行代码替换”或完全无适配成本。

## 2. 首版范围

### 2.1 必须完成

| 编号 | 能力 | 最小可观察结果 |
|---|---|---|
| M1 | 研究问题与检索计划 | 用户能看到并确认实际提交给文献源的检索式 |
| M2 | 结构化 arXiv 检索 | 返回统一的 ID、题名、作者、年份、摘要、链接和访问状态，不向上层暴露原始 Atom XML |
| M3 | 文献身份与去重 | 同一 DOI 或 arXiv 论文重复检索、重复选择时不会生成多个项目条目 |
| M4 | 加入现有项目研究库 | 所选记录进入现有 `sources.json` 工作流，保留来源、查询和获取时间 |
| M5 | 合法全文路径 | 支持获取 arXiv 开放 PDF；获取不可用时保留元数据并允许用户手工附加本地 PDF |
| M6 | 页码感知解析与索引 | 每个检索 chunk 都可解析回文档哈希、页码和精确原文 |
| M7 | 项目内多文献检索 | 查询必须同时限定 `project_root` 和用户选择的 `source_ids` |
| M8 | 证据约束回答 | 每个主要结论引用一个或多个真实证据；没有充分证据时明确说明不足 |
| M9 | 证据查看 | 点击或展开引用后能看到论文题名、页码、精确原文及必要上下文 |
| M10 | 稳定演示 | 具备带来源标记的缓存语料，公网失败时仍可演示同一条技术链路 |

### 2.2 首版明确不做

- 知网、万方、Web of Science 或其他商业数据库适配器；
- 导师匹配、跨校咨询、合作伙伴推荐；
- 登录注册、多租户、复杂权限和学校统一认证；
- 高并发、分布式服务、集群部署和亿级向量库；
- 模型训练、微调或自建大模型；
- 通用多模态课件理解、电路图理解和完整课程知识图谱；
- 复杂推荐算法或长期用户画像；
- 将 Reviewer、Argument Map、Claim Ledger 强行接入主演示链路；
- 将“研究趋势树”包装成未经证据约束的领域权威结论。

必要的路径边界、密钥保护、文件大小限制、下载超时、内容类型检查、来源记录和错误提示不属于“商业级安全”，不得因 PoC 定位而删除。

### 2.3 核心闭环完成后才可考虑

- 基于已有证据生成带引用的研究主题分类或对比矩阵；
- 增加 OpenAlex 等第二个公开元数据适配器；
- 把 Review 或 Argument Map 作为独立后续工作流接入；
- 将该闭环包装成 Agent 工具或多角色协作场景；
- 优化排序、混合检索和 rerank。

这些项目不得阻塞 M1–M10。

## 3. 当前代码基线与缺口

| 区域 | 已有基础 | 本次必须补齐的缺口 |
|---|---|---|
| 文献工作区 | `SourceLibraryView.vue` 已在原工作区内支持公开发现、检索计划确认、结果回执和批量入库 | P3 增加多文献问答与证据展示；不新建平行产品 |
| 项目文献存储 | 项目内 `.yanmo/sources.json` 已保存规范化身份、检索事件、最终检索计划和初始全文状态 | P2B 增加合法全文 artifact、页级解析与索引状态迁移 |
| PDF/OCR | `python/src/parser/` 已有逐页 `PageContent` 和 OCR fallback | 项目接口不能再只返回展平的 `full_text`；必须把页结构传入索引 |
| RAG | `python/routers/rag.py` 已有 Chroma 持久化、分块、项目与 source 过滤 | chunk 元数据增加页码、字符坐标、文档哈希和索引版本；查询必须显式限定项目和文献 |
| 公开检索 | `ArxivProvider`、Literature API 与应用服务已提供结构化检索、显式失败和项目入库 | 首版继续只用该确定性服务；第二公开源不属于当前缺口 |
| Agent | Agent V2 已能调用学术工具 | Agent 只能包装确定性服务，不能绕过项目范围、证据校验或失败状态 |
| 模型 | 已有 Anthropic、OpenAI-compatible、Ollama Provider | 直接复用；不另建平行 `ModelProvider` 抽象 |
| 方法文档 | 根目录 `METHODOLOGY.md` 是 Ledger、Anchor 与 venue profile 的独立审稿验证方案 | 不得直接套用到本毕设；正式评测前另建 `methods/literature_poc/` 方法协议 |

当前 `teaching-refactor` 与 `main` 的基线均为 `071c6b0`。本规划记录的是该基线之上的新工作，历史文档中的旧分支、脏文件数量和旧优先级不得复用为当前事实。

## 4. 架构合同

### 4.1 组件边界

```text
Literature Workspace UI
        |
        v
Literature API / Application Service
        |-----------------> LiteratureProvider
        |                       `-> ArxivProvider
        |                       `-> FixtureProvider
        |
        |-----------------> Project Source Store
        |                       `-> sources.json + references/
        |
        |-----------------> FullTextResolver
        |                       `-> arXiv open PDF / user attachment
        |
        |-----------------> Page-aware Parser + RAG Index
        |
        `-----------------> Evidence Answer Service
                                `-> existing LLM Provider
```

核心业务状态属于应用服务，不属于 Agent 对话历史。Agent 可以调用这些服务，但不得成为文献身份、索引状态或证据坐标的唯一保存位置。

### 4.2 `LiteratureProvider`

Provider 只负责发现与规范化元数据，不承诺所有来源都提供全文：

```python
class LiteratureProvider(Protocol):
    async def search(self, query: SearchQuery) -> SearchPage: ...
    async def get_record(self, external_id: str) -> PaperRecord: ...
    async def resolve_access(self, record: PaperRecord) -> list[AccessLocation]: ...
    def capabilities(self) -> ProviderCapabilities: ...
```

首版只实现：

- `ArxivProvider`：真实在线实现；
- `FixtureProvider`：固定、离线、可重复的契约测试与演示实现。

Provider 必须区分 `invalid_request`、`not_found`、`rate_limited`、`unavailable`、`invalid_response` 等失败，不得把它们统一伪装为“零结果”。

### 4.3 规范化数据模型

`SearchPlan` 至少包含：

- 原始研究问题；
- 系统建议的检索式与用户最终确认/编辑后的实际检索式；
- provider、过滤条件、分页参数和排序方式；
- 检索式生成方法，以及使用模型时的模型与配置记录；
- 服务端接收用户确认的时间、实际执行时间和结果快照标识。

系统与论文记录必须以“实际执行检索式”为准，不能只展示自然语言问题或未经确认的建议词。

`result_snapshot_id` 是 provider、实际查询和返回记录内容的稳定指纹，不承担一次 UI 操作的授权身份。每次成功搜索由服务端另行签发高熵 `search_execution_id`，并在内存中绑定该次 `SearchPage` 与最终 `SearchPlan`；客户端不能选择该标识所绑定的内容，项目入库只接受仍然有效的执行标识和其中的 `paper_id`。这只是单用户 PoC 的短期能力句柄，不外推为多用户安全授权。这样，相同查询返回相同内容快照时，不同研究问题或检索计划也不会互相覆盖。

`SearchQuery.query` 是已经由用户确认、可直接提交给当前 Provider 的表达式。自然语言关键词到 `all:`、字段限定和布尔表达式的转换属于上层检索计划服务；Provider 不得再次猜测或改写。Provider 追加类别或年份过滤后，`PaperRecord.source_query` 必须保存最终实际提交的完整表达式。

`PaperRecord` 至少包含：

- 内部稳定 ID；
- `provider`、`provider_record_id`；
- DOI、arXiv ID 等 `external_ids`；
- 题名、作者、年份、来源、摘要；
- 记录 URL 与候选访问位置；
- 原始查询、检索时间与规范化版本；
- 元数据快照哈希或等价 provenance。

去重顺序固定为：

1. 规范化 DOI；
2. 去掉版本后缀的 arXiv 基础 ID，同时单独保存版本；
3. 规范化题名 + 第一作者 + 年份的保守后备键。

后备键只能用于提示或合并候选；发生歧义时保留两条并提示用户，不能静默误合并。

`FullTextArtifact` 至少包含：

- `source_id`、本地文件路径、来源 URL；
- 访问方式与开放状态；
- 获取时间、文件大小、MIME 类型；
- SHA-256 与版本；
- `fulltext_ready`、`access_unavailable`、`acquire_failed` 等明确状态。

`EvidenceSpan` 至少包含：

- `source_id`、artifact SHA-256；
- `page_start`、`page_end`；
- 页内或规范全文字符坐标；
- `chunk_id`、精确原文和原文哈希；
- parser、chunker 与 embedding 配置版本。

`AnswerClaim` 至少包含：

- 结论文本；
- 一个或多个 `evidence_ids`；
- 证据不足或冲突标志；
- 生成时使用的模型与配置记录。

### 4.4 全文处理状态机

```text
metadata_only
  -> acquiring
      -> fulltext_ready
          -> parsing
              -> parsed
                  -> indexing
                      -> indexed

acquiring -> access_unavailable | acquire_failed
parsing   -> parse_failed
indexing  -> index_failed
```

失败后允许用户重试或附加本地文件，但必须保留原失败原因和尝试记录。

### 4.5 页码与证据不变量

- 首版 chunk 默认不得跨 PDF 页；如未来允许跨页，必须同时保存起止页与各页坐标。
- `exact_quote` 必须能在对应 artifact SHA-256 的规范页文本中精确匹配。
- 重新解析、替换 PDF、改变 parser/chunker/embedding 配置后，旧索引必须标记过期并重建。
- 前端不得渲染无法通过上述校验的证据引用。
- LLM 只能引用检索阶段返回的 `evidence_id`，不得自行生成页码或来源 ID。
- 多文献查询必须同时提交 `project_root` 和显式 `source_ids`；禁止使用无范围的全局 RAG 结果生成项目回答。

## 5. 实施顺序与阶段门

计划按信息价值和依赖关系推进，不按界面观感推进。

| 阶段 | 工作 | 依赖 | 阶段门 | 当前状态 |
|---|---|---|---|---|
| G0 范围冻结 | 本规划生效；旧路线标为历史；记录暂定题目和非目标 | 无 | 后续任务均能映射到 M1–M10 | 已完成（2026-09-20） |
| P1 合同与夹具 | 建立规范化模型、Provider 接口、失败语义、FixtureProvider 和契约测试 | G0 | FixtureProvider 完整通过同一公开接口；无 UI 依赖 | 已完成（2026-09-20） |
| P2A 文献发现与入库 | ArxivProvider、搜索 API、去重、选择、项目入库、全文状态 | P1 | 固定查询可在线或从明确标记的缓存返回结构化记录；重复加入不产生重复条目 | 已完成（2026-09-21；范围仅为文献发现、去重和项目入库） |
| P2B 页级解析与索引 | 项目内容 API 保留页结构；页内 chunk；证据元数据；索引过期规则 | P1 | 任一检索 hit 均能解析回同一哈希文档的真实页码与精确原文 | 未开始 |
| P3 证据问答 | 多文献选择、项目隔离检索、Evidence Answer Service、证据 UI、无证据拒答 | P2A + P2B | 每个渲染结论的证据均通过机器校验；无范围查询被拒绝 | 未开始 |
| P4 演示与技术评测 | 固定公开论文包、缓存路径、失败场景、重复运行、指标记录 | P3 | 在线与缓存模式均能完成同一演示脚本；失败不被伪装为成功 | 未开始 |
| P5 后续候选 | 主题分类、第二公开源、Review/Argument Map、检索优化 | P4 | 逐项另行立项，不反向改变首版验收 | 未开始 |

P2A 与 P2B 在 P1 合同冻结后可以并行；P3 不得在两者任一阶段门失败时提前开始。

### 5.1 P1 完成证据（2026-09-20）

- 已实现 `python/src/literature/models.py`、Provider 契约、显式失败语义和纯离线 `FixtureProvider`；未修改 Project、Parser、RAG 或前端。
- 规范化合同已冻结论文身份、检索快照、全文状态、1-based 单页证据坐标及回答证据状态；可变集合在合同边界转换为不可变快照。
- 44 个 P1 定向测试通过；Python 全量单元回归为 `1392 passed, 5 skipped`；P1 文件通过 Ruff 静态与格式检查。
- 两个只读复核任务在首轮提出的不变量问题已纳入修复；二次确认因工具额度中断，未作为完成证据。P1 阶段门以可重复的静态检查和测试结果为准。
- 这些结果只证明合同层与离线夹具行为，不证明在线 arXiv、项目入库、PDF 解析、向量检索或证据回答已经跑通。

### 5.2 P2A.1 `ArxivProvider` 完成证据（2026-09-20）

- 已实现结构化 Atom 解析、0-based API 分页到 1-based 应用分页的映射、年份/类别过滤、稳定无版本论文 ID、版本化元数据快照和确定性访问位置。
- 已区分 `invalid_request`、`not_found`、`rate_limited`、`unavailable` 与 `invalid_response`；合法零结果保持为成功空页，网络或响应失败不会伪装成零结果。
- 默认请求在同一进程事件循环内共享单连接限流门并保持三秒请求间隔；这遵循 [arXiv API Terms](https://info.arxiv.org/help/api/tou.html) 的旧 API 约束，但不主张多进程或分布式限流已解决。
- 39 个 `ArxivProvider` 定向测试与 44 个 P1 合同测试共同通过；Python 全量单元回归为 `1431 passed, 5 skipped`，旧 Agent arXiv 工具兼容测试为 `22 passed`，相关文件通过 Ruff 静态与格式检查。
- 一次真实在线只读冒烟检索 `all:"multi agent"` 成功返回 `live` 结果；当次响应为 1 条记录、`totalResults=17320`，首条 `provider_record_id=2203.08975`。该动态数量只记录当次网络兼容性，不作为检索质量或领域覆盖证据。
- 截至 P2A.1 收口时，搜索服务、API、项目内去重入库和全文状态编排尚未实现；后续完成证据见 5.3–5.4。

### 5.3 P2A.2 搜索服务与项目入库后端完成证据（2026-09-21）

- P2A.2 首先实现受服务端检索结果约束的批量入库；P2A.3 又把稳定内容快照与单次执行句柄分离。当前客户端只能提交有效 `search_execution_id` 与其中的 `paper_id`，不能指定项目 `source_id`、路径或全文状态。
- 已实现 DOI、arXiv 基础 ID、`paper_id` 的强身份预检与原子去重；题名、第一作者、年份只报告可能重复，不自动合并。入库在共享的进程内 manifest 事务锁中完成一次原子替换，未知版本、损坏结构和项目链接逃逸会拒绝写入。
- 每次入库均持久化实际检索事件；初始全文状态为 `metadata_only`。同一论文后续发现更优开放全文入口时，仅在尚未开始获取的状态下单调补全，不覆盖进行中、已有 artifact 或失败状态。
- P2A.2 相关 service、router、project、atomic I/O 与应用生命周期定向回归为 `172 passed, 5 skipped`，相关 Python 文件通过 Ruff 静态与格式检查。
- 完整后端套件为 `2640 passed, 14 skipped, 5 failed`；5 个失败均可在 `tests/integration/test_science_perspectives.py` 单文件稳定复现，属于既有 Science 多文章切分夹具只识别出 1 篇而非 3 篇，未伪报为本阶段全量通过。
- 一次真实在线冒烟已通过同一 `ArxivProvider -> LiteratureService -> ProjectSourceManifestStore` 链路：返回 1 条 `live` 记录，首次入库 `created_count=1`，重复入库 `reused_count=1`，项目清单保持 1 条，全文状态为 `metadata_only` 且具有来源 URL。
- 截至 P2A.2 收口时，文献发现、多选和入库的前端交互仍未接入；后续完成证据见 5.4。PDF 获取、页级解析、索引与证据回答不属于本节完成证据。

### 5.4 P2A.3 检索计划与前端收口完成证据（2026-09-21）

- 现有 Source Library 已支持输入研究问题、透明模板建议、编辑并确认实际检索式、查看实际查询回执、选择多篇结果和批量加入项目库；没有建立平行文献产品。
- HTTP 搜索强制接收 `SearchPlanDraft`；服务端补齐 provider、实际查询、接收确认请求时间、执行完成时间和内容快照 ID。只有实际入库所使用的选择会把最终 `SearchPlan` 随 `search_event` 持久化，未入库搜索不宣称永久保存。
- `result_snapshot_id` 只标识结果内容；每次成功搜索另有独立高熵 `search_execution_id` 绑定同一次 `SearchPage + SearchPlan`。相同内容快照下的不同研究问题不会互相覆盖。该句柄仅在当前进程与 LRU 容量内有效，重启或淘汰后需重新检索，也不代表多用户安全授权。
- manifest 写回前会验证事件、计划和内容快照的结构与关系一致性，并验证同一 execution 跨 source 的共同绑定；这是数据一致性检查，不是密码学防篡改保证。历史元数据刷新仍保留各次事件的旧哈希、查询和时间。
- 后端文献、Project、atomic I/O 和应用生命周期定向回归为 `267 passed, 5 skipped`；Python 全量单元回归为 `1494 passed, 5 skipped`。前端全量为 `839 passed`，并通过 Prettier、ESLint、TypeScript 检查与生产构建。
- 完整后端套件为 `2652 passed, 14 skipped, 5 failed`；5 个失败仍全部位于既有 `tests/integration/test_science_perspectives.py`，原因仍是三文章夹具只切出 1 篇，未出现本次 Literature/Project 改造导致的新失败。
- 真实在线冒烟通过 `ArxivProvider -> LiteratureService -> ProjectSourceManifestStore`：固定查询返回 1 条 `live` 记录，执行标识有效，首次入库 `created_count=1`，重复入库 `reused_count=1`，manifest 保持 1 条来源，并持久化研究问题、实际查询和 `metadata_only` 全文状态。
- P2A 阶段门已经满足，当前完成范围对应 M1–M4 及 M5 的状态前提；开放 PDF 获取、页级解析、索引、多文献证据问答和完整 PoC 仍属于 P2B–P4，不能据此宣称整体链路已经跑通。

### 5.5 P2A 层复核修复记录（2026-09-22）

对未提交的 P2A 改动做了两路独立只读复核（模型/Provider 层与前端层），下列问题已在本日修复；修复不改变 M1–M10 范围、数据模型字段或阶段顺序。

- 合同层：不可变快照此前可被两条公开路径绕过（对冻结集合重新调用 `__init__`，以及 `model_copy(update=...)` 返回可变 `list`/`dict`）；现在两者都被阻断，冻结模型也可哈希。DOI 规范化补齐 `https://www.doi.org/…`、`DOI …`、`info:doi/…` 与 `?query`/`#fragment` 后缀。fixture 对显式版本号的查询不再用另一个版本命中，其 `RELEVANCE` 也不再按方向反转自造排序。
- Provider 失败语义：arXiv 返回条数少于 `totalResults` 估算的短页不再判为 `invalid_response`；只有超过 `totalResults`、`start`、`max_results` 所能解释的条数才失败。httpx 客户端构造失败（例如本机无法解析的代理变量）包装为结构化 `unavailable`，不再以未处理异常穿透到 API。
- 前端：切换项目时清空上一个项目的检索快照、执行标识与选择；入库成功后的库刷新失败不再被报告成入库失败；组合式函数错误信息改走 i18n（新增 9 个双语键）；检索回执的 live/cache/fixture 徽标改用纸面墨色（此前在暗色 token 集下合成对比度约 1.5:1，等于没有标记）。
- 验证证据：Python 单元回归 `1506 passed, 5 skipped`；前端 `844 passed`；Ruff 静态与格式检查、ESLint、Prettier、`vue-tsc`（含 `tsconfig.test.json`）与 Vite 生产构建全部通过。真实在线冒烟仍为 `live`，返回首条 `provider_record_id=2203.08975`，`get_record` 元数据哈希一致，缺失记录仍返回结构化 `not_found`。
- 完整后端套件为 `2661 passed, 14 skipped, 8 failed`；8 个失败与本次改动无关：5 个仍是 §5.3–5.4 已记录的 `tests/integration/test_science_perspectives.py` 三文章切分夹具问题（在 `071c6b0` 干净工作树上同样复现），另 3 个是 `TestOllamaStatus` 与 `TestEditCloudSSE` 在本机代理变量（`no_proxy` 含 `[::1]`，httpx 无法解析）下失败，同样在 `071c6b0` 复现。修复前后失败集合完全一致，通过数由 `2649` 增至 `2661`。
- 仍未完成：缓存或 Fixture provider 未注册进运行中的应用，因此 §5.4 的 cache/fixture 标记路径与 M10 仍留待 P4；API 层仍不向前端透出服务端错误 `details`；结果分页与无障碍修饰属于 P3/P4 范围。
- 本节结果仍只证明工程行为与失败路径，不构成检索质量、回答正确性或教学效果证据。

## 6. 验证与毕设方法边界

### 6.1 当前可冻结的工程目标

- 验证完整技术链路可执行；
- 验证文献身份、项目隔离和证据坐标满足本合同；
- 验证失败路径可观察、可恢复且不会伪造成功；
- 记录系统在固定演示任务上的过程与结果。

### 6.2 尚未冻结的正式研究问题

用户尚未指定正式研究问题、假设和主要指标，因此本文不代替用户或教师确定 RQ，也不预写效果结论。正式实验前必须新建 `methods/literature_poc/METHODOLOGY.md`，至少冻结：

- 正式 RQ 与允许主张；
- 语料纳入、排除与授权规则；
- development、pilot 与 held-out 的隔离；
- 基线、单一 provider/model 与参数；
- 问题及证据 gold 的制作与复核方式；
- 主要指标、次要指标、阈值和失败处理；
- 代码、输入、配置、模型响应与输出的哈希/版本记录。

未经该方法协议，不得开展或报告正式效果实验。根目录现有 `METHODOLOGY.md` 继续只服务 Reviewer/Ledger/Anchor 研究，不得被描述为本 PoC 的方法方案。

### 6.3 候选技术指标，不是既定门槛

- 元数据必填字段完整性；
- DOI/arXiv ID 去重正确性；
- 全文获取、解析和索引成功状态；
- 检索 `Recall@k`、MRR 或 nDCG；
- 页码定位与精确原文匹配；
- 主要结论的证据支持度与无依据结论率；
- 无答案问题的拒答行为；
- 端到端成功率、延迟和失败类型。

具体阈值只能在研究问题确定、代表性 pilot 人工审计后冻结，不能现在凭经验填写。自动测试、一次成功演示和少量示例均不能替代该过程。

## 7. 固定演示脚本与验收不变量

### 7.1 演示脚本

1. 输入一个明确的科研问题。
2. 系统展示生成的检索式，用户确认后提交。
3. arXiv 返回结构化候选文献，并显示来源与访问状态。
4. 用户选择多篇论文加入项目库；重复项被识别。
5. 系统获取开放 PDF，或对不可获取项提示用户附加本地 PDF。
6. 系统显示解析、索引状态及失败原因。
7. 用户对所选论文提出跨文献问题。
8. 系统返回综合回答；每个主要结论附题名、页码和精确原文。
9. 用户展开一条证据，核对原文和上下文。
10. 对一个语料中没有答案的问题，系统明确说明证据不足，不伪造引用。

### 7.2 不变量

- 所有项目文献都有可追踪的来源和稳定身份。
- 所有全文都有哈希和明确访问状态。
- 所有回答都限定在当前项目和用户选择的文献中。
- 所有显示给用户的证据都能机器解析回原文。
- 所有缓存结果都标记为缓存，不冒充当前在线检索。
- 所有失败都保留原因，不通过静默换模型、换 provider 或删除失败记录制造成功。

## 8. 主要风险与控制

| 风险 | 影响 | 首版控制 |
|---|---|---|
| arXiv 网络、限流或响应变化 | 现场检索失败 | 官方 API、超时与错误状态、缓存 Fixture；缓存明确标记 |
| 文献没有可用开放全文 | 链路在全文阶段中断 | 保留 `metadata_only/access_unavailable`；允许手工附加合法 PDF |
| 扫描 PDF 或复杂版式解析失败 | 页码与原文不可用 | 首版固定文本型 PDF；OCR 作为兼容能力而非演示前提 |
| 页面在展平/切块时丢失 | 无法核验证据 | P2B 作为生成问答前的硬门；禁止无坐标 chunk 进入证据回答 |
| 模型生成不存在的页码或引用 | 回答失真 | Evidence ID 白名单、精确原文校验、渲染前验证、无证据拒答 |
| 旧路线图继续被当成当前任务 | 开发重新偏向多智能体调度 | 旧文档历史标记、本规划 SSoT、每项提交映射到节号 |
| 为展示效果不断增加功能 | 主链延期且难以验证 | M1–M10 完成前冻结 P5；新增范围必须走文档变更 |
| 测试数据参与调参后又作为正式结果 | 评测失真 | development/pilot/held-out 隔离，原始语料与 gold 不可变 |

## 9. 决策记录与变更控制

### 9.1 已确认决策

| ID | 决策 | 状态 | 依据 |
|---|---|---|---|
| D-001 | 选择学校方向（三）“辅助学术研究”中的文献科研辅助子链 | 已确认 | 与现有 Scholar Assistant 基础匹配 |
| D-002 | 当前只做技术可行性 PoC，不承担学校后续工程化问题 | 已确认 | 教师要求，经用户转述确认 |
| D-003 | 首版公开文献源为 arXiv，不接知网 | 已确认 | 最短可运行链路；当前已有 arXiv 原型 |
| D-004 | 页码和原文证据是首版核心，不是展示装饰 | 已确认 | 最终回答必须可核验 |
| D-005 | 扩展现有文献工作区，不另建平行产品 | 已确认 | 当前代码已有 Source Library 与项目存储 |
| D-006 | 不重建模型 Provider；复用 Agent V2 现有模型适配层 | 已确认 | 当前已有本地和 API Provider |
| D-007 | 原“多智能体调度策略”路线不再作为毕设主线 | 已确认 | 题目尚未备案，用户选择新方向 |
| D-008 | arXiv 首版走公开 API，不把网页抓取作为主通道 | 本文冻结 | 当前已有 API 原型，且结构化响应更适合作为可替换 Provider 的输入 |
| D-009 | `SearchQuery.query` 固定为用户确认的 Provider 原生表达式；关键词包装留在上层服务 | 本文冻结 | 防止 Provider 二次改写导致展示内容与实际请求不一致 |
| D-010 | arXiv 请求在单进程事件循环内共享串行限流门；多进程协调不属于首版保证 | 本文冻结 | 遵守 [arXiv API Terms](https://info.arxiv.org/help/api/tou.html) 的单连接和三秒间隔约束，同时诚实限定 PoC 边界 |
| D-011 | 项目入库只接受服务端持有的检索结果授权与其中的 `paper_id`；调用方不能提交 `source_id`、本地路径或全文状态 | 被 D-017 收紧 | 防止客户端把任意记录或文件状态伪装成检索结果，同时保留实际搜索 provenance |
| D-012 | 多选入库先完成全批强身份冲突预检，再在项目 manifest 的进程内共享事务锁中对 `sources.json` 做一次原子替换；DOI、arXiv 基础 ID 和 `paper_id` 可自动去重，题名、第一作者与年份仅报告候选 | 本文冻结 | 避免半批写入、同进程 Project/Literature 路由丢更新和题名近似造成的静默误合并；不外推为多进程事务保证 |
| D-013 | 新检索记录使用项目内随机 `source_id`，初始 `rag_status=unavailable` 且全文状态为 `metadata_only`；全局 `paper_id` 只保存在 metadata | 本文冻结 | 隔离项目本地条目身份与跨来源论文身份，禁止在未获取、解析和索引全文前伪报可用 |
| D-014 | 每次实际入库使用的检索事件必须随项目文献持久化，至少保存执行 ID、内容快照 ID、确认查询、Provider 最终表达式、模式、检索时间和元数据哈希 | 本文冻结 | 内存执行句柄只负责当次选择约束，不能作为重启后的唯一 provenance |
| D-015 | 项目内部 manifest 必须解析真实路径并仍位于项目根内；未知 manifest 版本或结构损坏时拒绝覆盖 | 本文冻结 | 防止 `.yanmo` 链接逃逸和兼容性不明时的数据丢失 |
| D-016 | 前端提交研究问题、建议检索式及生成方式组成的 `SearchPlanDraft`；服务端记录接收确认/执行时间并绑定返回快照，入库时把最终 `SearchPlan` 随检索事件持久化。首版 UI 的短语包装明确标记为 `template`，不伪装成 LLM 生成 | 本文冻结 | 让“研究问题—确认检索式—实际请求—项目入库”可追踪，并避免客户端伪造服务端执行时间或结果快照 |
| D-017 | `result_snapshot_id` 仅作为结果内容指纹；每次成功搜索另发随机 `search_execution_id`，服务端以它绑定不可分离的 `SearchPage + SearchPlan`，入库只接受该执行标识与其中的 `paper_id` | 本文冻结 | 相同查询和结果可以产生相同内容快照；独立执行标识防止后一次不同研究问题覆盖前一次计划并导致错误 provenance |
| D-018 | arXiv 返回条数少于 `totalResults` 估算的短页属于合法响应；只有超过 `totalResults`、`start`、`max_results` 所能解释的条数才判 `invalid_response`。fixture 的 `RELEVANCE` 固定为配置顺序，不随方向反转；显式版本号查询不匹配时必须 `not_found` | 本文冻结（2026-09-22） | `totalResults` 是估算值，把估算漂移当失败会制造假失败；反向"相关性"与跨版本命中会把离线夹具伪装成真实排序或真实版本 |
| D-019 | 合同模型的集合字段必须真正不可变：禁止对冻结集合重新 `__init__`，`model_copy(update=…)` 必须重新冻结集合字段，冻结模型必须可哈希 | 本文冻结（2026-09-22） | §5.1 声称"可变集合在合同边界转换为不可变快照"；修复前该保证可被 `__init__` 与 `model_copy` 两条公开路径绕过，使快照哈希与实例内容不一致 |

### 9.2 待确认但不阻塞 P1–P3 的事项

| ID | 事项 | 处理原则 |
|---|---|---|
| O-001 | 最终备案题目和正式 RQ | 在正式评测前由用户与教师确认；不得暗改技术范围 |
| O-002 | 正式语料规模、问题数量和指标阈值 | 由方法协议和 pilot 决定，不在本规划中猜测 |
| O-003 | 是否增加第二个公开元数据源 | 仅在 P4 完成后评估，不用于证明首版可替换性 |
| O-004 | 是否把该闭环包装成多 Agent 场景 | 作为后续展示选择，不得重新成为首版研究主线 |

### 9.3 变更流程

任何影响 M1–M10、数据模型、证据坐标、阶段门或允许主张的变更，必须：

1. 在本文追加带日期的决策；
2. 写明变更原因、被否决方案和影响范围；
3. 更新对应测试与验收门；
4. 重新检查是否需要使既有缓存、索引或实验结果失效；
5. 文档合并后才能实施代码变更。

## 10. 计划文件影响图

下表同时记录预计影响面与当前实施状态；“已实现”只表示对应阶段内容完成，不代表整个 PoC 已跑通。

| 类型 | 路径 | 计划职责 | 当前状态 |
|---|---|---|---|
| 新增 | `python/src/literature/__init__.py`、`python/src/literature/providers/__init__.py` | 暴露稳定的领域合同与 Provider 公共入口 | P1 已实现 |
| 新增 | `python/src/literature/models.py` | 规范化 Paper、Access、Artifact、Evidence、Answer 模型；P2A.3 增加检索计划草案 | P1、P2A.3 已实现 |
| 新增 | `python/src/literature/providers/base.py` | LiteratureProvider 契约 | P1 已实现 |
| 新增 | `python/src/literature/providers/arxiv.py` | arXiv 结构化实现 | P2A.1 已实现 |
| 新增 | `python/src/literature/providers/fixture.py` | 离线契约测试与缓存演示实现 | P1 已实现 |
| 新增 | `python/src/literature/service.py` | 搜索执行、强身份去重、项目批量入库和初始全文状态编排；完成并持久化检索计划 | P2A.2–P2A.3 已实现 |
| 新增 | `python/src/literature/evidence.py` | 证据校验与回答结构化 | 未开始 |
| 新增 | `python/routers/literature.py` | Literature API 与现有 Project 存储的窄适配层；接收检索计划草案 | P2A.2–P2A.3 已实现 |
| 修改 | `python/api_factory.py` | 注册 Literature API，并在应用生命周期关闭 Provider 资源 | P2A.2 后端已实现 |
| 修改 | `python/routers/project.py` | P2A.2 共享清单事务锁、内部路径与 manifest 版本边界；P2B 再补页结构 | P2A.2 后端已实现 |
| 修改 | `python/src/utils/atomic_io.py` | 提供同进程路径级可重入事务锁；不承诺跨进程协调 | P2A.2 后端已实现 |
| 修改 | `python/routers/rag.py` | 页级 chunk、证据 metadata、索引版本和范围约束 | 未开始 |
| 修改 | `python/src/agent_v2/tools/academic_tools.py` | 用结构化 Literature/RAG 工具替代原始 XML 与无范围查询 | 未开始 |
| 新增 | `src/composables/useLiteratureDiscovery.ts` | 检索、选择、入库和状态管理 | P2A.3 已实现 |
| 修改 | `src/components/SourceLibraryView.vue` | P2A.3：公开发现、检索计划确认、结果回执和多选入库；P3 再增加多文献问答与证据展示 | P2A.3 已实现；P3 未开始 |
| 新增/修改 | `python/tests/`、`src/__tests__/` | 契约、状态机、页码证据和端到端回归 | P1–P2A 已实现；P2B 以后未开始 |
| 后续新增 | `methods/literature_poc/` | 正式 RQ、语料、gold、协议和运行记录 | 未开始 |

## 11. 首版完成定义

只有同时满足以下条件，才可称“科研辅助 PoC 已跑通”：

1. M1–M10 均有真实实现和对应验证；
2. `ArxivProvider` 与 `FixtureProvider` 通过同一契约测试；
3. 固定演示可以从研究问题走到多文献证据回答；
4. 任一展示引用都能回到同一哈希文档的真实页码和精确原文；
5. 项目范围和所选文献范围无法被 Agent 或前端绕过；
6. 无全文、解析失败、索引失败、网络失败和无答案均有诚实状态；
7. 在线模式和明确标记的缓存模式均可完成演示；
8. 文档、实现、测试和演示口径一致；
9. 未把技术验证外推成教学效果、生产能力或商业数据库适配完成；
10. Git 基线、模型、provider、parser、chunker 与 embedding 配置可追踪。

在此之前，只能报告“部分链路已完成”，不能报告“系统已跑通”。

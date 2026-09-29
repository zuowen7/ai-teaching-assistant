# 科研辅助 PoC 规划与实施合同

> 状态：生效；G0、P1、P2A、P2B、P3、P4 已完成；A1 的工具层、会话级验收与前端引用展示已完成，真实在线 arXiv 已做一次性人工冒烟、真实模型行为仍未验证（见 5.11、5.12）；后续候选 P5 未开始
>
> 当前基线：`teaching-refactor@6740486`（DeepSeek 策略修复后；第四轮真实模型验证见 5.14，缺陷见 5.13 的 V-22…V-26）
>
> 生效日期：2026-09-20
>
> 暂定题目：**面向科研辅助的证据可追溯多文献检索问答系统设计与实现**
>
> 2026-09-29 开题稿建议题目：**基于智能体的科研文献检索与可溯源问答系统设计与实现**。用于体现用户要求保留的 Agent 特色，尚非正式备案题目；范围补充见 §2.4、D-025–D-027。
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
- 将该闭环扩展为多角色协作场景（单 Agent 接入见 §2.4）；
- 优化排序、混合检索和 rerank。

这些项目不得阻塞 M1–M10。

### 2.4 单 Agent 科研流程接入（2026-09-29 补充）

用户在正式开题讨论中明确要求保留 Agent 特色。为此，在 M1–M10 及 P4 阶段门完成后增加 A1 单 Agent 接入，作为本次毕设计划交付的一部分；原有证据链不改为多智能体调度研究。

- 复用 Agent V2，围绕研究问题提出可见的任务计划和检索式，依据真实工具结果选择下一步，支持当前项目内的连续追问。
- 检索式变更与论文选择由用户确认；Agent 通过确定性文献、全文、索引及证据问答服务完成操作，不维护第二套文献或索引状态。
- 每次检索和证据问答都继承 `project_root` 与已确认的 `source_ids`；不得用旧 Agent 无范围 `rag_search` 或原始 arXiv 响应替代本合同服务。
- 工具结果进入执行记录；参数非法、失败、证据不足及达到调用上限时必须显式停止或请求用户调整，不能仅凭模型文字宣称操作成功。
- A1 验收覆盖：新问题下的计划与确认；已有索引下跳过重复处理；证据不足时补充范围内检索或提出新的检索计划；用户取消；工具失败与调用上限；跨项目及未选文献拒绝；最终证据与普通服务入口一致。
- 证据问答可对所选论文的方法、条件和差异作有出处的文字归纳，供用户形成综述笔记和收敛选题。全面综述、选题新颖性保证、自主实验设计、导师匹配和多智能体协作不成为首版承诺。
- A1 实现时需要工具/服务/会话的后端覆盖及计划、确认、状态、引用展示的前端覆盖。本次仅更新开题规划，不宣称 A1 已实现或验收通过。

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
| P2B 页级解析与索引 | 项目内容 API 保留页结构；页内 chunk；证据元数据；索引过期规则 | P1 | 任一检索 hit 均能解析回同一哈希文档的真实页码与精确原文 | 已完成（2026-09-22，见 5.6、5.8；含 M5 开放 PDF 获取） |
| P3 证据问答 | 多文献选择、项目隔离检索、Evidence Answer Service、证据 UI、无证据拒答 | P2A + P2B | 每个渲染结论的证据均通过机器校验；无范围查询被拒绝 | 已完成（2026-09-29，见 5.9） |
| P4 演示与技术评测 | 固定公开论文包、缓存路径、失败场景、重复运行、指标记录 | P3 | 在线与缓存模式均能完成同一演示脚本；失败不被伪装为成功 | 已完成（2026-09-29，见 5.10；"在线模式"由离线注入的 live provider 承担，真实 arXiv 运行仍为一次性人工冒烟） |
| A1 单 Agent 接入 | 复用 Agent V2 规划任务、调用已验收服务、处理真实反馈与连续追问 | P4、M1–M10 全部门通过 | §2.4 场景通过；范围、证据和失败语义与服务入口一致；工具记录可复核 | 工具层、会话级验收与前端引用展示已完成（2026-09-29，见 5.11、5.12）；真实在线会话与真实模型行为未验证 |
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

### 5.6 P2B 页级解析与索引完成证据（2026-09-22）

- 新增 `python/src/literature/evidence.py`：冻结 `normalized_page_text_v1` 归一化规则（统一换行、行内空白折叠、空行折叠、页首尾裁剪）、页内切块（默认 1400/180，窗口不跨页）、由 artifact 哈希+页码+字符坐标+chunker 版本派生的稳定 `chunk_id`、索引指纹（artifact 哈希 + parser/chunker/embedding/index 版本）、以及只从坐标**派生**引文的 `resolve_evidence_span`。引用文本不由调用方提供，因此无法伪造。
- `python/routers/project.py` 的内容接口不再只返回展平文本：新增 `include_pages` 参数返回逐页原文，并始终返回 `document_sha256`；`text`/`chars`/`pages`（仍为整数计数）保持不变，逐页文本不写入 `sources.json`。
- `python/routers/rag.py` 新增页级索引通道：chunk 元数据携带 artifact SHA-256、`page_start`/`page_end`、`char_start`/`char_end`、`chunk_id`、parser/chunker/embedding/index 版本与索引指纹；同一 artifact 与指纹重复索引时复用，配置或文件变化时必须重建。原展平文本通道（翻译自动入库、上传、手写文本）行为不变。
- `python/src/literature/service.py` 新增 `index_source` 与 `resolve_evidence`，并驱动 §4.4 状态机：`metadata_only → fulltext_ready → parsing → parsed → indexing → indexed`，失败时落到 `parse_failed` / `index_failed` 并保留 artifact 字段与失败原因。证据解析会重新读取并重新哈希附件、重新按页解析，再校验坐标、块身份与块文本，任何不一致都以显式 `evidence_unresolved`（含 `artifact_hash_mismatch` / `stale_index` / `quote_mismatch` 等子码）失败。
- API：`POST /api/literature/index` 与 `POST /api/literature/evidence`；两者都在服务端查索引，调用方不能提交坐标或引文。Source Library 的"建立索引"动作对"PDF + 文献条目"改走页级通道，其它来源保持展平通道（因此永不成为证据）。
- 阶段门证据：`python/tests/unit/test_literature_indexing.py` 用 PyMuPDF 生成真实两页 PDF，逐块调用 `resolve_evidence`，断言每块 `page_start == page_end`、坐标切片等于归一化页文本中的精确原文、引文非空、页码覆盖两页、第 1 页的引文不包含第 2 页的标记文本；另覆盖替换 PDF（`artifact_hash_mismatch`）、版本变化与嵌入模型变化（`stale_index`）、无页元数据的翻译块（`missing_page_metadata`）、跨来源块、缺块、非 PDF 附件、不可解析 PDF、无可提取文本、缺附件、未启用索引与未知来源。
- `python/tests/unit/test_literature_router.py` 另用真实项目存储与 HTTP 客户端跑通同一条链路：文献入库 → 通过既有项目导入接口附加 PDF → `index` → 取指定页的 `chunk_id` → `evidence`，并验证 409/404 的显式失败映射。
- 未完成部分：自动获取 arXiv 开放 PDF（M5 前半）仍未实现，当前唯一取得全文的方式是用户在项目内附加本地 PDF；Parser 只产出页文本，没有 bbox 或页内字符坐标来源，因此坐标空间定义为归一化页文本而非 PDF 布局坐标；`project_scoped` 查询模式已加入 `/api/rag/query` 但尚未被 Agent 工具使用（§10 的 `academic_tools.py` 行仍未开始）。
- 定向测试：`python/tests/unit/test_literature_evidence.py` 94 例覆盖归一化规则、页内切片覆盖与重叠上界、块坐标往返、块身份与指纹对每个输入的敏感性、引文派生与上下文边界，以及每个失败码；该套件在首轮报告一例失败，暴露 `evidence_metadata` 缺少 `page_end` 存在性检查、会抛 `KeyError` 而不是结构化失败；接口已修复，并有两例测试固化"缺失键必须结构化拒绝"。Python 单元回归为 `1621 passed, 5 skipped`。
- 真实向量库冒烟（一次性、非自动化）：临时目录中的 chromadb 接受并原样返回全部页级元数据（`page_start` 保持整数），且带 `project_root` + `source_id` 过滤的查询不会召回同一集合中的展平翻译块。该冒烟不进入自动化测试，因此"真实 Chroma 持久化"仍只由这一次人工观察支撑。
- 本节结果只证明"检索块可核验回真实页码与精确原文"这一机制，不证明检索质量、回答正确性或教学效果。

### 5.7 P2B 分层验收规格（2026-09-22，文档先行）

以下规格在补写集成与端到端测试之前冻结：测试先写、再修实现；任一层暴露缺陷时先修实现并保留回归测试，不得通过放宽断言或跳过测试来"通过"。

| 层 | 位置 | 必须证明 | 依赖 |
|---|---|---|---|
| 单元 | `python/tests/unit/test_literature_evidence.py` | 归一化规则、页内切片、坐标往返、块身份与指纹、引文派生、每个失败码 | 纯函数，离线 |
| 单元 | `python/tests/unit/test_literature_indexing.py` | 服务编排与状态机：真实两页 PDF 逐块回链、替换 PDF/版本/嵌入变化、无页元数据、非 PDF、解析失败、幂等与强制重建 | 真实 PDF + 内存索引存储 |
| 单元 | `python/tests/unit/test_literature_router.py` | 两个新路由的 HTTP 契约与失败码映射（真实项目存储） | TestClient + 假索引存储 |
| 集成 | `python/tests/integration/test_literature_page_index_integration.py` | 真实 ChromaDB 持久化：页级元数据写入/读回、页码为整数、`get_chunk` 往返、重建删除旧块、`project_scoped` 查询只召回范围内的页级块并排除展平块 | 真实 chromadb（缺失时显式跳过） |
| 端到端 | `python/tests/integration/test_literature_evidence_e2e.py` | 真实应用（`create_app` + TestClient，离线 fixture provider）：列出文献源 → 检索 → 批量入库 → 附加本地 PDF → 建立页级索引 → 范围内多文献检索 → 命中 → 证据解析，且解析出的引文等于该页归一化文本的对应切片；无范围查询被显式拒绝 | 真实 chromadb + 真实项目目录，无网络 |

约束：三层都必须离线可跑，禁止依赖 arXiv 或任何外网；依赖缺失只能以显式 `skip` 表达，不得用空断言或吞异常代替；每层结果分别记录，不允许用低层结果替代高层结论。

- 客户端契约（D-022）：`/api/literature/index` 成功后，服务端已重写 `metadata.literature`，因此客户端必须**重新读取**该项目文献后再更新 `rag_status`；把索引前的旧 `literature` 元数据回传会被项目接口以"literature metadata 只能由文献服务维护"拒绝，导致索引成功却被报告为失败。该约束由前端单测固化（断言最终 upsert 携带的是索引后的元数据）。

分层结果（2026-09-22）：

- 单元层：`1622 passed, 5 skipped`。其中 `test_literature_evidence.py` 94 例、`test_literature_indexing.py` 17 例（含"替换 PDF 后重建并淘汰旧块"的服务级回归）、`test_literature_router.py` 28 例（含 1 例新的索引/证据 HTTP 契约）。
- 集成层：`test_literature_page_index_integration.py` 3 例通过（真实 ChromaDB：元数据往返、重建删除旧块、范围内查询只召回页级块且排除展平块）。
- 端到端层：`test_literature_evidence_e2e.py` 1 例通过（真实应用 + 离线 fixture provider；研究问题→检索→入库→附加 PDF→页级索引→范围内检索→证据解析，且引文等于该页归一化文本切片；无范围查询 400）。
- 全量后端为 `2781 passed, 14 skipped, 8 failed`，8 个失败仍全部是 §5.3–5.5 已记录且可在 `071c6b0` 复现的既存/环境问题。
- 集成层在首轮暴露一个单元层无法发现的真实缺陷：`_embedding_identity()` 在集合尚未打开时返回默认身份、打开后返回真实类名，导致索引指纹依赖调用顺序——重复索引不再复用，且新建索引后立刻解析证据会被误判为 `stale_index`。修复方式是先打开存储再读取嵌入身份，并把"同一存储两次读取身份必须一致"写成集成断言。该缺陷未出现在任何单元测试中，说明假存储不能替代真实持久化层。

### 5.8 M5 开放 PDF 获取规格（2026-09-22，文档先行）

P2B 只完成了 M5 的"用户手工附加"一半；本节在实现之前冻结自动获取的接口与规则，用于补齐 M5。自动获取复用既有项目 `references/` 存储与 P2B 索引链路，不新建第二套全文存储。

接口：

- 新增 `python/src/literature/fulltext.py`：`FullTextDownloader` 协议、`HttpFullTextDownloader`（httpx 实现）、`DownloadedArtifact`（content/sha256/size/mime_type/source_url）与 `FullTextDownloadError`（含 `invalid_url` / `not_open_access` / `download_failed` / `too_large` / `unexpected_content_type` / `not_a_pdf` 等显式码）。
- 服务新增 `LiteratureService.acquire_fulltext(*, project_path, source_id, force=False) -> LiteratureFullTextResult`，并驱动 §4.4 的 `metadata_only → acquiring → fulltext_ready`（失败为 `access_unavailable` / `acquire_failed`）。
- 项目存储协议新增 `store_source_artifact(project_path, source_id, filename, content) -> str`：写入 `<project>/references/` 内，拒绝路径逃逸、拒绝覆盖同名文件（自动加后缀），返回项目内绝对路径。
- 路由新增 `POST /api/literature/fulltext`，请求 `{project_path, source_id, force}`。

规则（冻结）：

1. 只接受 `https://` 位置，且只使用 `PaperRecord.access_locations` 中 `kind=pdf` 且 `access_status=open` 的条目（优先 `is_primary`）；没有这样的位置时不得尝试其它来源。
2. 单文件上限 25 MiB；连接与读取超时 30 秒；最多 3 次重定向；请求带明确 User-Agent。
3. 必须同时满足 Content-Type ∈ {`application/pdf`, `application/octet-stream`} 且响应体以 `%PDF-` 开头；任一不满足即失败，不写文件。
4. 成功后写入 source_url、sha256（以落盘字节计算）、file_size_bytes、mime_type、acquired_at，状态置 `fulltext_ready`；`artifact_id` 由 `source_id + sha256` 派生。
5. 无开放位置 → `access_unavailable`；网络、超限、类型或魔数校验失败 → `acquire_failed`；两者都必须带 failure_reason，并保留既有的检索 provenance 与元数据。
6. 失败不写文件、不改动已有 artifact；已存在 file-present 状态（`fulltext_ready`/`parsed`/`indexed`/失败态带文件）时默认拒绝并要求 `force`，避免静默替换用户已核验的全文。
7. 状态迁移必须落盘：先写 `acquiring`，再写终态；任何失败都不能表现为成功。
8. 获取成功后，既有的 `/api/literature/index` 与证据解析链路必须直接可用（不新增索引路径）。

分层验收：

| 层 | 必须证明 |
|---|---|
| 单元 | 下载器对非 https、非开放位置、HTTP 错误、超时、超限、错误 Content-Type、非 PDF 魔数分别给出对应错误码且不返回内容；成功路径返回字节与哈希。服务层状态迁移（含 `acquiring` 落盘）、失败态与 failure_reason、已存在 artifact 时不覆盖且 `force` 才重取。路由层请求校验与错误码到 HTTP 的映射。 |
| 集成 | 真实项目存储：文件确实落在项目 `references/` 内、`original_path` 指向它、manifest 里 sha256/大小/MIME/状态正确；随后真实页级索引能把这份自动获取的 PDF 建成可解析证据的索引。 |
| 端到端 | 真实应用 + 注入下载器（离线）：检索 → 入库 → **自动获取开放 PDF** → 页级索引 → 范围内检索 → 证据解析，且解析出的引文等于该 PDF 页文本切片；无开放位置时返回显式 `access_unavailable`。 |

决策 D-024：端到端必须离线，因此测试在 `create_app` 之前替换下载器实现（与替换 `ArxivProvider` 同理）；真实网络的下载只做一次性人工冒烟并在本节记录，不进入自动化测试，避免把网络波动当作回归信号。

分层结果（2026-09-22）：

- 单元层 `python/tests/unit/test_literature_fulltext.py` 31 例：位置选择（只认 open + https + PDF，优先 primary）、非 https/HTTP 错误/超时/传输错误/超限/错误 Content-Type/非 PDF 魔数各自的错误码、重定向跟随、以及服务层状态迁移（`acquiring` 落盘可被观察）、无开放位置 → `access_unavailable`、下载失败 → `acquire_failed` 且不写文件、已有 artifact 默认拒绝且 `force` 才重取、下载器谎报哈希被拒、获取后可直接进入页级索引。路由层另加 1 例覆盖 404（未知来源）/503（未接下载器）/422（禁止客户端指定路径）。
- 集成层 `python/tests/integration/test_literature_fulltext_integration.py` 1 例：真实项目目录与真实 ChromaDB 下，文件确实落在 `<project>/references/`、`original_path` 指向它、manifest 的 sha256/大小/MIME/状态正确，随后索引、范围内检索、证据解析全部通过，且引文等于对已落盘文件的重新解析结果。
- 端到端层 `test_literature_evidence_e2e.py` 2 例：手工附加路径与自动获取路径；自动获取用例同时验证"无开放位置"的记录返回显式 `access_unavailable`。
- 本阶段测试先行抓到两处实现问题并已修复：(1) 清单中的 sha256 一度采信下载器自报值，与 D-023 第 4 条"以落盘字节计算"不符——改为由服务端对将写入的字节重新计算，且下载器自报不一致时显式 `acquire_failed`；(2) 注入客户端默认不跟随重定向，导致 302 被判成下载失败——改为按请求显式 `follow_redirects=True`，使获取语义不依赖注入实现。
- 仍未完成：前端尚未提供"获取开放全文"按钮（P3 范围，当前只能通过 API 触发）；真实网络的 arXiv PDF 下载只做一次性人工冒烟，不作为自动化证据。
- 汇总：Python 单元 `1654 passed, 5 skipped`；P2B/M5 集成与端到端共 6 例通过；前端 `845 passed` 且未受影响；Ruff 静态与格式检查通过；全量后端为 `2815 passed, 14 skipped, 8 failed`，8 个失败仍是 §5.3–5.5 记录的既存/环境问题。

### 5.9 P3 证据问答规格（2026-09-29，文档先行）

P3 阶段门是"每个渲染结论的证据均通过机器校验；无范围查询被拒绝"。本节在实现之前冻结 M8、M9 的接口、失败语义与分层验收；测试先写、再修实现，任一层暴露缺陷时先修实现并保留回归测试，不得通过放宽断言或跳过测试来"通过"。§2.4 的 A1 只调用本节的确定性服务，不新增第二套问答路径，也不得绕过本节的校验。

影响文件：

| 类型 | 路径 | 职责 |
|---|---|---|
| 新增 | `python/src/literature/answer.py` | 提示构造、模型输出解析、白名单与页码一致性校验、拒答原因；纯逻辑，无 I/O |
| 新增 | `python/src/literature/answer_model.py` | `ModelIdentity`、`EvidenceAnswerModel` 窄协议、`AgentProviderAnswerModel` 适配器 |
| 修改 | `python/src/literature/service.py` | `LiteratureService.answer_question(...)`、答案结果信封与新增错误码 |
| 修改 | `python/routers/rag.py` | 把范围检索抽成可复用调用，路由与问答服务共用同一实现 |
| 修改 | `python/routers/literature.py` | `POST /api/literature/answer`、范围检索适配器与错误码映射 |
| 修改 | `python/api_factory.py` | 用既有 Provider 工厂装配答案模型（不新增模型层） |
| 新增 | `src/composables/useLiteratureAnswer.ts` | 前端问答状态、多文献范围选择、结论与证据状态 |
| 修改 | `src/components/SourceLibraryView.vue` | 多文献证据问答区、证据展开与"获取开放全文"入口 |

接口：

```python
class LiteratureAnswerRequest(BaseModel):
    project_path: str = Field(min_length=1, max_length=1000)
    question: str = Field(min_length=1, max_length=2000)
    source_ids: list[str] = Field(min_length=1, max_length=50)
    top_k: int = Field(default=8, ge=1, le=20)
```

```python
class LiteratureService:
    async def answer_question(
        self, *, project_path: str, question: str, source_ids: Sequence[str], top_k: int = 8
    ) -> LiteratureAnswerResult: ...
```

结果信封（`service.py`，与既有 `Literature*Result` 同处）：

```python
class LiteratureAnswerResult(BaseModel):
    question: str
    project_root: str
    source_ids: list[str]
    status: AnswerStatus                 # answered | insufficient
    insufficient_reason: InsufficientReason | None
    claims: list[AnswerClaim]            # 只含通过校验的 supported/conflicting 结论
    evidence: list[LiteratureEvidenceResult]   # 本轮被引用、且可机器解析回原文的证据
    rejected_claims: list[RejectedClaim]
    unresolved: list[UnresolvedEvidence]
    retrieved_chunk_count: int
    model_provider: str
    model_name: str
    model_config_hash: str
    generated_at: datetime
```

`AnswerStatus` = `answered | insufficient`；`InsufficientReason` = `no_retrieval_hits | no_resolvable_evidence | model_reported_insufficient | all_claims_rejected`；`RejectedClaimReason` = `unknown_evidence_id | fabricated_page_reference | missing_evidence | model_reported_insufficient | empty_claim_text | claim_limit_exceeded`。

规则（冻结）：

1. **范围强制**。`project_path` 与去重后的非空 `source_ids`（1..50）缺一不可；集合中任一来源不属于该项目或不是文献条目时，以显式 `source_not_found`(404) / `source_not_literature`(409) 失败，不得从检索中静默剔除后再回答。
2. **检索只走页级通道**。检索同时限定 `project_root` 与 `source_ids`；展平/翻译块即使被召回也不得作为证据，进入 `unresolved`（`missing_page_metadata`）。
3. **先解析再生成**。每个命中块都必须经过与 `/api/literature/evidence` 相同的 `resolve_evidence` 路径（重新读文件、重新哈希、重新按页解析、坐标与块身份校验）。解析失败的块进入 `unresolved` 并保留子码（`artifact_hash_mismatch` / `stale_index` / `quote_mismatch` / `chunk_not_found` 等）。可用证据为 0 时**不调用模型**，直接返回 `insufficient`（`no_retrieval_hits` / `no_resolvable_evidence`）。没有来源标识的命中以 `missing_source_scope` 记为未解析，不静默丢弃。去重后的命中数不得超过 `top_k` 上限 20；超出属于检索实现违约，以 `answer_request_invalid` 显式失败，**不裁剪证据去凑答案**（裁剪可能丢掉唯一可核验的引文）。
4. **生成约束**。`temperature=0`；模型只能使用服务端下发的 `evidence_id` 白名单；输出必须是单个 JSON 对象 `{"claims":[{"text","evidence_ids","evidence_status"}]}`（允许整体包在一个 ```` ```json ```` 围栏内，不允许夹带解释性文字）。非 JSON、缺字段、类型错误、单条结论文本超过 20 000 字符、单条结论引用超过 100 个证据 ID、或响应体超过 200 000 字符 → 显式 `answer_invalid_response`，不换模型、不静默重试制造成功。结论文本中的控制字符在进入合同模型前被剥离。证据集合超出提示预算（总长或单条引文上限）时以 `answer_request_invalid` 显式失败。
5. **渲染前机器校验**。任一不通过即拒绝该结论并记入 `rejected_claims`：引用不在本轮白名单内（`unknown_evidence_id`）；结论文本出现的页码未落在其绑定证据的页码集合内（`fabricated_page_reference`，识别 `第 N 页`、`N 页`、`第 N 頁`、`第 A-B 页`、`p.N`/`p N`/`pg. N`/`pageN`、`pp. A-B`；**这是有限枚举的正则识别，不是通用页码解析**，模型用未枚举写法（如 `página 9`）仍可能绕过，故本规则只降低而不能消除该风险）；supported/conflicting 却没有任何 `evidence_ids`（`missing_evidence`）；模型自述证据不足（`model_reported_insufficient`）；空文本（`empty_claim_text`）；结论数超过上限 20（`claim_limit_exceeded`）。**被拒结论不得降级为"无引用的结论"进入结果。**
6. **结果一致性**。`claims[*].evidence_ids` 必须是 `evidence[*].evidence_id` 的子集，`evidence[*].source_id` 必须是请求 `source_ids` 的子集；完全相同的结论按 `claim_id` 去重。全部结论被拒或模型未给出结论时 `status=insufficient`。
7. **失败即失败**。模型不可用 → `answer_model_unavailable`(503)；调用异常或超时 → `answer_generation_failed`(502)；索引存储不可用 → `index_store_unavailable`(503)。`index_store_unavailable` 与 `retrieval_unavailable` 必须**穿透**证据解析循环向上抛出：向量库坏了不等于"语料答不了"，不得被折叠成 200 + `insufficient`；其余单块解析失败仍记为 `unresolved`。
8. **不缓存答案**。每次请求都真实执行检索与解析；`model_config_hash` 由 provider、模型名、base_url、temperature、max_tokens 与提示版本 `evidence_answer_v1` 规范化哈希得到，**不得包含任何密钥**。注意：请求体确实发送 `temperature=0`，但若所选 provider 处于思考模式（如 DeepSeek reasoning），其客户端会丢弃该参数——此时"temperature=0"只成立于请求层，不成立于模型行为层，报告中不得写成"模型采样被冻结"。
9. **证据展示（M9）**。`evidence` 条目必须携带 `source_id`、题名、`page_start`/`page_end`、`exact_quote`、`context_before`/`context_after`、`chunk_id` 与 `artifact_sha256`；前端不得渲染未出现在 `evidence` 中的引用，且必须显示 `rejected_claims` 与 `unresolved` 的条数与原因。

分层验收：

| 层 | 位置 | 必须证明 |
|---|---|---|
| 单元 | `python/tests/unit/test_literature_answer.py` | 提示构造只下发白名单；响应解析的每个失败码；五类拒绝原因各自的判定；无可用证据时不调用模型；结论去重与上限；结果一致性不变量 |
| 单元 | `python/tests/unit/test_literature_answer_model.py` | 适配器身份与 `model_config_hash` 的稳定性；密钥、代理与无关配置不进入哈希；Provider 返回文本与异常各自的处理 |
| 单元 | `python/tests/unit/test_literature_answer_service.py` | 服务编排：范围校验、检索与解析顺序、无证据时不调用模型、模型/响应失败码、拒绝结论不降级、相同请求不被缓存 |
| 单元 | `python/tests/unit/test_literature_answer_limits.py` | 边界与极限：超长问题、`source_ids` 上限与重复、`top_k` 越界、模型返回海量结论、超长结论文本、重复证据 ID、控制字符、并发同一项目问答、证据在回答中途被替换 |
| 单元 | `python/tests/unit/test_literature_router.py` | `/api/literature/answer` 的 HTTP 契约与错误码映射（真实项目存储） |
| 集成 | `python/tests/integration/test_literature_answer_integration.py` | 真实 ChromaDB + 真实 PDF：范围内检索 → 证据解析 → 注入确定性模型 → 结论与证据一致，且引文等于页文本切片 |
| 端到端 | `python/tests/integration/test_literature_evidence_e2e.py` | 真实应用 + 离线 fixture provider：检索 → 入库 → 全文（手工/自动）→ 页级索引 → **多文献问答** → 证据回链；无答案问题返回 `insufficient` 且 `claims` 为空；无范围查询 400 |

约束：三层都必须离线可跑；依赖缺失只能以显式 `skip` 表达；每层结果分别记录，不允许用低层结果替代高层结论。

本阶段明确不做：答案缓存、流式输出、rerank 与混合检索、多轮追问、跨项目问答（进入 P5 或 A1 之外的后续评估）。

分层结果（2026-09-29）：

- 单元层：`test_literature_answer.py` 67 例（提示只下发本轮白名单、响应解析的每个失败码、六类拒绝原因、结论去重与上限、无证据不调用模型）、`test_literature_answer_model.py` 16 例（身份与 `model_config_hash` 稳定、换密钥不改哈希、Provider 文本与异常）、`test_literature_answer_service.py` 34 例（范围校验、先解析再生成、模型/响应失败码、拒绝不降级、相同请求不被缓存、存储不可用不被降级为"证据不足"）、`test_literature_answer_limits.py` 19 例（问题长度、来源与 `top_k` 上限、检索实现违约、结论洪泛、控制字符、并发、附件在回答中途被替换）、`test_literature_router.py` 新增 6 例问答 HTTP 契约与 4 例"证据路由拒绝客户端引文/坐标"（400/404/422/502/503）。Python 单元回归为 `1788 passed, 5 skipped`（该数字为 P3 收口提交 `b856dcf` 上的实测值；例数为后续复核时用 `pytest --collect-only` 实测，早期本节误记为 43/30）。
- 集成层：`test_literature_answer_integration.py` 2 例（真实 ChromaDB + 真实 PDF + 出货 `RagPageRetriever`，仅替换模型）：范围内问答的每条证据回链到重新解析的页文本切片，且不引用范围外来源；未索引来源返回 `insufficient` 且不调用模型。
- 端到端层：`test_literature_evidence_e2e.py` 新增 1 例（真实应用 `create_app` + 离线 fixture provider + 经 `_create_provider` 注入的确定性答案模型）：检索 → 入库 → 附加 PDF → 页级索引 → 范围内问答 → 证据回链，且引文等于页文本切片；无范围问题返回 400 `scope_required`；语料无法回答的问题返回 `insufficient` + `model_reported_insufficient`，`claims` 与 `evidence` 均为空。
- 集成层在首轮暴露一个单元层无法发现的真实缺陷并已修复（D-032）：页级 chunk 只用内容身份作为集合存储键，同一份 PDF 在两个来源或两个项目下索引时会互相覆盖页元数据，跨项目隔离失效。修复后同一份 PDF 的两个来源在真实 ChromaDB 中各自保持可检索，回归断言写入集成层。
- 前端：`useLiteratureAnswer.test.ts` 13 例、`SourceLibraryEvidenceAnswer.test.ts` 4 例覆盖范围选择、请求体、结论与证据绑定、证据展开、拒绝与未解析提示、`insufficient` 不渲染结论、获取开放全文后重新读取库。前端全量为 `862 passed`，并通过 Prettier、ESLint、`vue-tsc` 与 Vite 生产构建。
- 全量后端为 `2952 passed, 14 skipped, 8 failed`；8 个失败仍全部是 §5.3–5.5 已记录且可在 `071c6b0` 复现的既存/环境问题，未出现本次改造引入的新失败。
- 本节结果只证明"回答中的每条结论都能机器校验回真实页码与精确原文，且失败与不足都有明确状态"，不证明检索质量、回答正确性或教学效果。

### 5.10 P4 演示与技术评测规格（2026-09-29，文档先行）

P4 阶段门是"在线与缓存模式均能完成同一演示脚本；失败不被伪装为成功"。本节在实现之前冻结 M10 的缓存路径、固定演示脚本、失败场景与运行记录。缓存语料只服务演示与回归，不充当方法协议中的效果评测语料（§6.2）。

影响文件：

| 类型 | 路径 | 职责 |
|---|---|---|
| 新增 | `config/literature_demo_corpus.json` | 固定公开论文包：稳定顺序的记录元数据 + 逐页文本 |
| 新增 | `python/src/literature/demo_corpus.py` | 读取固定语料、构造 `PaperRecord`、物化离线 PDF；缺文件即显式不可用 |
| 新增 | `python/src/literature/demo_run.py` | 演示运行记录：步骤、状态、原因、耗时、计数、模式 |
| 修改 | `python/api_factory.py` | 语料可用时把 `fixture` provider 注册进运行中的应用（M10） |
| 新增 | `python/scripts/literature_demo.py` | 固定演示脚本 CLI：按 §7.1 顺序驱动既有 HTTP API，并写运行记录 |
| 新增 | `python/tests/unit/test_literature_demo.py`、`python/tests/integration/test_literature_demo_e2e.py` | 语料、注册、运行记录与两种模式的一致性 |

M10 缓存路径规则：

1. 运行中的应用在固定语料可加载时，除 `ArxivProvider` 外再注册 `fixture` provider；语料缺失或损坏只记录警告并保持在线 provider 可用，**不得让应用启动失败，也不得让 fixture 冒充 arxiv**。语料定位与 `providers.yaml` 一致：打包后 `_MEIPASS/config/`，开发态仓库根 `config/`。
2. `SearchPage.result_mode` 必须保持 `live` / `fixture` 的区分，缓存结果不得标成在线结果（§0）。
3. 固定语料是**演示与回归**用途；正式语料的纳入、排除与授权规则仍由 `methods/literature_poc/METHODOLOGY.md` 冻结。

固定演示脚本（§7.1 的可执行版本）：按顺序执行并逐步记录——检索（只用调用方已确认的检索式）→ 选择全部返回记录并入库 → 获取全文（在线走开放 PDF，缓存走离线包）→ 建立页级索引 → 对选定文献提问 → 解析一条证据。每步记录 `status`（`ok` / `failed` / `skipped`）、`reason`、`duration_ms` 与计数。

调用方式与退出码（2026-09-29 补充，见 D-041）：CLI 必须先确认目标项目，再执行固定七步。

- 目标项目二选一：`--project-path <已存在项目>`，或 `--create-location <目录>` 配合 `--project-name`（脚本先调用既有 `POST /api/project/create` 再使用其返回路径）。两者都不给或都给出 → `project_target_required`，退出码 4，且**不构造客户端、不写记录**。
- `--project-path` 指向的不是项目目录（服务端返回非 200）→ `project_not_found`，退出码 4，不写记录；不得把它伪装成"入库失败"。
- 退出码：`0` 七步全部执行且没有**未声明**的失败；`1` 存在未声明失败；`2` 语料缺失/损坏；`3` HTTP 客户端无法构造（例如代理变量不可解析）；`4` 项目目标非法。
- 语料可**声明预期失败**：`demo.expected_failures` 是「步骤名 → 原因码」映射（本仓库语料声明 `acquire_fulltext: access_unavailable`，因为它不声明任何开放全文位置）。命中的步骤在记录里**仍然是 `failed`**，只额外带 `detail.expected="true"`，并因此不计入退出码 1。**声明不得改变记录的状态与原因**，只用于区分"预期失败"与"回归"。

失败场景（必须显式记录，不得静默跳过）：

1. 无开放全文的记录 → 该步 `failed` 且 `reason=access_unavailable`；脚本继续处理其余文献。
2. 问题在语料中没有答案 → 回答步仍为 `ok`，但 `answer_status=insufficient` 并带不足原因；"证据不足"是成功执行，不是检索失败。
3. provider 不可用或限流 → 记录 `failed` 与结构化错误码，不重试成"成功"。
4. 解析或索引失败 → 记录 `failed` 与原因，依赖它的问答步记为 `skipped`，**不得凭空回答**。

运行记录：

- 每次运行写一份 JSON 记录，至少含 `run_id`、`mode`（`live` / `fixture`）、`provider`、`project_path`（本次作用的项目，便于两份记录逐字段比对）、`confirmed_query`、`steps`、`totals`（step/ok/failed/skipped）、`started_at`、`finished_at`、`duration_ms`、`answer_status`、`answer_model_config_hash`（答案步实际使用的模型配置哈希，取自该步应答）、`git_commit`。
- 同一语料与同一确认检索式下重复运行必须产生**结构相同、逐字段可比**的记录（时间戳与 `run_id` 除外）；记录写入 `methods/literature_poc/runs/`（运行记录，不是方法协议）。由于 `run_id` 由内容派生、重复运行相同，同一秒内的两次运行必须靠文件名后缀区分，**不得互相覆盖**。
- 记录不得包含任何密钥：既拒绝密钥类**键名**，也拒绝以 `sk-`、`ghp_`、`Bearer ` 等前缀开头的**值**；`answer_model_config_hash` 沿用 §5.9 的无密钥口径。
- 四类失败场景都必须有独立原因码并使用户可读：`rate_limited`/`unavailable`（provider 失败 → 检索步 failed）、`access_unavailable`（无开放全文 → 获取步 failed 后仍继续）、`source_artifact_missing` 等（解析/索引失败 → 该步 failed，依赖步 `dependency_failed`）、`no_indexed_source`（确实没有可索引来源）。

分层验收：

| 层 | 位置 | 必须证明 |
|---|---|---|
| 单元 | `python/tests/unit/test_literature_demo.py` | 语料解析与稳定顺序、缺失/损坏语料的显式失败、fixture 记录的 `result_mode=fixture`、步骤状态与计数聚合、四类失败场景的原因码、记录中不出现密钥 |
| 集成 | `python/tests/integration/test_literature_demo_e2e.py` | 真实应用 + 真实 ChromaDB：在线模式（离线注入元数据 provider）与缓存模式（fixture provider）跑同一脚本，步骤结构一致，且两种模式都能得到带页码与精确原文的回答；失败场景以 `failed` / `insufficient` 记录而不是成功 |
| 端到端（真实服务器） | `python/tests/integration/test_literature_demo_live_server.py` | 真实 uvicorn 子进程 + 真实 HTTP + 真实 ChromaDB：CLI 用 `--create-location` 建项目后跑完七步并写出记录；非项目目录在跑任何步骤前以 `project_not_found` 退出 4 |

本阶段明确不做：服务端"一键演示"端点（D-034）、演示指标阈值（属于 §6.3 的候选指标，须由方法协议冻结）、把缓存结果包装成在线结果。

分层结果（2026-09-29）：

- 固定语料 `config/literature_demo_corpus.json`：3 条合成演示记录（标识统一 `demo-` 前缀、无 DOI/arXiv 编号、全部不声明开放全文位置），逐页正文用于离线演示；`provenance` 字段明确写出"合成演示语料，不得作为检索质量或领域覆盖证据"。
- 单元层 `python/tests/unit/test_literature_demo.py` 23 例：语料加载与记录顺序、每条记录都带逐页文本、标识不可能被误认成真实论文、缺失/损坏/版本不符/类型不符/空记录/引用不存在记录等六类结构错误、`fixture` provider 的 `result_mode` 与来源标签、离线 PDF 物化后可被重新解析回同一页文本、运行记录的步骤顺序与计数、`run_id` 只随结果变化、非 ok 步骤必须带原因、密钥类键被拒绝、记录文件名逐次运行唯一。
- 集成层 `python/tests/integration/test_literature_demo_e2e.py` 4 例（真实应用 `create_app` + 真实 ChromaDB，离线替换元数据 provider、下载器与答案模型）：`fixture` provider 确实注册进运行中的应用且 `result_mode=fixture`；同一脚本在缓存模式与在线模式跑出**相同的七步顺序**，缓存模式的 `acquire_fulltext` 记为 `failed/access_unavailable`（3 条记录都无开放全文）后由 `attach_fulltext` 走既有本地附加路径完成，在线模式则相反（获取成功、附加 `skipped/user_attachment_required`），两者最终都得到 `answered` 且证据回链到真实页码与坐标空间；重复运行产生逐字段可比、`run_id` 相同的记录；无答案问题记为 `answer=ok` + `answer_status=insufficient`，证据步 `skipped/no_evidence_to_resolve`。
- 回归影响：M10 让运行中的应用多出一个名为 `fixture` 的 provider，与既有端到端测试注入的 fixture 实现同名。应用侧改为"同名已存在时只记警告不再注册"，而不是让服务在构造时抛错；P2A/P2B/P3 的既有端到端用例因此未被削弱或跳过。
- 仍未完成：真实在线（arXiv）模式下的**Agent 会话**仍未自动化，按 D-024 只做一次性人工冒烟（arXiv 检索本身已于 2026-09-29 人工跑通，见 §5.13）；方法协议 `methods/literature_poc/METHODOLOGY.md` 目前是未生效骨架，因此"指标记录"只记录过程事实，不含任何效果指标；`methods/literature_poc/runs/` 当前只由 CLI 写入，集成测试写到临时目录。
- 2026-09-29 补充（D-041）：CLI 的目标项目确认、`--create-location`、退出码 0/1/2/3/4 与语料声明式预期失败已实现并有单元覆盖；`test_literature_demo_live_server.py` 另用真实 uvicorn 子进程把"CLI 在真实服务器上跑完七步"自动化，因此本节原来"CLI 未在真实服务器上运行"的缺口已关闭。真实**模型**的答案质量与 Agent 会话语义仍未验证。
- 全量后端为 `2979 passed, 14 skipped, 8 failed`；8 个失败仍是 §5.3–5.5 已记录且可在 `071c6b0` 复现的既存/环境问题，未出现 P4 改造引入的新失败。
- 本节结果只证明"同一演示脚本能在缓存与在线两种模式下跑完，且失败与不足如实记录"，不证明检索质量、回答正确性或教学效果。

### 5.11 A1 单 Agent 接入规格（2026-09-29，文档先行）

本节按 §2.4 与 D-031 冻结 A1 的工具层契约。A1 只在 M1–M10 与 P4 已验收的确定性服务之上增加"由 Agent 组织调用"的一层，**不新增第二套文献、索引或证据状态**。

影响文件：

| 类型 | 路径 | 职责 |
|---|---|---|
| 修改 | `python/src/agent_v2/tools/academic_tools.py` | 移除 `arxiv_search`；新增 `literature_search` / `literature_import` / `literature_answer` / `literature_sources`；`rag_search` 改为强制范围内 |
| 新增 | `python/tests/unit/test_literature_agent_tools.py` | 工具层契约：工作区范围、确认元数据、结构化结果、失败码 |
| 新增 | `src/components/AgentLiteratureEvidence.vue` | 把 `literature_answer` 的工具结果渲染成结论与页码级证据（见 §5.12） |
| 修改 | `python/tests/agent_v2/test_academic_tools.py`、`test_router_prompt.py` | 按 D-031 重写旧 arXiv 工具与工具清单用例 |

规则（冻结）：

1. **只包装确定性服务**。工具只调用 `/api/literature/search`、`/import`、`/fulltext`、`/index`、`/answer` 与范围内的 `/api/rag/query`；Agent 不保存文献身份、索引状态或证据坐标，也不自行解释 PDF。
2. **项目范围来自工作区**。`project_root` 一律取工具注册表的工作区根，**调用方不能通过参数指定别的项目**；需要项目范围的工具（`literature_import`、`literature_answer`、`rag_search`）在工作区缺失时以 `project_scope_unavailable` 显式失败，不得回退到无范围查询。`literature_search` 本身不携带项目范围，因此不需要工作区，也不发送任何项目路径。
3. **三个确认点复用既有审批契约**。`literature_search`（确认检索式）、`literature_import`（确认论文选择）、`literature_answer`（确认问题与文献范围）以 `approval_scope="exact-input"` 注册，Agent 的调用停在既有 `await_approval` / `approval_received` 上；用户批准的参数就是实际提交的参数。
4. **移除旧路径**。`arxiv_search`（原始 Atom XML 截断）与"无 `source_ids` 也能查"的 `rag_search` 一并移除；`rag_search` 仍可用于扁平文本库，但必须显式给出 `source_ids`，否则显式失败并要求改用范围内工具。
5. **结果必须可核验**。检索结果返回结构化字段（`paper_id`、题名、作者、年份、访问状态、`result_mode`）；入库返回 `created/reused` 计数；应答返回 `status`、`insufficient_reason`、每条结论与 `evidence`（题名、页码、精确原文、`evidence_id`）。证据不足时工具只返回不足状态与原因，**不得被模型改写为肯定回答**。
6. **失败与上限沿用既有语义**。服务端错误码原样进入工具结果并标注 `is_error`；调用上限与停止条件使用 Agent V2 现有机制，工具层不额外重试或降级。

分层验收：

| 层 | 位置 | 必须证明 |
|---|---|---|
| 单元 | `python/tests/unit/test_literature_agent_tools.py` | 三个工具的注册元数据（`approval_scope=exact-input`、网络范围、权限）；工作区缺失即 `project_scope_unavailable`；`project_root` 恒为工作区而非入参；请求体字段与确认参数一致；结构化结果字段；服务端错误码进入 `is_error`；`rag_search` 缺 `source_ids` 时显式失败；`arxiv_search` 已不存在 |
| 单元 | `python/tests/agent_v2/test_academic_tools.py` | 重写后的工具清单与既有网络/路径安全用例；不含对原始 arXiv 响应的断言 |

本阶段（A1 后端工具层）明确不做：Agent 侧的前端计划/确认/引用展示改造、连续追问的会话级编排、以及真实在线 arXiv 的自动化运行。这些仍属于 A1 未完成部分，必须在报告中与已完成的工具层分开陈述。

分层结果（2026-09-29，仅 A1 工具层）：

- 新增 `literature_search` / `literature_import` / `literature_answer` / `literature_sources` / `literature_acquire_fulltext` / `literature_index`：分别包装 `/api/literature/search`、`/import`、`/answer`、`/api/project/sources`、`/fulltext`、`/index`；五个副作用工具以 `approval_scope="exact-input"` 注册（另有 `network_scope`），因此检索式、论文选择、全文获取、索引与问答范围都停在既有 `await_approval` / `approval_received` 契约上；工具 schema 中**没有** `project_path` / `project_root` 参数。
- 移除 `arxiv_search`（原始 Atom XML）与无范围 `rag_search`：`rag_search` 现在必须显式给出 `source_ids`，并以工作区为 `project_root` 发送 `project_scoped=true`；缺工作区时 `literature_import` / `literature_answer` / `rag_search` 以 `project_scope_unavailable` 显式失败，而 `literature_search` 本身不含项目范围、不发送任何项目路径。
- 工具结果结构化：检索返回 `result_mode`、`search_execution_id`、`total_results` 与每条记录的 `paper_id`/题名/作者/年份/访问状态；入库返回 `created`/`reused` 计数；应答返回 `status`、`insufficient_reason`、结论及其证据（题名、页码、精确原文）与未解析计数。服务端错误码（如 `rate_limited`、`source_not_found`）原样进入 `is_error` 结果，不重试、不降级。
- 单元层 `python/tests/unit/test_literature_agent_tools.py` 19 例覆盖上述全部规则，包括"入参伪造 `project_path` 不生效""入库缺执行句柄/空选择被拒""应答缺 `source_ids` 或空问题被拒""`arxiv_search` 已不存在"。
- 按 D-031 重写的既有用例：`tests/agent_v2/test_academic_tools.py` 的 arXiv 工具用例改为结构化服务用例；`tests/agent_v2/test_router_prompt.py` 的工具自省与 skill 工具契约表改用 `literature_search` / `literature_answer`；`runtime/conversation.py` 的选择期安全工具集与 `skills.py` 的 nature_citation 文本同步更新。`tests/agent_v2/` 全量为 `843 passed`（该目录单独运行；早期本节误把"该目录 + 新工具测试文件"合计的 860 写成目录全量）。
- 全量后端为 `2998 passed, 14 skipped, 8 failed`；8 个失败仍是 §5.3–5.5 已记录且可在 `071c6b0` 复现的既存/环境问题，未出现 A1 工具层改造引入的新失败；前端全量为 `862 passed` 并通过 Prettier、ESLint 与 `vue-tsc`。
- **仍未完成（A1 剩余部分）**：Agent 前端侧的计划展示、确认交互、状态与引用渲染未改造（`AgentExecutionGroup.vue` 只补了工具名到标签的映射：`literature_search` → 检索项目文献、`literature_answer` → 基于证据回答）；连续追问的会话级编排、证据不足时由 Agent 主动补充范围内检索或提出新检索计划的策略、以及 §2.4 八项验收场景的会话级覆盖都还没有实现或测试。**因此 A1 未通过阶段门，不能报告"智能体版本已完成"。**
- 本节结果只证明"Agent 只能通过确定性服务、在项目范围内、经用户确认地访问文献与证据"，不证明检索质量、回答正确性、教学效果或 A1 整体验收通过。

### 5.12 A1 会话与前端规格（2026-09-29，文档先行）

§5.11 只覆盖了工具层。本节冻结 A1 余下部分：会话侧的计划可见性、确认不可绕过、证据不足后的下一步、Agent 可驱动全文与索引，以及前端引用展示。

影响文件：

| 类型 | 路径 | 职责 |
|---|---|---|
| 修改 | `python/src/agent_v2/tools/academic_tools.py` | 新增只读的 `literature_sources` 与副作用工具 `literature_acquire_fulltext` / `literature_index`；`literature_answer` 在证据不足时返回 `next_actions` |
| 新增 | `python/tests/integration/test_agent_literature_session_e2e.py` | 真实运行时的会话级验收：计划 → 确认 → 全链执行 → 引用；取消、越界与重复索引 |
| 新增 | `src/components/AgentLiteratureEvidence.vue` | 把 `literature_answer` 的结果渲染成结论与证据卡片 |
| 修改 | `src/components/AgentExecutionGroup.vue` | 命中文献应答结果时改用证据渲染器，其余工具保持原样 |
| 修改 | `python/tests/unit/test_literature_agent_tools.py`、`src/__tests__/` | 覆盖新增契约 |

规则（冻结）：

1. **计划可见**。Agent 在检索前必须能列出当前项目的文献范围与索引状态：`literature_sources` 返回每条来源的 `source_id`、题名、年份、`rag_status`、全文状态、`original_path` 是否存在与 `paper_id`；对已经建过索引的来源，工具结果显式标注 `already_indexed=true`，使"已有索引下跳过重复处理"是**读得到的事实**而不是模型的猜测。`already_indexed` 必须由服务端写入的 literature 元数据（全文状态为 `indexed` 或存在 `index` 记录）判定，**不得**用工作区 UI 字段 `rag_status` 判定——该字段由前端在索引后自行更新，服务端索引成功时它仍是 `unavailable`。
2. **确认不可绕过**。`literature_search` / `literature_import` / `literature_acquire_fulltext` / `literature_index` / `literature_answer` 的 `effects` 必须包含 `network`，使 `ToolSpec.requires_approval` 为真；即使会话开启了自动批准，工具结果也必须在文本中回显实际提交的检索式、`paper_ids`、`source_ids` 与 `search_execution_id`，让执行记录可复核。
3. **证据不足必须改变下一步**。`literature_answer` 在 `status=insufficient` 时返回 `next_actions`（`widen_scope_within_project` / `propose_new_query_for_confirmation`）与 `must_not_answer_from_memory=true`；工具描述同时写明"证据不足时不得凭记忆或常识作答"。`status=answered` 时不返回 `next_actions`。
4. **Agent 可走完整条链**。除检索、入库与问答外，Agent 还能通过 `literature_acquire_fulltext` 与 `literature_index` 获取开放全文并建立页级索引；两者只调用既有服务接口，全文不可获取时返回 `access_unavailable` / `acquire_failed`，Agent 必须转告用户改用本地 PDF，不得声称已下载。
5. **前端引用展示**。`literature_answer` 的工具结果在会话中渲染为结论列表，每条结论可展开其证据（题名、页码、精确原文、上下文）；`status=insufficient` 时只显示不足原因、不显示任何结论；结果不是合法 JSON 时回退到原始文本，**不得隐藏或改写工具结果**。
6. 其余工具（含 `literature_search` / `literature_import`）继续由现有 `AgentExecutionGroup` 渲染，不新增平行面板，也不改变既有 SSE 事件名与 `tool_name` 契约。

分层验收：

| 层 | 位置 | 必须证明 |
|---|---|---|
| 单元 | `python/tests/unit/test_literature_agent_tools.py` | `literature_sources` 的工作区范围与字段、`already_indexed` 只由服务端索引状态判定、无工作区即失败；五个确认工具的 `requires_approval` 为真；`literature_acquire_fulltext` / `literature_index` 的请求体与失败码；`next_actions` 只在 `insufficient` 时出现；结果回显提交的检索式/论文/范围 |
| 会话 | `python/tests/integration/test_agent_literature_session_e2e.py` | 真实 `ConversationRuntime` + 真实工具 + 真实应用（工具 HTTP 由 ASGI 直连）：脚本模型按真实工具结果规划完整链路，**每一步副作用调用都停在审批上且顺序一致**，最终得到带页码与精确原文的结论；拒绝审批时检索从未到达服务；越界 `source_id` 被拒；第二轮看到 `already_indexed` 后不再重建索引 |
| 前端 | `src/__tests__/AgentLiteratureEvidence.test.ts` | 结论与证据渲染、证据展开与收起、`insufficient` 只显示原因、非 JSON 结果回退为原文、无证据的结论不产生空白卡片 |

本阶段仍不做：多轮追问的自动策略引擎（由模型依据工具反馈决定）、真实在线 arXiv 的自动化运行、以及把 Agent 会话历史作为证据来源（证据只能来自 §5.9 的服务）。

分层结果（2026-09-29，A1 会话与前端部分）：

- 新增只读 `literature_sources`：返回工作区项目的文献范围与状态（`source_id`、题名、年份、`rag_status`、`fulltext_status`、`has_fulltext`、`is_literature`、`paper_id`），并标注 `already_indexed`；"已有索引下跳过重复处理"因此是工具结果里读得到的事实。它不声明 `effects`，所以不会被审批打断（只读项目元数据）。
- 新增副作用工具 `literature_acquire_fulltext` 与 `literature_index`，使 Agent 能走完"检索 → 入库 → 获取开放全文 → 建立页级索引 → 证据问答"整条链；两者只调用既有服务接口，`access_unavailable` / `acquire_failed` 等失败码原样返回，工具描述要求失败时转告用户改用本地 PDF，不得声称已下载。
- 确认不可绕过由注册契约保证：五个副作用工具的 `effects` 含 `network`，`ToolSpec.requires_approval` 为真，运行时在既有 `await_approval` 上暂停；即便会话开启自动批准，结果也会回显实际提交的检索式、`search_execution_id` + `paper_ids`、`question` + `source_ids`。
- 证据不足必须改变下一步：`literature_answer` 在 `status=insufficient` 时额外返回 `next_actions=["widen_scope_within_project","propose_new_query_for_confirmation"]` 与 `must_not_answer_from_memory=true`，`answered` 时不返回这两个字段；工具描述写明"证据不足时不得凭记忆或常识作答"。
- 会话级验收 `python/tests/integration/test_agent_literature_session_e2e.py` 6 例：真实 `ConversationRuntime` + 真实 `literature_*` 工具 + 真实应用（工具的 `SCHOLAR_API_BASE` 调用由 `httpx.ASGITransport` 直连同一应用，不启服务、不出网）。脚本模型只依据**真实工具结果**规划下一步，断言包括：该链路全部 9 次副作用调用（检索、入库、3×获取全文、3×建索引、应答）逐一停在审批上且顺序与数量与导入结果一致；首个工具是 `literature_sources` 且初始 `source_count=0`；最终 `literature_answer` 为 `answered` 且每条证据都有真实页码与精确原文，并与直接调用 `/api/literature/answer` 的结论 ID 与证据（ID/页码/精确原文）逐项相等；结果经 `agent_event_to_sse` 转换后 `result_detail` 仍可 `json.loads` 且含证据（证明 SSE 边界不额外截断）；拒绝审批时**由计数传输层实测**只有 `/api/project/sources` 到达应用、检索从未发出；越界 `source_id` 被服务以 `source_not_found` 拒绝；第二轮读到本轮所建来源的 `already_indexed=true` 后不再调用 `literature_index` 或 `literature_acquire_fulltext`；**证据不足时按 `next_actions` 在同一项目内扩大 `source_ids` 重问并得到 `answered`**；**同一会话的追问只调用 `literature_sources` + `literature_answer`，不重新检索或重建索引**。
- 会话级测试暴露一个真实缺陷并已修复：`literature_sources` 最初用工作区 UI 字段 `rag_status == "ready"` 判定 `already_indexed`，而该字段由前端在索引后自行更新——服务端索引成功时它仍是 `unavailable`，于是第二轮会重复索引。现在判定改为服务端写入的 literature 元数据（全文状态 `indexed` 或存在 `index` 记录），并有单元用例固化"UI 字段为 unavailable 但服务端已索引时 `already_indexed` 必须为真"。
- 前端引用展示：新增 `AgentLiteratureEvidence.vue`（结论 → 可展开的页码级证据；`insufficient` 只显示原因；非合法 JSON 回退原文）；`AgentExecutionGroup.vue` 仅在工具名为 `literature_answer` 时改用该渲染器，SSE 事件名与 `tool_name` 契约未改动。
- 测试与检查：`test_literature_agent_tools.py` 扩展到 41 例；会话级 6 例；前端 `AgentLiteratureEvidence.test.ts` 7 例与 `agentExecution.test.ts` 渲染分流 1 例（该文件改为保留真实 `createI18n`，使组件内 i18n 回退可被真实语言包校验），另有 i18n 重复键守卫 2 例。全量后端最终为 `3068 passed, 14 skipped, 0 failed`（§5.13 的 V-14/V-15 修复后；本小节提交时为 `3046 passed, 14 skipped, 8 failed`，其中 8 例失败已分别定位并修复）；前端全量为 `874 passed`，Prettier、ESLint、`vue-tsc` 全部通过。
- 工具失败与调用上限沿用 Agent V2 既有机制，未新增独立限制：连续工具错误上限由 `_DEFAULT_MAX_TOOL_ERRORS`（选择期 `_SELECTION_MAX_TOOL_ERRORS`）控制，回归用例为 `tests/agent_v2/test_conversation_runtime.py::test_tool_error_limit_counts_consecutive_failures`；停滞调用上限由 `max_stalled_tool_calls` 控制。
- **仍未完成（A1 剩余部分）**：真实在线 arXiv 下的 Agent 会话未自动化（仍按 D-024 只做一次性人工冒烟）；`methods/literature_poc/METHODOLOGY.md` 仍未建立；多轮追问与"证据不足后补检索"由工具反馈驱动，会话级用例证明了**脚本模型按提示行动时链路成立**，但**没有**证明任意真实模型都会照做（这属于模型行为，不是确定性的系统保证，不得写成系统保证）。§2.4 的八项验收场景现已全部有对应覆盖：计划与确认、跳过重复处理、证据不足后补检索、用户取消、工具失败与调用上限（沿用既有回归）、跨项目与未选文献拒绝、最终证据与普通服务入口一致（同一 `/api/literature/answer` 响应）。
- 本节结果只证明"Agent 能读到项目范围、确认不可绕过、证据不足有明确的下一步、引用在界面上可展开核对，且整条链在会话中真的跑通"，不证明检索质量、回答正确性、教学效果或 A1 整体验收通过。

### 5.13 独立复核与订正（2026-09-29）

本节记录一次针对 §5.9–§5.12 的独立复核：两路只读复核（后端证据链、前端与 A1 各一路）+ 一次自测式复核。复核**发现并修复了真实缺陷**，也**订正了本文档自身的错误数字**。

**验证方法（可复现）**

- 全量后端：`cd python && pytest tests/ -q` → **`3097 passed, 14 skipped, 0 failed`**（含真实 uvicorn 子进程的 CLI 用例；V-14/V-15 修复前为 `3046 passed, 14 skipped, 8 failed`）。
- "那 8 个失败是既存问题"：`git worktree add <tmp> 071c6b0` 后在干净基线单跑这 8 个用例 → `8 failed, 17 passed`，失败集合与修复前逐项相同。**该说法已独立复现，不再只继承自 §5.5 的记录**；其中 5 例（多文章拆分）由 V-15 修复、3 例（`NO_PROXY` 解析）由 V-14 修复，现全部通过。§5.1–§5.12 各阶段记录中的失败数保持不变，它们是当时的真实观测。
- P3 历史快照：`git worktree add <tmp> b856dcf` 后 `pytest tests/unit/ -q` → `1788 passed, 5 skipped`，与 §5.9 一致。
- 例数：`pytest --collect-only -q <file>` 逐文件实测（参数化用例按实际收集数计）。
- 前端：`npx vitest run` → `874 passed`；Prettier、ESLint、`vue-tsc`、生产构建通过。

**复核发现并已修复的缺陷**

| # | 缺陷 | 影响 | 修复 | 回归测试 |
|---|---|---|---|---|
| V-1 | 工具结果默认上限 4000 字符，SSE 适配层又 `out[:4000]`；多文献答案（≥2 条页级证据）被截断成非法 JSON | 前端按 §5.12 规则回退成原始文本，"结论 + 可展开证据"在真实规模下静默失效；截断同时进入模型上下文 | 新增按工具的输出预算（`ToolSpec.max_output_chars`，默认仍 4000），`literature_answer` 用 24000；适配层只保留 32000 硬上限，不再二次截断 | `test_literature_agent_tools.py::TestAnswerPayloadBudget`（3×1330 字引文，断言未截断且可 `json.loads`）、`test_sse_adapter.py`（9000 字透传 / 40000 字按硬上限）、组件与 `agentExecution` 的真实 SSE 形状用例、会话 e2e 的 `agent_event_to_sse` 往返断言 |
| V-2 | "证据不足不渲染结论"只依赖服务端不变量，前端两个渲染点无条件透传 claims | 一旦服务端或缓存返回带 claims 的 `insufficient`，界面会把它当正常结论渲染，且没有测试会失败 | `useLiteratureAnswer` 的 claims/evidence 计算属性与 `AgentLiteratureEvidence` 的解析结果都按 `status === 'answered'` 收敛 | 两处 fixture 改为**故意违约**（insufficient + 非空 claims/evidence）；已实测"去掉门禁即变红" |
| V-3 | `index_store_unavailable` / `retrieval_unavailable` 在问答路径被折叠成 `unresolved` + 200 `insufficient` | 客户端无法区分"语料答不了"与"向量库坏了"，§5.9 规则 7 的错误码在该路径不可达 | 服务在证据解析循环中对这两个码直接上抛 | `test_literature_answer_service.py`（参数化两码） |
| V-4 | 页码伪造检测只识别 4 种写法，`见 9 页`、`第 9 頁`、`pg. 9`、`(p 9)`、`page9` 均可绕过 | 模型可用未枚举写法带出伪造页码 | 扩展为 6 类模式（含裸 `N 页/頁`、`pg`、无空格形式），并把超长数字段留给范围模式，使"荒谬范围"仍判为不可核验 | 新增 6 个正例 + 原有反例 + 荒谬范围回归 |
| V-5 | P4 运行记录缺 `answer_model_config_hash`；"不得含密钥"只查键名；同秒重复运行会覆盖同名记录 | 记录与密钥口径不一致，重复运行可能丢记录 | 记录新增该字段；密钥检查扩展为键名 + 值前缀；写文件遇同名自动加后缀 | `test_literature_demo.py` 三例 |
| V-6 | P4 四类失败场景只有两类有测试；`index` 步把真实失败码盖成 `no_indexed_source` | §5.10 承诺的失败原因码未被验证，真实原因会被掩盖 | 脚本改为优先报真实失败码；补 provider 429、索引 409、无开放全文三条用例 | `test_literature_demo.py::TestDemoRunFailureScenarios` 三例 |
| V-7 | 会话 e2e 两处断言不可证伪：拒绝用例只查运行时自造文本；复用用例的 `all(... if rag_status == "ready")` 恒真（服务端从不写该值） | "检索从未到达服务"与"跳过重复索引"实际未被验证 | 用计数传输层实测到达应用的路径集合；改用本轮真实建立的 `source_id` 集合断言 `already_indexed` | `test_agent_literature_session_e2e.py` |
| V-8 | `/api/literature/evidence` 拒绝客户端引文/坐标这条承诺没有测试 | 承诺无覆盖 | 补 4 例参数化负例（引文、证据 ID、字符坐标、页码坐标均 422） | `test_literature_router.py` |
| V-9 | Agent 侧证据投影缺 `chunk_id` 与 `artifact_sha256` | 会话内引用无法就地回链校验，与 §5.9 规则 9 的字段口径不一致 | 投影补齐两字段 | 既有工具测试覆盖字段集合 |

**已订正的文档错误**

- §5.9 单元例数：`test_literature_answer.py` 43 → **67**；`test_literature_answer_service.py` 30 → **34**。前者自首个提交起未变，属**当时写错**，不是时间漂移；其余例数（16/19/23/39）实测一致。
- §5.11 `tests/agent_v2/` 全量 860 → **843**（860 是"该目录 + 新工具测试文件"的合计）。
- §5.12 "七类副作用调用" → **9 次**（检索 + 入库 + 3×获取 + 3×建索引 + 应答），与实际断言一致。
- D-029/§5.9 规则 5 的页码格式清单补全，并明确"有限枚举、非通用解析"。
- §5.9 规则 8 补注：思考模式 provider 会丢弃 `temperature`，"temperature=0"只成立于请求层。

**复核确认成立的部分**（不重复证据）：坐标派生引文且调用方不能提交引文/坐标、项目与文献双范围强制、无可用证据不调用模型、被拒结论不降级、D-032 存储键与内容身份分离、运行中应用注册 `fixture` provider 且 `result_mode` 不混用、五个副作用工具 `requires_approval` 为真且项目范围不可由入参覆盖、`already_indexed` 由服务端元数据判定、`next_actions` 仅在 `insufficient` 出现、前端 i18n 键对称与分组正确、SSE 事件名与 `tool_name` 契约未被改动。

**本轮（2026-09-29 第二次复核后）进一步修复**

| # | 项 | 处理 |
|---|---|---|
| V-10 | 页码伪造检测可被未枚举写法绕过 | 定位词表扩展为多语言（中/英/西/德/日/葡/意/法）并保留裸 `N 页/頁`；仍明确是有限枚举 |
| V-11 | 思考模式下 `temperature` 被 provider 丢弃，"temperature=0"名不副实 | 答案模型持有独立 provider 实例，构造时经 `force_deterministic_thinking` 固定为非思考模式（3 例单测）；文档同步该口径 |
| V-12 | CLI 自身逻辑（退出码、记录落盘）无自动化 | `main()` 支持注入客户端；新增"成功=0 且写记录""失败步=1""语料缺失=2 且不建客户端"三例 |
| V-13 | P4 运行记录缺可比较的模型身份 | `answer_model_config_hash` 写入记录并在 CLI 用例中断言 |
| V-14 | **`NO_PROXY` 含 `[::1]` 使 httpx 报 `Invalid port: ':1]'`**，导致 3 个与文献无关的用例失败 | 根因定位后，在 `python/tests/conftest.py` 中丢弃 httpx 无法解析的方括号 IPv6 条目（保留其余绕过项）；3 例转为通过 |
| V-15 | **多文章拆分漏检"首字母被单独成行"的边界**（`I` + `n 2023` / `nflammation`）：检测器只认"空行 + 截断开头"，该形态无空行 | `src/parser/article_detector.py` 新增策略 C：孤立首字母 + 已知截断片段且二者拼回即为该词时才判为边界，并在切分后修复首词（`I`+`n` → `In`）。4 例合成单测（含两个反例）+ 真实样例集成测试 21 例通过 |

V-14 与 V-15 是本次复核顺带修掉的**与文献链路无关的既存缺陷**，此前一直被记为"环境问题"与"既存失败"；两者现在都有可复现的测试证据。

**第三轮（2026-09-29 真实服务器验证后）新增**

| # | 项 | 处理 |
|---|---|---|
| V-16 | `NO_PROXY` 含方括号 IPv6 时 httpx **在构造客户端时就抛异常**，真实服务器上的 CLI 直接以 traceback 退出（不只是测试失败） | 抽出 `src/net_env.py::normalize_proxy_env()`，在三个入口统一调用：`api.py`、`api_factory.create_app()`（服务端所有 httpx 客户端）与演示 CLI（另加 `client_unavailable` → 退出码 3 的可读错误）；`tests/conftest.py` 改用同一实现（6 例单测） |
| V-17 | 演示 CLI 指向非项目目录时，失败被归因成"存档导入失败"，不可操作 | CLI 先做项目预检，`project_not_found` → 退出码 4 且不写记录；新增 `--create-location`/`--project-name` 走既有 `POST /api/project/create`，使离线演示可一条命令跑通 |
| V-18 | 没有任何模型时整条链无法离线演示 | 新增 D-040 的确定性抽取式 fixture 答案模型（显式开关、身份可区分、不读问题、只抽取引用内原文），16 例单测 |
| V-19 | fixture 语料天然无开放全文，导致演示永远以退出码 1 结束 | 语料可声明 `demo.expected_failures`；命中时记录仍是 `failed/access_unavailable`，只加 `detail.expected="true"`，退出码按"是否存在未声明失败"判定（含"声明不改变状态"与"未声明失败仍为 1"两例单测） |
| V-20 | 运行记录缺少本次作用的项目，两份记录无法逐字段比对 | 记录新增 `project_path` |
| V-21 | "CLI 只在真实服务器上人工跑过" | 新增 `test_literature_demo_live_server.py` 2 例：真实 uvicorn 子进程 + 真实 HTTP + 真实 ChromaDB，`--create-location` 跑完七步并写出记录（退出码 0），非项目目录在跑任何步骤前退出 4 |

**第四轮（2026-09-29 真实 DeepSeek 模型验证后）新增**

这一轮的缺陷全部由"真实模型 + 真实论文"暴露，且都能在离线测试里复现：

| # | 项 | 处理 |
|---|---|---|
| V-22 | 官方把 V4.1 Flash 的模型名改为 `deepseek-flash`（旧的 `deepseek-v4-flash` 已下线但仍可调用），而仓库"V4 系列默认关思考以约束延迟"的策略与 `reasoning_effort` 开关只匹配 `deepseek-v4-`，改名后**静默失效** | 两处共用同一个"V4 系列前缀"常量，同时覆盖官方名与旧别名；12 例策略测试（含 `deepseek-chat`/`deepseek-reasoner` 必须保持 provider 默认的反向用例） |
| V-23 | **高**：思考模式下答案调用稳定返回 `answer_invalid_response`(502)。日志显示 `finish=length, text_len=0` —— 思考 token 吃完了 `max_tokens=2048`，正文为空 | 按官方 JSON Output 文档改用 `response_format={"type":"json_object"}`（仅答案调用、仅当 provider 支持；Agent 循环不受影响），输出预算提到 8192 并支持 `literature.answer.max_tokens` 覆盖。真实模型下同一问题由"稳定 502"变为"稳定 6 条结论" |
| V-24 | 空响应被当成"模型返回了不可解析的文本"，操作者拿不到真因（截断？拒答？） | 适配器对空正文抛出带 `stop_reason` 的显式错误；HTTP 错误体开始携带 `details`（解析原因、provider 停止原因），使 502 可诊断而不是死路 |
| V-25 | 四个 D-040 决策要求的"fixture 与 live 可区分"在真实链路里没有端到端证据 | §5.14 记录真实运行：`result_mode` 分别为 `live`/`fixture`，答案身份固定 `deepseek-flash`，两种模式都被真实模型跑通 |
| V-26 | 真实模型会**凭空指定 provider 名**（实测要 `semantic_scholar`，而注册的只有 `arxiv`/`fixture`），错误信息也不告诉它有哪些可用 | 新增只读工具 `literature_providers` 列出注册表；`literature_search` 在 `provider_not_found` 时把可用 provider 名附在错误里；工具 schema 明确说明 `arxiv`=在线、`fixture`=离线演示语料。真实模型在下一轮会话中确实先调用了该工具再检索 |

**仍未验证 / 已知限制**

- 真实**模型**的行为目前只有**一个模型、一套配置**的观测记录（§5.14：DeepSeek `deepseek-flash`、思考模式开启、2026-09-29）：它证明了"真实模型在真实接口下能按契约走完并如实报告不足"，**不构成**对其他模型、其他 provider 或"任意模型都会照做"的保证，也不是质量指标。换模型后必须重新观测。
- 真实**在线 arXiv** 已按 D-024 做一次性人工冒烟（2026-09-29），并在 §5.14 观测 2/4 中与真实模型一起跑通了"检索 → 入库 → 获取开放 PDF → 页级索引 → 证据问答"；这仍是人工观测，**不是**自动化用例，也不构成任何质量指标。
- 页码伪造检测仍是**有限枚举**的定位词表（中/英/西/德/日/葡/意/法），未收录语言的定位词（如 `página` 之外的写法）仍可绕过。
- CLI 的七步已在真实 uvicorn 服务上自动化（V-21），但**真实在线模式下的 Agent 会话**仍是人工步骤（§5.14 观测 4）。
- **Agent 没有附加本地 PDF 的工具**：离线语料下 Agent 无法独自走完整问答（§5.14 观测 6）；需要在界面或 CLI 先附加全文。
- `methods/literature_poc/METHODOLOGY.md` 目前只是**未生效的骨架**：正式 RQ、语料、gold、指标与阈值均标注【待确认】，需用户与指导教师填写后才成为协议（§6.2 与 D-039；§5.10、§5.12 记录中的"仍未建立"指当时的提交状态）。

### 5.14 真实模型验证记录（DeepSeek V4.1 Flash，2026-09-29）

一次性人工验证：真实 DeepSeek API（官方现行模型名 `deepseek-flash`，思考模式**开启**、effort 默认 `high`），凭据写在 gitignore 的 `python/config/default.local.yaml`（不提交、不打印）。**这不是自动化用例，也不构成任何质量指标**；它验证的是"真实模型在真实接口下会不会按契约行事"。

复现方式：对运行中的服务执行 `python scripts/literature_live_check.py --provider fixture|arxiv --query ... --question ...`。该脚本走检索 → 入库 → 获取/附加全文 → 页级索引 → 问答，并打印结论、证据页码与原文、被拒结论与未解析项；退出码 0=answered、1=insufficient、2=服务或传输失败。

**观测 1：答案契约（离线语料）** —— `status=answered`，5 条结论全部 `supported` 且各带 `evidence_ids`；证据解析到 p.1/p.2 的真实页码与原文；`unresolved=[]`；答案身份 `openai_compatible/deepseek-flash`。模型还给出了跨文献判断（"两篇论文并不共享同一套证据校验协议"）并同时引用两条证据。

**观测 2：真实论文（arXiv live）** —— 检索 `all:"retrieval augmented generation"` → 2 篇真实论文 → 自动获取开放 PDF（`2411.18583`、`2502.00306`）→ 页级索引 6 页 / 27 页 → `answered`，6 条结论全部 `supported`，证据落在 p.2/p.4/p.12/p.16，内容含 ROUGE、SciTLDR、TREC-COVID 等真实细节。**其中 1 条结论被拒**：`model_reported_insufficient`（模型自己写"证据不支持两篇共享同一协议"），按规则进 `rejected_claims` 而未被当作结论渲染——说明校验器在真实模型输出上确实会触发。

**观测 3：证据不足路径** —— 问一个语料答不出的问题（钨的熔点）→ `status=insufficient`、`insufficient_reason=model_reported_insufficient`、`claims=[]`、`evidence=[]`；模型明确说明所列证据没有涉及该问题，**没有编造**。

**观测 4：A1 真实会话（模型自己决定工具顺序）** —— 真实 `ConversationRuntime` + 真实 DeepSeek + 真实 arXiv：模型依次调用 `literature_sources` → `literature_providers` → `literature_search` → `literature_import` → `literature_acquire_fulltext`×2 → `literature_index`×2 → `literature_answer`；**9 次副作用调用全部停在 `await_approval`**（共 7 次审批）；最终 `literature_answer` 返回 `answered`（6 条结论），最终回复按页码给出原文引用。

**观测 5：行为边界（是模型的选择，不是系统保证）** —— 同一会话中模型曾对同一检索式连发 9 次检索，并从 `arxiv` 与 `fixture` 两个 provider 混合入库（记录里 `result_mode` 仍分别标注，未混淆）；对 arXiv 上不存在的检索式（`all:"evidence traceable question answering"` → 0 条）它如实停下、列出未执行的步骤并说明证据不足。换模型或换提示词，这些行为都可能不同。

**观测 6：离线语料在 Agent 会话中的缺口** —— fixture 语料不声明开放全文，而 Agent 侧**没有"附加本地 PDF"的工具**：模型按流程走到 `literature_index` 会得到 `source_artifact_missing`，最终只能如实报告证据不足（它确实这么做，并去工作区找过 PDF）。因此**离线演示要走完整问答，需先由用户或 CLI 附加 PDF**；这是已记录的产品边界，不是模型幻觉，也不在 A1 的既定范围内。

**本轮由真实模型暴露并修掉的缺陷**见 §5.13 的 V-22…V-26。最关键的一条：`answer_invalid_response` 的真因不是"模型不听话"，而是思考模式耗尽 `max_tokens=2048`（`finish_reason=length`、`content` 为空）；按官方 JSON Output 文档改为 `response_format={"type":"json_object"}` 并把输出预算提到 8192 后，同一问题稳定返回 6 条结论。

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

截至 2026-09-29，该文件已存在但**尚未生效**：`methods/literature_poc/METHODOLOGY.md` 目前是按上述清单搭好的骨架，每一项取值都标注【待确认】（决策 D-039）。它**不是**协议，不得据以开展或报告效果实验；只有用户与指导教师填入取值并确认后，该文件才产生约束力。

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
| D-020 | 证据坐标空间固定为 `normalized_page_text_v1`（统一换行、行内空白折叠、空行折叠、页首尾裁剪）；页内切块默认 1400/180 且不跨页；`chunk_id` 由 artifact SHA-256、页码、字符坐标与 chunker 版本派生；索引指纹由 artifact SHA-256 与 parser/chunker/embedding/index 版本派生；证据引文只能从坐标派生，调用方提供的引文一律不采信 | 本文冻结（2026-09-22） | 冻结归一化规则才能让"精确原文"可机器复算；派生引文与派生块身份使页码或引文无法被伪造，版本进入指纹使配置变化自动作废旧索引 |
| D-021 | 首版只为 PDF 附件建立页码级证据索引；其它解析器合成的单页 `page_num=1` 不能作为诚实页码；翻译等展平入库的 chunk 永不作为证据（显式 `missing_page_metadata`）；自动获取 arXiv 开放 PDF 未实现，当前取得全文的唯一方式是用户在项目内附加本地 PDF，复用既有项目导入接口 | 本文冻结（2026-09-22） | 合成页码会让"第 1 页"变成假坐标；把展平块排除在证据之外，避免用无坐标文本凑出看似可核验的引用；明确 M5 只完成"手工附加"这一半 |
| D-022 | 页级索引成功后，`metadata.literature` 已由服务端重写，客户端必须先重新读取该项目文献、再更新 `rag_status`；禁止把索引前的旧 `literature` 元数据回传 | 本文冻结（2026-09-22） | 项目接口拒绝客户端修改 literature 元数据；回传旧值会让一次成功的索引被报告为失败，并可能把界面状态与实际索引状态分离 |
| D-023 | 自动获取开放全文只使用 `access_locations` 中 `kind=pdf` 且 `access_status=open` 的 https 位置；上限 25 MiB、超时 30 秒、最多 3 次重定向；必须同时通过 Content-Type 白名单与 `%PDF-` 魔数校验；失败不写文件并保留 failure_reason；已存在 file-present artifact 时默认拒绝、仅 `force` 重取 | 本文冻结（2026-09-22） | 只认 provider 声明的开放位置，避免绕过机构认证或抓取受限全文；魔数校验防止把 HTML 错误页当成论文；不覆盖规则保护用户已核验的全文与既有证据 |
| D-024 | 端到端测试在 `create_app` 前替换下载器实现以保持离线；真实网络下载只做一次性人工冒烟，不进入自动化测试 | 本文冻结（2026-09-22） | 把网络波动当成回归信号会污染测试结论；同时保留“真实链路过一次”的人工证据 |
| D-025 | 正式开题沿用用户提供的八部分模板；学校为哈尔滨工业大学（威海），专业为测控技术与仪器；研墨及现有项目为用户个人独立开发的前期基础 | 用户确认（2026-09-29） | 不再写成课题组成果；姓名、学号、导师和日期未提供则留空，进度只作暂定安排 |
| D-026 | 增加 §2.4 的单 Agent 接入与 A1 验收，在 M1–M10/P4 完成后实施；开题稿建议题目突出智能体，最终备案题目仍待确认 | 用户要求保留 Agent 特色；具体边界由本次规划明确（2026-09-29） | 拒绝仅改名称的聊天包装、平行 Agent 状态库及恢复多智能体调度主线；新增工具集成覆盖，不改变证据数据模型，当前不使既有索引或缓存失效 |
| D-027 | 本次验证目标仍为技术可行性；演示选工科或计算机文献；本地设备为天选4，拟使用 GPT/DeepSeek 接口，GLM 为后续候选，经费与额外指导支持尚未落实 | 基础条件与目标由用户确认；具体交付边界由本次规划明确（2026-09-29） | 不编造准确率门槛、经费金额、学校算力或指导资源已获批；文献综述理解为所选论文的有据归纳，实验建议留作后续拓展 |
| D-028 | P3 答案生成复用既有模型 Provider，领域服务只依赖 `EvidenceAnswerModel` 窄协议（`identity` + `complete`）；Provider 选择仍由既有 `_create_provider` 完成，`src/literature` 不新建 provider 注册表、不直接读取模型配置文件 | 本文冻结（2026-09-29） | §3 要求"不另建平行 `ModelProvider` 抽象"；窄协议让领域服务保持离线可测，同时避免把 20 余项 provider 配置复制进领域层。被否决方案：在 `src/literature` 内新建模型抽象与注册表 |
| D-029 | 渲染前机器校验与拒绝语义：证据白名单、页码一致性（`第 N 页`/`p.N`/`page N`）、supported/conflicting 必须有证据；不通过的结论记入 `rejected_claims` 并**不得**降级为无引用结论；可用证据为 0 时不调用模型 | 本文冻结（2026-09-29） | §4.5 与 §11.4 要求"前端不得渲染无法通过校验的证据引用"；把无证据结论照常渲染再加角标会让界面出现不可核验文本。被否决方案：渲染全部结论并标注"未验证" |
| D-030 | 首版答案不缓存、`temperature=0`、`model_config_hash` 只含 provider/模型/base_url/temperature/max_tokens/提示版本且排除密钥；`/api/literature/answer` 为一次性 JSON，不引入 SSE 流式 | 本文冻结（2026-09-29） | §0 禁止缓存结果冒充在线；流式会把未校验文本提前送进界面，使"渲染前校验"无法成为唯一出口。被否决方案：先流式输出再由前端过滤 |
| D-031 | A1 只调用 §5.9 的确定性问答与既有文献服务；`academic_tools.py` 的 `arxiv_search`（原始 Atom XML 截断）与 `rag_search`（无 `project_root`/`source_ids`）在 A1 中必须被替换，旧兼容测试同步重写，不作为回归基线 | 本文冻结（2026-09-29） | 无范围检索与原始响应会让 Agent 绕过 §4.5 的项目/文献范围与证据校验，与 §2.4 的 A1 验收直接冲突 |
| D-032 | 页级 chunk 的**集合存储键**改为 `<doc_id>::<chunk_id>`；`chunk_id` 仍是不含项目 scope 的内容身份（artifact SHA-256 + 页码 + 坐标 + chunker 版本，D-020 不变），对外接口与证据字段只暴露该内容身份。查询命中与证据解析通过 metadata（`chunk_id`，可选再限定 `source_id`）定位行，并保留裸 id 回退以兼容本决策之前写入的行与展平块 | 本文冻结（2026-09-29） | P3 集成层发现的真实缺陷：同一份 PDF 在两个来源或两个项目下索引时内容身份相同，裸 id 使后写入的文档静默覆盖先写入文档的页元数据，跨项目隔离随之失效（违反 §7.2），且先建索引的一方只会看到 `chunk_not_found`。被否决方案：把 `doc_id` 混进 `chunk_id`（会改变 D-020 已冻结的内容身份并作废既有 `evidence_id`）；"只加来源校验不改存储键"（数据已被覆盖，校验只能报告失败而无法恢复）。影响范围：本决策之前建立的页级索引若曾被同内容文档覆盖，需要重新执行 `/api/literature/index`；旧行在本决策后仍可被读取 |
| D-033 | 固定公开论文包（`config/literature_demo_corpus.json`）只用于演示与回归：应用在语料可加载时额外注册 `fixture` provider，但 `result_mode` 必须保持 `live`/`fixture` 区分，语料缺失或损坏时只降级为"无缓存 provider"，不得阻止启动，也不得让缓存结果冒充在线 | 本文冻结（2026-09-29） | §2.1 M10 要求"具备带来源标记的缓存语料，公网失败时仍可演示同一条技术链路"；§0 禁止缓存结果与真实在线结果混写。被否决方案：把缓存作为 arxiv 的静默回退（会让在线失败看起来像检索成功） |
| D-034 | 固定演示脚本以 CLI 形式按 §7.1 顺序驱动既有 HTTP API，不新增服务端"一键演示"端点；检索式与文献选择仍由调用方显式给出 | 本文冻结（2026-09-29） | §7.1 的第 2、4 步是用户确认点；服务端自动跑完全程会把"确认过的检索式"和"用户选择"变成脚本内部行为，无法再声称链路包含确认环节 |
| D-035 | 每次演示运行写一份 JSON 记录到 `methods/literature_poc/runs/`，含步骤状态/原因/耗时、计数、模式与确认检索式；该目录只存运行记录，正式方法协议仍单独冻结 | 本文冻结（2026-09-29） | §5 的 P4 要求"重复运行、指标记录"，§6.2 要求正式评测前另建方法协议；把运行记录与方法协议混在一处会让"过程记录"被误当成"评测协议已冻结" |
| D-036 | A1 的 Agent 工具只包装 §5.9 的确定性服务：`project_root` 恒取工具注册表的工作区且不可由入参覆盖，检索式/论文选择/问答范围三个确认点以 `approval_scope="exact-input"` 复用既有 `await_approval` 契约，`arxiv_search` 与"无 `source_ids` 的 `rag_search`"直接移除而非保留兼容分支 | 本文冻结（2026-09-29） | §2.4 要求"Agent 通过确定性服务完成操作，不维护第二套状态"且"参数非法、失败、证据不足必须显式停止"；保留旧路径会让 Agent 能在无范围、无证据校验的情况下给出答案（违反 §4.5）。被否决方案：保留 `arxiv_search` 并标注"仅调试用"（调试路径同样会进入模型上下文并可能被当成证据） |
| D-037 | 修复独立复核发现的缺陷时，**只修可证伪的行为**并同时补回归测试；若某条承诺无法在离线环境证伪（如真实模型行为），则改文档措辞而不是加"看起来通过"的测试 | 本文冻结（2026-09-29） | §5.13 的 V-1…V-9 中，凡是"测试没红过"的承诺都被改为可证伪（例如故意构造 `insufficient` + 非空 claims 的违约载荷、用计数传输层实测请求路径）；真实模型行为与在线会话仍列在"仍未验证"，不以脚本模型的结果冒充。被否决方案：把在线/真实模型场景写成 `xfail` 或跳过（会掩盖未验证状态） |
| D-038 | 与文献链路无关但阻塞全量回归的既存缺陷一并修复：`NO_PROXY` 中的方括号 IPv6 条目在测试基础设施层规范化；多文章拆分的"孤立首字母"边界在检测器中新增策略 C | 本文冻结（2026-09-29） | 两者都让全量套件长期带红（`8 failed`），使"新增失败"与"既存失败"难以区分，直接削弱回归证据的价值。修复都带可复现测试（合成夹具 + 真实样例），且不改变对外契约。被否决方案：继续在文档里把它们记作"环境问题/既存失败"（读者无法据此判断回归是否引入新问题） |
| D-039 | `methods/literature_poc/METHODOLOGY.md` 先落**未生效骨架**：只列必须冻结的字段并逐项标注【待确认】，不代填 RQ、语料、gold、指标或阈值 | 本文冻结（2026-09-29） | AGENTS.md 与本合同 §6.2 一致要求这些取值由用户与指导教师确认；骨架让"缺什么"可见，同时不产生任何可被误读为协议的约束力。被否决方案：由我拟定一套指标与阈值（会把未经确认的取值伪装成协议）；或继续不建文件（缺项不可见） |
| D-040 | 增设**确定性、非 LLM** 的 fixture 答案模型（`provider="fixture"`、`model="deterministic-extractive-v1"`），仅在 `literature.answer.mode: fixture` 或 `SCHOLAR_LITERATURE_ANSWER_MODE=fixture` 时启用 | 本文冻结（2026-09-29） | 无模型/无网络时（答辩笔记本、CI）整条链仍须可演示，但**不得**让 fixture 结果看起来像模型产出：该模型不读问题、不做摘要，只为每条检索到的证据抽取一句原文作为结论，因此每条结论都字面存在于它的引用里；身份字段随答案落盘，fixture 与 live 始终可区分。被否决方案：离线时静默回退到某个云端模型（需要网络与密钥）；或让演示在没有模型时直接失败（无法离线演示） |
| D-041 | 演示 CLI 先确认项目目标再跑七步：`--project-path` 或 `--create-location`+`--project-name` 二选一；退出码细化为 0/1/2/3/4；语料可用 `demo.expected_failures` **声明**预期失败 | 本文冻结（2026-09-29） | 在真实服务器上的端到端运行暴露出两个问题：新目录直接以"存档导入失败"告终（归因错误、不可操作），以及 `access_unavailable` 这个**由语料自身性质决定**的失败让演示永远以非零码结束。声明只是让调用方区分"预期失败"与"回归"：记录中的状态与原因**一律照旧**（仍是 `failed/access_unavailable`），仅额外标记 `detail.expected="true"`。被否决方案：把预期失败记成 `skipped`（掩盖真实失败）；或让 fixture 语料谎称有开放全文（会把演示建立在不成立的网络假设上） |
| D-042 | 答案调用按 DeepSeek 官方接口文档加固：开启 JSON Output（`response_format={"type":"json_object"}`，仅答案调用、仅当 provider 支持），输出预算默认 2048 → **8192** 且可用 `literature.answer.max_tokens` 覆盖；空正文与截断一律作为**显式失败**并携带 `stop_reason`，HTTP 错误体开始带 `details` | 本文冻结（2026-09-29） | 真实模型实测：思考模式开启时 `max_tokens=2048` 被思考 token 耗尽（日志 `finish=length, text_len=0`），答案步稳定 502 `answer_invalid_response`；官方 JSON Output 文档明确要求"合理设置 max_tokens 防止 JSON 被截断"并提供 `response_format`。**不得**用"关掉思考模式"来绕过（用户明确要求保留思考模式），因此只改预算与输出模式。被否决方案：把思考模式强制关掉（剥夺用户配置且掩盖真因）；在 502 时按"无结论"静默返回（把配置缺陷伪装成模型结论） |
| D-043 | 新增只读工具 `literature_providers` 列出已注册 provider；`literature_search` 遇 `provider_not_found` 时把可用名附在错误里；工具 schema 写明 `arxiv`=在线、`fixture`=离线演示语料 | 本文冻结（2026-09-29） | 真实模型实测会**凭空指定 provider**（要 `semantic_scholar`，而注册的只有 arxiv/fixture），原错误信息也不告诉它有哪些可用，于是浪费轮次试探。只读发现工具不改变审批与范围规则：它无副作用、不需要审批，检索仍须停在审批上。被否决方案：让服务端在未知 provider 时静默回退到 arxiv（会把"检索式来自哪个库"变成不可见事实） |

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
| 新增 | `python/src/literature/service.py` | 搜索执行、强身份去重、项目批量入库和初始全文状态编排；完成并持久化检索计划；P2B 增加页级索引与证据解析；M5 增加开放全文获取与状态机；P3 增加 `answer_question` 与答案结果信封 | P2A.2–P2A.3、P2B、M5、P3 已实现（复核后 `index_store_unavailable` / `retrieval_unavailable` 不再被折叠成 `insufficient`） |
| 新增 | `python/src/literature/evidence.py` | 页内切块、坐标/块身份派生、索引指纹与证据校验；回答结构化仍属于 P3 | P2B、P3 已实现 |
| 新增 | `python/src/literature/fulltext.py` | M5 开放全文获取：只认 open + https 的 PDF 位置，大小/超时/Content-Type/魔数校验，显式错误码 | 已实现（2026-09-22） |
| 新增 | `python/src/literature/answer.py` | P3 提示构造、模型输出解析、白名单与页码一致性校验、拒答原因（§5.9） | P3 已实现（复核后定位词表扩展为多语言，仍属有限枚举） |
| 新增 | `python/src/literature/answer_model.py` | P3 `ModelIdentity`、`EvidenceAnswerModel` 窄协议与既有 Provider 适配器（D-028）；D-042 增加 JSON Output 模式、8192 输出预算、thinking/温度如实记录与空响应显式报错 | P3 已实现；D-042 已落地（2026-09-29） |
| 新增 | `python/src/literature/fixture_answer_model.py` | D-040 确定性抽取式答案模型：不读问题、只抽取证据原文，身份为 `fixture / deterministic-extractive-v1`，仅显式开启时使用 | 已实现（2026-09-29） |
| 新增 | `python/src/literature/demo_corpus.py`、`demo_run.py` | 固定演示语料的读取/校验/离线 PDF 物化；演示运行记录（步骤、状态、原因、耗时、计数、模式、`project_path`、语料声明的预期失败） | 已实现（2026-09-29，P4） |
| 新增 | `python/src/net_env.py` | 入口级网络环境整理：丢弃 httpx 无法解析的 `NO_PROXY` 条目（方括号 IPv6 会让客户端构造直接抛错）；`api.py`、`create_app()`、演示 CLI 与测试共用 | 已实现（2026-09-29，V-16/V-14） |
| 新增 | `python/routers/literature.py` | Literature API 与现有 Project 存储的窄适配层；接收检索计划草案；P2B 增加索引与证据路由；M5 增加全文获取路由；P3 增加答案路由与范围检索适配器 | P2A.2–P2A.3、P2B、M5、P3 已实现（复核后错误体携带 `details`） |
| 修改 | `python/api_factory.py` | 注册 Literature API，并在应用生命周期关闭 Provider 资源；P2B 注入 RAG 页级索引适配器；P3 用既有 Provider 工厂装配答案模型（D-028）；D-040/D-042 增加 `literature.answer.mode` 与 `max_tokens`、入口级代理环境整理 | P2A.2、P2B、P3、P4、A1 已实现 |
| 修改 | `python/api.py` | 启动时整理代理环境，避免 httpx 在客户端构造阶段抛错（V-16） | 已实现（2026-09-29） |
| 修改 | `python/src/agent_v2/providers/openai_compat.py` | D-042：非流式 `chat` 增加可选 `response_format`（答案调用用 JSON Output，Agent 循环不受影响） | 已实现（2026-09-29） |
| 修改 | `python/src/llm_request_policy.py` | 官方把 V4.1 Flash 改名为 `deepseek-flash` 后，思考模式默认与 `reasoning_effort` 开关仍须匹配该系列（同时保留旧别名） | 已实现（2026-09-29，V-22） |
| 新增 | `python/scripts/literature_demo.py` | 固定演示 CLI：按 §7.1 顺序驱动既有 HTTP API，先确认项目目标，写运行记录（D-034、D-041） | 已实现（2026-09-29，P4） |
| 新增 | `python/scripts/literature_live_check.py` | 对运行中的服务做真实模型答案契约检查：打印结论、证据页码/原文、被拒结论与未解析项；退出码 0/1/2 | 新增（2026-09-29，§5.14 的复现入口） |
| 新增 | `config/literature_demo_corpus.json` | 固定合成演示语料（3 条记录、逐页文本、无 DOI/arXiv 编号、声明预期失败 `acquire_fulltext: access_unavailable`） | 已实现（2026-09-29，P4） |
| 修改 | `python/routers/project.py` | P2A.2 共享清单事务锁、内部路径与 manifest 版本边界；P2B 补页结构与文档哈希 | P2A.2、P2B 已实现 |
| 修改 | `python/src/utils/atomic_io.py` | 提供同进程路径级可重入事务锁；不承诺跨进程协调 | P2A.2 后端已实现 |
| 修改 | `python/routers/rag.py` | 页级 chunk、证据 metadata、索引版本和范围约束；P3 把范围检索抽成路由与问答服务共用的调用，并按 D-032 把存储键改为 `<doc_id>::<chunk_id>` | P2B、P3 已实现（新增 `project_scoped` 查询模式；展平通道保持不变） |
| 修改 | `src/composables/useSourceLibrary.ts` | P2B：把"建立索引"动作切到页级证据通道（PDF + 文献条目），其它来源保持展平通道 | P2B 已实现 |
| 修改 | `python/src/agent_v2/tools/academic_tools.py` | 用结构化 Literature/RAG 工具替代原始 XML 与无范围查询；A1 增加 `literature_sources`、`literature_providers` 与证据不足后的 `next_actions` | A1 工具层已实现（2026-09-29，见 5.11、5.12、D-043） |
| 新增 | `src/composables/useLiteratureDiscovery.ts` | 检索、选择、入库和状态管理 | P2A.3 已实现 |
| 新增 | `src/composables/useLiteratureAnswer.ts` | P3 多文献范围选择、问答请求、结论与证据状态（§5.9）；复核后按 `status` 收敛 claims/evidence | P3 已实现（2026-09-29，V-2） |
| 新增 | `src/components/AgentLiteratureEvidence.vue` | A1：把 Agent 的 `literature_answer` 工具结果渲染为结论卡片与可展开证据；非 JSON 回退原文 | A1 已实现（2026-09-29） |
| 修改 | `src/components/SourceLibraryView.vue` | P2A.3：公开发现、检索计划确认、结果回执和多选入库；P3 增加多文献问答、证据展开与"获取开放全文"入口；复核后禁用未建索引的来源 | P2A.3、P3、A1 已实现 |
| 新增/修改 | `python/tests/`、`src/__tests__/` | 契约、状态机、页码证据和端到端回归；A1 增加 Agent 工具接入与计划、确认、失败处理覆盖；此后新增真实 uvicorn 子进程的 CLI 端到端、以及 `tests/conftest.py` 的代理环境整理 | P1–P4、A1 已实现；全量 3118 passed / 0 failed（2026-09-29） |
| 新增 | `methods/literature_poc/METHODOLOGY.md` | 正式 RQ、语料、gold、协议和运行记录 | **未生效骨架**：逐项标注【待确认】，需用户与指导教师填写（D-039） |
| 新增 | `methods/literature_poc/runs/` | 每次演示运行一份 JSON 记录 | 已实现（由 CLI 写入；目录本身不入库） |

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

依据 D-026，正式开题所承诺的智能体版本还须通过 A1。截至 2026-09-29 的诚实口径：

- **十项技术条件**（上表 1–10）现均有实现与分层验证证据：M1–M10 + P4 的分层结果见 §5.2–§5.10，A1 的工具层/会话级/前端展示见 §5.11–§5.12，独立复核与订正见 §5.13，真实模型一次性观测见 §5.14；全量回归 3118 passed / 0 failed。
- **A1 阶段门**：工具层、会话级验收（真实运行时 + 脚本模型）、前端引用展示均已完成；真实 arXiv 与真实模型的行为已各做一次性观测，但**只覆盖一个模型、一套配置**（§5.14），因此 A1 视为"证据齐备但观测面窄"，报告中必须同时写出这一限制。
- **仍不满足"效果已跑通"**：第十一节谈的是技术链路，不是效果。正式 RQ、语料、gold、指标与阈值仍在 `methods/literature_poc/METHODOLOGY.md` 中标注【待确认】（D-039），在此之前不得报告检索质量、回答正确性、教学效果或生产可用性。

因此当前可报告的准确说法是："**证据可追溯的文献检索问答链路已实现并通过分层验证，含一次真实模型观测；质量结论待方法协议冻结后另行测量。**"在此之前，不得报告"整个系统已完成"或"效果良好"。

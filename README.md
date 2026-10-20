# 简历投递自动化 Agent

> 把「腾讯文档智能表 → 企业信息补全 → 岗位抓取 → 简历匹配 → 表单预填」串成一条本地流水线。
> 浏览器只负责打开页面和填写，**最终提交始终由你人工确认**。

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![Playwright](https://img.shields.io/badge/Playwright-Chrome-green)
![LLM](https://img.shields.io/badge/LLM-DeepSeek-4B6BFB)

---

## 功能概览

- **免登录抓表**：直接读取公开分享的腾讯文档「智能表」，自动处理多子表、字段别名与链接单元格。
- **企业信息补全**：并发查询 A 股 / 港股 / 美股行情，补全上市状态、市值、板块；用本地名单判定中国 500 强。
- **岗位抓取**：优先走招聘站点接口（Moka / 北森 / 飞书 / 通用门户），拿不到时回退 Playwright 浏览器 + LLM 抽取。
- **简历匹配**：LLM 对「岗位 JD × 简历」逐条打分，输出命中点、差距与建议；支持多份简历变体，自动选最匹配的一份。
- **已投递去重**：公司名清洗 + 字符相似度 / embedding 余弦相似度，重复公司自动跳过。
- **可断点续跑**：SQLite 缓存公司信息、已查找公司、投递状态；重复执行不会重复消耗 token。

## 工作流程

```
腾讯文档智能表（公司 + 投递链接）
   │  ① fetch   抓取 + 字段归一化 + 已投递标记
   ▼
data/raw/companies.csv
   │  ② enrich  市值 / 上市状态 / 中国500强
   ▼
data/enriched/companies_enriched.csv
   │  ③ scrape  接口或浏览器抓岗位 → 与简历逐条匹配 → 保留达标岗位
   ▼
data/jobs/jobs.jsonl
   │  ④ select  分成 applicable / failed / skipped
   ▼
data/output/applicable_companies.json + report.md
```

`python run_pipeline.py run` 会执行 ①→④ 并生成报告；`fill` 单独执行，因为投递环节需要人工在场。

## 目录结构

```
.
├── main.py                  # 入口（默认执行 run）
├── run_pipeline.py          # 分阶段 CLI
├── config.example.yaml      # 配置模板（复制为 config.yaml）
├── requirements.txt
├── tencent_docs/            # 智能表抓取（JSONP + base64/zlib 解包）
├── core/                    # 配置、数据模型、落盘、缓存、去重、字段别名
├── enrichment/              # 市值行情 + 中国500强本地名单匹配
├── llm/                     # DeepSeek 客户端、embedding（本地/API）
├── browser/                 # Playwright 封装、多轮 Agent、动作执行器
├── resume/                  # 简历文本提取 + 结构化画像
├── matching/                # 匹配打分、岗位偏好调权
├── autofill/                # 表单分析、逐字段填写、附件
├── pipeline/                # 各阶段编排 + 招聘站点适配器
├── report/                  # Markdown 报告
└── data/                    # 示例数据、本地名单、运行产物（产物不入库）
```

## 快速开始

### 1. 环境要求

- Python **3.10+**
- Google Chrome（代码默认 `channel="chrome"`，复用系统浏览器，无需 `playwright install`）
- 一个 DeepSeek API Key（用于 JD 抽取、匹配打分、看图填表）
- 可选：`fastembed`（本地 embedding，用于公司名语义去重；不装会自动回退字符相似度）

### 2. 安装

```bash
git clone <your-repo-url>
cd <repo>

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
# source .venv/bin/activate

pip install -r requirements.txt
```

> 如果使用 `browser.channel: chromium` 而不是系统 Chrome，需要额外执行 `playwright install chromium`。

### 3. 配置

```bash
# Windows
copy config.example.yaml config.yaml
# macOS / Linux
cp config.example.yaml config.yaml
```

至少填写这三项：

```yaml
docs:
  - url: https://docs.qq.com/smartsheet/YOUR_DOC_ID   # 你的智能表分享链接
    name: 我的秋招信息汇总表

llm:
  api_key: "sk-..."                                   # 你的 DeepSeek Key

resume:
  path: data/resume/我的简历.pdf                       # txt / pdf / docx 均可
```

`config.yaml` 含密钥与个人信息，已加入 `.gitignore`，**不要提交**。

### 4. 准备数据（可选）

- **已投递清单**：`data/applied/applied_companies.xlsx`（或 csv / txt），用于跳过投递过的公司。
- **附件**：把证书、实习证明等放进 `data/attachments/`，填表时会尝试上传。
- **简历变体**：在 `resume.alternates` 里列出其它方向的简历，匹配阶段会选用分数更高的那份。

仓库自带一份虚构简历 `data/sample_resume.txt`（张三 / 清华大学，人物与经历均为虚构），可直接用来跑通流程。

### 5. 运行

```bash
python run_pipeline.py fetch                  # 抓表
python run_pipeline.py enrich --limit 20      # 先小批量补全
python run_pipeline.py scrape --limit 3       # 先抓 3 家试试
python run_pipeline.py select                 # 查看可投递公司
```

每一步都会打印进度并落盘；失败信息会写进 `scrape_failures.jsonl`，便于人工排查。

## 常用命令

| 命令 | 说明 |
|---|---|
| `python run_pipeline.py fetch` | 抓取全部配置的智能表，标记已投递公司 |
| `python run_pipeline.py enrich [--limit N] [--names A,B] [--no-lists]` | 补全市值/上市状态/500强 |
| `python run_pipeline.py scrape [--limit N] [--names A,B] [--force]` | 抓岗位并匹配；`--force` 忽略「已查找」缓存 |
| `python run_pipeline.py match [--limit N]` | 独立匹配（`match.enabled: true` 时才在 `run` 中执行） |
| `python run_pipeline.py select` | 生成 applicable / failed / skipped 三个 JSON |
| `python run_pipeline.py fill [--limit-each N] [--auto-confirm]` | 浏览器预填（人工确认后提交）；`--auto-confirm` 跳过开始前确认 |
| `python run_pipeline.py run [--limit N] [--no-lists] [--force]` | fetch → enrich → scrape → [match] → select → report |

`--limit` 限制处理的公司数（测试用），`--names 美团,腾讯` 按公司名过滤，`--no-lists` 跳过 500 强名单匹配。

## 配置说明

| 配置 | 作用 |
|---|---|
| `docs[]` | 腾讯文档智能表 `url` 与 `name`，可配置多份 |
| `applied_xlsx` / `applied_threshold` | 已投递清单路径 / 相似度阈值（默认 0.85） |
| `llm.api_key` / `model` / `base_url` | DeepSeek 或任意 OpenAI 兼容服务 |
| `embedding.backend` | `local`（本地 fastembed）/ `api`（OpenAI 兼容 embedding）；留空则用字符相似度 |
| `embedding.workers` | 大批量向量计算的多进程数，受限环境会自动回退单进程 |
| `browser.channel` / `headless` | `chrome` 或 `chromium`；投递建议 `headless: false` |
| `browser.user_data_dir` | 复用登录态（扫码一次，后续免登录） |
| `agent.max_rounds` | 页面 Agent 最大交互轮数 |
| `agent.use_screenshot` / `vision_model` | 是否用截图 + 多模态模型；用 DeepSeek 时视觉模型为 `deepseek-flash`（`scrape` / `fill` 段可单独覆盖） |
| `agent.vision_base_url` / `vision_api_key` | 视觉模型换用其它供应商时填写，留空复用 `llm` |
| `scrape.match_threshold` / `top_k` | 岗位入选分数阈值 / 每家公司最多保留数 |
| `scrape.max_positions` | 每家公司最多查看的岗位数（防止无限翻页） |
| `scrape.max_matched_companies` | 找到 N 家入选公司后停止本轮抓取 |
| `scrape.browser_fallback` | 无接口适配时是否回退浏览器（默认关闭，直接记失败） |
| `scrape.skip_domains` | 跳过的域名（默认微信公众号：反爬且不是岗位列表页） |
| `fill.use_screenshot` / `keep_pages` | 填表是否看图 / 填完是否保留标签页供人工检查 |
| `matching.min_score` | 独立匹配阶段的分数阈值 |
| `resume.path` / `alternates` / `attachments_dir` | 主简历 / 变体 / 附件目录 |
| `preference.algo_penalty` / `dev_bonus` | 可选的岗位偏好调权（默认 0 = 不做任何调整） |

## 各阶段细节

### fetch：低层接口抓智能表

腾讯文档个人版智能表的公开分享页无需登录。实现路径：

1. 从分享链接提取 `docid`；
2. 请求 `dop-api/opendoc`，拿到 `clientVars...initialAttributedText.text[]`；
3. `workbook` 给出子表列表，`smartsheet` 是 base64 + zlib 压缩的数据；
4. 解压后按 mutation 类型还原表结构（`t=3005`）与记录（`t=3028`），链接单元格取真实 URL。

会跳过「面经 / 笔试 / 题库 / 资料」等非招聘子表，并把不同列名（企业名称 / 公司名称 / 公司…）归一化到统一字段。

### enrich：企业信息

- 市值 / 上市状态：东方财富搜索建议接口定位证券，再用腾讯行情取总市值与流通市值；仅接受股票类目，排除基金、债券、新三板等。
- 中国 500 强：只查 `data/lists/china500.txt` 本地名单（精确 → 包含 → 相似度）；配置了 embedding 时用余弦语义匹配。查不到即判「非 500 强」，不调用 LLM。
- 结果按公司名写入 `data/cache/cache.db`，重复执行不会重复请求。

### scrape：岗位抓取 + 匹配

先按「500 强 → 上市 → 其它」排序，再逐家处理：

1. 跳过已投递、已查找、无投递链接的公司，以及 `skip_domains` 中的域名；
2. 依次尝试站点适配器：

   | 适配器 | 识别方式 | 接口 |
   |---|---|---|
   | Moka | 域名含 `mokahr.com` | `ats-apply/website/jobs/v2`（AES-CBC 解密） |
   | 北森 | `*.zhiye.com` 或接口探测 | `api/Jobad/GetJobAdPageList` |
   | 通用门户 | 统一 SaaS 模板 | `prod-api/recruit/portal/listPosition` |
   | 飞书 | `jobs.feishu.cn` | `search/job/posts`（需 csrf token） |

3. 都不适配时，若 `browser_fallback: true`，用 Playwright 打开页面：先做确定性链接/表格抽取，失败再交给 LLM 或页面 Agent；
4. 每个岗位 × 每份简历并发打分，保留 `>= match_threshold` 的前 `top_k` 条；
5. 结果写入 `jobs.jsonl`，并记录 `resume_path`，`fill` 阶段直接用这份简历投递。

### match：独立匹配（可选）

读取 `jobs.jsonl`，用 `resume.path` 对每个岗位重新打分，输出 `match_report.csv/json`，按分数排序并标注低/中/高档位。默认 `match.enabled: false`，因为 `scrape` 已完成匹配。

### select：结果分类

- `applicable_companies.json`：有岗位达标的公司，按最高分排序，含全部达标岗位；
- `failed_companies.json`：抓到了岗位但没有一个达标；
- `skipped_companies.json`：链接打不开、站点未适配、主动跳过。

### fill：表单预填（人工确认）

对 `applicable` 中分数最高的若干家公司：

1. 新开标签页打开投递页，若停在岗位详情页则尝试点「立即投递 / 申请职位」；
2. 上传简历，等待平台自动解析；
3. 对整页截图 + 页面文本做一次分析，得到 `{state, required[], optional[]}`；
4. 按 `label` 匹配输入框/下拉框逐项填写，值必须是简历原文（不做改写）；
5. 再截一次图复查「页面上实际的值」和「仍为空的必填项」，结果写入 `fill_analysis.jsonl`；
6. 保留标签页，由你检查、补填并**手动提交**。

人工修改后的字段会记录到 `data/output/fill_context.json`，下次投递自动复用，减少重复输入。

## 输出文件

```
data/
├── raw/companies.csv                 # 抓取 + 已投递标记
├── raw/fetch_summary.json
├── enriched/companies_enriched.csv   # 追加市值/上市/500强列
├── jobs/jobs.jsonl                   # 达标岗位（含分数/档位/简历）
├── jobs/scrape_failures.jsonl        # 未抓到岗位的公司及原因
├── jobs/scraped_companies.csv        # 已查找公司状态
├── jobs/YYYY-MM-DD/                  # 当日归档
├── output/applicable_companies.json
├── output/failed_companies.json
├── output/skipped_companies.json
├── output/match_report.csv|json
├── output/fill_analysis.jsonl
├── output/fill_summary.json
├── output/fill_context.json          # 人工修改后的字段，供下次复用
├── output/report.md                  # 汇总报告
├── cache/cache.db                    # 公司信息 / 已查找 / 投递状态
└── cache/embeddings.db               # 公司名向量缓存
```

## 扩展新的招聘站点

在 `pipeline/` 下新增一个适配器，约定：

```python
def is_xxx(url: str) -> bool: ...

def fetch_jobs(url: str) -> tuple[list[dict], bool]:
    """返回 (jobs, ok)。ok=False 表示该站不适用，交给下一个适配器。"""
```

每个 job 至少包含：

```python
{
    "title": "岗位名称",
    "responsibilities": "岗位职责",
    "requirements": "任职要求",
    "location": "工作地点",
    "url": "岗位详情链接",
}
```

然后在 `pipeline/scrape.py` 的适配器链里加一行判断即可。

## 数据与隐私

本仓库发布前已做脱敏处理：

- 不含任何 API Key、Cookie、浏览器登录态；
- 不含真实姓名、电话、邮箱、学校、证书或简历文件；
- 腾讯文档链接、个人路径均替换为占位符；
- `data/sample_resume.txt` 为虚构示例；
- `config.yaml`、`data/` 下的个人产物均被 `.gitignore` 忽略。

使用自己的数据时请同样注意：

- 只在本地保存简历与附件，不要把 `config.yaml`、`data/` 提交到公开仓库；
- 如果曾把真实 Key 写进任何文件，请立即到平台**吊销并重新生成**；
- 报告与日志可能包含公司名、投递记录，分享前先检查。

## 已知限制

- 站点适配器只覆盖常见 ATS（Moka / 北森 / 飞书 / 部分统一门户）；页面改版会导致接口失效，失败公司会记录到 `scrape_failures.jsonl`。
- 登录、短信、图形/滑块验证码无法自动处理，需要人工在浏览器中完成。
- `fill` 只填不交；不同站点的组件库（Element / Ant Design 等）可能需要调整 `autofill/fill_agent.py` 中的选择器逻辑。
- LLM 打分与表单识别结果仅供参考，不保证准确；投递前请逐项核对。
- 腾讯文档仅支持公开分享的个人版「智能表」，不处理需要登录或企业版权限的文档。

## 合规声明

本项目仅用于**本人求职**的效率工具，请遵守目标网站的服务条款与 robots 规则：

- 不要高频、批量骚扰招聘方；
- 不要绕过验证码或访问控制；
- 投递前确认信息真实，最终提交由本人完成；
- 因使用本工具产生的任何后果由使用者自行承担。

## License

本仓库未附带开源协议（默认保留所有权利）。如需开源，可自行添加 `LICENSE`（例如 MIT）。

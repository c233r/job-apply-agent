# data/

仓库只保留**示例数据**与**本地名单**，运行产物不入库。

```
data/
├── sample_resume.txt          # 示例简历（虚构人物），可直接用于跑通流程
├── lists/china500.txt         # 中国500强公司名单（一行一个）
├── resume/                    # （自建）你的真实简历，已在 .gitignore 忽略
├── attachments/               # （自建）证书/证明材料，投递时上传
├── applied/                   # （自建）已投递清单 xlsx/csv/txt
├── raw/                       # fetch 产物
├── enriched/                  # enrich 产物
├── jobs/                      # scrape 产物
├── output/                    # match/select/fill 报告
├── cache/                     # SQLite 缓存（公司信息/向量/字段答案）
├── screenshots/               # 运行期截图（默认结束后清理）
└── browser_profile/           # 浏览器登录态（体积大，勿提交）
```

放入自己的文件后，在 `config.yaml` 中把对应路径指过去即可。

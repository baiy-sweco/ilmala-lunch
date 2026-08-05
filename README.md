# Ilmala 午餐板

自动抓取 Pasila / Ilmala 附近 3 家餐厅的当日午餐菜单，翻译成中/英/西/芬四种语言（外加繁体中文本地转换），并按过敏原标签（M/L/VL/G/KM/Veg）多选筛选。

- `docs/index.html` — 静态页面，运行时 `fetch('./menu.json')` 拿数据，纯前端，无需后端。
- `scripts/update_menu.py` — 抓取三家餐厅页面、解析今日菜单、调用 DeepL 翻译、写出 `docs/menu.json`。
- `.github/workflows/update-menu.yml` — 每个工作日早上自动跑一次上面的脚本并 commit。

## 部署步骤（一次性）

1. **建仓库**：在 GitHub 新建一个仓库（public 或 private 都行，Pages 免费层 public 仓库最简单），把这个文件夹的内容全部 push 上去，保留目录结构不变。

2. **申请 DeepL 免费 API key**：去 https://www.deepl.com/pro-api 注册 "DeepL API Free"（不是 Pro），拿到一个类似 `xxxxxxxx-xxxx-...:fx` 结尾是 `:fx` 的 key。免费额度每月 500,000 字符，这个用量（一天几十道菜名）完全用不完。

3. **把 key 加成仓库密钥**：仓库 → Settings → Secrets and variables → Actions → New repository secret，名字填 `DEEPL_API_KEY`，值填上一步拿到的 key。

4. **开 GitHub Pages**：仓库 → Settings → Pages → Build and deployment → Source 选 "Deploy from a branch" → Branch 选 `main`，文件夹选 `/docs` → Save。几分钟后页面就能在 `https://<你的用户名>.github.io/<仓库名>/` 访问。

5. **手动跑一次工作流**（不用等到明天早上）：仓库 → Actions → 左侧选 "Update Ilmala Lunch Menu" → 右侧 "Run workflow" 按钮 → Run。跑完之后 `docs/menu.json` 会被自动更新并 commit，Pages 会在下一次构建后显示新数据（通常 1-2 分钟）。

完成以上五步，之后就是全自动的：每个工作日早上 6:00 UTC（夏令时相当于赫尔辛基 9:00，冬令时 8:00）自动抓取、翻译、发布，你不用再手动做任何事。

## 已知局限（如实说明，不是隐藏的坑）

- **抓取逻辑基于关键词和正则，不是官方 API**。三家餐厅的页面结构如果改版，解析可能失效——脚本设计成"某一家解析失败不会拖垮另外两家"，失败的那家会在页面上显示"未能自动抓取，请查看官网"，而不是显示错误的数据或让整个工作流报错退出。
- **菜品分类（主菜/汤/配菜/甜点）是启发式猜的**，依据是"有没有价格"和关键词（比如出现"keitto"就归类成汤），不是源网站给出的显式标签。绝大多数情况下猜得对，但个别菜可能分错类。
- **菜名翻译是机器翻译（DeepL）**，不是人工校对。日常用语基本没问题，但涉及过敏原时，页面上也提示了"请向工作人员当面确认"——过敏原代码（M/L/VL/G/KM/Veg）本身是从源网站原始文本里用正则精确提取的，不经过翻译，相对更可信。
- **ninankeittio.fi 的日期标注本身偶尔有错**（我们抓取时就发现过"周二"标了三周后的日期这种情况）。脚本用"跟今天最接近的日期"来兜底匹配，如果偏差超过 2 天会在页面上标注提示，但仍建议偶尔人工抽查一下。
- 定时任务默认是工作日跑一次；如果发现该餐厅经常在你早上看页面时还没更新（比如某天很晚才发菜单），可以把 `update-menu.yml` 里的 cron 时间往后调，或者加一个第二次运行作为兜底。

## 本地测试

```bash
pip install -r requirements.txt --break-system-packages   # 或用虚拟环境
export DEEPL_API_KEY=你的key   # 不设置也能跑，只是新菜名不会被翻译，会显示芬兰语原文
python scripts/update_menu.py
cat docs/menu.json
```

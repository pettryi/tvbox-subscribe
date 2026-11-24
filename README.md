# 影视源自动维护 · GitHub Actions 版

用 GitHub 免费的定时任务，每天自动抓取影视源、测活、去重，并生成订阅供影视仓（TVBox）使用。

## 为什么用 GitHub 版

之前 Gitee Go 一直「空跑」（显示成功但阶段不执行），GitHub Actions 更成熟稳定，且同样免费。核心设计：

- **真实源不进仓库**：通过 Secrets 存放 Base64，运行时才注入
- **成人内容单独留存**：成人订阅和报告作为运行产物（artifact），不提交到公开仓库
- **空订阅保护**：万一抓不到任何源，不会把正常订阅覆盖成 0 站

> ⚠️ **国内访问提醒**：GitHub 的 raw 链接在国内有时不稳定。如果影视仓拉取失败，可把订阅文件通过之前的方式同步到 Gitee（那份方案已验证可用）。定时任务用 GitHub 跑，订阅文件用 Gitee 分发，两者可以组合。

---

## 一、准备真实源的 Base64

真实源**不要**明文写进仓库，要先转成 Base64。

我已经帮你生成好了，就在这个文件夹里的 **`真实源BASE64-填Secrets用.txt`**（2084 字符），打开全选复制即可。

> 🔒 **这个文件已在 `.gitignore` 里排除，绝对不要手动上传到 GitHub 仓库**——它解码后就是全部真实源地址（含成人站点）。它只用于填到 GitHub Secrets 里，填完就没用了。

需要自己重新生成时，在存有真实源的目录（`影视源工具-本地版`）打开 PowerShell 执行：

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("sources.txt"))
```

如果你之前填过 Gitee 的 `SOURCES_B64`，那串和这里的是一样的，直接复用也行。

---

## 二、新建 GitHub 仓库

1. 打开 <https://github.com> 登录，点右上角 `+` → `New repository`
2. 填写：
   - **Repository name**：例如 `tvbox-subscribe`（随便起，记住它）
   - **Description**：可留空
   - **选 Public（公开）** ← 重要！私有仓库的 raw 链接影视仓访问不了
   - **不要**勾选 "Add a README file" / ".gitignore" / "license"（保持空仓库）
3. 点 `Create repository`

记下两个信息：
- 用户名：例如 `pettry`
- 仓库名：例如 `tvbox-subscribe`

---

## 三、上传本文件夹的内容到仓库

在本文件夹（`影视源工具-GitHub版`）打开 PowerShell（右键菜单已配置「在此处打开 PowerShell」），依次执行：

```powershell
git init
git add .
git commit -m "init"
git branch -M main
git remote add origin https://github.com/你的用户名/你的仓库名.git
git push -u origin main
```

把 `你的用户名/你的仓库名` 换成实际的，例如 `pettry/tvbox-subscribe`。

> 如果提示要登录：GitHub 现在用 **Personal Access Token** 当密码（不是账号密码）。没有的话去 Settings → Developer settings → Personal access tokens 生成一个，勾 `repo` 权限。
>
> 如果完全不想用命令行，也可以在 GitHub 网页仓库点 `uploading an existing file`，把本文件夹里**除 `.git` 外的所有文件**（含隐藏的 `.github` 文件夹）拖进去提交。

---

## 四、配置 Secrets（关键步骤）

在 GitHub 仓库页面：**Settings** → 左侧 **Secrets and variables** → **Actions** → **New repository secret**

添加这一条：

| Name | Value |
|---|---|
| `SOURCES_B64` | 第一步生成的那一长串 Base64 |

（如果有手工源，再加一条 `MANUAL_SOURCES_B64`；没有就不用加。）

> Secrets 的值在 GitHub 上会显示为加密的圆点，任何人（包括你）都无法再次查看，只能重新覆盖。填完保存即可。

---

## 五、启用 Actions 并运行

1. 点仓库顶部的 **Actions** 标签。首次进入会提示启用 workflows，点 `I understand, enable workflows`
2. 左侧选 `订阅源自动维护`，右侧点 **Run workflow** → 选 `main` 分支 → 点绿色 `Run workflow` 按钮
3. 等 2～5 分钟（20 个源展开测活需要几分钟），点进这次运行看实时日志

日志里出现这些就说明对了：

```
SOURCES_B64 长度: 2084
sources.txt 行数: 45
主订阅站点数: 84
```

---

## 六、影视仓订阅地址

运行成功后，订阅文件在：

```
https://raw.githubusercontent.com/你的用户名/你的仓库名/main/output/subscribe.json
```

例如：

```
https://raw.githubusercontent.com/pettry/tvbox-subscribe/main/output/subscribe.json
```

把这个链接填进影视仓的「配置地址」即可。

---

## 七、定时与手动运行

- **自动运行**：默认每天北京时间 08:00（配置里 cron `0 0 * * *` 是 UTC 时间）
  - 想改时间：编辑 `.github/workflows/subscribe.yml` 里的 `cron`，注意换算成 UTC（北京时间减 8 小时）
  - GitHub 定时任务在高峰期可能延迟几十分钟，属正常现象
- **手动运行**：随时按第五步点 `Run workflow`

---

## 八、常见问题

| 现象 | 原因 / 解决 |
|---|---|
| 日志显示 `SOURCES_B64 长度: 0` | Secret 没配或名字拼错，回第四步检查 |
| 提示 `base64: invalid input` | Base64 复制时多了空格或换行，重新生成一整串 |
| 主订阅站点数是 0 | 源暂时不可访问，保护机制会阻止提交，不会覆盖正常订阅 |
| push 被拒绝（403 / protected branch） | 仓库 Settings → Branches 里给 `main` 加了保护，取消或允许 bot 推送 |
| raw 链接 404 | 确认仓库是 Public、分支是 `main`、文件已成功提交 |
| 国内 raw 链接打不开 | GitHub 网络问题，把订阅文件同步到 Gitee 分发（组合方案） |

---

## 内容校验（防"假存活"）

测活不只是看"能不能返回页面"，还会**校验返回内容到底是不是视频源数据**，避免把错误页、验证码页、空壳响应误当成有效源。

判定规则：

| 返回内容 | 判定 |
|---|---|
| JSON 有 `list` 且条目含 `vod_id`/`vod_name` 等视频字段 | 有效视频源 |
| XML 含 `<rss>` + `<video>` + `recordcount` | 有效视频源 |
| 标准 CMS 空结果（`list:[]` + `code:1`/`recordcount:0`，源正常只是没搜到） | 有效（空结果） |
| `403`/验证码/登录墙/`code:-1`/纯 HTML 首页/无视频字段的空壳 | 失效 |

相关开关在 `config.json`（按需调整）：

| 开关 | 默认 | 说明 |
|---|---|---|
| `content_check` | `true` | 总开关。设为 `false` 回退到旧的"只看能否返回"逻辑 |
| `content_strict` | `false` | 严格模式。设为 `true` 时连"标准空结果"也判失效 |
| `allow_empty_result` | `true` | 是否把"标准 CMS 空结果"算作有效源 |
| `invalid_keywords` | 见配置 | 命中即判失效的页面特征词（错误页/验证码等），可自行增删 |

> 说明：搜索关键词默认是 `test`。很多视频源库里没有这个词，会返回**合法的空结果**——这类源本身是好的，默认算作有效。如果你想只保留"能搜出实际视频"的源，把 `content_strict` 设为 `true` 即可。

---

## 文件说明

| 文件 | 作用 |
|---|---|
| `.github/workflows/subscribe.yml` | 定时任务配置（核心） |
| `main.py` | 抓取、测活、去重、分流的主程序 |
| `config.json` | 运行参数（超时、并发、成人处理等） |
| `sources.txt` | 空占位模板，真实源通过 Secrets 注入 |
| `真实源BASE64-填Secrets用.txt` | 真实源的 Base64，**仅用于填 Secrets，不要上传仓库**（已在 .gitignore 排除） |
| `output/subscribe.json` | 主订阅，已预置线上验证过的 84 站数据，上传后影视仓立即可用；之后由定时任务自动更新 |
| `check.py` / `web_check.py` | 本地手动测活工具 |
| `discover.py` | 从种子渠道发现新候选源 |

预置的 84 站订阅和线上 Gitee 版本完全一致（已脱敏、HTTP 200 验证过），所以即使定时任务还没跑通，影视仓填上方第六步的链接也能立刻拉到内容。

除 `真实源BASE64-填Secrets用.txt` 外，本文件夹内没有真实源、令牌等敏感数据。

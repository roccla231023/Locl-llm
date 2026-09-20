# 本地 LLM 网关（Termux Edition）

当前版本：`v0.4.0`

一个为 **Android Termux、个人使用、随用随开** 设计的轻量 LLM API 网关。

本版本的核心层级是：

```text
本地访问 Key
└── 绑定完整中转站
    ├── Grok 分组       -> 真实上游 Key A1
    ├── GPT 分组        -> 真实上游 Key A2
    ├── Gemini 分组     -> 真实上游 Key A3
    └── 模型路由        -> 指向某个分组
```

客户端始终只填写：

```text
Base URL: http://127.0.0.1:8787/v1
API Key:  当前中转站对应的本地访问 Key
```

网关根据请求 JSON 中的 `model`，先锁定本地 Key 绑定的中转站，再在该站内找到模型所属分组，最后使用该分组的真实 Key 转发。

## 特点

- 手机浏览器本地管理中转站、分组凭证、模型路由和本地访问 Key
- 一个中转站支持多个真实上游 API Key
- 模型路由明确绑定到某个分组，避免同站多 Key 随机选择
- 同一模型名可以在不同中转站重复使用，不需要改名
- 每个分组可以单独拉取和导入上游 `/v1/models`
- 支持 `Authorization: Bearer`、`x-api-key` 和无认证
- 支持额外请求头 JSON
- 支持统一 Key 兼容模式和绑定整站的本地访问 Key
- 支持普通 JSON 响应和 SSE 流式响应
- SQLite 持久化，关闭终端后配置不会丢失
- 请求日志显示中转站、分组、首字耗时和总耗时，不保存聊天正文与完整 Key
- 默认只监听 `127.0.0.1`
- **零第三方 Python 依赖**：不需要 Docker、Node.js、Flask、Redis 或 MySQL

## v0.4.0 模型变化检查

本版本增加面向中转站模型新增、下线和改名场景的半自动维护工具：

- 分组凭证卡片新增“检查变化”，可单独检查一个分组；
- 模型路由页新增“检查全部变化”，依次检查所有启用的分组；
- 检查结果区分上游新增模型、正常路由、疑似下线或改名的启用路由以及检查失败；
- 新增模型可勾选后批量导入，路由继续绑定被检查的具体分组；
- 上游模型改名时，可以选择新模型并保留原“对外模型名”，客户端的本地 URL、Key 和模型名均无需修改；
- 已失效路由也可手动停用或在本次检查中暂时忽略；
- 同站模型名冲突只提示，不会自动移动分组或覆盖已有路由；
- 检查只读取上游 `/v1/models`，不会自动修改任何配置，也不会向浏览器返回真实上游 Key。

由于 `/v1/models` 通常不提供“旧模型改名为哪个新模型”的对应关系，网关不会自行猜测替代关系，最终替换必须由本地管理员确认。

本次不新增数据库表或字段，数据库结构版本仍为 `SCHEMA_VERSION = 3`。

## v0.3.2 体验优化

- 弹窗的"取消"和"×"按钮不再触发表单验证，可以随时关闭空白表单；
- 分组凭证页面通过"管理分组"进入后显示筛选状态栏，可一键"显示全部"；通过侧边栏导航进入分组凭证时自动重置筛选；
- 弹窗外点击半透明遮罩可直接关闭弹窗；
- 保存按钮在等待 API 响应期间自动禁用，防止重复提交；
- 移动端侧边栏打开时显示遮罩，点击遮罩关闭菜单；
- 错误提示 Toast 延长到 5 秒，成功提示保持 2.6 秒。

## v0.3.1 可靠性修复

本版本针对“真实上游 Key 可用，但客户端经过本地 Key 偶发失败”的链路进行了专项加固：

- 多分组中转站的公共 `/v1/models` 只展示已经绑定具体分组、能够实际路由的模型，不再展示选中后必然返回 409 的未映射模型；
- 一个中转站只有一个启用分组时，仍可展示和直通该分组拉取到的未映射模型；
- 客户端使用 `Authorization: Bearer` 或 `x-api-key` 均可识别本地 Key；同时存在两个认证头时，会尝试其中的有效本地 Key；
- 转发前移除客户端本地认证头，再按模型路由写入分组的真实 Bearer 或 `x-api-key`；
- 保持请求 JSON 中除顶层 `model` 以外的值和类型不变，包括 `0`、`false`、`null`、空字符串、数组及嵌套对象；
- 支持部分 SDK 使用的 HTTP/1.1 chunked JSON 请求体；
- 保留中转站 Base URL 自带的查询参数，并与客户端接口查询参数合并；
- 已删除的旧版默认凭证不会在 v3 数据库重启后被遗留字段重新生成。

## 当前支持范围

面向 **OpenAI 兼容中转站**，即上游通常提供：

```text
https://relay.example.com/v1
Authorization: Bearer sk-xxx
```

网关接受所有 `/v1/*` JSON POST 请求，只要请求体含 `model` 字段，就能按模型路由并原样转发其余字段。已重点测试：

- `GET /v1/models`
- `POST /v1/chat/completions`
- SSE 流式输出
- 同一中转站内多个分组使用不同真实 Key

同样适用于 JSON 请求格式的：

- `/v1/responses`
- `/v1/embeddings`
- `/v1/images/generations`
- `/v1/moderations`
- 其他包含 `model` 字段的 OpenAI 兼容 `/v1/*` 路径

暂不支持：

- Anthropic `/v1/messages` 原生格式与 OpenAI 格式互转
- Gemini 原生 `generateContent` 格式转换
- multipart 文件上传（如音频转写）
- 同一个统一 Key 下的同名模型自动故障转移和备用渠道
- 自动计费、用户注册、充值与多用户系统

## Termux 安装

建议使用 F-Droid 或 GitHub 发布的新版 Termux，不要使用多年未更新的 Play 商店旧版。

先把 ZIP 从 Android 共享“下载”目录复制到 Termux 私有目录，再解压和安装：

```bash
termux-setup-storage
pkg install -y unzip
cp ~/storage/downloads/local-llm-gateway-v0.4.0.zip ~/
cd ~
unzip local-llm-gateway-v0.4.0.zip
cd local-llm-gateway
chmod +x install-termux.sh
./install-termux.sh
```

不要直接在 `~/storage/downloads` 中运行项目。Android 共享存储通常带有 `noexec` 限制，也不适合保存包含真实 Key 的数据库。

安装脚本只会通过 Termux 安装：

```text
python
ca-certificates
```

项目本身不需要运行 `pip install`。

## 启动与关闭

启动：

```bash
./start.sh
```

看到：

```text
本地 LLM 网关已启动
管理后台：http://127.0.0.1:8787
API 地址：http://127.0.0.1:8787/v1
按 Ctrl+C 停止服务
```

用手机浏览器打开：

```text
http://127.0.0.1:8787
```

第一次打开时设置管理员密码。

关闭：回到 Termux，按 `Ctrl+C`。

## 网页配置步骤

### 1. 添加中转站

进入 **中转站 → 添加中转站**：

- 名称：自己能认出的名称
- API Base URL：例如 `https://relay.example.com/v1`
- 启用或禁用站点

中转站页面只保存站点身份和 Base URL。**真实上游 Key 不在这里填写，而是在“分组凭证”页面填写。**

Base URL 支持以下形式，网关会自动避免重复 `/v1`：

```text
https://relay.example.com
https://relay.example.com/v1
https://relay.example.com/v1/chat/completions
```

### 2. 添加分组凭证

进入 **分组凭证 → 添加分组凭证**，选择中转站并填写：

- 分组名称，例如 `Grok 分组`、`GPT 分组`、`Gemini 分组`
- 该分组对应的真实上游 API Key
- 认证方式
- 可选额外请求头
- 启用状态

例如：

```text
站 A / Grok 分组    -> 真实 Key A1
站 A / GPT 分组     -> 真实 Key A2
站 A / Gemini 分组  -> 真实 Key A3
```

真实 Key 只在手机本地管理页面录入，不要发送到聊天、截图、日志或诊断输出中。

### 3. 拉取并导入模型

在中转站或分组凭证卡片点击 **拉取模型**：

1. 选择具体分组；
2. 网关使用该分组的真实 Key 请求该站的 `/v1/models`；
3. 勾选模型并导入；
4. 导入的每条路由会记录所属中转站和分组。

也可以在 **模型路由 → 添加路由** 手动填写：

- 中转站
- 分组凭证
- 对外模型名：客户端请求的名称
- 上游模型名：转发给上游的真实模型 ID

同一个中转站内的对外模型名必须唯一。不同中转站可以使用相同的对外模型名。若同站已经存在同名路由，导入会报告冲突，不会静默改用另一个分组。

### 4. 检查中转站模型变化

日后中转站增加、删除或改名模型时，可以：

1. 在“分组凭证”卡片点击 **检查变化**，只检查这个分组；或在“模型路由”点击 **检查全部变化**；
2. 在“上游新增 / 尚未导入”中勾选需要的模型并批量导入；
3. 在“疑似下线或改名”中，为旧路由选择一个当前上游模型；
4. 点击 **替换并保留对外名**，只更新上游模型名；
5. 不再使用的旧路由可以停用，也可以本次暂时忽略。

例如原路由是：

```text
gpt-main -> gpt-4.1-2026-01
```

上游改名后更新为：

```text
gpt-main -> gpt-4.1-2026-07
```

客户端仍然请求 `gpt-main`，本地 URL 和本地 Key 也都不变。

注意：“消失一个旧模型，同时新增一个新模型”不一定代表两者是改名关系，也可能是权限或上游临时异常。因此检查不会自动替换，必须由你确认。建议先确认中转站公告或模型能力，再执行替换。

### 5. 创建本地访问 Key

进入 **渠道 Key → 添加渠道 Key**，绑定一个完整中转站：

```text
本地 Key A -> 站 A
本地 Key B -> 站 B
```

注意：本地访问 Key 绑定的是整个中转站，不是某一个分组。

客户端配置：

```text
Base URL: http://127.0.0.1:8787/v1
API Key:  站 A 的本地 Key A
```

客户端请求：

```json
{
  "model": "Chicken-GPT",
  "messages": [
    {"role": "user", "content": "你好"}
  ]
}
```

网关处理：

```text
本地 Key A
-> 站 A
-> Chicken-GPT 对应的 GPT 分组
-> 使用真实 Key A2
-> 转发到站 A
```

如果一个中转站只有一个启用分组且没有显式模型映射，网关保留原模型名直通这个分组；如果一个中转站有多个启用分组但没有映射，网关会返回明确错误，绝不随机选择 Key。

### 6. 统一 Key（兼容模式）

概览页和设置页仍保留统一 API Key。统一 Key 按全局模型路由选择中转站和分组：

- 全局模型名唯一时可以使用；
- 同名模型出现在多个中转站时返回歧义；
- 这时请使用绑定目标中转站的本地访问 Key。

## API 示例

先在网页中复制某个本地访问 Key：

```bash
export LOCAL_KEY='网页中复制的本地访问 Key'
```

模型列表：

```bash
curl http://127.0.0.1:8787/v1/models \
  -H "Authorization: Bearer $LOCAL_KEY"
```

聊天请求：

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer $LOCAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "你的对外模型名",
    "messages": [{"role":"user","content":"你好"}],
    "stream": true
  }'
```

## 数据迁移

`v0.1.2` / `v0.2.0` 数据库会在启动时原地迁移：

```text
旧 channels.api_key
        ↓
该中转站的“默认凭证”

旧 models.channel_id
        ↓
保留 channel_id，并补上默认 credential_id
```

迁移保留：

- 管理员密码；
- 统一 Key；
- 旧中转站和 Base URL；
- 旧真实上游 Key、认证方式、额外请求头；
- 旧模型映射和模型别名；
- 本地访问 Key 及其整站绑定关系；
- 请求日志。

升级前建议先备份：

```bash
./backup.sh
```

备份文件包含真实 Key，请只保存在手机私有目录，不要上传或分享。

## 真实中转站验收

真实上游 Key 只在本机管理网页的“分组凭证”表单中填写，不要粘贴到聊天、截图或诊断输出中。

1. 在 Termux 前台执行 `./start.sh`；
2. 浏览器打开 `http://127.0.0.1:8787`；
3. 添加一个中转站；
4. 在该站下添加多个分组凭证；
5. 分别在每个分组卡片点击“拉取模型”；
6. 导入模型或手动建立模型与分组绑定；
7. 创建绑定该站的本地访问 Key；
8. 用同一个本地 Key 请求多个模型，确认它们使用不同分组；
9. 在请求日志确认中转站和分组正确，且没有完整真实 Key；
10. 回到 Termux 按 `Ctrl+C`，再次 `./start.sh`，确认配置仍然存在。

排查问题时只需记录 `/health` 返回、HTTP 状态码、管理页中的脱敏错误和 Termux 异常堆栈。发送前应再次确认内容中没有真实上游 Key。

## 自定义端口

临时更改端口：

```bash
GATEWAY_PORT=9000 ./start.sh
```

也可以直接：

```bash
python app.py --port 9000
```

除非你理解局域网暴露的风险，否则不要修改默认监听地址。若确实需要让局域网设备访问：

```bash
GATEWAY_HOST=0.0.0.0 ./start.sh
```

## 数据、备份与恢复

数据库路径：

```text
data/gateway.db
```

里面包含真实上游 Key，文件权限会设置为仅当前用户可读写。

创建一致性备份：

```bash
./backup.sh
```

恢复前先关闭网关：

```bash
./restore.sh backups/gateway-日期时间.db
```

恢复脚本会在替换前保留当前数据库副本。备份同样包含真实 Key，不要上传到网盘、GitHub 或发送给别人。

## 安全说明

- 默认仅监听 `127.0.0.1`；
- 管理密码使用 PBKDF2-SHA256，随机盐保存；
- 管理会话仅保存在进程内存，重启后失效；
- 真实上游 Key 保存在本地 SQLite，当前没有数据库静态加密；
- 上游请求不自动跟随 HTTP 重定向；
- 不把客户端本地 `Authorization` / `x-api-key` 直接传给上游；
- 日志不保存聊天正文、完整本地 Key 或完整真实 Key；
- 管理列表中的真实 Key 打码，只有本地管理员编辑表单可以查看；
- `.gitignore` 排除数据库、备份、缓存和 ZIP；
- ZIP 发布包不包含 `gateway.db`、`backups/`、缓存或任何真实 Key。

如果手机已 root、Termux 文件目录被其他程序读取，或你主动暴露 `0.0.0.0`，请自行加强设备与网络安全。

## 开发与测试

要求 Python 3.11+：

```bash
python -m unittest discover -s tests -v
```

当前测试覆盖：

- 管理后台初始化与 Cookie 会话；
- 旧数据库迁移；
- 旧配置、Key、模型和日志保留；
- 一个中转站多个分组凭证；
- 多个模型绑定不同分组；
- 同一个本地 Key 请求多个模型并使用正确真实 Key；
- 同站重复模型名拒绝；
- 多分组下未映射模型不随机选 Key；
- 每个分组单独拉取模型；
- 发现上游新增、正常和已消失模型；
- 批量导入新增模型并保持具体分组绑定；
- 保留对外别名替换上游模型名后继续成功路由；
- 模型变化报告不泄露真实上游 Key；
- 不同中转站同名模型隔离；
- 多分组公共模型列表不展示无法路由的未映射模型；
- 本地 Bearer / `x-api-key` 识别及上游认证头替换；
- `0`、`false`、`null`、空值及嵌套 JSON 值原样保留；
- chunked JSON 请求体与 Base URL 查询参数保留；
- v3 重启不重新生成已删除的旧默认凭证；
- 普通请求、SSE 流式请求、首字耗时；
- 错误脱敏和 ZIP 清洁性。

## 目录结构

```text
local-llm-gateway/
├── app.py                    # HTTP 服务、管理 API、统一 API 路由
├── gateway/
│   ├── database.py           # SQLite 数据层与 v0.1.2/v0.2.0 迁移
│   ├── proxy.py              # URL、上游认证、模型拉取与请求头处理
│   └── security.py           # 密码哈希与管理会话
├── static/
│   ├── index.html            # 本地管理页面
│   ├── app.css
│   └── app.js
├── tests/
│   └── test_integration.py
├── data/                     # 本地数据库（不会提交 Git）
├── install-termux.sh
├── start.sh
├── backup.sh
└── restore.sh
```

## 许可证

MIT License。仅用于你有权使用的中转站与 API Key，并遵守对应服务条款。

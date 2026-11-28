# Nexus Testgate

跨项目的测试门禁，自带 Codex 用户级技能、运行时和安装管理。目标仓库无需增加任何工具相关文件、配置或说明，也不依赖 `codex-home-sync` 或 `codex-skills`。

运行时只依赖 Python 3.10+。默认规则使用本机执行；Linux/macOS 支持本机执行，Windows 使用 Docker 后端或 WSL。项目的测试工具和依赖需要预先准备。

## Codex 用户级接入

每套 agent 环境准备一次：从 Releases 取得单文件工具，在仓库外执行安装：

```bash
python /path/outside-repositories/testgate.pyz codex install
```

安装器将完整技能和运行时放入 `${CODEX_HOME:-~/.codex}/skills/nexus-testgate`，在用户 `~/.local/bin` 创建 `testgate` 启动器。安装完成后，原始下载文件可以删除。技能会在下一轮对话可用，自动发现保持启用。

随后在任意目标项目目录使用：

```bash
testgate -- make test
testgate -- python -m pytest -q --tb=short
```

如果用户 bin 目录不在 PATH，技能会调用自身的 `scripts/run.py`，不需要修改 shell、全局指令或项目配置。Windows 创建 `testgate.cmd`；技能管理支持 Windows，实际测试仍按下文的平台边界执行。

```bash
testgate codex status
testgate codex update
testgate codex uninstall
```

旧版本运行 `codex update` 若出现 HTTP 415，请从 Releases 下载新版单文件到仓库外，运行 `python /path/to/new-testgate.pyz codex update --local` 修复。该操作跳过旧更新器的联网查询，之后即可恢复使用 `testgate codex update`。

`update` 从公开 GitHub API 获取最新正式版本，校验 SHA-256 后同时更新技能和运行时。使用指定版本离线更新时运行 `python /path/to/new-testgate.pyz codex update --local`。`--codex-home` 和 `--bin-dir` 可指定仓库外的安装位置。已存在的外来技能、命令或被手工修改的安装文件不会被覆盖或删除。

工具与接入资源在同一仓库、同一版本发布；安装器不修改 `AGENTS.md`、Codex 配置、PATH、Git 配置或其他技能仓库。技能负责发现和调用，不拦截绕过它直接执行的测试命令。

## 独立执行

构建后的 `testgate.pyz` 是独立可分发文件，不必安装到项目环境：

```bash
python /path/to/testgate.pyz
```

默认调用当前目录的 `make test`，强制执行以下内置限额：

| 指标 | 默认预算 |
| --- | --- |
| 整次测试命令耗时 | 10 秒 |
| stdout/stderr 合计 | 64 KiB |
| 输出行数 | 2000 行 |
| 单行长度，不含 LF | 4096 字节 |

默认值是一套开箱即用的基线，可以通过中央策略整体替换。当前项目没有 `make test` 时，默认策略允许显式指定命令：

```bash
python /path/to/testgate.pyz -- python -m pytest -q --tb=short
python /path/to/testgate.pyz -- npm test
```

工具进程保持当前 PATH，不主动安装依赖，不改变项目虚拟环境。`make test` 应调用原始测试命令，不要在其中再次调用默认执行 `make test` 的 Testgate。

## 联网使用与中央规则

公开源码：[Gtyro/nexus-testgate](https://github.com/Gtyro/nexus-testgate)。下载后直接在目标项目目录运行：

```bash
curl --fail --location https://github.com/Gtyro/nexus-testgate/releases/latest/download/testgate.pyz --output /tmp/testgate.pyz
python /tmp/testgate.pyz
```

Windows PowerShell：

```powershell
Invoke-WebRequest 'https://github.com/Gtyro/nexus-testgate/releases/latest/download/testgate.pyz' -OutFile "$env:TEMP/testgate.pyz"
python "$env:TEMP/testgate.pyz" --policy $env:TESTGATE_POLICY
```

下载入口同时提供 `default-policy.json` 和 SHA256SUMS。自动化应校验预先信任的校验值，并使用固定版本入口，例如 `https://github.com/Gtyro/nexus-testgate/releases/download/v0.2.2/testgate.pyz`。下载文件应存放在仓库外；这是环境安装入口，无须逐项目重复执行。

统一规则使用本地 JSON 或 HTTPS JSON，可在每台机器/CI 的环境变量中设置一次，后续不必逐项目配置：

```bash
export TESTGATE_POLICY='https://github.com/Gtyro/nexus-testgate/releases/download/v0.2.2/default-policy.json'
# 需要固定规则内容时，再设置已确认的 SHA-256：
# export TESTGATE_POLICY_SHA256='已确认的策略文件SHA256'
python /tmp/testgate.pyz
```

也可以通过 `--policy`、`--policy-sha256` 显式传入。策略 URL 只能使用 HTTPS，重定向不得降级到 HTTP。下载错误、摘要不符、未知字段、格式不合法或不支持的约束一律失败，不回退到默认策略。每次结果记录完整策略摘要。

若环境无法连接 `github.com`，可使用 GitHub API 的公开 release asset URL 下载文件：请求时带 `Accept: application/octet-stream`。Testgate 的 HTTPS 策略请求已包含此请求头，因此也支持 `https://api.github.com/repos/Gtyro/nexus-testgate/releases/assets/<策略资产ID>`。资产 ID 可从公开的 releases API 查询，无需登录。

中央策略是受信任的配置，其中的 `command` 会实际执行。固定 `allow_command_override: false` 可拒绝命令覆盖。CI 还需固定执行器和策略摘要，并将对应检查设为合并必需项；本地运行工具不等于不可绕过的隔离环境。

## 可执行规则

| 对象 | 检查方式 |
| --- | --- |
| 测试整体耗时 | 使用单调时钟监管，超时强制结束 |
| 输出字节、行数、单行长度 | 流式检查 stdout/stderr，超限立即失败 |
| 内存、CPU 配额、任务数 | Docker 后端分别设置 memory、cpus、pids-limit |
| 网络 | Docker 后端默认 `network: none` |
| 临时空间 | Docker 根文件系统只读，`/tmp` 使用有大小上限的 tmpfs |
| 跳过、用例数量、失败 | 读取本次生成的 JUnit XML；跳过超限、数量不足或报告失败则不通过 |
| 行/分支覆盖率 | 读取本次生成的 Cobertura XML，低于阈值则不通过 |
| 产物大小 | 测试结束后计算指定文件/目录大小，超限失败 |

规则示例见 [严格策略](examples/strict-policy.json) 和 [Docker 策略](examples/docker-policy.json)。所有预算值均集中在策略中，不从项目文件隐式读取，也不能通过 CLI 参数悄悄放宽。

JUnit/Cobertura 报告是可选适配：启用后必须存在且本次重新生成，旧报告、空用例报告（默认最少 1 项）、缺失报告和无效格式都会失败。报告路径相对于工具提供的仓库外报告目录。pytest 可以这样执行，不改项目配置：

```bash
testgate --policy /outside-project/policy.json -- python -m pytest -q '--junitxml={testgate_reports}/junit.xml'
```

`{testgate_reports}` 在传入测试命令前替换为实际目录；测试进程也能通过 `TESTGATE_REPORT_DIR` 环境变量取得该目录。覆盖率报告需要项目已有的覆盖率工具。Testgate 不自动安装插件，也不将总覆盖率当成变更覆盖率。

JUnit 按实际 `testcase` 元素计数，而非相信汇总数量。当前不推断重试次数，也不做变异测试、变更覆盖率或测试价值判断。源代码和测试报告均由被检查项目产生，防止蓄意伪造还需要受保护的 CI。

## Docker 执行

```bash
python /path/to/testgate.pyz --policy /path/to/docker-policy.json
```

Docker 策略必须指定含测试依赖和命令入口的 Linux 镜像。示例 `python:3.12-slim` 只表示镜像字段格式，不包含 GNU Make 或项目依赖；使用时应集中准备实际测试镜像，并建议固定镜像摘要。本机项目以可写目录挂载到 `/workspace`，使用容器内工具运行，不挂载 Docker socket。

CPU 配额限制并行计算能力，不是累计 CPU 秒数预算；`pids` 包括线程。`tmpfs_bytes` 只限制容器 `/tmp`，不限制可写 workspace；产物检查属于执行后的验收，不能阻止测试写满宿主磁盘。运行前检查 Docker daemon 的内存、swap、CPU 和 pids 控制能力，缺少请求的能力时直接失败。内核环境部署后仍应执行容器集成验证。

## 结果与退出码

```bash
testgate --json /outside-project/testgate-result.json
testgate --report-dir /outside-project/test-reports
python /path/to/testgate.pyz --show-policy
```

默认报告目录位于仓库外的系统临时目录，执行完后删除；显式指定 `--report-dir` 时保留报告。`--json`、`--report-dir` 和安装位置若落在目标项目或任何有效 Git 仓库内都会被拒绝，包括忽略目录。工具不在目标项目保存状态；被测项目原有测试自行产生的缓存或产物仍由项目决定。Docker 后端将外部报告目录挂载到 `/testgate-reports`。

成功仅打印一行摘要。失败打印原因和最多 4096 字节已捕获日志尾部；完整日志不落盘、不常驻内存。超限后不再排空输出，统计值为截止前读取数量。原始字节计数不包括框架内部捕获而未输出的日志；行数按 LF 分隔，末尾无 LF 的非空内容也计一行。

| 退出码 | 含义 |
| --- | --- |
| 0 | 命令及策略检查均通过 |
| 2 | 策略、环境或启动错误 |
| 124 | 测试时间超限 |
| 125 | 输出超限 |
| 126 | 报告或产物检查失败 |
| 其他 | 保留测试命令失败码，信号映射为 128 + 信号编号 |

测试命令也可能使用上述退出码，机器判断原因请读取 JSON 的 `violations` 和 `command_exit_code`。

耗时预算包含命令启动、测试收集、准备、执行和命令自身清理；Docker 守护进程探测及容器创建在计时前完成，各设 10 秒和 30 秒上限。终止、容器清理和报告分析仍有额外开销，不提供实时系统保证。原生后端终止整个进程组，无法管理主动脱离该组的进程；需要完整资源边界时采用 Docker 后端。镜像必须预先准备，门禁不会隐式下载镜像。

## 开发与公开发布

```bash
python -m unittest discover -s tests -v
python tools/build.py
python dist/testgate.pyz --version
```

`dist/testgate.pyz`、`dist/default-policy.json` 与 `dist/SHA256SUMS` 可发布到任意 HTTPS 下载入口。技能与启动脚本作为包资源内置在 zipapp 和安装包中，由同一个安装器管理。也可使用 `python -m pip install .` 在仓库外的工具环境安装 `testgate` 命令，或在 uv 可用时使用 `uvx --from git+https://github.com/Gtyro/nexus-testgate@v0.2.2 testgate`；源码和单文件分发均不依赖 zero-nexus。

GitHub Actions 自动测试 Linux/macOS/Windows 上的 Python 3.10 和 3.12；Windows 验证策略加载、打包与容器参数，本机执行测试仅运行在 POSIX。Linux CI 额外使用 Docker 验证资源限制。推送 `v*` 标签后，必须先通过完整跨平台验证，才会构建 zipapp 并附加到 GitHub Release。

手动运行 Docker 验证：

```bash
TESTGATE_DOCKER_TEST_IMAGE=python:3.11-slim python -m unittest discover -s tests -k DockerTests -v
```

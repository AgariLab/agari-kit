# agari-kit

AgariLab 公共工具，目标运行环境为 Python 3.12 + RiichiEnv 0.4.10。
当前实现存储检查、Agent 接口、自博弈采集与引擎回放验证、整场登记与冻结划分、
轮换座位评测、独立离线牌谱回放及 riichi.dev 客户端。随机／摸切／基础牌效与外部模型共用接口。
基线已完成 1,000 半庄可靠性运行；组织 fork 已建立，外部强对手和实际线上验收仍待完成。

## 存储与安装

默认大文件根目录为 `/Volumes/新加卷/AgariLab`。`AGARILAB_STORAGE_ROOT` 可显式覆盖；
Linux 外置挂载点还可设置 `AGARILAB_STORAGE_MOUNT`。Mac `/Volumes/<卷名>` 路径强制检查挂载。
根目录或子目录不能通过符号链接逃离外置卷；没有自动回退路径。

以下命令需要外置卷写权限、可用网络和 Python 3.12。启动器在调用 uv 前检查存储并配置项目缓存：

包索引已在本仓库 `pyproject.toml` 配置为清华源：
`https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple`，下面的命令自动使用该源。

```sh
python3 scripts/with_storage.py uv sync --frozen
python3 scripts/with_storage.py uv run --frozen agari storage check --probe
```

`uv.lock` 已生成，包索引均为清华源；Python 3.12.16 与固定依赖已实际安装、验证。
常规安装使用 `--frozen`；仅在明确更新依赖时运行 `uv lock` 并审查变化。
启动器可用系统 Python 3.9 执行，仅用于安装前检查；项目运行仍固定 Python 3.12。
启动器设置 `UV_LINK_MODE=copy`，适应外置卷缓存和内置盘虚拟环境的跨盘安装。

本机可按需给单条命令设置 `HTTPS_PROXY=http://127.0.0.1:10800/ HTTP_PROXY=http://127.0.0.1:10800/`。
不要修改其他项目的全局代理或缓存配置。

## 已实现命令

```sh
agari storage check                    # 只读，不代表已确认可写
agari storage init                     # 写入探测并建立目录
agari storage env                      # 输出缓存配置 JSON
agari data inspect /path/game.jsonl.gz # 流式结构检查，不是麻将合法性验收
agari data validate /path/game.jsonl.gz --trace /path/game.trace.jsonl.gz
agari data selfplay --name selfplay-v1 --games 1000 --seed-start 0
agari replay /path/game.jsonl.gz --output /path/new-replay.html
agari data split selfplay-v1/catalog.sqlite selfplay-v1/split-v1.json
agari evaluate --candidate random --opponents tsumogiri tsumogiri tsumogiri --seed-count 2 --run smoke-v1
agari evaluate --candidate efficiency --opponents random tsumogiri random --seed-count 250 --run reliability-v2
agari connect validate --agent tsumogiri --token-file .riichi-token --run validation-v1
agari connect ranked --agent efficiency --token-file .riichi-token --games 100 --run ranked-v1
```

划分命令的输入／输出相对 `datasets/manifests/`；评测结果相对 `runs/`。
一个种子执行四次完整半庄、轮换四座。`--seed-count 250` 对应 1,000 半庄可靠性运行，
`2500` 对应 10,000 场评测。已完成 250 个种子／1,000 场可靠性运行，
见 [基线报告](reports/reliability-20261028-v1/report.json)；正式 10,000 场晋级评测尚未开展。
报告包含平均顺位、名次率、得失点和按种子组 bootstrap 的 95% 区间；
单种子不报告置信区间，达到样本量也不会自动宣称棋力提升。
该可靠性报告的对手是随机与摸切智能体，不能用于证明强基线复现或世界级棋力。
研究仓库导出的 PPO 模型也已完成 [1,000 半庄可靠性验收](reports/ppo-local-reliability-20261028-v1/README.md)，
包含权重和源码绑定、独立候选延迟与逐场统计；该小模型同样尚未达到强基线棋力验收。

`efficiency` 只用当前手牌和公开信息，先最小化向听数，再最大化有效进张，平手时保留赤五；
接受和牌和立直，拒绝可选鸣牌。向听计算调用固定 RiichiEnv，不自行实现麻将规则。

## 离线回放

`agari replay` 接受完整四人 MJAI 的 `.jsonl` 或 `.jsonl.gz`，生成一个可直接用浏览器打开的
HTML 文件。输出目录须已存在，已有文件不覆盖；建议写到外置卷 `runs/` 下。
使用外置文件系统兼容的独占创建；可捕获的写入失败会清理本次文件，进程崩溃或拔盘仍可能留下
不完整产物，重试时应使用新路径或先确认并删除该残留文件。
页面支持前后事件、前后巡、切局、进度拖动、自动播放和桌面朝向切换，显示手牌、弃牌、鸣牌、
宝牌和分数。末局结算和 `end_game` 为不同步骤，保留终局剩余供托分配的变化，并可回退查看。
需要支持 `DecompressionStream` 的近期 Chrome、Safari 或 Firefox；实际交互验收
使用 Chrome。JS、WASM 和牌面来自已安装的固定 RiichiEnv 0.4.10，并全部内联，
无需服务进程、CDN、上传牌谱或联网加载。页面附上游来源、完整 Apache-2.0 许可证和资产 SHA-256。

```sh
agari replay /path/game.jsonl.gz --output /path/research.html
agari replay /path/game.jsonl.gz --player 0 --output /path/player-0.html
```

默认是明确标注的**全信息研究视角**。`--player 0..3` 在导出前通过现有玩家事件白名单移除
其他玩家的初始暗牌、未公开摸牌和任意分析注释，浏览器收到的文件中也没有这些牌；切换朝向
不会恢复隐藏牌。这是保守可见视角，暂不补录和牌／流局揭示的对手手牌。
两种 HTML 都包含整场历史和未来事件、终局信息，均不能作为当时 Agent 的策略输入；
Agent 的 Observation 和网络协议保持原接口。

分数依据日志中的 `scores`，或顺序累计立直扣点与结算 `deltas`。不调用上游 Python
`MetadataInjector`，也不重算和牌得分／役种；上游结算弹窗可能显示 `Score data unavailable`，
实际各家分数以牌桌与原始事件为准。导出只做结构与显示字段检查，清单仍标记 `structural-only`，
不能替代 `agari data validate --trace ...` 的引擎验证。原有只读摘要使用 `agari data inspect`。

单元回归覆盖投影、结算、错误输入、无覆盖写入和脚本注入。真实浏览器回归独立运行，使用本机
Chrome 临时 profile 和已有 `websockets`，无需额外驱动依赖；浏览器禁用网络，只读取指定冻结
manifest 的第一场 `train` 对局，不读取 held-out 牌谱内容：

```sh
AGARILAB_STORAGE_ROOT=/Volumes/新加卷/AgariLab \
AGARI_REPLAY_TRAIN_MANIFEST=/Volumes/新加卷/AgariLab/datasets/manifests/smoke-v1/split-v1.json \
.venv/bin/python -m unittest discover -s tests/browser -v
```

可设置 `CHROME_BIN` 指向 Chrome/Chromium，`AGARI_REPLAY_EVIDENCE_DIR` 指向新的外置或临时
目录，以保留 HTML、渲染截图和 train 来源校验信息；不设置则测试后清理临时产物。
`AGARI_REPLAY_TRAIN_GAME` 可指定该 manifest 的 `train` 分区内某个牌谱文件名，未属于该分区会失败。

## riichi.dev 连接

连接前需激活虚拟环境，或用存储启动器执行 `uv run --frozen agari ...`。Token 可放入本地环境
`RIICHI_BOT_TOKEN`，或保存为仅含 Token 的 `.riichi-token` 文件，使用 `--token-file` 指定。
该文件已忽略提交；Token 内容不作为 CLI 参数，也不写入日志。客户端只响应唯一 `request_action`，记录 `action_ack`，
使用独立进程控制推理时限；超时后终止进程、记录降级并让服务器默认出牌，避免丢失事件状态后继续假装正常推理。
断线会终止本次运行，不将新的匹配算作旧局恢复。验证模式等待 `validation_result`；
线上协议的原始事件不会重复交给 Agent，只有 Observation 中的事件供 Agent 消费。
2026-10-28 已使用研究仓库的固定 PPO 完成一次真实验证，120 次动作均 accepted，
服务端返回 `passed=true`，用户确认 Bot active。完整证据见
[平台验证报告](https://github.com/AgariLab/agari-research/tree/main/experiments/riichi-validation-20261028-v1)。
active Bot 直接进入排位，不重复验证。

长批次排位使用监督脚本，在每场结束后检查完整终局、动作回执、超时与降级，再决定是否续排：

```sh
python3 scripts/with_storage.py uv run --frozen python scripts/run_online_campaign.py \
  --agent efficiency --token-file .riichi-token --games 100 \
  --idle-timeout 1800 --run ranked-supervised-v1
```

研究模型须在含研究依赖的环境中运行同一脚本，并传入对应 `@agent.json`。
请求数、发送数和 accepted 数必须相同且非零；错误回执、超时、降级或身份变化会停止后续匹配，
同时保留停止场的结果。成绩不参与停止条件，既有目录不覆盖。summary 每场更新完成数、健康数、计数和处理延迟；
脚本、模型、工具源码和引擎摘要逐场核验。同一 Bot 同时只能运行一个排位连接。

## 数据与模型版本

采集器保留原始引擎日志，另存补齐终局分数的压缩 MJAI、种子和完整动作轨迹。
每场独立重放，校验原生合法动作、玩家可见 Observation 哈希、牌数、逐事件日志和终局分数，
成功后生成绑定文件哈希的验证收据。失败文件复制到 `datasets/quarantine/<name>/` 并记录原因。
采集状态使用 SQLite，元数据导出 Parquet；重复采集会重新校验并复用已完成场次。
恢复时必须使用相同配置、源码和模型，改变版本应选择新的数据集名称。

`Catalog.register` 是内部登记接口；单独写入 `engine-verified` 字符串不能取得训练资格。
`agari data split` 对实际文件重新运行引擎验证，再按整场去重、时间顺序划分 90/5/5。
四个视角从整场继承分区；冻结文件原子发布，不可被不同内容覆盖。没有种子动作轨迹的
第三方 MJAI 目前只能取得结构检查结果，尚未实现通用第三方牌谱合法性验证。

评测、采集和连接的 Agent 参数支持 `@/path/agent.json`。例如研究仓库导出的描述文件：

```json
{
  "factory": "agari_research.model_agent:ModelAgent",
  "kwargs": {"device": "cpu"},
  "artifacts": {"weights_path": {"path": "ppo.pt", "sha256": "<64位小写SHA-256>"}}
}
```

执行环境需安装 factory 所属 Python 包；模型文件位于描述文件目录内。
这是显式加载本地代码的接口，应使用自己确认的 factory。运行清单绑定描述内容、实际权重哈希和
factory 所属包源码。每局开始检查版本，防止同一实验混入不同模型；评测另记录候选模型的决策延迟。
运行同时绑定实际引擎二进制 SHA-256，区分包版本号相同的上游与 fork 构建。
可选 `Agent.close()` 用于释放外部资源；采集与评测在正常结束、异常和部分加载失败时都执行清理。

## 测试

无需下载依赖的独立逻辑检查（测试只写系统临时目录中的微小样本）：

```sh
.venv/bin/python -m unittest discover -s tests -v
```

安装固定依赖后，必须显式运行真实引擎测试；缺依赖会失败，不会作为跳过计入通过：

```sh
python3 scripts/with_storage.py uv run --frozen python -m unittest discover -s tests/integration -v
```

规则场景覆盖赤五、吃碰杠、立直两段、振听、抢杠、多家和了、流局和终局。
固定上游 0.4.10 的一个摸切映射缺陷保留为明确的 expected failure；公共客户端自行严格生成
物理摸牌对应的摸切标记，修复在组织 fork 单独验证。历史 1,000 场报告包含 700,448 次合法决策，
其源码指纹代表报告生成时的版本，不替代当前版本与未来模型的验收。

## 来源

接口核对基于 [RiichiEnv v0.4.10 类型定义](https://github.com/smly/RiichiEnv/blob/v0.4.10/src/riichienv/_riichienv.pyi)。
规则由 RiichiEnv 负责，不另行实现规则引擎。线上适配依据 [riichi.dev 协议](https://riichi.dev/docs/protocol)。
组织引擎 fork 与版本晋级方式见 [引擎维护说明](docs/engine-fork.md)。
外部对手可用权重、许可证和真实平台接口调研见 [外部对手与平台说明](docs/external-opponents.md)。

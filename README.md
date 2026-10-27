# agari-kit

AgariLab 公共工具，目标运行环境为 Python 3.12 + RiichiEnv 0.4.10。
当前实现存储检查、Agent 接口、自博弈采集与引擎回放验证、整场登记与冻结划分、
轮换座位评测及 riichi.dev 客户端。随机／摸切／基础牌效与外部模型共用接口。
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
agari replay /path/game.jsonl.gz       # 当前提供牌谱摘要，图形回放待接入
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

`efficiency` 只用当前手牌和公开信息，先最小化向听数，再最大化有效进张，平手时保留赤五；
接受和牌和立直，拒绝可选鸣牌。向听计算调用固定 RiichiEnv，不自行实现麻将规则。

连接前需激活虚拟环境，或用存储启动器执行 `uv run --frozen agari ...`。Token 可放入本地环境
`RIICHI_BOT_TOKEN`，或保存为仅含 Token 的 `.riichi-token` 文件，使用 `--token-file` 指定。
该文件已忽略提交；Token 内容不作为 CLI 参数，也不写入日志。客户端只响应唯一 `request_action`，记录 `action_ack`，
使用独立进程控制推理时限；超时后终止进程、记录降级并让服务器默认出牌，避免丢失事件状态后继续假装正常推理。
断线会终止本次运行，不将新的匹配算作旧局恢复。验证模式等待 `validation_result`；
线上协议的原始事件不会重复交给 Agent，只有 Observation 中的事件供 Agent 消费。
目前通过本地状态机与真实引擎测试，尚未持有 Token 连接 riichi.dev。

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

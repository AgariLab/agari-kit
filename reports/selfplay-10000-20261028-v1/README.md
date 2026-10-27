# 10,000 场自博弈数据验收

2026-10-28 完成 `selfplay-10000-v1`：四个基础牌效 Agent，独立 seed 500000–509999，
RiichiEnv 0.4.10 / Tenhou 四人赤牌半庄。生产源码为 kit
`9e3e866a046e40fecf94d03d315c9cc000d015b1`，未使用实验 fork wheel。

| 项目 | 实际结果 |
| --- | ---: |
| 完整半庄 / 唯一 ID / 唯一规范化哈希 | 10,000 / 10,000 / 10,000 |
| 局数 | 98,042 |
| 引擎步数 / 玩家决策数 | 6,064,610 / 6,142,629 |
| 训练 / 验证 / 测试划分 | 9,000 / 500 / 500 |
| 原始转换与回执绑定检查 | 10,000 / 10,000 全部通过 |
| 隔离失败记录 | 0 |
| 与原 smoke-v1 / selfplay-v1 的规范化哈希重叠 | 0 / 0 |

冻结数据集摘要：`fad436fee0a6798ab733740df846153a889bfe481bad0aafbfa8ee8e0ca489ef`。
冻结文件摘要：`98d2fef20db93fa907a2efb3ea85d0f8a65d4cf9bc00a13ef4b6f927d96a01ea`。
实际引擎动态库摘要：`169c8c2c53bdace01e4ca319f54050565fe728a5b1b2fc6b7f3046738887071d`。

采集耗时 1,803.04 秒，每场生成后独立原生重放校验；冻结前再次重放全部 10,000 场，耗时 425.51 秒。
另做 49.04 秒归档审计：SQLite、Parquet、逐场 JSON 的全部 11 个字段一致；
逐文件核对压缩哈希、规范化内容、种子轨迹顺序、身份、计数和回执。
每场 raw 与 normalized 的非末尾事件字节一致，唯一转换是末尾 `end_game` 补入与轨迹、回执一致的终分。
raw / trace / normalized / receipt / metadata 各 10,000 文件，五类归档合计 380,258,585 bytes；
不含数据库、划分及审计清单。完整牌谱和逐文件 SHA 清单均保存在外置卷。

本目录只发布小型证据：`report.json` 为汇总，`audit.json` 为归档审计，
`freeze-timing.json` 为全量原生重放及冻结记录。`audit.py` 是当次原样审计脚本，含本机路径和固定身份；
它不执行原生回放，也没有替代公共工具的 `agari data split`。复验时须匹配输入和源码，选择新输出目录，
不覆盖既有审计记录。逐文件原始 SHA 清单在外置 `runs/selfplay-10000-v1-audit/raw-sha256.jsonl`，
归档全清单在同目录 `artifacts.jsonl`；二者摘要见 `audit.json`。

数据位置：`datasets/{raw,mjai,manifests}/selfplay-10000-v1/`。实际生成与冻结命令：

```sh
python3 scripts/with_storage.py uv run --frozen agari data selfplay \
  --name selfplay-10000-v1 --games 10000 --seed-start 500000
python3 scripts/with_storage.py uv run --frozen agari data split \
  selfplay-10000-v1/catalog.sqlite selfplay-10000-v1/split-v1.json
```

重新采集应选新数据集名称，保持生产源码和引擎不变；采集时间和文件位置变化会改变冻结清单摘要。
本次没有再次运行 10,000 场复用验收；原 1,000 场新增 0 / 复用 1,000 的证据单独保留。

这是规则 Agent 自博弈数据，尚未用于模型训练。全部事件中没有吃、碰或杠，这是生成策略的覆盖限制，
不能把增加场数当作获得全面行为数据或强牌谱。现有完整轮次训练和模型改善实验使用原先的 1,000 场数据，
不与这批新数据混为一个运行。

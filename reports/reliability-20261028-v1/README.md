# 2026-10-28 基线可靠性运行

已完成 1,000 个四人赤牌半庄，250 个种子各轮换四座。累计 700,448 次决策，
零非法动作、零未处理异常，游戏执行累计约 33.77 秒（不含 bootstrap 汇总等开销）。

环境：Python 3.12.16、RiichiEnv 0.4.10、Tenhou 规则。
阵容：候选 `efficiency`，固定对手 `random`、`tsumogiri`、`random`。
源码指纹、完整种子列表在 `manifest.json`，逐场统计在 `games.jsonl`，汇总在 `report.json`。
本目录仅保存小型统计报告；没有保存完整牌谱、特征数据或模型。

可在 agari-kit 目录复现（使用不同输出目录以保留原报告）：

```sh
.venv/bin/python -c 'from pathlib import Path; from agari_kit.evaluation import evaluate; evaluate(list(range(250)), ["efficiency", "random", "tsumogiri", "random"], Path("reports/reliability-reproduction"))'
```

事件统计包含吃碰杠、立直、和牌及流局，但事件出现不等于每个规则边界均已验证。
振听、抢杠、多家和了等明确场景需要独立回归用例。
候选在弱对手池的名次不构成强基线复现、正式棋力晋级或榜单成绩。

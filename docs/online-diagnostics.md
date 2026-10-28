# 单场传输诊断

`scripts/run_online_diagnostic.py` 在独立父进程中观察现有 WebSocket 连接，复用固定的
`run_online_campaign.py`。用于排查已记录的 `connection_closed`；每次仅允许一场 ranked，
结束或失败后退出，没有自动重连。Bot 已 active 时直接使用 ranked，不重新 validate。

生产连接继续使用 websockets 15.0.1 的 ping interval/timeout 20/20 秒及原有连接参数。
观察器只统计收到的应用消息数量、库报告的延迟、距最后收包的时间、数值关闭码和固定异常类别。
只有本地先发送 1011 且匹配库的固定 keepalive 超时签名，才记录本地 keepalive 超时。
原始帧、headers、Token、异常文本和任意 close reason 文本均不写入诊断。

观察器是单独的运行变体。`diagnostic/manifest.json` 记录 wrapper 与原 supervisor 摘要、
Python/websockets 版本、实际网络路径及连接配置；`same_runtime_as_uninstrumented=false`，
不能仅因模型和 toolkit 摘要相同就把它与原始运行视为完全相同的传输环境。

从相邻 `agari-research` 的锁定环境执行，选择全新的 run 名：

```sh
HTTPS_PROXY=http://127.0.0.1:10800/ HTTP_PROXY=http://127.0.0.1:10800/ \
python3 ../agari-kit/scripts/with_storage.py uv run --frozen python ../agari-kit/scripts/run_online_diagnostic.py \
  --agent '@/Volumes/新加卷/AgariLab/models/full-coverage-trace-1000-v1/ppo-agent.json' \
  --token-file ../agari-kit/.riichi-token --games 1 \
  --idle-timeout 1800 --reserve-ms 100 --run riichi-ranked-diagnostic-v1
```

`--direct` 会关闭环境代理；应先确认该网络路径可用。示例中的凭证仅由原 supervisor
读取，不进入模型工作进程或日志。运行前确认旧 Bot 进程已经退出；平台会拒绝同一 Bot
在队列或游戏中的并发连接。[官方匹配规则](https://riichi.dev/docs/matchmaking)

原 supervisor 继续独占创建 run 目录。诊断在该目录被本次运行创建后才延迟创建：

- `diagnostic/manifest.json`：观察器身份和参数。
- `diagnostic/events.jsonl`：连接生命周期及安全类别。
- `diagnostic/result.json`：诊断收尾状态及原 campaign 状态。

原 manifest、summary、games 索引及单场记录布局保持不变。诊断收尾完成不表示对局成功，
必须同时检查原 campaign 和真实 end_game。目录或诊断文件已存在时拒绝覆盖；
源码变化、诊断写入失败或额外连接请求也会阻止正常完成。

局中断线不支持恢复同一对局；服务器会为剩余动作使用默认动作，并计入评级。
本地缺失终分时应保留未知结果，不能补造健康完成或零评级影响。[官方协议](https://riichi.dev/docs/protocol)

20 项本地测试覆盖真实 localhost 关闭、TCP 中断、keepalive 超时、握手失败、取消还原、
源码变化、目录竞态、日志失败及敏感文本排除；测试仅使用合成凭证，不连接平台。

```sh
../agari-research/.venv/bin/python -B -m unittest discover -s tests -p test_online_diagnostic.py -v
```

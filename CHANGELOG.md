# Changelog

## 1.0.0 (2026-11-17)

首发版本。

- `scan`：僵尸 / 新晋孤儿 / 同名批量重生嫌疑，一条进程只产一条主发现（ZOMBIE > LOOP > ORPHAN 去重）
- `watch`：多轮采样双判据——服务整体死而复生（CHURN crit）与池化 worker pid 换血（CHURN_PID 梯度 WARN/CRIT）
- 基线：`--save-baseline` / `--baseline`，NEW / DEAD / RESTARTED / ETIME_RESET 四事件；瞬时工具与 proclens 自身行不参与
- 脱敏：命令行输出中 token / password / bearer / 32hex 一律前4后4
- 退出码即告警：0 / 1 / 2 / 3 / 4
- 49 条不变量自测全绿；真机 E2E 造罪三连（僵尸 / 重生风暴 / 基线确定性）全过

### 开发期自测逮住的真 bug（狗粮记录）

1. 豁免名单含通用词 `worker`/`runner`——会把真实应用 crash loop 藏掉，改为只放系统批量工作者 + shell 工具箱
2. `--save-baseline` 裁剪行漏 `name` 字段，diff 直接内部错（exit 4）
3. 孤儿豁免参数语义混乱（最终名单 vs 追加），统一为「追加」
4. 自排除按进程名匹配，透过 `python3`/`env` 启动时认不出自己，改按 cmd 内容匹配
5. 同一进程可同时命中僵尸+孤儿双发现，发现轰炸，加优先级去重
6. Watcher 名字级生死追踪看不见池化 worker 换血（名字永远在场）——补 pid 集合追踪与 CHURN_PID 梯度判据
7. shell 工具箱（sleep/git/grep…）高频短命在 watch/baseline 里全是误报，进豁免
8. 基线把 proclens 自己的行也存了进去，秒后复测必报 RESTARTED——`filter_self` 全链路剔除

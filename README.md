# proclens

[中文](README.md) | [English](README_EN.md)

**进程生命周期透镜** —— `ps` 一眼扫过去全是活着的，没人告诉你**谁死得不干净、谁没了爹、谁在反复横死**。

哨兵套件各管一段：[porteye](https://github.com/tomlen045/porteye) 盯「谁在听」，[authguard](https://github.com/tomlen045/authguard) 盯「谁想进来」，[callhome](https://github.com/tomlen045/callhome) 盯「谁在偷偷打电话回家」，**proclens 盯「谁在反复横死」**。

[![tests](https://img.shields.io/badge/self--tests-49%2F49-green)]() [![e2e](https://img.shields.io/badge/live--drill-zombie%2Fchurn%2Fbaseline-green)]() [![deps](https://img.shields.io/badge/deps-zero-yellow)]() [![license](https://img.shields.io/badge/license-MIT-blue)]()

---

## 解决什么问题

* 服务半夜崩过一轮又被拉起，早上起来`ps`里干干净净，**没人留下证据** → `watch` 多轮采样：同名进程换血 N 个 pid 直接点名（池化 worker 反复重生就是 crash loop 的指纹）
* 子进程死了，父进程不 `wait`，僵尸排队占着 pid 表 → `scan` 按 `Z` 状态逐个点名，连父 pid 一起报
* 进程莫名其妙的孤儿（父进程先走，被托管给 pid 1）到底是不是刚出的事 → `scan` 只报「年轻的 ppid=1」，跑了几年的正常守护进程不骚扰你
* 重启了没？还是崩了重启？ → 基线比对区分「换 pid 重启」和「etime 倒流」（后者是计数异常/pid 复用的信号）
* 一切输出先脱敏：命令行里的 token/密码/bearer 片段一律前4后4，截图发群里不翻车

## 四个命令

```bash
# 全量扫描：僵尸 / 新晋孤儿 / 同名批量重生嫌疑
python3 proclens.py scan

# 多轮采样：抓反复横死（服务整体死而复生 = CHURN crit；名字常驻 pid 换血 = CHURN_PID）
python3 proclens.py watch --interval 5 --rounds 6

# 基线：存一份进程表，之后比对 NEW / DEAD / RESTARTED（瞬时工具自动豁免，只盯服务）
python3 proclens.py scan --save-baseline base.json
python3 proclens.py scan --baseline base.json

# 自测：49 条不变量
python3 proclens.py selftest
```

## 真机实测（M4 / macOS 27，2026-11-17）

* 全量扫描 691 进程：22 条新晋孤儿全部为分钟级 XPC 服务（正常形态，info 不误报 crit）
* 现场造尸：子进程 2 秒退场、父进程装死不收尸 → 下一秒 `scan` 即 `[WARN] ZOMBIE pid 64256 父 pid 64254`，exit 2
* 现场造风暴：`exec -a looper.jar` 每 0.3s 重生一个真 sleep → 快照 `LOOP_SUSPECT 同名 6 个实例`；`watch` 6 轮 `[CRIT] CHURN_PID looper.jar`，exit 3
* 附带收获：同一台机器上还逮住桌面助手类进程 6 轮换血 21 个 pid——工具装上第一天就能看见以前从没人看的东西
* 基线确定性：存→1 秒后复测 → `无变化`，exit 0

## 设计取舍（为什么值得抄）

* **豁免名单不放通用词**：`worker`/`runner` 这种名字谁都在用，放进豁免等于把真实 crash loop 藏掉——名单只放系统批量工作者和 shell 工具箱（sleep/git/grep 这类脚本短命是正常形态）
* **池化 worker 是名字级生死追踪的盲区**：旧 pid 死、新 pid 顶上，名字永远「在场」。proclens 用「见过的不同 pid 数」做梯度判级（阈值 WARN / 翻倍 CRIT），换血速度本身就是实锤
* **一条进程只产一条主发现**：ZOMBIE > LOOP > ORPHAN 优先级去重，不搞发现轰炸
* **基线剔除自己 + 瞬时工具**：proclens 自己的行不进基线，ps/sleep/bash 的新陈代谢不产事件——秒级窗口里它们全是噪音
* **退出码即告警**：0 干净 / 2 有 WARN / 3 有 CRIT，cron 里接一条 `||` 就能进告警链

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 干净（只有 info 或无发现） |
| 1 | 用法错误 |
| 2 | 有 WARN（僵尸 / 换 pid 重启 / 池化换血初级） |
| 3 | 有 CRIT（服务整体死而复生 / etime 倒流 / 换血翻倍） |
| 4 | 内部错误（带信息退出，不静默） |

## 安装

```bash
# 方式一：直接拉单文件（仅标准库，Python 3.8+）
curl -fsSLO https://gitee.com/tomlen/proclens/raw/main/proclens.py

# 方式二：GitHub
curl -fsSLO https://raw.githubusercontent.com/tomlen045/proclens/main/proclens.py

# 跑起来
python3 proclens.py selftest   # 期望 49/49 GREEN
python3 proclens.py scan
```

零依赖单文件：`ps` 一条命令取数，macOS / Linux 通吃（ps 输出格式相同），拷进跳板机就能跑。

## 不变量（49 条自测覆盖）

etime 五形态解析 / 坏行跳过不炸 / 五族凭据脱敏 / 僵尸点名带父 pid / 孤儿只报年轻的+默认豁免永生效 / loop 嫌疑同名聚合+混龄不报 / watch 三判据（respawn crit、池化换血梯度、稳定静默）/ shell 工具箱豁免 / diff 四事件 + 瞬时工具静默 / 基线裁剪行可 diff / 汇总计数与退出码映射 / JSON schema 字段稳定 / 空输入不炸 / 自身行全链路自排除 / 演示森林发现链确定性

## English

`proclens` is a zero-dependency single-file CLI that answers three questions nobody watches:
who is a zombie (dead child, unwaited), who just got orphaned (fresh ppid=1), and who keeps
dying and respawning (crash-loop fingerprint via pid-churn tracking across sampling rounds).
Baselines diff NEW/DEAD/RESTARTED with transient tools excluded; every output line is
credential-masked (first4…last4). Exit codes: 0 clean / 2 warn / 3 crit — wire it into cron
with `||`. See the Chinese README for the full story.

## License

MIT

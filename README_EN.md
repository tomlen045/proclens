# proclens

**Process lifecycle lens** — `ps` shows you everything that is alive. Nobody tells you
**which process died without being reaped, which one just lost its parent, and which one
keeps dying and respawning**.

The sentinel suite covers one surface each: [porteye](https://github.com/tomlen045/porteye)
watches who is listening, [authguard](https://github.com/tomlen045/authguard) watches who
knocks, [callhome](https://github.com/tomlen045/callhome) watches who phones home,
**proclens watches who keeps dying**.

[![tests](https://img.shields.io/badge/self--tests-49%2F49-green)]() [![e2e](https://img.shields.io/badge/live--drill-zombie%2Fchurn%2Fbaseline-green)]() [![deps](https://img.shields.io/badge/deps-zero-yellow)]() [![license](https://img.shields.io/badge/license-MIT-blue)]()

[中文](README.md) | English

## What it answers

* **Zombies** — dead children the parent never `wait()`ed on, listed with the guilty parent pid (`scan`)
* **Fresh orphans** — ppid=1 and young: the parent died moments ago. Long-lived daemons are normal, not reported
* **Crash loops** — `watch` samples several rounds: a service that dies and respawns (name disappears and returns) is `CHURN` crit; a pooled worker whose pids keep rotating while the name stays present is `CHURN_PID`, graded warn → crit by distinct-pid count — name-level life/death tracking alone is blind to this shape
* **Baseline diff** — save the table, compare later: NEW / DEAD / RESTARTED / ETIME_RESET; transient tools (sleep/git/grep/ps) and proclens itself are excluded so a 1-second recheck diffs clean
* **Masked output** — anything that looks like a token/password/bearer in a command line is reduced to first4…last4 before it hits your terminal or a screenshot

## Quick start

```bash
curl -fsSLO https://raw.githubusercontent.com/tomlen045/proclens/main/proclens.py
python3 proclens.py selftest   # expect 49/49 GREEN
python3 proclens.py scan
python3 proclens.py watch --interval 5 --rounds 6
```

Zero dependencies, Python 3.8+ standard library, single file, `ps` is the only data source.
macOS and Linux. Exit codes as alerting: 0 clean / 2 warn / 3 crit — pipe into cron with `||`.

## Design notes

* The loop whitelist never contains generic words like `worker`/`runner` — that would hide real crash loops. Only system batch workers and shell toolbox binaries are exempt.
* One process yields at most one primary finding: ZOMBIE > LOOP > ORPHAN, deduplicated.
* Baselines store slim rows and skip proclens's own line; diffing ignores transient tools.

See the Chinese README for the full field manual and the live-drill log.

## License

MIT

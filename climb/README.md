# Climbing loadwatch

`climb/morning.py` asks whether whoever reads loadwatch in the morning knows what needs them. Small agents get a fake morning from
`worlds.py` and only the CLI, and must name what needs attention and what to fix first. The report's own JSON is graded on the same
answers, with no noise. The rule is in the script, fixed before the first measurement. `--held-out` runs mornings written after the
changes, to catch a gain that only fits the mornings it was tuned on.

```bash
uv run python climb/morning.py evaluate --label base
uv run python climb/morning.py compare new --best base
```

## First climb, 2026-11-01

Haiku and Luna, 10 mornings, two runs each (40 runs per measurement).

| Iteration | Right | First right | Steps | Report | Gain | z | Verdict |
|---|---|---|---|---|---|---|---|
| Start | 33/40 | 36/40 | 236 | 910 | | | |
| 1. Rank alerts, and say where to start | 34/40 | 40/40 | 274 | 1000 | -2.4 | -0.32 | kept: the report is better, agents no worse |
| 2. The report stands alone: what it looked at, and that nothing else adds to it | 36/40 | 40/40 | 110 | 1000 | +12.5 | 3.22 | kept: agents did better |
| 3. One attention list, and old failures apart | 37/40 | 40/40 | 100 | 1000 | +2.0 | 0.40 | reverted |

On four held-out mornings (16 runs each, side by side): the original got 13/16 in 96 steps and the kept version 14/16 in 48; iteration 3
13/16 in 38, and a lower score than the kept version, which supports reverting it. It is on the branch `climb-iter3`.

The held-out mornings found one real gap, left unfixed so that they stay honest: when a job overruns and so a later job misses its
deadline, the report starts with the deadline and not the job holding it up. `blocks` is only worked out for failed jobs.

The grader is strict in two places, and was left as it was: naming a job that a failure blocked counts as a wrong set, and so does
leaving out a person's test run or an old failure. Most of the 40 scores' remaining misses are those.

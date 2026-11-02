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

The held-out mornings found one real gap, fixed after the climb: when a job overran and so a later job missed its
deadline, the report started with the deadline and not the job holding it up.

The Windows lab found what the fake mornings could not: on a job that failed days ago, Start here said "it is new tonight". The reason
now only says what is true of the alert.

The grader is strict in two places, and was left as it was: naming a job that a failure blocked counts as a wrong set, and so does
leaving out a person's test run or an old failure. Most of the 40 scores' remaining misses are those.

## Second climb, 2026-11-02

Ten more, on a wider set: first 31 scenarios, then 36, then 42, with questions as well as mornings, because the first set had reached its
maximum. Haiku and Luna, two runs each.

| # | Change | Verdict |
|---|---|---|
| 1 | `flaky`: fine tonight, failing on and off | kept: the report is better |
| 2 | `nothing-started`: most of a server's jobs stop starting, so the finding is the server | kept: the report is better |
| 3 | `shared-cause`: the same error in several jobs is one finding | kept: the report is better |
| 4 | A failure says who deployed just before it | kept: a blind spot fixed (`q-what-changed`) |
| 5 | `--help` shows how to get the page | reverted: +2.3, z 1.14 |
| 6 | Everything that needs attention, in one line | reverted: +1.2, z 0.62 |
| 7 | A Finished column | reverted: -1.6, z -0.81 |
| 8 | A Fabric item running far longer than usual | kept: the report is better |
| 9 | A chain stopped by a disabled job names that job | kept: the report is better |
| 10 | `creeping`: slower night after night | kept: a blind spot fixed |

The rule moved twice during this climb, each time written down before the measurements it applied to: a scenario going from at most 1 of 4
right to at least 3 of 4 also keeps a change, and the gain needed became half a scenario, as 5 points had been with 10 scenarios. Climbs 4
and 10 needed the first. Neither changed an earlier verdict.

What most changed the numbers was not a climb. Agents went from 131 to 140 of 144 right when the prompt stopped asking for "job or model
names" on questions that wanted a yes or a number: much of what looked like the tool's weakness was the grader's. Between identical
measurements the score moves by about 5 runs in 144.

On six held-out mornings written after all ten, before and after are the same: 20/24, with the same four misses (+0.29 points, z 1.29). The
climb did not make general reading better, which was already good. It closed specific blind spots, which those mornings hardly have. The
four misses are one morning, a refresh failing every other night: `flaky` and `creeping` have no Fabric twin.

On the Windows lab all of it ran on real data, and `flaky` found a job failing 8 of its last 10 runs.

`flaky` and `creeping` now read Fabric too, from the same code as SQL Server. Held-out went to 32/32 and 700/700 from the report (the three
Fabric mornings scored 30 each before). That is no evidence of generality: `flaky-refresh` led to the change, and `flaky-pipeline` and
`slowing-refresh` were written after it, so they only show the whole path works. All 42 tuned mornings give the same report as before, byte
for byte, and 200 jobs still take 1.6 s. Fabric's two rules have not run on a real tenant, and the Windows lab was not rerun on this commit.


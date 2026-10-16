# Reading the analysis, and testing further

How to read the `NAME.md` report that `qrng.py` writes, what its numbers can and cannot tell you, and how to run other randomness test suites on the output. A worked example uses the overnight capture `data/geiger-20261013-005942` (194,124 events, 11.2 hours).

Contents: [What is being tested](#what-is-being-tested) · [Reading the report](#reading-the-report) · [Reading p-values](#reading-p-values) · [Worked example](#worked-example-the-overnight-capture) · [Other test suites](#running-other-test-suites) · [What a test can't tell you](#what-no-test-can-tell-you)

## What is being tested

There are three different things, and the report keeps them apart:

| Stage | File | What it is | What you want to know |
|---|---|---|---|
| 1. Source | the `.csv` | gaps between decay events | Is it really Poisson (exponential gaps)? |
| 2. Raw symbols | `.raw` | the low `k` bits (default 8) of each gap, one per byte | How much entropy per symbol? Any bias or structure? |
| 3. Output | `.hex` | SHA-256 of enough raw symbols to carry 256 + 64 bits of *measured* min-entropy per 32-byte block | Does it look random to every test? |

Stage 3 will pass almost any test, because SHA-256 is a good mixer even if its input is mediocre. Passing tests on the output is therefore weak evidence about the *source*. The real claim ("this is quantum randomness") rests on stages 1 and 2, and on the entropy estimate that decides how much input goes into each block. Test the raw symbols as hard as the output, and trust a failure there more than a pass on the output.

## Reading the report

### Flags

The report's first section lists only the checks that need attention, each ending in `<-- CHECK`. No flags means nothing crossed a threshold. A flag is a prompt to look, not a verdict: see [Reading p-values](#reading-p-values).

### Summary block

| Line | Meaning | Healthy |
|---|---|---|
| `events / usable intervals` | pulses recorded, and gaps between consecutive ones inside a segment | intervals = events − 1 per segment |
| `recorded ... segment(s)` | time covered, and how many unbroken runs. Outages and file boundaries split segments | 1 for a clean overnight run |
| `rate ... CPM` | counts per minute, and the mean gap | stable across the run |
| `gap CV` | standard deviation ÷ mean of the gaps | 1.0 for an exponential distribution |
| `min gap` | shortest gap seen | tube dead time, a few hundred µs; far lower means ringing or double-triggering |
| `Fano factor` | variance ÷ mean of counts in time windows | 1.0 for Poisson; above 1 means rate drift or clustering |
| `Shannon` | average information per raw symbol | close to `k` |
| `min-entropy (MCV)` | conservative entropy per symbol, SP 800-90B Most Common Value estimator at 99% confidence | below `k`, see below |
| `chi-square p` | are all 256 symbol values equally likely? | not tiny (see [p-values](#reading-p-values)) |
| `lag-1 corr` | does one symbol predict the next? | near 0 |
| `pair-compare` | the textbook "gap A < gap B" bit method, for teaching | about 50% ones |
| `conditioned` | bytes produced, and events used per 256-bit block | the block size is set by the min-entropy |

**Shannon vs min-entropy.** Shannon entropy averages over all symbols and is easy to inflate. Min-entropy depends only on the single most likely symbol (`-log2 p_max`) and is what an attacker's best guess is worth, so it is what the extractor credits. The MCV estimate adds a 99% confidence margin, so even ideal uniform data scores below `k`, and the margin shrinks as the sample grows. The report prints `ideal uniform, same n` so you can compare: a source within a few hundredths of the ideal figure is indistinguishable from perfect at that sample size.

### Section 1: Is the source Poisson?

- **KS vs exponential.** The Kolmogorov-Smirnov distance `D` is the largest difference between the empirical CDF of the gaps and the best-fit exponential. The p-value comes from a Monte-Carlo simulation (the rate is fitted from the data, so the textbook table does not apply). `D` is the effect size, the p-value only says whether it is detectable. With 194,000 events even `D = 0.007` (0.7 percentage points) is detected.
- **KS after removing slow rate drift.** The same test after rescaling each of 20 time blocks to its own rate. If the first fails and this passes, drift was the cause. If both fail, the gap shape itself differs from a pure exponential (dead time, afterpulses, ringing).
- **Rate by decile.** CPM over each tenth of the run. A flat row means a stable source. A slow trend usually means temperature or supply drift, or the source moving.
- **Fano vs window.** Counts per window of 5, 10, 20 and 50 mean gaps. Values near 1.0 at all window sizes mean true Poisson counting. Values rising with window size mean slow rate drift.

### Section 2: Raw symbol quality

- **Per-bit P(1).** The fraction of ones in each of the `k` bit positions, with the worst z-score. For a fair bit, z beyond about ±3 is rare, but with 8 bits tested expect an occasional ±2.5.
- **Autocorrelation z, lag 1-8.** Correlation between a symbol and the one `lag` events later, in standard deviations. Values within ±3 are fine.
- **zlib ratio.** Compressed size ÷ original. Random data is about 1.000 (or slightly above, from the overhead). Below 1.00 means the compressor found structure.
- **Min-entropy vs k.** The same estimator at 2, 4, 6, 8, 10 and 12 bits. The percentage of `k` falls as `k` grows even for ideal data, because the estimator cannot populate 4,096 symbols with 194,000 samples, so compare against the `ideal uniform, same n` line. If the `k` you use shows a much lower figure than a smaller `k`, the extra bits are not pulling their weight.
- **First/second half.** MCV on each half. A large difference means the source changed during the run.

### Section 3: Conditioned output

Plain tests on the `.hex` bytes. Duplicate 32-byte blocks should be 0, monobit and runs z-scores near 0, byte chi-square p not tiny. These are sanity checks that the extractor works, not evidence of quantum origin.

### NIST SP 800-22 table

Fifteen tests from [NIST SP 800-22 rev 1a](https://csrc.nist.gov/pubs/sp/800/22/r1/upd1/final), each returning a p-value. The table shows the minimum p across a test's sub-tests. The suite looks for different weaknesses:

| Test | Looks for |
|---|---|
| monobit, frequency within block | too many ones or zeros, overall and locally |
| runs, longest run of ones | too many or too few streaks |
| binary matrix rank | linear dependence between rows of bits |
| DFT (spectral) | periodic patterns |
| non-overlapping / overlapping template | too many or too few occurrences of specific bit patterns |
| Maurer's universal | compressibility |
| linear complexity | whether a short LFSR could generate the stream |
| serial, approximate entropy | uneven frequency of overlapping m-bit patterns |
| cumulative sums | the random walk drifting away from zero |
| random excursions (and variant) | behaviour of the walk at particular states |

Results are `PASS`, `FAIL`, or `SKIPPED`. The upstream suite reports "not enough data" as FAIL, so `qrng.py` re-labels those as SKIPPED and shows the reason, e.g. the two excursion tests need at least 500 walk cycles. The non-overlapping template test uses one random template per call, so it is repeated 20 times and judged by how many fail. The full suite needs 1,028,016 bits (≈129 KB), so most overnight runs can now run 13 of 15 tests, as the one in the worked example did.

## Reading p-values

A p-value answers: *if the data were perfectly random, how often would a result this extreme occur?* It is not the probability that the data is random.

- **Pass threshold is 0.01.** A perfectly random source fails about 1 test in 100. With 15 tests, several sub-tests, and a handful of other checks, an occasional p of 0.005 to 0.02 is **expected**. Re-run on different data before worrying.
- **Judge the pattern, not one number.** Many small p-values, or a p that is tiny (< 1e-4) and repeats on fresh data, is real. One 0.016 among 13 is not.
- **p-values themselves should be uniform.** An all-PASS table with every p above 0.5 would be suspicious too. NIST's full procedure runs the suite on many sequences and checks that the p-values are uniform and that the pass proportion is within about 0.99 ± 3·√(0.99·0.01/m) (for m sequences, e.g. ≥ 0.9615 of 100). `nist22` on a single sequence is a spot check, not that procedure.
- **Big samples find small effects.** The more data, the smaller the deviation a test detects. A 1% distortion that no sensible application would notice will eventually give p = 0. Always look at the effect size (`D`, the z-score, the percentage) beside the p-value, and ask whether it matters for the use.
- **Multiple comparisons.** Counting every number in the report, a few `CHECK` marks on perfect data are normal. Pre-decide what you care about (for an RNG: raw-symbol uniformity, min-entropy and the output tests) and treat the rest as exploratory.

## Worked example: the overnight capture

`geiger-20261013-005942`: 194,124 events in one segment of 40,371 s (11.2 h), 288.5 CPM, 4,621 output blocks (147,872 bytes = 1,182,976 bits).

**What passed.**

- Source: gap CV 1.005 and Fano 1.00 at every window size, and the rate is flat across deciles (283-294 CPM). Counting behaviour is Poisson.
- Raw symbols: Shannon 7.999 of 8, MCV min-entropy 7.678 against 7.74 for ideal data of the same size, first and second half agree (7.62 / 7.62), zlib 1.0003, autocorrelation within ±1.2σ at all lags.
- Output: monobit 50.07% ones, runs z −1.28, byte chi-square p 0.20, zero duplicate blocks, **13 of 13 runnable SP 800-22 tests pass**, with the other two skipped for too few walk cycles (339 of the 500 needed).

**What was flagged, and what I found when I looked.**

1. **KS vs exponential: D = 0.0072, p ≈ 0**, unchanged after detrending. The gap distribution is slightly off a pure exponential, as on the first capture (D = 0.0096). The effect is under 1% at most, and has the signature of tube dead time or pulse-line ringing. It is a statement about the source's gap shape, not about the low bits.
2. **Worst bit z = +3.76.** One of the eight bits is slightly biased.
3. **Raw symbol chi-square 385 on 255 degrees of freedom, p ≈ 0.** The raw 8-bit symbols are *not* perfectly uniform at this sample size. This one is the real finding, so I looked closer at the raw file:
   - The excess is concentrated in symbols 112 to 127 (high nibble 7), z = +8.9. They are over-represented by about 0.5% of all events (roughly 15% above their expected count of ~758 each). The low nibble is clean (all |z| < 1.5).
   - The symbol histogram is uniform at smaller `k`: chi-square for `k=4` is 12.3 on 15 dof, `k=5` 40.5 on 31, `k=6` 74.1 on 63 (all unremarkable). It starts to show at `k=7` (z = +3.1) and clearly at `k=8` (z = +5.7). So the unevenness lives in the top two bits of the symbol, which carry the 32 to 128 µs part of the gap (the symbol spans 128 µs in all).
   - I have **not** established the cause. A fixed-spacing artifact, such as ringing that double-triggers at a particular delay, is a plausible candidate. A scope on the pulse line, or a rising-edge capture (`CAPTURE_RISING 1`), would test that.

**Does it matter for the output?** Not for what the extractor does. The credited entropy comes from the most-common-symbol estimate, which looks at the *worst* symbol and so already sees this excess: 7.678 bits per symbol, not 8. The extractor takes 42 symbols per block to cover 256 + 64 bits at that figure (42 × 7.678 ≈ 322 bits). The measured penalty is exactly what this effect costs. The SHA-256 output passes everything. The practical recommendations are:

- If you want clean raw symbols, use fewer bits: `./qrng.py report data/geiger-20261013-005942.csv -k 6 -o data/geiger-k6`. The cost is more events per block (fewer bits per symbol), and no loss of rigour.
- For a demo, say so honestly: the raw bits are *nearly* uniform, with a 0.5% excess in one region that the min-entropy estimate captures, and the output is conditioned accordingly.

## Running other test suites

Files to feed them:

| File | Format | Size here |
|---|---|---|
| `NAME.hex` | text, one 64-hex-character block per line | 300 KB |
| `./qrng.py generate FILE -n BYTES -o out.bin` | conditioned output, binary | any size up to what's available |
| `./qrng.py generate FILE --raw -o raw.bin` | one symbol per byte | 194 KB |

To turn the `.hex` into binary: `xxd -r -p NAME.hex > NAME.bin` (or `python3 -c "import sys;sys.stdout.buffer.write(bytes.fromhex(sys.stdin.read()))" < NAME.hex > NAME.bin`).

> **Size matters more than anything.** The large batteries need far more data than a Geiger counter makes. One overnight run gives about 148 KB of output. SmokeRand `express` needs 64 MiB, dieharder `-a` needs hundreds of MB or more, and TestU01's BigCrush needs several GB. At about 148 KB per 11 hours, 64 MiB is roughly 200 days of capture. Running them on short files makes the generator *rewind* and re-read the data, which gives meaningless p-values (and shows up as spurious failures). Check what each tool says about the file it read. Do **not** pad the data out with a PRNG or a hash counter to reach the size: that tests the PRNG, not your source.

### What fits in one overnight file (148 KB)

| Tool | Install | Run | Notes |
|---|---|---|---|
| **ent** | `sudo dnf install ent` (if your repo has it) | `ent NAME.bin` / `ent -b NAME.bin` | Entropy, chi-square, mean, π estimate, serial correlation. Quick sanity check. |
| **rngtest** (FIPS 140-2) | `sudo dnf install rng-tools` | `rngtest < NAME.bin` | 20,000-bit blocks, so 59 blocks for this file. Expect a failure rate of about 1 in 1,000 blocks on perfect data. |
| **PractRand** | build from [pracrand.sourceforge.net](https://pracrand.sourceforge.net/) | `RNG_test stdin8 < NAME.bin` | Reports at each power-of-two length (up to 128 KB here) and says `no anomalies in N test result(s)`. Fine with short files. |
| **SP 800-22** | the submodule | `./qrng.py nist22 FILE` | Needs 1,028,016 bits for all 15 tests. |
| **SP 800-90B** | see below | `ea_non_iid -v raw.bin 8` | Run it on `--raw` symbols, not the output. |

PractRand build, from the unpacked source:

```
g++ -std=c++14 -O3 -pthread -Iinclude -o RNG_test tools/RNG_test.cpp src/*.cpp src/RNGs/*.cpp src/RNGs/other/*.cpp
```

`stdin8` reads the stream as bytes; `stdin` auto-sizes to 32-bit words.

### NIST SP 800-90B, the entropy assessment

This is the measurement for what the extractor claims. The report's MCV figure is one of its estimators; the full tool runs about ten and takes the smallest.

```
git clone https://github.com/usnistgov/SP800-90B_EntropyAssessment
cd SP800-90B_EntropyAssessment/cpp && make          # needs g++, bzip2, divsufsort, jsoncpp, openssl, mpfr dev packages
./ea_non_iid -v ../../data/geiger-20261013-005942.raw 8
```

The final number is the min-entropy per 8-bit symbol, and the **smallest** estimator is the answer. If it is lower than the report's 7.678, the extractor's `SAFETY_BITS` margin shrinks and you should raise it or use `-k 6`. For files captured with `-k` below 8, change the last argument to the same `k`. NIST recommends 1,000,000 samples for the non-IID track; we have 194,000, so the estimate will be conservative, and it is worth pooling more runs (`./qrng.py generate a.csv b.csv --raw -o raw.bin`).

### The large batteries (need much more data)

Use these once you have accumulated enough, by pooling captures or continuing to run.

**dieharder**

```
sudo dnf install dieharder
dieharder -a -g 201 -f NAME.bin          # all tests; 201 = raw binary file input
dieharder -d 0 -g 201 -f NAME.bin        # a single test (-d 0 is birthdays)
dieharder -l                             # list tests
```

Check the `rewinds` figure in the output. A non-zero count means the file was too short and the results are not trustworthy. Dieharder has several tests with known false-positive tendencies and marks results `PASSED`, `WEAK` or `FAILED`. A `WEAK` on one test out of ~100 is expected.

**TestU01** ([simul.iro.umontreal.ca/testu01](https://simul.iro.umontreal.ca/testu01/tu01.html), C library, build from source)

```c
#include "bbattery.h"
int main(void) {
    bbattery_SmallCrushFile("NAME.bin");     // also: bbattery_CrushFile, bbattery_BigCrushFile
    return 0;
}
```

```
gcc -O2 -o run_tu01 run_tu01.c -ltestu01 -lprobdist -lmylib -lm
```

SmallCrush is the entry level (15 tests); Crush has 144 and BigCrush 160, needing gigabytes. TestU01 reads the file as 32-bit words and wants the file in the byte order you intend. It prints the statistic and p-value per test, and a summary of tests with `eps` or `1 - eps` p-values (both extremes are failures).

**SmokeRand** ([github.com/alvoskov/SmokeRand](https://github.com/alvoskov/SmokeRand))

```
cat NAME.bin | ./smokerand express stdin32
```

Batteries from smallest to largest: `express` (7 tests, 64 MiB), `brief`, `default`, `full`. `stdin32` for 32-bit words, `stdin64` for 64-bit. A short file ends the run early. SmokeRand can also wrap TestU01 batteries.

**NIST STS (the reference C implementation of SP 800-22)** can replace the Python suite for independent cross-checking. It is run on many sequences of ≥ 1 Mbit each, which gives the pass-proportion check described above.

### A practical sequence

1. `./qrng.py report FILE` after every run, and read the Flags.
2. Pool runs until you have ≥ 1,028,016 output bits and check that all 15 SP 800-22 tests run.
3. Run `ea_non_iid` on the pooled `--raw` data. Make sure its figure is no lower than the extractor's credited entropy.
4. `PractRand` and `rngtest` on the output as an independent check.
5. For dieharder, SmokeRand and TestU01, accumulate enough data first, then run once. Do not run them on a short file and quote the result.

## What no test can tell you

Statistical tests only say a stream *behaves* randomly. They cannot see where it came from. A good PRNG passes everything above, as does a hash of a counter. What makes this a quantum RNG is the physics and the construction:

- the timing of each decay is fundamentally unpredictable, and the low bits of a gap measured to 0.5 µs are far below any predictable structure;
- the extractor credits only the entropy it *measured*, with a margin;
- the hardware is the one you built, and the timestamps come from a hardware capture register.

What the tests *do* catch are the ways that can go wrong: drift, dead-time artifacts, ringing, serial loss, a stuck bit, an injected periodic signal, or a wrongly small `k`. Re-run the report after any change to the hardware, the firmware or the source, and keep a record.

Note the limits of the evidence: this is a weak source near the tube on one board, and the result is for this capture only. A radioactive source brings health, legal and shipping considerations, and the output is only as good as the board's pulse line. For security-grade keys, mix this output with your operating system's CSPRNG (as the kernel does for hardware RNGs) instead of replacing it.

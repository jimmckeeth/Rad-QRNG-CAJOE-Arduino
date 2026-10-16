#!/usr/bin/env python3
"""Geiger-counter quantum random number generator.

  capture    record event timestamps from the Arduino (or --simulate / --replay for demos)
  analyze    statistics + figure for a recorded file
  generate   raw symbols -> SHA-256 conditioned random bytes
  dashboard  live view of a file that `capture` is writing
  nist22     run the NIST SP 800-22 statistical test suite (external, see README)
  report     write <base>.png/.hex/.raw/.md for one or more captures (capture does this automatically)

Entropy source: radioactive decay is a Poisson process, so the gaps between Geiger pulses are
exponentially distributed and unpredictable. The low bits of each gap, measured at 0.5 us, are
essentially uniform. Those raw bits are then conditioned with SHA-256, crediting only the
min-entropy we can actually measure.
"""
import argparse
import collections
import datetime
import glob
import hashlib
import math
import os
import sys
import time
from dataclasses import dataclass, field

import numpy as np

DEFAULT_HZ = 2_000_000
OUT_BITS = 256          # bits per conditioned block
CPM_WINDOW_S = 60       # rolling window for the live CPM readout
OUTAGE_FACTOR = 20      # a gap > 20x the typical gap (P ~ 2e-9 for a Poisson source) is an outage
SAFETY_BITS = 64        # extra input entropy per block (SP 800-90C style margin)


# --------------------------------------------------------------------------- data

@dataclass
class Events:
    seq: np.ndarray
    ticks: np.ndarray      # on one global timeline across all files/segments
    seg: np.ndarray        # segment id: one per uninterrupted timer run
    hz: int
    source: str
    outages: list = field(default_factory=list)   # seconds; implausibly long gaps split out

    @property
    def intervals(self):
        """Tick gaps between consecutive events, never across a segment boundary or a seq gap."""
        if len(self.ticks) < 2:
            return np.zeros(0, np.int64)
        d = np.diff(self.ticks.astype(np.int64))
        ok = (np.diff(self.seq) == 1) & (np.diff(self.seg) == 0)
        return d[ok]

    def segment_bounds(self):
        """(first, last) event index of each segment."""
        starts = np.r_[0, np.flatnonzero(np.diff(self.seg)) + 1]
        return list(zip(starts, np.r_[starts[1:], len(self.seg)] - 1))


def _read_file(path):
    """-> (hz, source, start, [(seqs, ticks), ...]) with one list entry per timer run."""
    hz, source, start = DEFAULT_HZ, "unknown", None
    segs, seq, ticks = [], [], []
    with open(path) as f:
        for line in f:
            if line.startswith("#"):
                for tok in line[1:].split():
                    if tok.startswith("tick_hz="):
                        hz = int(tok[8:])
                    elif tok.startswith("source="):
                        source = tok[7:]
                    elif tok.startswith("start="):
                        try:
                            start = datetime.datetime.fromisoformat(tok[6:])
                        except ValueError:
                            pass
                continue
            if not line.endswith("\n"):          # capture is mid-write
                break
            s_, _, t_ = line.strip().partition(",")
            try:
                s_i, t_i = int(s_), int(t_)
            except ValueError:
                continue
            if ticks and t_i < ticks[-1]:        # board reset: the timer restarted
                segs.append((seq, ticks))
                seq, ticks = [], []
            seq.append(s_i)
            ticks.append(t_i)
    if ticks:
        segs.append((seq, ticks))
    return hz, source, start, segs


def load_events(paths):
    """Load one or more capture files into a single Events on a common timeline.

    Files are ordered by their `start=` time and placed on the timeline by wall clock, so
    breaks between runs show up as real gaps. Each uninterrupted run is its own segment."""
    if isinstance(paths, str):
        paths = [paths]
    files = [_read_file(p) for p in paths]
    files = [x for x in files if x[3]]
    if not files:
        return Events(*(np.zeros(0, np.int64) for _ in range(3)), DEFAULT_HZ, "unknown")
    hz = files[0][0]
    if any(x[0] != hz for x in files):
        sys.exit("files were captured with different tick rates; cannot combine")
    files.sort(key=lambda x: x[2] or datetime.datetime.max)      # stable; undated files keep order
    t0 = next((x[2] for x in files if x[2]), None)
    seqs, tks, ids, prev_end, n = [], [], [], None, 0
    for _, _, start, segs in files:
        for i, (sq, tk) in enumerate(segs):
            tk = np.array(tk, np.int64)
            off = 0
            if i == 0 and start and t0:
                off = int(round((start - t0).total_seconds() * hz))
            if prev_end is not None:
                off = max(off, prev_end + hz - int(tk[0]))        # keep order, >= 1 s apart
            tk = tk + off
            prev_end = int(tk[-1])
            seqs.append(np.array(sq, np.int64))
            tks.append(tk.astype(np.uint64))
            ids.append(np.full(len(sq), n, np.int64))
            n += 1
    source = "+".join(sorted({x[1] for x in files}))
    seq, ticks, seg = np.concatenate(seqs), np.concatenate(tks), np.concatenate(ids)
    # Outage detection: events lost without a seq gap (older captures numbered events on the host,
    # and a sleeping laptop loses serial data silently). Split segments at implausibly long gaps.
    outages = []
    d = np.diff(ticks.astype(np.int64))
    same = np.diff(seg) == 0
    if same.sum() >= 100:
        scale = float(np.median(d[same])) / math.log(2)
        big = same & (d > OUTAGE_FACTOR * scale)
        outages = (d[big] / hz).tolist()
        seg = seg + np.concatenate([[0], np.cumsum(big)])
    return Events(seq, ticks, seg, hz, source, outages)


# --------------------------------------------------------------------------- extraction

def symbols(d, k):
    """Low k bits of each interval (in ticks)."""
    return (d & ((1 << k) - 1)).astype(np.int64)


def pair_bits(d):
    """Classic textbook method: compare successive gaps; ties discarded. Unbiased for iid gaps."""
    n = len(d) // 2 * 2
    a, b = d[0:n:2], d[1:n:2]
    keep = a != b
    return (a < b)[keep].astype(np.uint8)


def shannon(x, k):
    c = np.bincount(x, minlength=1 << k)
    p = c[c > 0] / len(x)
    return float(-(p * np.log2(p)).sum())


def min_entropy_mcv(x, k):
    """NIST SP 800-90B 6.3.1 most-common-value estimate (bits per k-bit symbol)."""
    n = len(x)
    if n < 2:
        return 0.0
    p = np.bincount(x, minlength=1 << k).max() / n
    p_u = min(1.0, p + 2.576 * math.sqrt(p * (1 - p) / (n - 1)))
    return -math.log2(p_u)


def chi2_uniform(x, k):
    """(chi2, approx p) for uniformity; Wilson-Hilferty approximation. None if too few samples."""
    m, n = 1 << k, len(x)
    if n < 5 * m:
        return None
    c = np.bincount(x, minlength=m)
    e = n / m
    chi = float(((c - e) ** 2 / e).sum())
    df = m - 1
    z = ((chi / df) ** (1 / 3) - (1 - 2 / (9 * df))) / math.sqrt(2 / (9 * df))
    return chi, 0.5 * math.erfc(z / math.sqrt(2))


def serial_corr(x):
    if len(x) < 3 or x.std() == 0:
        return 0.0
    return float(np.corrcoef(x[:-1], x[1:])[0, 1])


def condition(x, k, h_sym, nbytes):
    """SHA-256 conditioning. Each block hashes enough symbols to carry OUT_BITS+SAFETY_BITS of
    measured min-entropy. Returns (bytes, symbols_per_block, symbols_consumed)."""
    if h_sym <= 0:
        return b"", 0, 0
    per_block = math.ceil((OUT_BITS + SAFETY_BITS) / h_sym)
    dt = "<u2" if k > 8 else "u1"
    out, used = bytearray(), 0
    while len(out) < nbytes and used + per_block <= len(x):
        out += hashlib.sha256(x[used:used + per_block].astype(dt).tobytes()).digest()
        used += per_block
    return bytes(out[:nbytes]), per_block, used


# --------------------------------------------------------------------------- analysis

def analyze(ev, k):
    d = ev.intervals
    r = {"events": len(ev.ticks), "intervals": len(d), "k": k, "source": ev.source, "outages": ev.outages}
    if len(d) < 10:
        return r
    sec = d / ev.hz
    mean = float(sec.mean())
    r.update(
        duration=float(sum(int(ev.ticks[j]) - int(ev.ticks[i]) for i, j in ev.segment_bounds()) / ev.hz),
        segments=len(ev.segment_bounds()),
        cpm=60.0 / mean,
        mean_gap=mean,
        cv=float(sec.std() / mean),                 # exponential -> 1 (a little under, from dead time)
        dead_time_us=float(sec.min() * 1e6),
    )
    bounds = ev.segment_bounds()
    w = mean * 10                                   # ~10 expected counts per window
    counts = []
    for i, j in bounds:
        ts = (ev.ticks[i:j + 1].astype(np.float64) - float(ev.ticks[i])) / ev.hz
        n = int(ts[-1] // w)
        if n >= 1:
            counts.append(np.bincount((ts // w).astype(int), minlength=n + 1)[:n])
    c = np.concatenate(counts) if counts else np.zeros(0)
    if len(c) >= 8:
        r["fano"] = float(c.var() / c.mean())       # Poisson -> 1
    x = symbols(d, k)
    r["symbols"] = x
    r["shannon"] = shannon(x, k)
    r["min_entropy"] = min_entropy_mcv(x, k)
    r["chi2"] = chi2_uniform(x, k)
    r["serial"] = serial_corr(x.astype(np.float64))
    pb = pair_bits(d)
    r["pair_bits"] = pb
    r["out"], r["per_block"], r["used"] = condition(x, k, r["min_entropy"], 1 << 30)
    return r


def text_report(r):
    L = [f"source        : {r['source']}" + ("   <-- NOT QUANTUM, PRNG DEMO DATA" if "SIMULATED" in r["source"] else ""),
         f"events        : {r['events']}   usable intervals: {r['intervals']}"]
    if "cpm" not in r:
        return "\n".join(L + ["(need >= 10 intervals)"])
    if r["outages"]:
        L.append(f"OUTAGES       : {len(r['outages'])} excluded, longest {max(r['outages']):.0f} s")
    L += [f"recorded      : {r['duration']:.0f} s in {r['segments']} segment(s)",
          f"rate          : {r['cpm']:.1f} CPM   mean gap {r['mean_gap']*1e3:.1f} ms",
          f"gap CV        : {r['cv']:.3f}   (Poisson/exponential: 1.0)",
          f"min gap       : {r['dead_time_us']:.0f} us  (tube dead time)"]
    if "fano" in r:
        L.append(f"Fano factor   : {r['fano']:.2f}   (Poisson counts: 1.0)")
    L += [f"raw symbols   : low {r['k']} bits of each gap",
          f"  Shannon     : {r['shannon']:.3f} / {r['k']} bits",
          f"  min-entropy : {r['min_entropy']:.3f} / {r['k']} bits  (SP 800-90B MCV, 99% conf.)"]
    if r["chi2"]:
        L.append(f"  chi-square  : {r['chi2'][0]:.0f}  p~{r['chi2'][1]:.3f}  (uniformity)")
    L.append(f"  lag-1 corr  : {r['serial']:+.4f}")
    pb = r["pair_bits"]
    if len(pb):
        L.append(f"pair-compare  : {len(pb)} bits, {pb.mean()*100:.1f}% ones")
    L.append(f"conditioned   : {len(r['out'])} bytes ({r['per_block']} events per 256-bit block)")
    return "\n".join(L)


# --------------------------------------------------------------------------- deep analysis

def ks_exponential(sec, sims=200, seed=0):
    """KS distance of gaps vs a fitted exponential, with a Monte-Carlo p-value (the fitted mean
    makes the textbook KS table invalid)."""
    def dist(v):
        v = np.sort(v)
        n = len(v)
        f = 1 - np.exp(-v / v.mean())
        return max((np.arange(1, n + 1) / n - f).max(), (f - np.arange(n) / n).max())
    x = sec - sec.min()
    D = dist(x)
    rng = np.random.default_rng(seed)
    hits = sum(dist(rng.exponential(1.0, len(x))) >= D for _ in range(sims))
    return D, (1 + hits) / (sims + 1)


def runs_z(bits):
    n1 = int(bits.sum()); n0 = len(bits) - n1; n = n0 + n1
    runs = 1 + int((bits[1:] != bits[:-1]).sum())
    mu = 2 * n0 * n1 / n + 1
    var = (mu - 1) * (mu - 2) / (n - 1)
    return (runs - mu) / math.sqrt(var)


def deep_analysis(ev, k):
    import zlib
    r = analyze(ev, k)
    if "symbols" not in r:
        return
    d, x, n = ev.intervals, r["symbols"], len(r["symbols"])
    flag = lambda z: "  <-- CHECK" if abs(z) > 3.5 else ""
    print("\n=== 1. Is the source Poisson? ===")
    D, p = ks_exponential(d / ev.hz)
    print(f"KS vs exponential : D={D:.4f}  Monte-Carlo p={p:.2f}" + ("  <-- CHECK (gaps deviate from exponential)" if p < 0.01 else ""))
    sec = d / ev.hz
    chunks = np.array_split(sec, 10)
    rates = [60 * len(c) / c.sum() for c in chunks]
    print("rate by decile CPM: " + " ".join(f"{v:.0f}" for v in rates) + f"   (spread {min(rates):.0f}-{max(rates):.0f})")
    flat = np.concatenate([c / c.mean() for c in np.array_split(sec, 20)])
    D2, p2 = ks_exponential(flat)
    print(f"KS after removing slow rate drift (20 blocks): D={D2:.4f}  Monte-Carlo p={p2:.2f}" + ("  <-- CHECK" if p2 < 0.01 else ""))
    print("Fano vs window    : " + "  ".join(f"{m:g}x:{_fano(ev, r['mean_gap'] * m):.2f}" for m in (5, 10, 20, 50)))

    print("\n=== 2. Raw symbol quality (low %d bits of each gap) ===" % k)
    print("per-bit P(1)      : " + " ".join(f"b{b}:{((x >> b) & 1).mean():.3f}" for b in range(k)))
    zs = [(((x >> b) & 1).mean() - 0.5) * 2 * math.sqrt(n) for b in range(k)]
    print(f"  worst bit z     : {max(zs, key=abs):+.2f}{flag(max(zs, key=abs))}")
    xf = x.astype(np.float64)
    ac = [float(np.corrcoef(xf[:-l], xf[l:])[0, 1]) * math.sqrt(n) for l in range(1, 9)]
    print("autocorr z, lag1-8: " + " ".join(f"{z:+.1f}" for z in ac) + flag(max(ac, key=abs)))
    raw = x.astype("<u2" if k > 8 else "u1").tobytes()
    print(f"zlib ratio        : {len(zlib.compress(raw, 9)) / len(raw):.4f}  (random ~1.00; <{k / (16 if k > 8 else 8):.2f} means structure)")
    print("min-entropy vs k (MCV, 99% conf.):")
    for kk in (2, 4, 6, 8, 10, 12):
        xx = symbols(d, kk)
        h = min_entropy_mcv(xx, kk)
        print(f"  k={kk:<2}  Shannon {shannon(xx, kk):5.2f}  MCV {h:5.2f}  ({h / kk * 100:4.0f}% of {kk})")
    ideal = np.mean([min_entropy_mcv(np.random.default_rng(i).integers(0, 1 << k, n), k) for i in range(10)])
    print(f"ideal uniform, same n: MCV {ideal:.2f} at k={k}   (estimator is sample-size limited)")
    halves = [min_entropy_mcv(x[: n // 2], k), min_entropy_mcv(x[n // 2:], k)]
    print(f"first/second half : MCV {halves[0]:.2f} / {halves[1]:.2f}")

    print("\n=== 3. Conditioned output ===")
    out = r["out"]
    bits = np.unpackbits(np.frombuffer(out, np.uint8))
    nb = len(bits)
    zm = (bits.mean() - 0.5) * 2 * math.sqrt(nb)
    print(f"bytes             : {len(out)} from {r['used']} events ({r['per_block']} per block, {len(out) // 32} blocks)")
    print(f"monobit           : {bits.mean() * 100:.2f}% ones, z={zm:+.2f}{flag(zm)}")
    zr = runs_z(bits)
    print(f"runs test         : z={zr:+.2f}{flag(zr)}")
    c = chi2_uniform(np.frombuffer(out, np.uint8).astype(np.int64), 8)
    print(f"byte chi-square   : {c[0]:.0f}  p~{c[1]:.3f}" if c else "byte chi-square   : too few bytes")
    ob = np.frombuffer(out, np.uint8).astype(np.float64)
    zc = float(np.corrcoef(ob[:-1], ob[1:])[0, 1]) * math.sqrt(len(ob))
    print(f"byte lag-1 corr z : {zc:+.2f}{flag(zc)}")
    print(f"zlib ratio        : {len(zlib.compress(out, 9)) / len(out):.4f}")
    blk = [out[i:i + 32] for i in range(0, len(out), 32)]
    print(f"duplicate blocks  : {len(blk) - len(set(blk))}")
    print("\nFor authoritative min-entropy run NIST ea_non_iid on `generate --raw`; for the output use dieharder/PractRand.")


def _fano(ev, w):
    counts = []
    for i, j in ev.segment_bounds():
        ts = (ev.ticks[i:j + 1].astype(np.float64) - float(ev.ticks[i])) / ev.hz
        m = int(ts[-1] // w)
        if m >= 1:
            counts.append(np.bincount((ts // w).astype(int), minlength=m + 1)[:m])
    c = np.concatenate(counts) if counts else np.zeros(0)
    return float(c.var() / c.mean()) if len(c) >= 8 else float("nan")


# --------------------------------------------------------------------------- NIST SP 800-22

NIST22_TESTS = [
    "monobit_test", "frequency_within_block_test", "runs_test", "longest_run_ones_in_a_block_test",
    "binary_matrix_rank_test", "dft_test", "non_overlapping_template_matching_test",
    "overlapping_template_matching_test", "maurers_universal_test", "linear_complexity_test",
    "serial_test", "approximate_entropy_test", "cumulative_sums_test", "random_excursion_test",
    "random_excursion_variant_test"]


# This suite's template test draws ONE random template per call, so a single P is a 1-in-100 lottery.
# Minimum input lengths from NIST SP 800-22 rev 1a; below these the suite can emit bogus P-values
# (e.g. P=0 FAIL from the rank test on 27 kbit), so such tests are skipped instead.
NIST22_MIN_BITS = {"monobit_test": 100, "frequency_within_block_test": 100, "runs_test": 100,
                   "longest_run_ones_in_a_block_test": 128, "binary_matrix_rank_test": 38912, "dft_test": 1000,
                   "cumulative_sums_test": 100, "serial_test": 100, "approximate_entropy_test": 100,
                   "non_overlapping_template_matching_test": 100}
NIST22_REPEAT = {"non_overlapping_template_matching_test": 20}


HERE = os.path.dirname(os.path.abspath(__file__))


def find_suite(arg=None):
    """Locate dj-on-github/sp800_22_tests (git submodule `sp800_22_tests-py`, GPL-2)."""
    for c in (arg, os.environ.get("SP800_22_DIR"), os.path.join(HERE, "sp800_22_tests-py"),
              os.path.join(HERE, "third_party", "sp800_22_tests")):
        if c and os.path.isfile(os.path.join(c, "sp800_22_monobit_test.py")):
            return c
    return None


SUITE_HELP = "git submodule update --init sp800_22_tests-py"


def cmd_nist22(a):
    suite = find_suite(a.suite)
    if not suite:
        sys.exit(f"SP 800-22 suite not found. Fetch the submodule:\n  {SUITE_HELP}")
    run_nist22(load_events(a.files), a.k, a.raw, suite)


def run_nist22(ev, k, raw, suite):
    """Run the SP 800-22 suite on the conditioned output (or raw symbols); prints a table."""
    import contextlib
    import importlib
    import io
    x = symbols(ev.intervals, k)
    if raw:
        data, what = x.astype("<u2" if k > 8 else "u1").tobytes(), f"raw low-{k}-bit symbols"
    else:
        data, _, _ = condition(x, k, min_entropy_mcv(x, k), 1 << 30)
        what = "conditioned output"
    bits = np.unpackbits(np.frombuffer(data, np.uint8)).tolist()
    print(f"{what}: {len(data)} bytes = {len(bits)} bits (source: {ev.source})")
    sys.path.insert(0, suite)
    counts = {"PASS": 0, "FAIL": 0, "SKIPPED": 0, "ERROR": 0}
    print(f"{'test':42} {'min P':>10}  result")
    walk = np.cumsum(2 * np.unpackbits(np.frombuffer(data, np.uint8)).astype(np.int64) - 1)
    cycles = int((walk == 0).sum()) + 1             # random-walk cycles J, needed by the excursion tests
    for name in NIST22_TESTS:
        if name.startswith("random_excursion") and cycles < 500:
            print(f"{name:42} {'-':>10}  SKIPPED (invalid: only {cycles} walk cycles, NIST needs >= 500)")
            counts["SKIPPED"] += 1
            continue
        need = NIST22_MIN_BITS.get(name, 0)
        if len(bits) < need:
            print(f"{name:42} {'-':>10}  SKIPPED (not enough bits: needs {need:,}, have {len(bits):,})")
            counts["SKIPPED"] += 1
            continue
        func = getattr(importlib.import_module("sp800_22_" + name), name)
        buf = io.StringIO()
        reps = NIST22_REPEAT.get(name, 1)
        try:
            with contextlib.redirect_stdout(buf):
                results = [func(bits) for _ in range(reps)]
            ok, p, plist = results[0]
            text = buf.getvalue()
            if reps > 1:        # randomized test (picks one template per call): judge the proportion
                fails = sum(not r[0] for r in results)
                med = float(np.median([r[1] for r in results]))
                status = "PASS" if fails <= 2 else "FAIL"      # P(>=3 of 20 fail | random) ~ 0.001
                print(f"{name:42} {med:10.4f}  {status} ({reps} random templates, {fails} failed, median P shown)")
                counts[status] += 1
                continue
            if "Insufficient data" in text or "Need at least" in text:
                status, ptxt = "SKIPPED", "-"
                need = [l.strip() for l in text.splitlines() if "Need at least" in l or "Insufficient" in l]
                ptxt = need[0][:60] if need else "-"
                print(f"{name:42} {'-':>10}  SKIPPED (not enough bits: {ptxt})")
                counts[status] += 1
                continue
            pv = [p] if p is not None else []
            pv += list(plist or [])
            status = "PASS" if ok else "FAIL"
            print(f"{name:42} {min(pv):10.4f}  {status}" + (f" ({len(pv)} sub-tests)" if len(pv) > 1 else ""))
        except Exception as e:  # the suite is third-party code; never let one test kill the report
            status = "ERROR"
            print(f"{name:42} {'-':>10}  ERROR {type(e).__name__}: {e}")
        counts[status] += 1
    print("\n" + ", ".join(f"{v} {k.lower()}" for k, v in counts.items() if v))
    print("Pass threshold is P >= 0.01, so even perfect data fails ~1% of sub-tests; judge by the pattern, not one P.")
    if counts["SKIPPED"]:
        print("Skipped tests need ~1,000,000 bits; capture longer (or pool captures) to run them.")


# --------------------------------------------------------------------------- reports

def write_reports(files, base, k, nist="out", suite=None):
    """Write <base>.png / .hex / .raw / .md for one or more capture files. Returns the paths."""
    import contextlib
    import io
    say = lambda m: print(m, file=sys.stderr, flush=True)
    ev = load_events(files)
    r = analyze(ev, k)
    outs = {e: f"{base}.{e}" for e in ("png", "hex", "raw", "md")}
    os.makedirs(os.path.dirname(base) or ".", exist_ok=True)
    md = [f"# Analysis: {os.path.basename(base)}", "",
          f"Generated {datetime.datetime.now():%Y-%m-%d %H:%M} by `qrng.py` from "
          + ", ".join(f"`{os.path.basename(p)}`" for p in files) + f" (low {k} bits of each gap).", ""]
    if "cpm" not in r:
        md += ["Not enough events to analyze (need at least 10 intervals).", "", "```", text_report(r), "```"]
        open(outs["md"], "w").write("\n".join(md) + "\n")
        say(f"too few events for a full report; wrote {outs['md']}")
        return {"md": outs["md"]}

    say("report: figure")
    import matplotlib
    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt
    fig, axs = make_axes()
    draw(axs, ev, r)
    fig.savefig(outs["png"], dpi=130)
    plt.close(fig)

    x, out = r["symbols"], r["out"]
    open(outs["raw"], "wb").write(x.astype("<u2" if k > 8 else "u1").tobytes())
    if out:
        open(outs["hex"], "w").write("\n".join(out[i:i + 32].hex() for i in range(0, len(out), 32)) + "\n")

    say("report: detailed checks")
    deep = io.StringIO()
    with contextlib.redirect_stdout(deep):
        deep_analysis(ev, k)

    nist_text = {}
    if nist != "none":
        suite = find_suite(suite)
        for which in ({"out": ["out"], "raw": ["raw"], "both": ["out", "raw"]}[nist]):
            if not suite:
                nist_text[which] = f"SP 800-22 suite not found; fetch it with `{SUITE_HELP}` and run `qrng.py report`."
                continue
            say(f"report: SP 800-22 on {'raw symbols' if which == 'raw' else 'conditioned output'} "
                f"(pure Python, ~1 min per Mbit)")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                run_nist22(ev, k, which == "raw", suite)
            nist_text[which] = buf.getvalue().rstrip()

    flags = []
    if "SIMULATED" in r["source"]:
        flags.append("Data is **SIMULATED** (PRNG), not quantum.")
    if r["outages"]:
        flags.append(f"{len(r['outages'])} outage gap(s) excluded, longest {max(r['outages']):.0f} s (lost events or laptop sleep).")
    for line in deep.getvalue().splitlines() + [l for t in nist_text.values() for l in t.splitlines()]:
        if "CHECK" in line or line.rstrip().endswith("FAIL") or " FAIL " in line:
            flags.append(f"`{line.strip()}`")
    md += ["![analysis](%s)" % os.path.basename(outs["png"]), "", "## Flags", ""]
    md += [f"- {f}" for f in flags] or ["- none"]
    md += ["", "## Summary", "", "```", text_report(r), "```", "", "## Detailed checks", "", "```",
           deep.getvalue().strip(), "```"]
    for which, text in nist_text.items():
        md += ["", f"## NIST SP 800-22 ({'raw symbols' if which == 'raw' else 'conditioned output'})", "", "```", text, "```"]
    md += ["", "## Files", ""]
    for e, p in outs.items():
        if e == "md" or os.path.exists(p):
            md.append(f"- `{os.path.basename(p)}`" + {"png": " figure", "hex": " conditioned random output, one 32-byte block per line",
                                                      "raw": f" unconditioned {k}-bit symbols, one per byte" if k <= 8 else " unconditioned symbols (16-bit LE)",
                                                      "md": " this report"}[e])
    open(outs["md"], "w").write("\n".join(md) + "\n")
    say("report: wrote " + ", ".join(os.path.basename(p) for p in outs.values() if os.path.exists(p) or p == outs["md"]))
    return outs


def default_base(files, output=None):
    if output:
        return os.path.splitext(output)[0] if output.endswith((".png", ".hex", ".raw", ".md", ".csv")) else output
    if len(files) != 1:
        sys.exit("several input files: give the output base name with -o (e.g. -o data/combined)")
    return os.path.splitext(files[0])[0]


def cmd_report(a):
    base = default_base(a.files, a.output)
    existing = [f"{base}.{e}" for e in ("png", "hex", "raw", "md") if os.path.exists(f"{base}.{e}")]
    if existing and not a.force:
        sys.exit("would overwrite: " + ", ".join(existing) + "\n  (use --force, or choose another base with -o)")
    write_reports(a.files, base, a.k, a.nist22, a.suite)


# --------------------------------------------------------------------------- plotting

def draw(axs, ev, r):
    import matplotlib.pyplot as plt  # noqa: F401
    for a in axs.flat:
        a.clear()
    ax_rate, ax_gap, ax_sym, ax_raw, ax_out, ax_txt = axs.flat
    ax_txt.axis("off")
    ax_txt.text(0, 1, text_report(r), va="top", family="monospace", fontsize=7.5,
                color="crimson" if "SIMULATED" in r["source"] else "black")
    if "cpm" not in r:
        return
    d = ev.intervals
    t = (ev.ticks.astype(np.float64) - float(ev.ticks[0])) / ev.hz

    c, edges = np.histogram(t, bins=np.linspace(0, t[-1], 61))
    bw = edges[1]
    ax_rate.stairs(c / bw * 60, edges, color="C0")
    ax_rate.axhline(r["cpm"], color="k", lw=0.8, ls="--")
    ax_rate.set(title="Event rate (CPM per bin)", xlabel="seconds")

    sec = d / ev.hz
    m = r["mean_gap"]
    ax_gap.hist(sec, bins=np.linspace(0, 5 * m, 50), density=True, color="C1", alpha=0.8)
    xs = np.linspace(0, 5 * m, 200)
    ax_gap.plot(xs, np.exp(-xs / m) / m, "k", lw=1.2, label="exponential (Poisson)")
    ax_gap.set(title="Gap between pulses", xlabel="seconds", yscale="log")
    ax_gap.legend(fontsize=7)

    k, x = r["k"], r["symbols"]
    nb = min(1 << k, 64)
    ax_sym.hist(x >> max(k - 6, 0), bins=np.arange(nb + 1) - 0.5, color="C2", alpha=0.8)
    ax_sym.axhline(len(x) / nb, color="k", lw=0.8, ls="--")
    ax_sym.set(title=f"Raw symbol (low {k} bits of gap), {nb} bins", xlabel="value")

    def bitmap(ax, bits, title):
        img = np.full(64 * 64, 0.5)
        n = min(len(bits), 4096)
        img[:n] = bits[:n]
        ax.imshow(img.reshape(64, 64), cmap="gray", vmin=0, vmax=1, interpolation="nearest")
        ax.set(title=f"{title} ({n} bits)", xticks=[], yticks=[])

    raw_bits = np.unpackbits(x.astype(">u2").view(np.uint8).reshape(-1, 2), axis=1)
    raw_bits = raw_bits[:, 16 - k:].ravel()
    bitmap(ax_raw, raw_bits, "Raw bits")
    bitmap(ax_out, np.unpackbits(np.frombuffer(r["out"], np.uint8)), "Conditioned output")


def make_axes():
    import matplotlib.pyplot as plt
    fig, axs = plt.subplots(2, 3, figsize=(14, 7.5), gridspec_kw={"width_ratios": [1, 1, 1.1]})
    fig.tight_layout(pad=2.5)
    return fig, axs


# --------------------------------------------------------------------------- capture

# USB vendor IDs used by Mega 2560 boards: Arduino, Arduino SA, WCH CH340 (many clones), FTDI.
MEGA_VIDS = {0x2341, 0x2A03, 0x1A86, 0x0403}


def find_port():
    """Pick the serial port of a Mega-like board; fall back to any ttyACM/ttyUSB."""
    from serial.tools import list_ports
    found = [p for p in list_ports.comports() if p.vid in MEGA_VIDS]
    if found:
        found.sort(key=lambda p: p.device)
        if len(found) > 1:
            print("several candidate ports, using the first (override with --port):", file=sys.stderr)
            for p in found:
                print(f"  {p.device}  {p.description}", file=sys.stderr)
        print(f"using {found[0].device} ({found[0].description})", file=sys.stderr)
        return found[0].device
    ports = sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
    if not ports:
        sys.exit("no serial port found; plug in the board or pass --port")
    print(f"no recognised Arduino; guessing {ports[0]}", file=sys.stderr)
    return ports[0]


def paced(ticks, hz, speed):
    t0, first = time.monotonic(), None
    for t in ticks:
        first = t if first is None else first
        wait = (t - first) / hz / speed - (time.monotonic() - t0)
        if wait > 0:
            time.sleep(wait)
        yield t


def sim_ticks(cpm, hz, dead_us=150):
    rng = np.random.default_rng()
    t = 0
    while True:
        t += int(rng.exponential(60 / cpm * hz)) + int(dead_us * 1e-6 * hz)
        yield t


def cmd_capture(a):
    out = a.output or time.strftime("data/geiger-%Y%m%d-%H%M%S.csv")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    if os.path.exists(out) and not a.force:
        sys.exit(f"{out} exists; refusing to overwrite (use --force, or pick another name)")
    f = open(out, "w")
    seq, hz = 0, DEFAULT_HZ

    started = datetime.datetime.now()
    recent = collections.deque()          # event ticks within the last CPM_WINDOW_S seconds

    def event(t, n=None):
        nonlocal seq
        f.write(f"{seq if n is None else n},{t}\n")
        f.flush()
        seq += 1
        recent.append(t)
        while t - recent[0] > CPM_WINDOW_S * hz:
            recent.popleft()
        if time.monotonic() - event.last >= 1:
            event.last = time.monotonic()
            span = min(CPM_WINDOW_S, (t - recent[0]) / hz)
            cpm = f"{(len(recent) - 1) / span * 60:6.1f}" if span >= 2 else "   ..."
            now = datetime.datetime.now()
            elapsed = str(now - started).split(".")[0]
            print(f"\r{seq:>8} events | {cpm} CPM (last {CPM_WINDOW_S}s) | last event {now:%H:%M:%S} | running {elapsed} ",
                  end="", file=sys.stderr, flush=True)
    event.last = time.monotonic()

    try:
        if a.simulate or a.replay:
            if a.replay:
                src = load_events(a.replay)
                hz, label, ticks = src.hz, f"replay-of-{src.source}", iter(src.ticks.tolist())
            else:
                hz, label, ticks = DEFAULT_HZ, "SIMULATED", sim_ticks(a.simulate, DEFAULT_HZ)
            f.write(f"# source={label} start={started:%Y-%m-%dT%H:%M:%S} tick_hz={hz}\n")
            print(f"writing {out} ({label}, speed x{a.speed})", file=sys.stderr)
            for t in paced(ticks, hz, a.speed):
                event(t)
        else:
            import serial
            port = a.port or find_port()
            print(f"reading {port} -> {out}   (Ctrl-C to stop)", file=sys.stderr)
            with serial.Serial(port, a.baud, timeout=1) as ser:
                while True:
                    line = ser.readline().decode("ascii", "replace").strip()
                    if not line:
                        continue
                    if line.startswith("#"):
                        if "tick_hz=" in line:
                            # Opening the port resets the Mega: discard events from before the reboot.
                            f.seek(0)
                            f.truncate()
                            seq = 0
                            recent.clear()
                            hz = int(line.split("tick_hz=")[1].split()[0])
                            started = datetime.datetime.now()
                            f.write(f"# source=geiger start={started:%Y-%m-%dT%H:%M:%S} {line[1:].strip()}\n")
                        elif "dropped=" in line and not line.endswith("dropped=0"):
                            f.write(line + "\n")
                            print("\n" + line, file=sys.stderr)
                        continue
                    s, _, t = line.partition(",")
                    if s.isdigit() and t.isdigit():
                        event(int(t), int(s))
    except KeyboardInterrupt:
        pass
    finally:
        f.close()
        print(f"\n{seq} events saved to {out}", file=sys.stderr)
    if seq and not a.no_report:
        try:
            write_reports([out], default_base([out]), a.k, a.nist22, None)
        except KeyboardInterrupt:
            print("\nreport interrupted; the capture file is safe. Re-run: ./qrng.py report " + out + " --force", file=sys.stderr)
        except Exception as e:      # never risk the capture over a reporting problem
            print(f"\nreport failed ({type(e).__name__}: {e}); capture is saved. Retry: ./qrng.py report {out} --force", file=sys.stderr)


# --------------------------------------------------------------------------- other commands

def cmd_analyze(a):
    ev = load_events(a.files)
    r = analyze(ev, a.k)
    print(text_report(r))
    if a.full:
        deep_analysis(ev, a.k)
    if a.png and "cpm" in r:
        import matplotlib
        matplotlib.use("Agg")
        fig, axs = make_axes()
        draw(axs, ev, r)
        fig.savefig(a.png, dpi=130)
        print(f"figure: {a.png}")


def cmd_generate(a):
    ev = load_events(a.files)
    d = ev.intervals
    x = symbols(d, a.k)
    if a.raw:
        data = x.astype("<u2" if a.k > 8 else "u1").tobytes()
        note = f"{len(x)} raw {a.k}-bit symbols"
    else:
        h = min_entropy_mcv(x, a.k)
        data, per_block, used = condition(x, a.k, h, a.bytes)
        note = f"{len(data)} bytes from {used} events (min-entropy {h:.2f}/{a.k} bits each, {per_block} events/block)"
        if len(data) < a.bytes:
            need = per_block * math.ceil((a.bytes - len(data)) / 32) if per_block else 0
            note += f"; wanted {a.bytes}, need ~{need} more events"
    if a.output:
        open(a.output, "wb").write(data)
    elif a.raw:
        sys.stdout.buffer.write(data)
    else:
        print(data.hex())
    print(note, file=sys.stderr)


def cmd_dashboard(a):
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation
    fig, axs = make_axes()

    def update(_):
        ev = load_events(a.files)
        draw(axs, ev, analyze(ev, a.k))

    anim = FuncAnimation(fig, update, interval=a.refresh * 1000, cache_frame_data=False)  # noqa: F841
    plt.show()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("capture", help="record events to CSV")
    c.add_argument("--port"); c.add_argument("--baud", type=int, default=115200)
    c.add_argument("--force", action="store_true", help="overwrite an existing output file")
    c.add_argument("-o", "--output", help="default data/geiger-<timestamp>.csv")
    c.add_argument("--simulate", type=float, metavar="CPM", help="PRNG-generated Poisson events (NOT quantum)")
    c.add_argument("--replay", metavar="FILE", help="re-emit a recorded file in real time")
    c.add_argument("--speed", type=float, default=1.0, help="time scale for --simulate/--replay")
    c.add_argument("--no-report", action="store_true", help="don't write png/hex/raw/md when the capture ends")
    c.add_argument("-k", type=int, default=8, choices=range(1, 17), metavar="1-16",
                   help="low bits of each gap to use in the final report (default 8)")
    c.add_argument("--nist22", choices=("none", "out", "raw", "both"), default="out",
                   help="SP 800-22 in the final report: conditioned output (default), raw, both, or none")
    c.set_defaults(fn=cmd_capture)

    for name, fn in (("analyze", cmd_analyze), ("generate", cmd_generate), ("dashboard", cmd_dashboard), ("nist22", cmd_nist22), ("report", cmd_report)):
        s = sub.add_parser(name)
        s.add_argument("files", nargs="+", metavar="FILE", help="one or more capture files (pooled)")
        s.add_argument("-k", type=int, default=8, choices=range(1, 17), metavar="1-16",
                       help="low bits of each gap to use (default 8)")
        s.set_defaults(fn=fn)
        if name == "analyze":
            s.add_argument("--png", help="save the dashboard figure")
            s.add_argument("--full", action="store_true", help="extra statistical checks")
        if name == "generate":
            s.add_argument("-n", "--bytes", type=int, default=32)
            s.add_argument("-o", "--output")
            s.add_argument("--raw", action="store_true", help="unconditioned symbols (for NIST ea_non_iid)")
        if name in ("nist22", "report"):
            s.add_argument("--suite", help="path to dj-on-github/sp800_22_tests (default: the sp800_22_tests-py submodule, "
                           "then $SP800_22_DIR)")
        if name == "nist22":
            s.add_argument("--raw", action="store_true", help="test the unconditioned symbols instead of the output")
        if name == "report":
            s.add_argument("-o", "--output", metavar="BASE", help="output base name (default: the input file name; required for several files)")
            s.add_argument("--force", action="store_true", help="overwrite existing report files")
            s.add_argument("--nist22", choices=("none", "out", "raw", "both"), default="out",
                           help="SP 800-22: conditioned output (default), raw, both, or none")
        if name == "dashboard":
            s.add_argument("--refresh", type=float, default=1.0)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()

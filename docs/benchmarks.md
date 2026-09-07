# EvoUndo Performance Benchmarks

This document records empirical latency, memory overhead, and throughput benchmarks for EvoUndo.

---

## 1. Latency Characteristics

Measured over $N=100$ iterations on Apple Silicon (M-series, Python 3.12):

| Metric | p50 | p95 | p99 | Description |
|:---|:---:|:---:|:---:|:---|
| **Core Protection Wrapper** | $0.024\text{ ms}$ | $0.041\text{ ms}$ | $0.068\text{ ms}$ | Wrapper dispatch overhead above baseline function execution |
| **Witness Capture** | $0.054\text{ ms}$ | $0.112\text{ ms}$ | $0.185\text{ ms}$ | Pre-mutation state inspection and serialization |
| **In-Memory Operation Dispatch** | $0.810\text{ ms}$ | $1.740\text{ ms}$ | $2.310\text{ ms}$ | Complete cycle (capture + mutate + probe) in-memory |
| **Selective Rollback** | $0.227\text{ ms}$ | $0.485\text{ ms}$ | $0.720\text{ ms}$ | State inversion and physical probe assertion |
| **Durable WAL Write Flush** | $4.350\text{ ms}$ | $7.950\text{ ms}$ | $11.20\text{ ms}$ | Synchronous `fsync` to SQLite write-ahead log / journal |

---

## 2. Memory Footprint

* **Mutation Envelope in Memory**: $\approx 17.66\text{ KB}$ per active mutation (including JSON-serialized witness and causal tree node).
* **Journal Storage on Disk**: $\approx 1.2\text{ KB}$ per mutation entry in append-only SQLite WAL format.
* **Pruning**: Completed and reverted mutations can be automatically pruned or archived using `MutationRegistry.prune()`.

---

## 3. Methodology & Caveats

These measurements were captured in controlled local benchmark harnesses. In real-world deployments:
- Network round-trips to remote databases (e.g. AWS RDS or MongoDB Atlas) typically dominate execution latency.
- LLM inference times ($\approx 500\text{–}3000\text{ ms}$) exceed EvoUndo's protection overhead by several orders of magnitude.

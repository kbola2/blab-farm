# Pooling the RAM of several machines for one solve

**Status: design note, not a feature.** The farm splits a sweep by *frequency*: every job still has to
fit in one machine's memory. This note records what it would take to split one frequency's matrix across
machines, so that the question has a written answer and a path.

## What the farm can and cannot do today

A dense BEM solve builds one `N x N` complex matrix per frequency (N = P1 unknowns, about half the
triangle count). Measured on Boundary Lab 0.5.0 / BEAT 0.6.0 (Metal, 25,932 unknowns, 5.4 GB matrix):

| configuration | peak engine RSS | notes |
|---|---|---|
| dense LU, 1 thread | 11.65 GB | LU keeps a factored copy |
| GMRES, 1 thread | 11.1 GB | ~88 matvecs, no copy, 3.5x faster than LU here |
| default (auto, pipelined) | 10.6 GB | 2 frequencies, lookahead limited by free memory |
| GMRES + pipelining off | 11.1 GB | no saving on a machine where lookahead was already 1 |

So the engine's minimum is about **2.1 matrices + 0.5 GiB** regardless of solver (`estimate_gib` in
`farm.py` uses exactly that), and a bigger machine is used *faster*, not *leaner*: with spare RAM the
Metal path assembles up to four frequencies ahead. None of this lets two machines share one matrix.

## What true pooling needs

The hooks exist inside BEAT, but they are engine-level, not scheduler-level:

1. **Row-block assembly.** `assemble_burton_miller_neumann_system_cpu/metal` take
   `element_indices`, but that restricts *both* test and trial elements (a submesh). Pooling needs a
   variant that restricts only the *test* elements, producing the rows belonging to one machine's share
   of the unknowns while integrating against the whole mesh. Each machine then holds `N^2 / k` entries.
2. **Distributed matrix-vector product.** BEAT's GMRES (`beat_gmres!` in `BeatEngineDenseSolve.jl`)
   only touches the matrix through `_beat_apply_operator!` (one `mul!`). An operator type whose `mul!`
   broadcasts the vector to k workers over HTTP and concatenates their row-block products is all the
   solver needs. Per iteration that is one `N`-vector each way: 0.2 MB at 26k unknowns, 1 MB at 110k,
   times roughly 90 iterations. A home LAN is not the bottleneck; the matvec on each machine is.
3. **Post-processing stays on the coordinator.** Field points, polars and radiated power only need the
   surface solution vector, which the coordinator owns after GMRES converges.
4. **LU fallback disappears.** BEAT falls back to dense LU when GMRES stalls (it does, on sliver-rich
   meshes). A pooled solve has no machine that can hold the factorization, so a stalled GMRES must be
   reported as a failure, or preconditioned better (block-Jacobi from the row blocks is the natural
   choice and is free in this layout).

Estimated effort: a CPU-backend proof of concept is a few days of Julia inside a 60k-line engine;
Metal row-block assembly is harder because the fused kernels are precompiled. Either way every worker
runs a *modified engine*, which breaks this package's promise that stock Boundary Lab is enough. The
right home is upstream BEAT, with the farm providing the worker registry and transport. Until then:

- use the farm's per-machine memory model to send big bands to the machine that fits them,
- use quarter symmetry and frequency banding (coarse mesh for low bands), and
- reserve the largest machine for the top band (jobs with the fewest eligible workers run first).

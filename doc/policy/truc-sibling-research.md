# Separate research branch: indexed next-block projection

Branch: `research/truc-sibling-projection`, starting from draft commit
`f1fa0d3605`. This work is separate from the upstream draft PR. It remains opt-in
and is not a production recommendation.

## Improvement

The original selector reconstructed every remaining ancestor package after each
selection. This made even independent transactions expensive: the test with 1,755
mempool entries exhausted its two-million-step budget and rejected the replacement.

The research version constructs ancestor packages once, keeps them ordered by
actual feerate and txid, and maintains reverse membership so selecting an ancestor
only updates affected packages. Comparator keys are removed before mutation;
packages affected by ancestor selection are reconsidered. Both projections still
share a work budget and operate on an immutable snapshot under the mempool lock.

The eligibility rules are unchanged. In particular, this does not enable direct
shared-anchor replacements, alter transaction versions or consensus, or make the
projection reproduce Knots' priority mining policy. Arithmetic and invalid-index
checks reject malformed projection inputs rather than overflowing.

## Evidence

- A deliberately independent, slow oracle reconstructs all remaining ancestor
  packages. The optimized selection matches it on 500 deterministic randomized
  DAGs, varying ancestry, fees, capacity, sigops, exclusions and minimum feerates.
- A 5,000-independent-transaction unit fixture completes below 100,000 charged
  work steps, within the existing two-million-step budget.
- Explicit work-exhaustion and invalid-parent-index unit cases fail closed.
- The previous 1,755-entry congestion fixture now accepts the eligible replacement
  and removes the old sibling. Its assertion changed from expected exhaustion to
  expected successful recovery.
  One isolated run accepted it in 0.0189 seconds, versus an earlier 0.1293-second
  budget-exhaustion rejection. These are illustrative timings, not a controlled
  benchmark or a guaranteed speedup.
- Normal/RDTS cases and existing TRUC, package-RBF, package-limit and ordinary RBF
  regression coverage exercise the same unchanged policy boundaries.

The P2P fixture's default two-second log timeout proved too short under concurrent
load. It now waits up to 60 seconds for the peer's actual rejection diagnostic;
the assertion was not removed or replaced by an arbitrary sleep.
The failed seed and a fresh-seed rerun both passed with that timeout correction.
All eight functional variants/regressions are covered by successful runs, and all
six RBF unit cases pass, including the 500-graph differential test.

Work steps do not count every comparison, allocation, byte or CPU instruction.
These results do not establish a worst-case DoS bound. Dense ancestor graphs,
large configured limits, repeated invalid requests and lock contention remain
important profiling targets. Randomized differential testing is evidence of
equivalence, not a formal proof.

## Still unresolved

1. The fee-only projection can disagree with actual default mining templates.
   This optimization deliberately preserves that model rather than silently
   changing the admission condition. Miner-template/revenue semantics need a
   separately tested decision.
2. Shared P2A anchor conflicts remain excluded, as do legacy v2 Lightning paths.
   Broadening that scope requires a distinct experiment and threat analysis.
3. A rising congestion boundary can permit repeated shrinking; this is not a
   persistent one-use allowance or a lifetime relay-cost budget.
4. Real signed CLN/LND HTLC and Ark exit recovery still needs end-to-end testing.
   No fee-reserve reduction or production rollout follows from these tests.

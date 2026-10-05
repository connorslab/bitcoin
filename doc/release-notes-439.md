Transaction relay and mining policy
----------------------------------

- The default `mempooltruc` setting is now `enforce`, applying the requested
  TRUC topology and package-fee rules to version-3 transactions. Explicit
  `mempooltruc=accept` and `mempooltruc=reject` settings remain available.
  The redundant TRUC override in `corepolicy` has been removed; `corepolicy`
  itself and its other overrides are unchanged. The sub-dust fee penalty
  remains enabled by default. This change does not alter ephemeral-dust fee
  eligibility.

- Transactions with dust outputs are excluded from individual priority-space
  mining selection. They remain eligible for fee-based package selection,
  preventing priority space from mining an ephemeral-dust parent alone.

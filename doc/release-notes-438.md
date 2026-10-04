Transaction relay and mining policy
----------------------------------

- Ephemeral-dust transactions may have negative modified fees down to the
  calculated sub-dust fee penalty, while still requiring zero actual fees. The child must
  cover the penalty in package fee evaluation. Positive modified fees remain
  disallowed, as are modified fees below that bound. Policy defaults, `corepolicy` overrides, and existing dust and
  package restrictions are unchanged. The intended version-3 parent/child
  anchor packages require `-mempooltruc=enforce`. (#438)

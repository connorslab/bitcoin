Transaction relay and mining policy
----------------------------------

- Bitcoin now defaults to `mempooltruc=enforce`. Version-3 transactions use
  TRUC's bounded unconfirmed topology and fee-bumping rules, including package
  fee evaluation for a zero-fee parent and its fee-paying child. Operators can
  select `mempooltruc=accept` for the previous behavior or `mempooltruc=reject`
  to reject version-3 transactions from the mempool.
- `subdustfeepenalty` now defaults to `0`. This allows otherwise-permitted
  zero-fee ephemeral anchor parents to retain the zero modified fee required
  for package acceptance, supporting settlement and fee bumping for monetary
  protocols such as Ark. Set `subdustfeepenalty=1` to restore the penalty.
  Dust and ephemeral-output acceptance rules still apply. Explicit settings,
  including settings saved through the GUI, take precedence over the defaults.

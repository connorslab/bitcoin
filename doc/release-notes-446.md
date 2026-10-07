Mining policy
-------------

- Coin-age priority selection now respects `blockmintxfee`, including the
  `getblocktemplate` `minfeerate` override, using modified fees and policy
  virtual size. Transactions below this floor can still be selected with
  fee-paying descendants through package selection. This supports monetary
  protocols such as Ark that use a child to fund a pre-signed parent.
  Setting the mining fee floor to zero permits transactions with zero
  modified fees; it does not require an anchor parent and child to confirm
  together. Negative modified fees still need sponsorship at a zero floor.
  Version-3 anchor packages require `mempooltruc=enforce`; admission of
  ephemeral parents with negative modified fees is addressed separately
  in #438. This change does not alter relay policy defaults.

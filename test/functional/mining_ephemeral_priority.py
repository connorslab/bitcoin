#!/usr/bin/env python3
# Copyright (c) 2026 The Bitcoin Knots developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php.
"""Priority space must not select an ephemeral-dust parent independently."""

from decimal import Decimal
import time

from test_framework.messages import COutPoint, CTxIn, CTxInWitness, CTxOut
from test_framework.script_util import PAY_TO_ANCHOR
from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import assert_equal
from test_framework.wallet import MiniWallet


class EphemeralPriorityTest(BitcoinTestFramework):
    def add_options(self, parser):
        parser.add_argument("--test-subdust-penalty", action="store_true",
                            help="Also test native dust penalties (requires the ephemeral fee precheck fix)")

    def set_test_params(self):
        self.num_nodes = 1

    def template_txids(self):
        # Invalidate the five-second mempool-change template cache.
        self.mock_time += 10
        self.nodes[0].setmocktime(self.mock_time)
        return {tx["txid"] for tx in self.nodes[0].getblocktemplate({"rules": ["segwit"]})["transactions"]}

    def run_test(self):
        self.mock_time = int(time.time())
        for penalty in ([0, 1] if self.options.test_subdust_penalty else [0]):
            self.test_priority(penalty)

    def test_priority(self, penalty):
        self.log.info(f"Check priority selection with subdustfeepenalty={penalty}")
        args = ["-corepolicy=0", "-acceptnonstdtxn=0", "-persistmempool=0",
                "-blockprioritysize=1000000", "-blockmaxsize=1000000",
                "-blockmintxfee=0.01000000", f"-subdustfeepenalty={penalty}"]
        self.restart_node(0, extra_args=args)
        node = self.nodes[0]
        wallet = MiniWallet(node)
        parent = wallet.create_self_transfer(version=3, fee_rate=0, confirmed_only=True)["tx"]
        parent.vout.append(CTxOut(0, PAY_TO_ANCHOR))
        parent.rehash()
        sponsor = wallet.get_utxo(confirmed_only=True)
        child = wallet.create_self_transfer(utxo_to_spend=sponsor, version=3, fee=Decimal("0.00001000"))["tx"]
        child.vin.append(CTxIn(COutPoint(parent.sha256, 1)))
        child.wit.vtxinwit.append(CTxInWitness())
        child.rehash()
        assert_equal(node.submitpackage([parent.serialize().hex(), child.serialize().hex()])["package_msg"], "success")

        # Even with a child, priority selection must not bypass package fees.
        assert parent.hash not in self.template_txids()
        assert child.hash not in self.template_txids()

        # Replace only the child's independent sponsor spend, stranding the parent.
        conflict = wallet.create_self_transfer(utxo_to_spend=sponsor, version=3, fee=Decimal("0.00005000"))["tx"]
        node.sendrawtransaction(conflict.serialize().hex())
        assert_equal(set(node.getrawmempool()), {parent.hash, conflict.hash})
        template = self.template_txids()
        assert parent.hash not in template
        # The high mining fee floor excludes this ordinary transaction from
        # fee selection; its inclusion proves priority space is still active.
        assert conflict.hash in template

        # Restore the child with a fee meeting the mining package threshold.
        restored = wallet.create_self_transfer(version=3, fee=Decimal("0.01000000"), confirmed_only=True)["tx"]
        restored.vin.append(CTxIn(COutPoint(parent.sha256, 1)))
        restored.wit.vtxinwit.append(CTxInWitness())
        restored.rehash()
        node.sendrawtransaction(restored.serialize().hex(), 0)
        assert_equal(set(node.getrawmempool()), {parent.hash, conflict.hash, restored.hash})
        assert {parent.hash, restored.hash}.issubset(self.template_txids())
        block = node.getblock(self.generate(node, 1)[0])
        assert {parent.hash, restored.hash}.issubset(set(block["tx"]))
        assert_equal(node.gettxout(parent.hash, 1), None)


if __name__ == "__main__":
    EphemeralPriorityTest(__file__).main()

#!/usr/bin/env python3
# Copyright (c) 2026 The Bitcoin Knots developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php.
"""Test the mining fee floor in priority space and CPFP package selection."""

from decimal import Decimal
import time

from test_framework.messages import COIN, COutPoint, CTxIn, CTxInWitness, CTxOut
from test_framework.script_util import PAY_TO_ANCHOR
from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import assert_equal, assert_greater_than
from test_framework.wallet import MiniWallet


MIN_FEE_RATE = 10  # sat/vB


class PriorityFeeTest(BitcoinTestFramework):
    def add_options(self, parser):
        parser.add_argument("--test-subdust-penalty", action="store_true",
                            help="Also test native dust penalties (requires the ephemeral fee precheck fix)")

    def set_test_params(self):
        self.num_nodes = 1
        self.extra_args = [[
            "-corepolicy=0", "-acceptnonstdtxn=0", "-persistmempool=0",
            "-blockprioritysize=1000000", "-blockmaxsize=1000000",
            f"-blockmintxfee={Decimal(MIN_FEE_RATE) / 100000:.8f}",
        ]]

    def template_txids(self, **options):
        # Invalidate the five-second mempool-change template cache.
        self.mock_time += 10
        self.nodes[0].setmocktime(self.mock_time)
        return [tx["txid"] for tx in self.nodes[0].getblocktemplate({"rules": ["segwit"], **options})["transactions"]]

    def run_test(self):
        self.mock_time = int(time.time())
        self.test_priority_floor()
        self.test_anchor_package(dust_rate=3, penalty_enabled=False)
        if self.options.test_subdust_penalty:
            for dust_rate in (3, 6):
                self.test_anchor_package(dust_rate=dust_rate, penalty_enabled=True)

    def test_priority_floor(self):
        self.log.info("Test the priority fee boundary, ordering, and template overrides")
        node = self.nodes[0]
        wallet = MiniWallet(node)
        payments = []
        for adjustment in (-1, 0, 1):
            tx = wallet.create_self_transfer(fee_rate=0, confirmed_only=True)["tx"]
            tx.vout[0].nValue -= MIN_FEE_RATE * tx.get_vsize() + adjustment
            tx.rehash()
            node.sendrawtransaction(tx.serialize().hex())
            payments.append(tx)
        below, exact, above = payments
        # MiniWallet selects the oldest equal-value confirmed coins first.
        priorities = [node.getmempoolentry(tx.hash)["currentpriority"] for tx in payments]
        assert_greater_than(priorities[0], priorities[1])
        assert_greater_than(priorities[1], priorities[2])
        template = self.template_txids()
        assert below.hash not in template, "Priority selection bypassed the mining fee floor"
        # Priority order is the opposite of fee order for these same-size txs.
        assert_equal(template, [exact.hash, above.hash])
        assert_equal(self.template_txids(minfeerate=MIN_FEE_RATE + 1), [])
        assert_equal(self.template_txids(minfeerate=MIN_FEE_RATE - 1), [tx.hash for tx in payments])

        self.log.info("Test modified fees, including zero and negative values")
        node.prioritisetransaction(txid=below.hash, fee_delta=1)
        node.prioritisetransaction(txid=exact.hash, fee_delta=-1)
        node.prioritisetransaction(txid=above.hash, fee_delta=-(MIN_FEE_RATE * above.get_vsize() + 2))
        assert_equal(self.template_txids(), [below.hash])
        # Reduce exact's modified fee from floor-1 to zero. A zero floor permits
        # zero modified fees but still excludes negative modified fees.
        node.prioritisetransaction(txid=exact.hash, fee_delta=-(MIN_FEE_RATE * exact.get_vsize() - 1))
        assert_equal(self.template_txids(minfeerate=0), [below.hash, exact.hash])

        self.log.info("Test an explicitly configured zero mining fee floor")
        self.restart_node(0, extra_args=self.extra_args[0] + ["-blockmintxfee=0"])
        for tx in payments:
            node.sendrawtransaction(tx.serialize().hex())
        assert_equal(self.template_txids(), [tx.hash for tx in payments])

    def test_anchor_package(self, *, dust_rate, penalty_enabled):
        self.log.info(f"Test anchor CPFP with dust rate {dust_rate} and penalty enabled={penalty_enabled}")
        self.restart_node(0, extra_args=self.extra_args[0] + [
            "-mempooltruc=enforce",
            f"-dustrelayfee={Decimal(dust_rate) / 100000:.8f}",
            f"-subdustfeepenalty={int(penalty_enabled)}",
        ])
        node = self.nodes[0]
        wallet = MiniWallet(node)
        parent = wallet.create_self_transfer(version=3, fee_rate=0, confirmed_only=True)["tx"]
        parent.vout.append(CTxOut(0, PAY_TO_ANCHOR))
        parent.rehash()
        sponsor = wallet.get_utxo(confirmed_only=True)

        def make_child(utxo):
            child = wallet.create_self_transfer(version=3, fee_rate=0, utxo_to_spend=utxo)["tx"]
            child.vin.append(CTxIn(COutPoint(parent.sha256, 1)))
            child.wit.vtxinwit.append(CTxInWitness())
            return child

        child = make_child(sponsor)
        penalty = (len(parent.vout[1].serialize()) + 67) * dust_rate if penalty_enabled else 0
        required_fee = MIN_FEE_RATE * (parent.get_vsize() + child.get_vsize()) + penalty
        child.vout[0].nValue -= required_fee - 1
        child.rehash()
        assert_equal(node.submitpackage([parent.serialize().hex(), child.serialize().hex()])["package_msg"], "success")
        assert_equal(node.getmempoolentry(parent.hash)["fees"]["modified"], -Decimal(penalty) / COIN)
        assert_equal(self.template_txids(), [])
        assert_equal(self.template_txids(minfeerate=MIN_FEE_RATE - 1), [parent.hash, child.hash])

        # Replacing the child's independent sponsor spend can leave the parent
        # in the mempool. Priority cannot bypass a positive mining fee floor.
        conflict = wallet.create_self_transfer(utxo_to_spend=sponsor, version=3,
                                               fee=Decimal(required_fee + 1000) / COIN)["tx"]
        node.sendrawtransaction(conflict.serialize().hex())
        assert_equal(set(node.getrawmempool()), {parent.hash, conflict.hash})
        assert_equal(self.template_txids(), [conflict.hash])
        # This is a fee-floor policy, not a requirement to confirm parent and
        # child together. An explicit zero floor permits an unpenalized parent.
        assert_equal(parent.hash in self.template_txids(minfeerate=0), not penalty_enabled)

        # A new sponsor funds the exact package floor, including the parent's
        # negative modified fee from the automatic dust penalty when enabled.
        restored = make_child(wallet.get_utxo(confirmed_only=True))
        assert_equal(restored.get_vsize(), child.get_vsize())
        restored.vout[0].nValue -= required_fee
        restored.rehash()
        node.sendrawtransaction(restored.serialize().hex())
        template = self.template_txids()
        assert {parent.hash, restored.hash}.issubset(template)
        assert template.index(parent.hash) < template.index(restored.hash)
        block = node.getblock(self.generate(node, 1)[0])
        assert {parent.hash, restored.hash}.issubset(block["tx"])
        assert_equal(node.gettxout(parent.hash, 1), None)


if __name__ == '__main__':
    PriorityFeeTest(__file__).main()

#!/usr/bin/env python3
# Copyright (c) 2026 The Bitcoin developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
"""Test TRUC and ephemeral anchor packages with native policy defaults."""

from test_framework.messages import COutPoint, CTxIn, CTxInWitness, CTxOut
from test_framework.script_util import PAY_TO_ANCHOR
from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import assert_equal
from test_framework.wallet import MiniWallet


class MempoolPolicyDefaultsTest(BitcoinTestFramework):
    def set_test_params(self):
        self.num_nodes = 1
        # TestNode normally enables corepolicy, which would mask both defaults.
        self.policy_args = ["-corepolicy=0", "-acceptnonstdtxn=0", "-persistmempool=0"]
        self.extra_args = [self.policy_args]

    def run_test(self):
        self.wallet = MiniWallet(self.nodes[0])
        self.test_truc_modes()
        self.test_anchor_package()
        self.test_dust_restrictions()

    def test_truc_modes(self):
        self.log.info("Test default TRUC enforcement and explicit policy overrides")
        node = self.nodes[0]
        parent = self.wallet.create_self_transfer(version=3, confirmed_only=True)
        child = self.wallet.create_self_transfer(utxo_to_spend=parent["new_utxo"], version=2)

        for mode in [None, "accept", "reject", "enforce"]:
            self.restart_node(0, extra_args=self.policy_args + ([] if mode is None else [f"-mempooltruc={mode}"]))
            if mode == "reject":
                result = node.testmempoolaccept([parent["hex"]])[0]
                assert_equal(result["allowed"], False)
                assert_equal(result["reject-reason"], "version")
                continue
            node.sendrawtransaction(parent["hex"])
            result = node.testmempoolaccept([child["hex"]])[0]
            assert_equal(result["allowed"], mode == "accept")
            if mode != "accept":
                assert_equal(result["reject-reason"], "truc-spent-by-nontruc")

        self.restart_node(0)

    def test_anchor_package(self):
        self.log.info("Test a zero-fee parent with an ephemeral anchor and a fee-paying child")
        node = self.nodes[0]
        parent = self.wallet.create_self_transfer(version=3, fee_rate=0, confirmed_only=True)["tx"]
        parent.vout.append(CTxOut(0, PAY_TO_ANCHOR))
        parent.rehash()
        child = self.wallet.create_self_transfer_multi(version=3, fee_per_output=1000, confirmed_only=True)["tx"]
        child.vin.append(CTxIn(COutPoint(parent.sha256, 1)))
        child.wit.vtxinwit.append(CTxInWitness())
        child.rehash()
        package = [parent.serialize().hex(), child.serialize().hex()]

        # Each old setting independently prevents this otherwise-standard package.
        for option, reason in [
            ("-mempooltruc=accept", "min relay fee not met"),
            ("-subdustfeepenalty=1", "dust, tx with dust output must be 0-fee"),
        ]:
            self.restart_node(0, extra_args=self.policy_args + [option])
            result = node.submitpackage(package)
            assert_equal(result["package_msg"], "transaction failed")
            assert result["tx-results"][parent.getwtxid()]["error"].startswith(reason)
            assert_equal(node.getrawmempool(), [])

        # Check native defaults, explicit settings, and the compatibility preset.
        for options in [[], ["-mempooltruc=enforce", "-subdustfeepenalty=0"], ["-corepolicy=1"]]:
            self.restart_node(0, extra_args=self.policy_args + options)
            assert_equal(node.submitpackage(package)["package_msg"], "success")
            assert_equal(set(node.getrawmempool()), {parent.hash, child.hash})
            assert_equal(node.getmempoolentry(parent.hash)["fees"]["base"], 0)
            assert_equal(node.getmempoolentry(parent.hash)["fees"]["modified"], 0)
            child_fees = node.getmempoolentry(child.hash)["fees"]
            assert_equal(child_fees["base"], child_fees["modified"])

        # Verify block selection under native defaults as well as admission.
        self.restart_node(0)
        assert_equal(node.submitpackage(package)["package_msg"], "success")
        template = node.getblocktemplate({"rules": ["segwit"]})
        assert {parent.hash, child.hash}.issubset({tx["txid"] for tx in template["transactions"]})
        block = node.getblock(self.generate(node, 1)[0])
        assert {parent.hash, child.hash}.issubset(set(block["tx"]))

    def test_dust_restrictions(self):
        self.log.info("Test that disabling the penalty does not relax dust acceptance")
        node = self.nodes[0]
        # Native ephemeral policy permits only zero-value anchors, not ordinary
        # dust outputs or nonzero sub-dust anchors, even with a zero-fee parent.
        for value, script, reason in [
            (0, self.wallet.get_output_script(), "dust-nonanchor"),
            (1, PAY_TO_ANCHOR, "dust-nonzero"),
        ]:
            tx = self.wallet.create_self_transfer(version=3, fee_rate=0, confirmed_only=True)["tx"]
            tx.vout[0].nValue -= value
            tx.vout.append(CTxOut(value, script))
            result = node.testmempoolaccept([tx.serialize().hex()])[0]
            assert_equal(result["allowed"], False)
            assert_equal(result["reject-reason"], reason)


if __name__ == '__main__':
    MempoolPolicyDefaultsTest(__file__).main()

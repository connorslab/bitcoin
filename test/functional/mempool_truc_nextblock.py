#!/usr/bin/env python3
# Copyright (c) 2026 The Bitcoin Core developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
"""Experimental sibling-only next-block exception (synthetic transactions)."""
from decimal import Decimal

from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import assert_equal, assert_raises_rpc_error
from test_framework.wallet import MiniWallet


class TrucNextBlockTest(BitcoinTestFramework):
    def add_options(self, parser):
        parser.add_argument('--rdts', action='store_true')

    def set_test_params(self):
        self.num_nodes = 1
        self.setup_clean_chain = True
        self.activation_args = ['-testactivationheight=blake2b@100', '-rdtsexpiry=2000000000'] if self.options.rdts else []
        self.extra_args = [['-persistmempool=1', '-mempooltruc=enforce', '-trucsiblingnextblock=1'] + self.activation_args]

    def run_test(self):
        node = self.nodes[0]
        wallet = MiniWallet(node)
        self.generate(wallet, 500)

        def restart(enabled=True, mode='enforce', core=1):
            self.restart_node(0, extra_args=['-persistmempool=1', f'-corepolicy={core}', f'-mempooltruc={mode}', f'-trucsiblingnextblock={int(enabled)}'] + self.activation_args)
            wallet.rescan_utxos()

        def child(utxo, fee, outputs=1, version=3):
            if outputs > 1:
                return wallet.create_self_transfer_multi(utxos_to_spend=[utxo], num_outputs=outputs,
                    fee_per_output=fee // outputs, version=version)
            return wallet.create_self_transfer(utxo_to_spend=utxo, version=version, fee=Decimal(fee)/100000000)

        def pair(parent_fee=2000):
            parent = wallet.create_self_transfer_multi(confirmed_only=True, num_outputs=2, version=3, fee_per_output=parent_fee//2)
            old = child(parent['new_utxos'][0], 10000, outputs=20)
            assert_equal(node.submitpackage([parent['hex'], old['hex']])['package_msg'], 'success')
            return parent, old

        def reject(tx):
            assert_raises_rpc_error(-26, 'insufficient fee', node.sendrawtransaction, tx['hex'])

        self.log.info('Empty mempool: old package is already next-block eligible; no exception')
        parent, old = pair()
        reject(child(parent['new_utxos'][1], 3000))
        self.generate(node, 1)
        wallet.rescan_utxos()

        self.log.info('Fill fee-based projection at 15 sat/vB, leaving less than a parent of space')
        # Real outputs avoid synthetic data-padding exceptions under native policy.
        # Fill 999,000 vB (4,000,000 weight minus 4,000 coinbase/header reserve).
        remaining = 199000 if self.options.rdts else 999000
        while remaining >= 104:
            outputs = min(230, (remaining - 61) // 43)
            tx = wallet.create_self_transfer_multi(confirmed_only=True, num_outputs=outputs, fee_per_output=700)
            size = tx['tx'].get_vsize()
            # Select an exact fee; rounding among outputs may add < outputs sats.
            tx = wallet.create_self_transfer_multi(utxos_to_spend=[wallet.get_utxo(confirmed_only=True)],
                num_outputs=outputs, fee_per_output=(size*15 + outputs-1)//outputs)
            node.sendrawtransaction(tx['hex'], 0)
            remaining -= tx['tx'].get_vsize()
        assert remaining < 104
        # Small competing transactions fill gaps after larger packages move in.
        for _ in range(100):
            wallet.send_self_transfer(from_node=node, confirmed_only=True, fee=Decimal(1560)/100000000)
        wallet.rescan_utxos()

        parent, old = pair()
        honest = child(parent['new_utxos'][1], 5000)
        self.log.info('Disabled feature preserves absolute fee requirement')
        restart(enabled=False)
        reject(honest)
        restart()
        self.log.info('Higher feerate but still outside projected block: rejected')
        reject(child(parent['new_utxos'][1], 1600))
        self.log.info('Direct conflict is never exempted')
        reject(child(parent['new_utxos'][0], 3000))
        self.log.info('Local fee delta cannot manufacture eligibility')
        node.prioritisetransaction(parent['txid'], 0, 1)
        reject(honest)
        node.prioritisetransaction(parent['txid'], 0, -1)
        self.log.info('Native policy: same-parent smaller sibling enters next block')
        restart(core=0)
        assert_equal(node.sendrawtransaction(honest['hex']), honest['txid'])
        assert old['txid'] not in node.getrawmempool()
        assert parent['txid'] in node.getrawmempool()
        self.log.info('No second cheap replacement once the parent is next-block eligible')
        # The existing 104-vB child cannot shrink further; this is also prevented
        # by the already-selected-package gate, independently tested below.
        reject(child(parent['new_utxos'][0], 2500))

        self.log.info('An already-selected high-fee parent cannot hide behind a low-fee child')
        parent2, old2 = pair(parent_fee=10000)
        reject(child(parent2['new_utxos'][1], 3000))

        self.log.info('Core policy: second independent eligible pair uses same exception')
        restart(core=1)
        parent3, old3 = pair()
        honest3 = child(parent3['new_utxos'][1], 5000)
        assert_equal(node.sendrawtransaction(honest3['hex']), honest3['txid'])
        assert old3['txid'] not in node.getrawmempool()

        self.log.info('Further shrinking with sufficient ratio still fails once package is selected')
        parent4, old4 = pair()
        first = child(parent4['new_utxos'][1], 8000, outputs=4)
        assert_equal(node.sendrawtransaction(first['hex']), first['txid'])
        second = child(parent4['new_utxos'][0], 7000)
        assert second['tx'].get_vsize() < first['tx'].get_vsize()
        reject(second)

        self.log.info('Ordinary sufficiently funded sibling eviction remains available')
        funded = child(parent4['new_utxos'][0], 12000)
        assert_equal(node.sendrawtransaction(funded['hex']), funded['txid'])

        self.log.info('Accept mode never gains TRUC sibling eviction from the experimental flag')
        restart(mode='accept')
        parent5 = wallet.send_self_transfer_multi(from_node=node, version=3, confirmed_only=True, num_outputs=2)
        a = child(parent5['new_utxos'][0], 10000, outputs=20)
        b = child(parent5['new_utxos'][1], 5000)
        node.sendrawtransaction(a['hex'])
        node.sendrawtransaction(b['hex'])
        assert a['txid'] in node.getrawmempool()
        assert b['txid'] in node.getrawmempool()


if __name__ == '__main__':
    TrucNextBlockTest(__file__).main()


// Copyright (c) 2016-2022 The Bitcoin Core developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or http://www.opensource.org/licenses/mit-license.php.

#include <policy/rbf.h>

#include <consensus/amount.h>
#include <consensus/consensus.h>
#include <kernel/mempool_entry.h>
#include <policy/feerate.h>
#include <policy/policy.h>
#include <policy/truc_policy.h>
#include <primitives/transaction.h>
#include <sync.h>
#include <tinyformat.h>
#include <txmempool.h>
#include <uint256.h>
#include <util/check.h>
#include <util/moneystr.h>
#include <util/rbf.h>

#include <limits>
#include <map>
#include <set>
#include <vector>

#include <compare>

RBFTransactionState IsRBFOptIn(const CTransaction& tx, const CTxMemPool& pool)
{
    AssertLockHeld(pool.cs);

    // First check the transaction itself.
    if (SignalsOptInRBF(tx)) {
        return RBFTransactionState::REPLACEABLE_BIP125;
    }

    // If this transaction is not in our mempool, then we can't be sure
    // we will know about all its inputs.
    if (!pool.exists(GenTxid::Txid(tx.GetHash()))) {
        return RBFTransactionState::UNKNOWN;
    }

    // If all the inputs have nSequence >= maxint-1, it still might be
    // signaled for RBF if any unconfirmed parents have signaled.
    const auto& entry{*Assert(pool.GetEntry(tx.GetHash()))};
    auto ancestors{pool.AssumeCalculateMemPoolAncestors(__func__, entry, CTxMemPool::Limits::NoLimits(),
                                                        /*fSearchForParents=*/false)};

    for (CTxMemPool::txiter it : ancestors) {
        if (SignalsOptInRBF(it->GetTx())) {
            return RBFTransactionState::REPLACEABLE_BIP125;
        }
    }
    return RBFTransactionState::FINAL;
}

RBFTransactionState IsRBFOptInEmptyMempool(const CTransaction& tx)
{
    // If we don't have a local mempool we can only check the transaction itself.
    return SignalsOptInRBF(tx) ? RBFTransactionState::REPLACEABLE_BIP125 : RBFTransactionState::UNKNOWN;
}

std::optional<std::string> GetEntriesForConflicts(const CTransaction& tx,
                                                  CTxMemPool& pool,
                                                  const CTxMemPool::setEntries& iters_conflicting,
                                                  CTxMemPool::setEntries& all_conflicts,
                                                  const ignore_rejects_type& ignore_rejects)
{
    AssertLockHeld(pool.cs);
    const uint256 txid = tx.GetHash();
    uint64_t nConflictingCount = 0;
    for (const auto& mi : iters_conflicting) {
        nConflictingCount += mi->GetCountWithDescendants();
        // Rule #5: don't consider replacing more than MAX_REPLACEMENT_CANDIDATES
        // entries from the mempool. This potentially overestimates the number of actual
        // descendants (i.e. if multiple conflicts share a descendant, it will be counted multiple
        // times), but we just want to be conservative to avoid doing too much work.
        if (nConflictingCount > MAX_REPLACEMENT_CANDIDATES && !ignore_rejects.count("too-many-replacements") && !ignore_rejects.count("too many potential replacements")) {
            return strprintf("rejecting replacement %s; too many potential replacements (%d > %d)",
                             txid.ToString(),
                             nConflictingCount,
                             MAX_REPLACEMENT_CANDIDATES);
        }
    }
    // Calculate the set of all transactions that would have to be evicted.
    for (CTxMemPool::txiter it : iters_conflicting) {
        pool.CalculateDescendants(it, all_conflicts);
    }
    return std::nullopt;
}

std::optional<std::string> HasNoNewUnconfirmed(const CTransaction& tx,
                                               const CTxMemPool& pool,
                                               const CTxMemPool::setEntries& iters_conflicting)
{
    AssertLockHeld(pool.cs);
    std::set<uint256> parents_of_conflicts;
    for (const auto& mi : iters_conflicting) {
        for (const CTxIn& txin : mi->GetTx().vin) {
            parents_of_conflicts.insert(txin.prevout.hash);
        }
    }

    for (unsigned int j = 0; j < tx.vin.size(); j++) {
        // Rule #2: We don't want to accept replacements that require low feerate junk to be
        // mined first.  Ideally we'd keep track of the ancestor feerates and make the decision
        // based on that, but for now requiring all new inputs to be confirmed works.
        //
        // Note that if you relax this to make RBF a little more useful, this may break the
        // CalculateMempoolAncestors RBF relaxation which subtracts the conflict count/size from the
        // descendant limit.
        if (!parents_of_conflicts.count(tx.vin[j].prevout.hash)) {
            // Rather than check the UTXO set - potentially expensive - it's cheaper to just check
            // if the new input refers to a tx that's in the mempool.
            if (pool.exists(GenTxid::Txid(tx.vin[j].prevout.hash))) {
                return strprintf("replacement %s adds unconfirmed input, idx %d",
                                 tx.GetHash().ToString(), j);
            }
        }
    }
    return std::nullopt;
}

std::optional<std::string> EntriesAndTxidsDisjoint(const CTxMemPool::setEntries& ancestors,
                                                   const std::map<Txid, bool>& direct_conflicts,
                                                   const uint256& txid, bool* const out_violates_policy)
{
    for (CTxMemPool::txiter ancestorIt : ancestors) {
        const Txid& hashAncestor = ancestorIt->GetTx().GetHash();
        const auto& conflictit = direct_conflicts.find(hashAncestor);
        if (conflictit != direct_conflicts.end()) {
            if (!conflictit->second /* mere SPK conflict, NOT invalid */) {
                if (out_violates_policy) *out_violates_policy = true;
                continue;
            }
            return strprintf("%s spends conflicting transaction %s",
                             txid.ToString(),
                             hashAncestor.ToString());
        }
    }
    return std::nullopt;
}

std::optional<std::string> PaysMoreThanConflicts(const CTxMemPool::setEntries& iters_conflicting,
                                                 CFeeRate replacement_feerate,
                                                 const uint256& txid)
{
    for (const auto& mi : iters_conflicting) {
        // Don't allow the replacement to reduce the feerate of the mempool.
        //
        // We usually don't want to accept replacements with lower feerates than what they replaced
        // as that would lower the feerate of the next block. Requiring that the feerate always be
        // increased is also an easy-to-reason about way to prevent DoS attacks via replacements.
        //
        // We only consider the feerates of transactions being directly replaced, not their indirect
        // descendants. While that does mean high feerate children are ignored when deciding whether
        // or not to replace, we do require the replacement to pay more overall fees too, mitigating
        // most cases.
        CFeeRate original_feerate(mi->GetModifiedFee(), mi->GetTxSize());
        if (replacement_feerate <= original_feerate) {
            return strprintf("rejecting replacement %s; new feerate %s <= old feerate %s",
                             txid.ToString(),
                             replacement_feerate.ToString(),
                             original_feerate.ToString());
        }
    }
    return std::nullopt;
}

std::optional<std::string> PaysForRBF(CAmount original_fees,
                                      CAmount replacement_fees,
                                      size_t replacement_vsize,
                                      CFeeRate relay_fee,
                                      const uint256& txid)
{
    // Rule #3: The replacement fees must be greater than or equal to fees of the
    // transactions it replaces, otherwise the bandwidth used by those conflicting transactions
    // would not be paid for.
    if (replacement_fees < original_fees) {
        return strprintf("rejecting replacement %s, less fees than conflicting txs; %s < %s",
                         txid.ToString(), FormatMoney(replacement_fees), FormatMoney(original_fees));
    }

    // Rule #4: The new transaction must pay for its own bandwidth. Otherwise, we have a DoS
    // vector where attackers can cause a transaction to be replaced (and relayed) repeatedly by
    // increasing the fee by tiny amounts.
    CAmount additional_fees = replacement_fees - original_fees;
    if (additional_fees < relay_fee.GetFee(replacement_vsize)) {
        return strprintf("rejecting replacement %s, not enough additional fees to relay; %s < %s",
                         txid.ToString(),
                         FormatMoney(additional_fees),
                         FormatMoney(relay_fee.GetFee(replacement_vsize)));
    }
    return std::nullopt;
}

std::optional<std::pair<DiagramCheckError, std::string>> ImprovesFeerateDiagram(CTxMemPool::ChangeSet& changeset)
{
    // Require that the replacement strictly improves the mempool's feerate diagram.
    const auto chunk_results{changeset.CalculateChunksForRBF()};

    if (!chunk_results.has_value()) {
        return std::make_pair(DiagramCheckError::UNCALCULABLE, util::ErrorString(chunk_results).original);
    }

    if (!std::is_gt(CompareChunks(chunk_results.value().second, chunk_results.value().first))) {
        return std::make_pair(DiagramCheckError::FAILURE, "insufficient feerate: does not improve feerate diagram");
    }
    return std::nullopt;
}

bool TrucSiblingNextBlock(const CTxMemPool& pool, const CTxMemPoolEntry& replacement,
                          const CTxMemPoolEntry& sibling, const CTxMemPoolEntry& parent,
                          int64_t block_weight_limit)
{
    AssertLockHeld(pool.cs);
    // Fail closed. These are prototype resource bounds, not new transaction limits.
    constexpr size_t MAX_ENTRIES{10000};
    constexpr size_t MAX_WORK{2000000};
    if (pool.size() > MAX_ENTRIES || block_weight_limit <= 4000) return false;
    if (replacement.GetTx().version != TRUC_VERSION || sibling.GetTx().version != TRUC_VERSION ||
        parent.GetTx().version != TRUC_VERSION || parent.GetCountWithAncestors() != 1 ||
        parent.GetCountWithDescendants() != 2 || sibling.GetCountWithAncestors() != 2 ||
        sibling.GetCountWithDescendants() != 1 || sibling.GetMemPoolParentsConst().size() != 1 ||
        &sibling.GetMemPoolParentsConst().begin()->get() != &parent ||
        replacement.GetTxSize() > TRUC_CHILD_MAX_VSIZE || sibling.GetTxSize() > TRUC_CHILD_MAX_VSIZE ||
        parent.GetTxSize() > TRUC_MAX_VSIZE) return false;

    // No operator fee deltas may manufacture eligibility. Other entries are also
    // evaluated at actual fees, rather than local prioritisetransaction deltas.
    for (const auto* entry : {&replacement, &sibling, &parent}) {
        if (entry->GetFee() != entry->GetModifiedFee() || !MoneyRange(entry->GetFee())) return false;
    }
    // A deliberately narrow first experiment: strictly shrinking children only.
    if (replacement.GetTxSize() >= sibling.GetTxSize()) return false;
    // Keep an independent fee floor. This is NOT proof that aggregate relay is paid for.
    const auto relay_floor = pool.m_opts.min_relay_feerate.GetFee(replacement.GetTxSize()) +
                             pool.m_opts.incremental_relay_feerate.GetFee(replacement.GetTxSize());
    if (replacement.GetFee() < relay_floor) return false;
    // At least 25% improvement at BOTH child and parent-package levels. FeeFrac
    // uses wide multiplication; multiplying valid fees by four/five is safe.
    if (FeeFrac{replacement.GetFee() * 4, replacement.GetTxSize()} <<
        FeeFrac{sibling.GetFee() * 5, sibling.GetTxSize()}) return false;
    if (FeeFrac{(replacement.GetFee() + parent.GetFee()) * 4, replacement.GetTxSize() + parent.GetTxSize()} <<
        FeeFrac{(sibling.GetFee() + parent.GetFee()) * 5, sibling.GetTxSize() + parent.GetTxSize()}) return false;

    struct Entry {
        const CTxMemPoolEntry* tx;
        std::vector<size_t> parents;
    };
    std::vector<Entry> entries;
    std::map<Txid, size_t> positions;
    for (const auto& entry : pool.mapTx) {
        positions.emplace(entry.GetTx().GetHash(), entries.size());
        entries.push_back({&entry, {}});
    }
    for (auto& entry : entries) {
        for (const auto& ancestor : entry.tx->GetMemPoolParentsConst()) {
            entry.parents.push_back(positions.at(ancestor.get().GetTx().GetHash()));
        }
    }
    const size_t parent_pos = positions.at(parent.GetTx().GetHash());
    const size_t sibling_pos = positions.at(sibling.GetTx().GetHash());
    const size_t replacement_pos = entries.size();
    entries.push_back({&replacement, {parent_pos}});

    size_t work{0};
    // An intentionally simple ancestor-aware projection for review. Recompute
    // packages after each selection; never use stale ancestor scores. Both views
    // share a work budget and run under the same mempool lock. Optimizing this
    // without changing its decisions is follow-up work before deployment.
    const auto project = [&](bool replacing) -> std::optional<std::set<size_t>> {
        std::set<size_t> selected;
        const size_t excluded = replacing ? sibling_pos : replacement_pos;
        int64_t weight_left{block_weight_limit - 4000};
        int64_t sigops_left{MAX_BLOCK_SIGOPS_COST - 400};
        while (true) {
            std::set<size_t> best;
            FeeFrac best_rate{0, 1};
            Txid best_id;
            int64_t best_weight{0}, best_sigops{0};
            for (size_t i = 0; i < entries.size(); ++i) {
                if (++work > MAX_WORK) return std::nullopt;
                if (i == excluded || selected.count(i)) continue;
                std::vector<size_t> pending{i};
                std::set<size_t> package;
                FeeFrac rate{0, 0};
                int64_t weight{0}, sigops{0};
                bool valid{true};
                while (!pending.empty()) {
                    if (++work > MAX_WORK) return std::nullopt;
                    const size_t j = pending.back();
                    pending.pop_back();
                    if (j == excluded) { valid = false; break; }
                    if (selected.count(j) || !package.insert(j).second) continue;
                    const auto& entry = entries[j];
                    if (!MoneyRange(entry.tx->GetFee())) return std::nullopt;
                    rate += FeeFrac{entry.tx->GetFee(), entry.tx->GetTxSize()};
                    weight += entry.tx->GetTxWeight() + entry.tx->GetExtraWeight();
                    sigops += entry.tx->GetSigOpCost();
                    pending.insert(pending.end(), entry.parents.begin(), entry.parents.end());
                }
                if (!valid || weight > weight_left || sigops > sigops_left ||
                    rate.fee < pool.m_opts.min_relay_feerate.GetFee(rate.size)) continue;
                const auto& id = entries[i].tx->GetTx().GetHash();
                const auto comparison = FeeRateCompare(rate, best_rate);
                if (best.empty() || comparison > 0 || (comparison == 0 && id < best_id)) {
                    best = std::move(package);
                    best_rate = rate;
                    best_id = id;
                    best_weight = weight;
                    best_sigops = sigops;
                }
            }
            if (best.empty()) break;
            selected.insert(best.begin(), best.end());
            weight_left -= best_weight;
            sigops_left -= best_sigops;
        }
        return selected;
    };
    const auto before = project(false);
    // Stronger than checking the old child's standalone feerate: neither old
    // package member may already be selected. In particular, a high-fee parent
    // must not be hidden by averaging it with a low-fee child.
    if (!before || before->count(sibling_pos) || before->count(parent_pos)) return false;
    const auto after = project(true);
    return after && after->count(replacement_pos) && after->count(parent_pos);
}

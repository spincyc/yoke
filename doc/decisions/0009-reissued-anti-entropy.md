# 0009 — Anti-entropy re-issues intents; rejected intents are evicted

Date: 2026-08-08. Status: accepted. Amends the anti-entropy design in
ADR 0003 / PROTOCOL.md.

The original design re-multicast cached intents verbatim (original
origin, seq, signature). The simulation suite found a livelock: replay
defense tracks the highest seq per origin, so once a consumer has seen
*any* later frame from an intent's author — for example an intent that
author sent to a different consumer — a verbatim copy of the earlier
intent is stale-dropped forever. The offline consumer can never
converge, and bystanders re-offer the intent endlessly; the mesh never
quiesces (`tests/test_simnet_scenarios.py` pins the reproduction).

Accepted fix: a relaying agent re-issues the cached intent **body** as
a fresh `int` frame under its own origin and seq. The `version` field
inside the body preserves authorship and the total order, so
convergence semantics are unchanged; the relayed frame is signed with
the same site PSK, which is the same trust domain as the original
(ADR 0005 has a single key). Suppression now observes peers' re-issues
through the ordinary intent path instead of a special byte-identical
duplicate case, which also removes that special case from the node.

Same root class, second instance: bystanders cached intents that their
consumer had *rejected* (self-pair, kind-mismatch, malformed producer
id), re-offering a doomed highest-version intent forever. Agents now
observe rejection acks on the multicast group and evict/pin the exact
rejected version per consumer.

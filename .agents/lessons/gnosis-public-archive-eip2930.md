---
name: gnosis-public-archive-eip2930
symptom: probe "rpc_N: failed (RpcResponseError)"; eth_call at old blocks returns "EIP-2930 is not enabled"
area: rpc
status: active
updated: 2026-07-21
---

## Symptom

`probe --persist` reports one endpoint `failed (RpcResponseError)` and exits nonzero. The
underlying error on a historical `eth_call` is:
`Code -32603 Internal error ... EIP-2930 is not enabled` (Nethermind
`IntrinsicGasCalculator.AccessListCost`), even for a call with no access list.

## Root cause

The Gnosis public archive `rpc-archive.gnosischain.com` (a Nethermind node) cannot execute
`eth_call`/EIP-1898-by-number at **pre-EIP-2930 blocks** (e.g. the WXDAI deploy block
11,173,937). Recent blocks, `eth_getBlockByNumber`, `eth_getCode`, batch, and finality all
work — only deep-historical state execution fails. So it is unusable as a deep-archive source
and fails the probe's strict earliest-token state read.

## Fix / correct pattern

Use a genuine archive node (a full-history erigon/reth, or an internal node) for historical
state. For the legacy pre-Multicall quorum you need **two independent** such endpoints —
the public archive does not qualify. For recent (post-Multicall) dates a single capable
endpoint is enough; the census probe marks the failing one unavailable and proceeds
([[anchor-genesis-archive-gating]] had to be fixed first for that path to work).

## How to avoid / detect

Probe every candidate endpoint at your oldest required block before relying on it (a plain
`eth_call` of `totalSupply()` at the earliest token deploy block is the quick check). Don't
assume "archive" in the hostname means deep `eth_call` works. Provider-group requirements:
[[provider-group-quorum]].

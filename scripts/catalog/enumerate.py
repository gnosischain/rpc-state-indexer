"""Enumerate Gnosis DEX pools + tokens from on-chain factory/vault events.

Build-time catalog generator. NOT part of the runtime service — it keeps the indexer's
catalog independent of dbt by reading canonical pool-creation events directly from each DEX's
factory/Vault. Emits YAML fragments for ``config/gnosis/{tokens,pools}.yaml``.

Usage (inside the jobs container, where the archive RPC is reachable):

    docker compose --profile jobs run --rm \
      -v "$(pwd)/scripts:/app/scripts:ro" -v "$(pwd)/src:/hostsrc:ro" \
      -e PYTHONPATH=/hostsrc --entrypoint python jobs \
      scripts/catalog/enumerate.py --sources uniswap_v3,swapr_v3,balancer_v2,balancer_v3 \
      --out /app/scripts/catalog/out

Add ``--from-block N --to-block M`` to bound the scan (defaults to full history). Addresses and
topics are confirmed on-chain (see .agents/memory/gnosis-dex-factories.md).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx
from eth_utils.crypto import keccak

sys.path.insert(0, "/hostsrc")
from rpc_state_indexer.evm.calldata import get_pool_token_info_calldata  # noqa: E402
from rpc_state_indexer.evm.decoding import decode_balancer_v3_pool_token_info  # noqa: E402

UNISWAP_V3_FACTORY = "0xe32f7dd7e3f098d518ff19a22d5f028e076489b1"
SWAPR_ALGEBRA_FACTORY = "0xa0864cca6e114013ab0e27cbd5b6f4c8947da766"
BALANCER_V2_VAULT = "0xba12222222228d8ba445958a75a0704d566bf2c8"
BALANCER_V3_VAULT = "0xba1333333333a1ba1108e8412f11850a5c319ba9"

T_UNI = "0x" + keccak(text="PoolCreated(address,address,uint24,int24,address)").hex()
T_ALGEBRA = "0x" + keccak(text="Pool(address,address,address)").hex()
T_BAL2_REG = "0x" + keccak(text="PoolRegistered(bytes32,address,uint8)").hex()
T_BAL2_TOK = "0x" + keccak(text="TokensRegistered(bytes32,address[],address[])").hex()

MAX_RANGE = 90_000  # rpc_2 caps getLogs at 100k blocks / 20k results
SYMBOL_SEL = bytes.fromhex("95d89b41")
DECIMALS_SEL = bytes.fromhex("313ce567")

def _url() -> str:
    return [u.strip() for u in os.environ["RPC_URLS"].split(",") if u.strip()][-1]


def rpc(method: str, params: list) -> object:
    r = httpx.post(_url(), json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                   timeout=90)
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"{method}: {body['error']}")
    return body["result"]


def _addr(word_hex: str) -> str:
    return "0x" + word_hex[-40:].lower()


def scan_logs(address: str, topic0: str, lo: int, hi: int):
    """Yield logs of one event over [lo, hi], chunking and splitting on the result cap."""
    start = lo
    while start <= hi:
        end = min(start + MAX_RANGE - 1, hi)
        try:
            res = rpc("eth_getLogs", [{"address": address, "topics": [topic0],
                                       "fromBlock": hex(start), "toBlock": hex(end)}])
        except RuntimeError as exc:
            if "max results" in str(exc) and end > start:
                mid = (start + end) // 2  # split the window and retry
                yield from scan_logs(address, topic0, start, mid)
                yield from scan_logs(address, topic0, mid + 1, hi)
                return
            raise
        yield from res
        start = end + 1


def find_deploy_block(address: str, hi: int) -> int:
    """Binary-search the first block where ``address`` has code."""
    lo, ans = 0, hi
    while lo <= hi:
        mid = (lo + hi) // 2
        has = rpc("eth_getCode", [address, hex(mid)]) not in ("0x", "0x0", "")
        if has:
            ans, hi = mid, mid - 1
        else:
            lo = mid + 1
    return ans


# --------------------------------------------------------------- per-source scans

def enum_uniswap(lo, hi, tokens, pools, first_seen):
    # PoolCreated(token0 idx, token1 idx, uint24 fee idx, int24 tickSpacing, address pool):
    # fee is topic3; data = [tickSpacing][pool]. Capturing both avoids a per-anchor read.
    for lg in scan_logs(UNISWAP_V3_FACTORY, T_UNI, lo, hi):
        t0, t1 = _addr(lg["topics"][1]), _addr(lg["topics"][2])
        fee = int(lg["topics"][3], 16)
        data = bytes.fromhex(lg["data"][2:])
        tick_spacing = int.from_bytes(data[0:32], "big", signed=True)
        pool = "0x" + data[32:64].hex()[-40:]
        blk = int(lg["blockNumber"], 16)
        pools.append({"address": pool, "name": "uniswap_v3", "pool_class": "uniswap_v3",
                      "deployment_block": blk, "tick_spacing": tick_spacing, "fee": fee,
                      "assets": [t0, t1]})
        for t in (t0, t1):
            tokens.add(t); first_seen[t] = min(first_seen.get(t, blk), blk)


def enum_swapr(lo, hi, tokens, pools, first_seen):
    # Algebra V1 uses a fixed tickSpacing of 60; the fee is dynamic (read from globalState),
    # so it is left unset here.
    for lg in scan_logs(SWAPR_ALGEBRA_FACTORY, T_ALGEBRA, lo, hi):
        t0, t1 = _addr(lg["topics"][1]), _addr(lg["topics"][2])
        pool = _addr(lg["data"][2:66])
        blk = int(lg["blockNumber"], 16)
        pools.append({"address": pool, "name": "swapr_v3", "pool_class": "swapr_v3_algebra",
                      "deployment_block": blk, "tick_spacing": 60, "assets": [t0, t1]})
        for t in (t0, t1):
            tokens.add(t); first_seen[t] = min(first_seen.get(t, blk), blk)


def enum_balancer_v2(lo, hi, tokens, pools, first_seen):
    reg, toks, blk_of = {}, {}, {}
    for lg in scan_logs(BALANCER_V2_VAULT, T_BAL2_REG, lo, hi):
        pid = lg["topics"][1]
        reg[pid] = _addr(lg["topics"][2]); blk_of[pid] = int(lg["blockNumber"], 16)
    for lg in scan_logs(BALANCER_V2_VAULT, T_BAL2_TOK, lo, hi):
        pid = lg["topics"][1]
        data = bytes.fromhex(lg["data"][2:])
        # data = offset(tokens), offset(managers), then tokens array [len][elems...]
        off = int.from_bytes(data[0:32], "big")
        n = int.from_bytes(data[off:off + 32], "big")
        toks[pid] = ["0x" + data[off + 32 + i * 32:off + 64 + i * 32].hex()[-40:] for i in range(n)]
    for pid, pool in reg.items():
        assets = [t for t in toks.get(pid, []) if t != pool]  # drop the pool's own BPT
        if not assets:
            continue
        blk = blk_of[pid]
        pools.append({"address": pool, "name": "balancer_v2", "pool_class": "balancer_v2",
                      "deployment_block": blk, "pool_id": pid.lower(), "assets": assets})
        for t in assets:
            tokens.add(t); first_seen[t] = min(first_seen.get(t, blk), blk)


def enum_balancer_v3(lo, hi, tokens, pools, first_seen, v3_topic, blk_tag):
    for lg in scan_logs(BALANCER_V3_VAULT, v3_topic, lo, hi):
        pool = _addr(lg["topics"][1])
        blk = int(lg["blockNumber"], 16)
        try:
            ret = rpc("eth_call", [{"to": BALANCER_V3_VAULT,
                                    "data": "0x" + get_pool_token_info_calldata(pool).hex()}, blk_tag])
            pairs = decode_balancer_v3_pool_token_info(bytes.fromhex(ret[2:]))
        except Exception:
            continue
        assets = [t for t, _ in pairs if t != pool]
        pools.append({"address": pool, "name": "balancer_v3", "pool_class": "balancer_v3",
                      "deployment_block": blk, "assets": assets})
        for t in assets:
            tokens.add(t); first_seen[t] = min(first_seen.get(t, blk), blk)


def resolve_v3_topic(hi: int) -> str:
    """Find the V3 PoolRegistered topic0: scan a low-activity window after vault deploy and pick
    the topic0 whose topic1 is a pool that getPoolTokenInfo accepts."""
    deploy = find_deploy_block(BALANCER_V3_VAULT, hi)
    blk_tag = hex(hi - 32)
    end = min(deploy + MAX_RANGE - 1, hi)
    res = rpc("eth_getLogs", [{"address": BALANCER_V3_VAULT,
                               "fromBlock": hex(deploy), "toBlock": hex(end)}])
    for lg in res:
        if len(lg["topics"]) < 2:
            continue
        cand = _addr(lg["topics"][1])
        try:
            ret = rpc("eth_call", [{"to": BALANCER_V3_VAULT,
                                    "data": "0x" + get_pool_token_info_calldata(cand).hex()}, blk_tag])
            decode_balancer_v3_pool_token_info(bytes.fromhex(ret[2:]))
            return lg["topics"][0]
        except Exception:
            continue
    raise RuntimeError("could not resolve Balancer V3 PoolRegistered topic0")


# --------------------------------------------------------------- token metadata

def decode_symbol(ret: str) -> str:
    raw = bytes.fromhex(ret[2:]) if ret.startswith("0x") else b""
    if len(raw) >= 64:  # dynamic string: [offset][len][data]
        length = int.from_bytes(raw[32:64], "big")
        if 0 < length <= 64 and 64 + length <= len(raw):
            try:
                return raw[64:64 + length].decode("utf-8").strip("\x00") or "?"
            except UnicodeDecodeError:
                pass
    if raw:  # bytes32 symbol
        try:
            return raw.rstrip(b"\x00").decode("utf-8") or "?"
        except UnicodeDecodeError:
            pass
    return "?"


def token_metadata(tokens, blk_tag):
    meta = {}
    for t in sorted(tokens):
        try:
            sym = decode_symbol(rpc("eth_call", [{"to": t, "data": "0x95d89b41"}, blk_tag]))
            dec_raw = rpc("eth_call", [{"to": t, "data": "0x313ce567"}, blk_tag])
            dec = int(dec_raw, 16) if dec_raw not in ("0x", "") else 18
        except Exception:
            sym, dec = "?", 18
        meta[t] = {"symbol": sym[:32] or "?", "decimals": min(max(dec, 0), 255)}
    return meta


# --------------------------------------------------------------- watermark

DEFAULT_WATERMARK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watermark.json")


def load_watermark(path: str) -> int | None:
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return int(json.load(fh)["head"])


def save_watermark(path: str, head: int) -> None:
    with open(path, "w") as fh:
        json.dump({"head": int(head)}, fh)
        fh.write("\n")


def scan_start(*, explicit_from: int | None, incremental: bool,
               watermark: int | None, overlap: int) -> int | None:
    """The low block for a source scan, or None to scan from the source's deploy block.

    Explicit --from-block wins. Incremental with a watermark rescans from
    ``watermark - overlap`` (a safety window for late-finalized blocks). Otherwise full
    history (None -> the caller substitutes find_deploy_block).
    """
    if explicit_from is not None:
        return explicit_from
    if incremental and watermark is not None:
        return max(0, watermark - overlap)
    return None


# --------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default="uniswap_v3,swapr_v3,balancer_v2,balancer_v3")
    ap.add_argument("--from-block", type=int, default=None)
    ap.add_argument("--to-block", type=int, default=None)
    ap.add_argument("--incremental", action="store_true",
                    help="Scan from watermark - overlap to head; no watermark -> full history.")
    ap.add_argument("--overlap", type=int, default=10_000,
                    help="Blocks to rescan below the watermark (late-finality safety window).")
    ap.add_argument("--watermark", default=DEFAULT_WATERMARK)
    ap.add_argument("--out", default="scripts/catalog/out")
    args = ap.parse_args()

    head = int(rpc("eth_blockNumber", []), 16)
    hi = args.to_block or head - 32
    sources = args.sources.split(",")
    blk_tag = hex(hi)
    watermark = load_watermark(args.watermark)
    start = scan_start(explicit_from=args.from_block, incremental=args.incremental,
                       watermark=watermark, overlap=args.overlap)
    if args.incremental:
        print(f"incremental: watermark={watermark} overlap={args.overlap} "
              f"start={start} head={hi}", flush=True)

    tokens: set[str] = set()
    pools: list[dict] = []
    first_seen: dict[str, int] = {}

    for src in sources:
        n0 = len(pools)
        if src == "uniswap_v3":
            enum_uniswap(start if start is not None else find_deploy_block(UNISWAP_V3_FACTORY, hi),
                         hi, tokens, pools, first_seen)
        elif src == "swapr_v3":
            enum_swapr(start if start is not None else find_deploy_block(SWAPR_ALGEBRA_FACTORY, hi),
                       hi, tokens, pools, first_seen)
        elif src == "balancer_v2":
            enum_balancer_v2(start if start is not None else find_deploy_block(BALANCER_V2_VAULT, hi),
                             hi, tokens, pools, first_seen)
        elif src == "balancer_v3":
            v3_topic = resolve_v3_topic(hi)
            enum_balancer_v3(start if start is not None else find_deploy_block(BALANCER_V3_VAULT, hi),
                             hi, tokens, pools, first_seen, v3_topic, blk_tag)
        else:
            raise SystemExit(f"unknown source {src}")
        print(f"  {src}: {len(pools) - n0} pools", flush=True)

    meta = token_metadata(tokens, blk_tag)
    os.makedirs(args.out, exist_ok=True)

    tok_rows = [{"address": t, "symbol": meta[t]["symbol"], "decimals": meta[t]["decimals"],
                 "token_class": "standard_erc20", "deployment_block": first_seen.get(t, 0),
                 "discovery_events": [{"abi": "erc20", "event": "Transfer", "holder_topics": [1, 2]}]}
                for t in sorted(tokens)]
    for p in pools:
        p["assets"] = [{"token": a} for a in p["assets"]]

    import yaml
    with open(os.path.join(args.out, "tokens.yaml"), "w") as fh:
        yaml.safe_dump({"tokens": tok_rows}, fh, sort_keys=False, default_flow_style=False)
    with open(os.path.join(args.out, "pools.yaml"), "w") as fh:
        yaml.safe_dump({"pools": pools}, fh, sort_keys=False, default_flow_style=False)
    # JSON alongside for quick diffing/inspection.
    with open(os.path.join(args.out, "tokens.json"), "w") as fh:
        json.dump({"tokens": tok_rows}, fh, indent=2)
    with open(os.path.join(args.out, "pools.json"), "w") as fh:
        json.dump({"pools": pools}, fh, indent=2)

    # Advance the watermark only when we scanned all the way to head (no explicit --to-block),
    # so the next --incremental run resumes from here. assemble.py then appends these pools.
    if args.to_block is None:
        save_watermark(args.watermark, hi)
        print(f"watermark -> {hi} ({args.watermark})")
    print(f"TOTAL: {len(pools)} pools, {len(tokens)} tokens -> {args.out}/{{tokens,pools}}.yaml")


if __name__ == "__main__":
    main()

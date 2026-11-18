"""Execute one capped Raydium swap using a disposable, faucet-funded Devnet keypair.

The generated keypair exists only in runner memory. It is never printed,
persisted, uploaded, or reused. Mainnet endpoints and user wallets are forbidden.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import time
from typing import Any

import requests
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

RPC = "https://api.devnet.solana.com"
GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"
TRADE = "https://transaction-v1-devnet.raydium.io"
SOL = "So11111111111111111111111111111111111111112"
DEV_USDC = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"
FAUCET_AIRDROP_LAMPORTS = 50_000_000
SWAP_INPUT_LAMPORTS = 1_000_000
MAX_PRICE_IMPACT_PCT = 2.0
ALLOWED_PROGRAMS = {
    "DRaybByLpbUL57LJARs3j8BitTxVfzBg351EaMr5UTCd",
    "DRaycpLY18LhpbydsBWbVJtxpNv9oXPgjRSfpF2bWpYb",
    "DRaya7Kj3aMWQSy19kSjvmuwq9docCHofyP9kanQGaav",
    "DRayAUgENGQBKVaX8owNhgzkEDyoHTGVEGHVJT1E9pfH",
    "DRayDdXc1NZQ9C3hRWmoSf8zK4iapgMnjdNZWrfwsP8m",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "11111111111111111111111111111111",
    "ComputeBudget111111111111111111111111111111",
    "SysvarRent111111111111111111111111111111111",
}


class EphemeralSwapError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EphemeralSwapEvidence:
    evidence_type: str
    status: str
    cluster: str
    genesis_hash: str
    ephemeral_wallet_address: str
    faucet_airdrop_lamports: int
    input_mint: str
    output_mint: str
    input_amount_lamports: int
    quoted_output_amount_raw: str
    price_impact_pct: float
    transaction_count: int
    transaction_signatures: list[str]
    confirmed_signatures: list[str]
    output_token_amount_raw: str | None
    private_key_exported: bool
    private_key_persisted: bool
    user_wallet_used: bool
    mainnet_touched: bool
    transaction_signed: bool
    transaction_submitted: bool
    transaction_broadcast: bool
    swap_executed: bool


class EphemeralDevnetSwap:
    def __init__(self, *, session: Any | None = None, sleep=time.sleep) -> None:
        self.session = session or requests.Session()
        self.sleep = sleep

    def rpc(self, method: str, params: list[Any] | None = None) -> Any:
        response = self.session.post(
            RPC,
            json={"jsonrpc":"2.0","id":1,"method":method,"params":params or []},
            timeout=20,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("error") is not None or "result" not in body:
            raise EphemeralSwapError(f"Devnet RPC {method} failed")
        return body["result"]

    def json_get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        r=self.session.get(url,params=params,timeout=20); r.raise_for_status()
        data=r.json()
        if not isinstance(data,dict): raise EphemeralSwapError("Raydium returned invalid JSON")
        return data

    def json_post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        r=self.session.post(url,json=body,timeout=20); r.raise_for_status()
        data=r.json()
        if not isinstance(data,dict): raise EphemeralSwapError("Raydium returned invalid JSON")
        return data

    def confirm(self, signature: str) -> None:
        for _ in range(30):
            result=self.rpc("getSignatureStatuses",[[signature],{"searchTransactionHistory":True}])
            values=result.get("value") if isinstance(result,dict) else None
            status=values[0] if isinstance(values,list) and values else None
            if status:
                if status.get("err") is not None:
                    raise EphemeralSwapError("Devnet transaction landed with an execution error")
                if status.get("confirmationStatus") in {"confirmed","finalized"}:
                    return
            self.sleep(2)
        raise EphemeralSwapError("Devnet transaction confirmation timed out")

    def run(self) -> EphemeralSwapEvidence:
        if self.rpc("getGenesisHash") != GENESIS:
            raise EphemeralSwapError("RPC genesis hash is not canonical Solana Devnet")
        kp=Keypair()
        wallet=str(kp.pubkey())
        # The disposable wallet is newly generated for this run; no user wallet or key is loaded.
        airdrop=self.rpc("requestAirdrop",[wallet,FAUCET_AIRDROP_LAMPORTS,{"commitment":"confirmed"}])
        if not isinstance(airdrop,str) or not airdrop:
            raise EphemeralSwapError("Devnet faucet did not return an airdrop signature")
        self.confirm(airdrop)
        balance=self.rpc("getBalance",[wallet,{"commitment":"confirmed"}])
        lamports=balance.get("value") if isinstance(balance,dict) else None
        if not isinstance(lamports,int) or lamports < SWAP_INPUT_LAMPORTS + 5_000_000:
            raise EphemeralSwapError("Faucet-funded disposable wallet lacks required swap and rent reserve")

        quote=self.json_get(f"{TRADE}/compute/swap-base-in",{
            "inputMint":SOL,"outputMint":DEV_USDC,"amount":str(SWAP_INPUT_LAMPORTS),
            "slippageBps":"100","txVersion":"V0",
        })
        q=quote.get("data") if quote.get("success") is True else None
        if not isinstance(q,dict) or q.get("inputMint")!=SOL or q.get("outputMint")!=DEV_USDC:
            raise EphemeralSwapError(f"No valid Devnet SOL/USDC quote: {quote.get('msg','no route')}")
        route=q.get("routePlan")
        if not isinstance(route,list) or not route:
            raise EphemeralSwapError("Quote has no liquidity route")
        impact=float(q.get("priceImpactPct",999))
        if impact > MAX_PRICE_IMPACT_PCT:
            raise EphemeralSwapError(f"Quote price impact {impact:.4f}% exceeds {MAX_PRICE_IMPACT_PCT:.2f}% safety ceiling")

        built=self.json_post(f"{TRADE}/transaction/swap-base-in",{
            "computeUnitPriceMicroLamports":"50000","swapResponse":quote,"txVersion":"V0",
            "wallet":wallet,"wrapSol":True,"unwrapSol":False,
        })
        tx_items=built.get("data") if built.get("success") is True else None
        if not isinstance(tx_items,list) or not tx_items or len(tx_items)>3:
            raise EphemeralSwapError(f"Raydium transaction build failed: {built.get('msg','invalid bundle')}")

        signatures=[]
        confirmed=[]
        for item in tx_items:
            encoded=item.get("transaction") if isinstance(item,dict) else None
            if not isinstance(encoded,str):
                raise EphemeralSwapError("Raydium returned a malformed transaction")
            raw=base64.b64decode(encoded,validate=True)
            unsigned=VersionedTransaction.from_bytes(raw)
            message=unsigned.message
            keys=list(message.account_keys)
            if not keys or str(keys[0])!=wallet:
                raise EphemeralSwapError("Transaction fee payer does not match disposable wallet")
            required=message.header.num_required_signatures
            if required != 1:
                raise EphemeralSwapError("Transaction has unexpected additional signers; refusing to sign")
            program_ids={str(keys[ix.program_id_index]) for ix in message.instructions}
            if not program_ids.issubset(ALLOWED_PROGRAMS):
                raise EphemeralSwapError("Transaction contains an unapproved program ID")
            signed=VersionedTransaction(message,[kp])
            if not all(signed.verify_with_results()):
                raise EphemeralSwapError("Locally generated transaction signature did not verify")
            send=self.rpc("sendTransaction",[
                base64.b64encode(bytes(signed)).decode("ascii"),
                {"encoding":"base64","skipPreflight":False,"preflightCommitment":"confirmed","maxRetries":3},
            ])
            if not isinstance(send,str) or not send:
                raise EphemeralSwapError("Devnet RPC did not return a transaction signature")
            signatures.append(send)
            self.confirm(send)
            confirmed.append(send)

        accounts=self.rpc("getTokenAccountsByOwner",[
            wallet,{"mint":DEV_USDC},{"encoding":"jsonParsed","commitment":"confirmed"},
        ])
        rows=accounts.get("value",[]) if isinstance(accounts,dict) else []
        amounts=[]
        for row in rows:
            try:
                amounts.append(int(row["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]))
            except (KeyError,TypeError,ValueError):
                continue
        output_raw=str(sum(amounts)) if amounts else None
        if output_raw is None or int(output_raw) <= 0:
            raise EphemeralSwapError("Transaction confirmed but no Devnet USDC balance was observed")
        return EphemeralSwapEvidence(
            evidence_type="raydium_devnet_ephemeral_swap",
            status="swap_confirmed",
            cluster="devnet",genesis_hash=GENESIS,ephemeral_wallet_address=wallet,
            faucet_airdrop_lamports=FAUCET_AIRDROP_LAMPORTS,input_mint=SOL,output_mint=DEV_USDC,
            input_amount_lamports=SWAP_INPUT_LAMPORTS,quoted_output_amount_raw=str(q.get("outputAmount")),
            price_impact_pct=impact,transaction_count=len(tx_items),transaction_signatures=signatures,
            confirmed_signatures=confirmed,output_token_amount_raw=output_raw,
            private_key_exported=False,private_key_persisted=False,user_wallet_used=False,
            mainnet_touched=False,transaction_signed=True,transaction_submitted=True,
            transaction_broadcast=True,swap_executed=True,
        )


def main() -> int:
    parser=argparse.ArgumentParser(description="Run one disposable-wallet Raydium Devnet test swap")
    parser.add_argument("--output",default="artifacts/raydium-devnet-ephemeral-swap.json")
    args=parser.parse_args()
    try:
        evidence=EphemeralDevnetSwap().run()
        payload=asdict(evidence); code=0
    except Exception as exc:
        payload={
            "evidence_type":"raydium_devnet_ephemeral_swap","status":"failed",
            "cluster":"devnet","error_type":type(exc).__name__,"error":str(exc),
            "private_key_exported":False,"private_key_persisted":False,
            "user_wallet_used":False,"mainnet_touched":False,
            "transaction_signed":False,"transaction_submitted":False,
            "transaction_broadcast":False,"swap_executed":False,
        }
        code=1
    path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(payload,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(payload,indent=2))
    return code


if __name__=="__main__":
    raise SystemExit(main())

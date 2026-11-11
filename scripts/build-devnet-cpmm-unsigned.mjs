import fs from "node:fs";
import { Connection, PublicKey, VersionedTransaction } from "@solana/web3.js";
import { Raydium, TxVersion, CurveCalculator, FeeOn } from "@raydium-io/raydium-sdk-v2";
import BN from "bn.js";

const RPC = "https://api.devnet.solana.com";
const GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG";
const WALLET = process.env.WALLET_ADDRESS || "FQtHmufHsELvyMHvQctu1kqZQ2zY8wDCJ4KpdxTQKb9U";
const POOL = "5QYRTeSXweZXLRcMocdJcDZ6HCUJCzPgK8HZYvjoMiwo";
const WSOL = "So11111111111111111111111111111111111111112";
const USDC = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU";
const INPUT = 1_000_000;
const OUT = process.env.OUTPUT || "artifacts/devnet-cpmm-unsigned.json";
const fail = message => { throw new Error("Devnet CPMM acquisition rejected: " + message); };

async function main() {
  if (process.env.CLUSTER && process.env.CLUSTER !== "devnet") fail("cluster override is not permitted");
  const connection = new Connection(RPC, "confirmed");
  if (await connection.getGenesisHash() !== GENESIS) fail("RPC genesis hash mismatch");
  const owner = new PublicKey(WALLET); // public-key-only owner; intentionally has no signing capability
  const raydium = await Raydium.load({
    owner, connection, cluster:"devnet", disableFeatureCheck:true, disableLoadToken:true,
    blockhashCommitment:"finalized",
    urlConfigs:{
      BASE_HOST:"https://api-v3-devnet.raydium.io",
      OWNER_BASE_HOST:"https://owner-v1-devnet.raydium.io",
      SWAP_HOST:"https://transaction-v1-devnet.raydium.io",
      CPMM_LOCK:"https://dynamic-ipfs-devnet.raydium.io/lock/cpmm/position"
    }
  });
  const pool = await raydium.cpmm.getPoolInfoFromRpc(POOL);
  const {poolInfo,poolKeys,rpcData}=pool;
  if (poolInfo.id !== POOL || poolInfo.programId !== "DRaycpLY18LhpbydsBWbVJtxpNv9oXPgjRSfpF2bWpYb") fail("RPC pool identity/program mismatch");
  const mintA=poolInfo.mintA.address, mintB=poolInfo.mintB.address;
  if (![mintA,mintB].includes(WSOL) || ![mintA,mintB].includes(USDC)) fail("pool mint pair is not WSOL/Devnet USDC");
  const baseIn = mintA === WSOL;
  const inputAmount = new BN(String(INPUT));
  const swapResult = CurveCalculator.swapBaseInput(
    inputAmount,
    baseIn ? rpcData.baseReserve : rpcData.quoteReserve,
    baseIn ? rpcData.quoteReserve : rpcData.baseReserve,
    rpcData.configInfo.tradeFeeRate,
    rpcData.configInfo.creatorFeeRate,
    rpcData.configInfo.protocolFeeRate,
    rpcData.configInfo.fundFeeRate,
    rpcData.feeOn === FeeOn.BothToken || rpcData.feeOn === FeeOn.OnlyTokenB
  );
  if (!swapResult.outputAmount || swapResult.outputAmount.lte(new BN(0))) fail("pool quote output is zero");
  const minOut = swapResult.outputAmount.mul(new BN(9900)).div(new BN(10000));
  if (minOut.lte(new BN(0))) fail("computed minimum output is zero");
  const built = await raydium.cpmm.swap({
    poolInfo,poolKeys,inputAmount,swapResult,slippage:0.01,baseIn,txVersion:TxVersion.V0,
    ownerInfo:{useSOLBalance:true}
  });
  const tx = built.transaction;
  if (!(tx instanceof VersionedTransaction) || tx.version !== 0) fail("SDK did not return one v0 transaction");
  if (tx.signatures.length !== 1 || tx.signatures.some(sig=>sig.some(byte=>byte!==0))) fail("SDK transaction is not unsigned with one empty wallet signature");
  const serialized=tx.serialize();
  const roundTrip=VersionedTransaction.deserialize(serialized);
  if (Buffer.compare(Buffer.from(serialized),Buffer.from(roundTrip.serialize()))!==0) fail("v0 serialization round-trip mismatch");
  const sim=await connection.simulateTransaction(tx,{sigVerify:false,replaceRecentBlockhash:true,commitment:"confirmed"});
  if (sim.value.err) fail("unsigned direct-CPMM simulation failed: "+JSON.stringify(sim.value.err));
  const message=tx.message;
  const keys=message.staticAccountKeys.map(k=>k.toBase58());
  const ixSummary=message.compiledInstructions.map(ix=>({
    programId:keys[ix.programIdIndex],
    accountIndexes:[...ix.accountKeyIndexes],
    dataBase58:Buffer.from(ix.data).toString("base64")
  }));
  if (!ixSummary.some(ix=>ix.programId==="DRaycpLY18LhpbydsBWbVJtxpNv9oXPgjRSfpF2bWpYb")) fail("built transaction has no direct CPMM instruction");
  if (ixSummary.some(ix=>ix.programId==="DRaybByLpbUL57LJARs3j8BitTxVfzBg351EaMr5UTCd")) fail("router instruction found in direct pool build");
  const evidence={
    evidence_type:"veltrix_direct_devnet_cpmm_unsigned_transaction",
    status:"simulation_succeeded",
    cluster:"devnet",genesis_hash:GENESIS,rpc_endpoint:RPC,
    wallet:WALLET,pool:POOL,program:poolInfo.programId,
    input_mint:WSOL,output_mint:USDC,input_amount_lamports:String(INPUT),
    quoted_output_raw:swapResult.outputAmount.toString(),minimum_output_raw:minOut.toString(),
    slippage_bps:100,transaction_version:0,transaction_count:1,
    unsigned_transaction_base64:Buffer.from(serialized).toString("base64"),
    static_account_keys:keys,instructions:ixSummary,
    address_lookup_tables:message.addressTableLookups.map(x=>({
      account_key:x.accountKey.toBase58(),writable_indexes:[...x.writableIndexes],readonly_indexes:[...x.readonlyIndexes]
    })),
    simulation:{err:sim.value.err,logs:(sim.value.logs||[]).slice(-80),units_consumed:sim.value.unitsConsumed??null},
    signed:false,submitted:false,broadcast:false
  };
  fs.mkdirSync("artifacts",{recursive:true});
  fs.writeFileSync(OUT,JSON.stringify(evidence,null,2)+"\n");
  process.stdout.write(JSON.stringify({status:evidence.status,pool:POOL,input:String(INPUT),
    quoted_output_raw:evidence.quoted_output_raw,minimum_output_raw:evidence.minimum_output_raw,
    instruction_programs:ixSummary.map(x=>x.programId),alt_count:evidence.address_lookup_tables.length,
    artifact:OUT,signed:false,submitted:false},null,2)+"\n");
}
main().catch(error=>{console.error(error.stack||String(error));process.exitCode=1;});

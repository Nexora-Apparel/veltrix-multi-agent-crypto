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
    ownerInfo:{wallet:new PublicKey(WALLET),useSOLBalance:true}
  });
  let tx = built.transaction;
  if (!(tx instanceof VersionedTransaction) || tx.version !== 0) fail("SDK did not return one v0 transaction");
  if (tx.signatures.length !== 1 || tx.signatures.some(sig=>sig.some(byte=>byte!==0))) fail("SDK transaction is not unsigned with one empty wallet signature");
  const minimumOut = swapResult.outputAmount.mul(new BN(9900)).div(new BN(10000));
  if (minimumOut.lte(new BN(0))) fail("calculated 1% slippage floor is zero");
  const directIx = tx.message.compiledInstructions.find(ix =>
    tx.message.staticAccountKeys[ix.programIdIndex]?.toBase58() === "DRaycpLY18LhpbydsBWbVJtxpNv9oXPgjRSfpF2bWpYb");
  if (!directIx || directIx.data.length !== 24 || Buffer.from(directIx.data.slice(0,8)).toString("hex") !== "8fbe5adac41e33de") {
    fail("SDK CPMM instruction discriminator/layout mismatch");
  }
  if (new BN(Buffer.from(directIx.data.slice(8,16)).reverse()).toString() !== String(INPUT)) fail("SDK CPMM input amount mismatch");
  const adjustedInstructions = tx.message.compiledInstructions.map(ix => {
    if (ix !== directIx) return ix;
    const data = new Uint8Array(ix.data);
    const raw = minimumOut.toArrayLike(Buffer,"le",8);
    data.set(raw,16);
    return {...ix,data};
  });
  const adjustedTx=VersionedTransaction.deserialize(tx.serialize());
  const directIndex=adjustedTx.message.compiledInstructions.findIndex(ix =>
    adjustedTx.message.staticAccountKeys[ix.programIdIndex]?.toBase58() === "DRaycpLY18LhpbydsBWbVJtxpNv9oXPgjRSfpF2bWpYb");
  if(directIndex<0) fail("direct CPMM instruction disappeared while applying slippage floor");
  adjustedTx.message.compiledInstructions[directIndex].data=adjustedInstructions.find(ix=>ix===directIx)?.data || adjustedInstructions[tx.message.compiledInstructions.indexOf(directIx)].data;
  tx=adjustedTx;
  const finalCpmmIx = tx.message.compiledInstructions.find(ix =>
    tx.message.staticAccountKeys[ix.programIdIndex]?.toBase58() === "DRaycpLY18LhpbydsBWbVJtxpNv9oXPgjRSfpF2bWpYb");
  if (new BN(Buffer.from(finalCpmmIx.data.slice(16,24)).reverse()).toString() !== minimumOut.toString()) {
    fail("final CPMM instruction does not carry the reviewed 1% slippage floor");
  }
  const serialized=tx.serialize();
  const roundTrip=VersionedTransaction.deserialize(serialized);
  if (Buffer.compare(Buffer.from(serialized),Buffer.from(roundTrip.serialize()))!==0) fail("v0 serialization round-trip mismatch");
  const sim=await connection.simulateTransaction(tx,{sigVerify:false,replaceRecentBlockhash:true,commitment:"confirmed",innerInstructions:true});
  if (sim.value.err) fail("unsigned direct-CPMM simulation failed: "+JSON.stringify(sim.value.err));
  const eventLine=(sim.value.logs||[]).find(line=>line.startsWith("Program data: "));
  if (!eventLine) fail("CPMM swap event missing from successful simulation");
  const event=Buffer.from(eventLine.slice("Program data: ".length),"base64");
  if (event.length < 72 || event.slice(0,8).toString("hex") !== "40c6cde8260871e2" ||
      new PublicKey(event.slice(8,40)).toBase58() !== POOL ||
      new BN(event.slice(56,64),"le").toString() !== String(INPUT)) {
    fail("CPMM swap event does not match the exact pool/input");
  }
  const actualOutput=new BN(event.slice(64,72),"le");
  if (actualOutput.lt(minimumOut)) fail("actual simulated output is below the final minimum output");
  const adversarialMinimum=actualOutput.add(new BN(1));
  const adversarialInstructions=tx.message.compiledInstructions.map(ix=>{
    if(tx.message.staticAccountKeys[ix.programIdIndex]?.toBase58()!=="DRaycpLY18LhpbydsBWbVJtxpNv9oXPgjRSfpF2bWpYb") return ix;
    const data=new Uint8Array(ix.data); data.set(adversarialMinimum.toArrayLike(Buffer,"le",8),16); return {...ix,data};
  });
  const adversarialTx=VersionedTransaction.deserialize(serialized);
  const adversarialIndex=adversarialTx.message.compiledInstructions.findIndex(ix =>
    adversarialTx.message.staticAccountKeys[ix.programIdIndex]?.toBase58() === "DRaycpLY18LhpbydsBWbVJtxpNv9oXPgjRSfpF2bWpYb");
  if(adversarialIndex<0) fail("direct CPMM instruction missing from adversarial clone");
  adversarialTx.message.compiledInstructions[adversarialIndex].data=adversarialInstructions[tx.message.compiledInstructions.findIndex(ix =>
    tx.message.staticAccountKeys[ix.programIdIndex]?.toBase58() === "DRaycpLY18LhpbydsBWbVJtxpNv9oXPgjRSfpF2bWpYb")].data;
  const negative=await connection.simulateTransaction(adversarialTx,{sigVerify:false,replaceRecentBlockhash:true,
    commitment:"confirmed",innerInstructions:false});
  if(!negative.value.err) fail("CPMM accepted a minimum output one raw unit above the observed output");
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
    quoted_output_raw:swapResult.outputAmount.toString(),minimum_output_raw:minimumOut.toString(),actual_output_raw:actualOutput.toString(),
    slippage_bps:100,transaction_version:0,transaction_count:1,
    unsigned_transaction_base64:Buffer.from(serialized).toString("base64"),
    static_account_keys:keys,instructions:ixSummary,
    address_lookup_tables:message.addressTableLookups.map(x=>({
      account_key:x.accountKey.toBase58(),writable_indexes:[...x.writableIndexes],readonly_indexes:[...x.readonlyIndexes]
    })),
    simulation:{err:sim.value.err,logs:(sim.value.logs||[]).slice(-80),inner_instructions:sim.value.innerInstructions??[],units_consumed:sim.value.unitsConsumed??null},
    adversarial:{minimum_output_raw:adversarialMinimum.toString(),rejected:!!negative.value.err,error:negative.value.err},
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

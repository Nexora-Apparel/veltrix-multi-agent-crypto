const RPC="https://api.devnet.solana.com";
const SIGNATURE="47Ztj6oCNgpUTiRaSzYKp8f2fn26UPhEfSVGfJR54YWwznn4U8HhTuCo6jDrqxhhZ3iPfqZYNsKd9UJK4d7WL62M";
const WALLET="FQtHmufHsELvyMHvQctu1kqZQ2zY8wDCJ4KpdxTQKb9U";
const USDC="4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU";
const GENESIS="EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG";
async function rpc(method,params){
 const res=await fetch(RPC,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({jsonrpc:"2.0",id:method,method,params})});
 if(!res.ok)throw new Error(method+" HTTP "+res.status);
 const body=await res.json();if(body.error)throw new Error(method+" RPC "+JSON.stringify(body.error));return body.result;
}
function balance(rows){return(rows||[]).filter(x=>x.mint===USDC&&x.owner===WALLET).reduce((n,x)=>n+BigInt(x.uiTokenAmount.amount),0n);}
async function main(){
 const genesis=await rpc("getGenesisHash",[]);if(genesis!==GENESIS)throw new Error("Noncanonical Devnet RPC");
 const statuses=await rpc("getSignatureStatuses",[[SIGNATURE],{searchTransactionHistory:true}]);
 const tx=await rpc("getTransaction",[SIGNATURE,{encoding:"json",commitment:"confirmed",maxSupportedTransactionVersion:0}]);
 if(!tx){console.log(JSON.stringify({result:"not_found_or_not_yet_available",signature:SIGNATURE,genesis,status:statuses?.value?.[0]||null,note:"Not evidence of success"},null,2));return;}
 const pre=balance(tx.meta?.preTokenBalances),post=balance(tx.meta?.postTokenBalances),delta=post-pre;
 const keys=tx.transaction?.message?.accountKeys||[],ix=tx.transaction?.message?.instructions||[];
 console.log(JSON.stringify({
  result:tx.meta?.err===null?"confirmed_transaction_no_execution_error":"transaction_failed",
  signature:SIGNATURE,genesis,slot:tx.slot,blockTime:tx.blockTime,
  confirmationStatus:statuses?.value?.[0]?.confirmationStatus||null,
  executionError:tx.meta?.err??null,feeLamports:tx.meta?.fee,
  wallet:WALLET,usdcMint:USDC,usdcPreRaw:pre.toString(),usdcPostRaw:post.toString(),usdcDeltaRaw:delta.toString(),
  accountKeys:keys.map(k=>typeof k==="string"?k:k.pubkey),
  topLevelProgramIds:ix.map(i=>{const k=keys[i.programIdIndex];return typeof i.programId==="string"?i.programId:(typeof k==="string"?k:k?.pubkey||null)}),
  logs:tx.meta?.logMessages||[],
  caution:"This reports chain execution and wallet token delta. Exact reviewed-message matching needs the original reviewed bytes."
 },null,2));
}
main().catch(e=>{console.error("READ_ONLY_RECONCILIATION_FAILED",e.stack||e);process.exitCode=1;});

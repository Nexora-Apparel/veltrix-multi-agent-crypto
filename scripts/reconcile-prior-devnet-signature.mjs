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
function decode58(value){
 const alphabet="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
 let n=0n;for(const c of value){const i=alphabet.indexOf(c);if(i<0)throw new Error("Invalid base58 instruction data");n=n*58n+BigInt(i);}
 let bytes=[];while(n>0n){bytes.unshift(Number(n&255n));n>>=8n;}
 for(let i=0;i<value.length&&value[i]==="1";i++)bytes.unshift(0);
 return Uint8Array.from(bytes);
}
function u64le(bytes,offset){let n=0n;for(let i=7;i>=0;i--)n=(n<<8n)|BigInt(bytes[offset+i]||0);return n.toString();}
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
  topLevelInstructions:ix.map(i=>{
   const k=keys[i.programIdIndex],programId=typeof i.programId==="string"?i.programId:(typeof k==="string"?k:k?.pubkey||null);
   const row={programId,dataBase58:i.data,accountIndexes:i.accounts||[]};
   if(programId==="DRaycpLY18LhpbydsBWbVJtxpNv9oXPgjRSfpF2bWpYb"){
    const data=decode58(i.data);row.dataHex=Buffer.from(data).toString("hex");
    row.swapInputLamports=data.length>=24?u64le(data,8):null;
    row.minimumOutputRaw=data.length>=24?u64le(data,16):null;
   }
   return row;
  }),
  logs:tx.meta?.logMessages||[],
  caution:"This reports chain execution and wallet token delta. Exact reviewed-message matching needs the original reviewed bytes."
 },null,2));
}
main().catch(e=>{console.error("READ_ONLY_RECONCILIATION_FAILED",e.stack||e);process.exitCode=1;});

const RPC="https://api.devnet.solana.com";
const SIGNATURE="ZkB6tUuBzSaeK74pZMig6gToTi4pkmBY1ymG3PHX9g2EBMr8GLWukqQNaeR1gmnuR9auQY7rCEy3eizfjBG8zdS";
const WALLET="FQtHmufHsELvyMHvQctu1kqZQ2zY8wDCJ4KpdxTQKb9U";
const USDC="4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU";
const GENESIS="EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG";
const COMPUTE_BUDGET="ComputeBudget111111111111111111111111111111";
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
function u32le(bytes,offset){if(offset+4>bytes.length)throw new Error("Truncated u32");return new DataView(bytes.buffer,bytes.byteOffset,bytes.byteLength).getUint32(offset,true);}
function decodeComputeBudget(data58){
 const data=decode58(data58),tag=data[0];
 if(tag===0&&data.length===9)return{tag,name:"RequestUnitsDeprecated",units:u32le(data,1),additionalFeeLamports:u32le(data,5)};
 if(tag===1&&data.length===5)return{tag,name:"RequestHeapFrame",bytes:u32le(data,1)};
 if(tag===2&&data.length===5)return{tag,name:"SetComputeUnitLimit",units:u32le(data,1)};
 if(tag===3&&data.length===9)return{tag,name:"SetComputeUnitPrice",microLamports:u64le(data,1)};
 if(tag===4&&data.length===5)return{tag,name:"SetLoadedAccountsDataSizeLimit",bytes:u32le(data,1)};
 return{tag:tag??null,name:"UnknownOrMalformed",dataLength:data.length,dataHex:Buffer.from(data).toString("hex")};
}
async function main(){
 const genesis=await rpc("getGenesisHash",[]);if(genesis!==GENESIS)throw new Error("Noncanonical Devnet RPC");
 const statuses=await rpc("getSignatureStatuses",[[SIGNATURE],{searchTransactionHistory:true}]);
 const tx=await rpc("getTransaction",[SIGNATURE,{encoding:"json",commitment:"confirmed",maxSupportedTransactionVersion:0}]);
 if(!tx){console.log(JSON.stringify({result:"not_found_or_not_yet_available",signature:SIGNATURE,genesis,status:statuses?.value?.[0]||null,note:"Not evidence of success"},null,2));return;}
 const pre=balance(tx.meta?.preTokenBalances),post=balance(tx.meta?.postTokenBalances),delta=post-pre;
 const keys=tx.transaction?.message?.accountKeys||[],ix=tx.transaction?.message?.instructions||[];
 const keyStrings=keys.map(k=>typeof k==="string"?k:k.pubkey);
 const loaded=tx.meta?.loadedAddresses||{writable:[],readonly:[]};
 const resolvedKeys=[...keyStrings,...(loaded.writable||[]),...(loaded.readonly||[])];
 const topLevelInstructions=ix.map((instruction,index)=>{
  const key=resolvedKeys[instruction.programIdIndex]??keys[instruction.programIdIndex];
  const programId=typeof instruction.programId==="string"?instruction.programId:(typeof key==="string"?key:key?.pubkey||null);
  const row={index,programId,dataBase58:instruction.data,accountIndexes:instruction.accounts||[]};
  if(programId===COMPUTE_BUDGET)row.computeBudget=decodeComputeBudget(instruction.data);
  if(programId==="DRaycpLY18LhpbydsBWbVJtxpNv9oXPgjRSfpF2bWpYb"){
   const data=decode58(instruction.data);row.dataHex=Buffer.from(data).toString("hex");
   row.swapInputLamports=data.length>=24?u64le(data,8):null;
   row.minimumOutputRaw=data.length>=24?u64le(data,16):null;
  }
  return row;
 });
 const computeBudgetInstructions=topLevelInstructions.filter(row=>row.programId===COMPUTE_BUDGET);
 console.log(JSON.stringify({
  result:tx.meta?.err===null?"confirmed_transaction_no_execution_error":"transaction_failed",
  signature:SIGNATURE,genesis,slot:tx.slot,blockTime:tx.blockTime,version:tx.version??"legacy",
  confirmationStatus:statuses?.value?.[0]?.confirmationStatus||null,
  executionError:tx.meta?.err??null,feeLamports:tx.meta?.fee,
  wallet:WALLET,usdcMint:USDC,usdcPreRaw:pre.toString(),usdcPostRaw:post.toString(),usdcDeltaRaw:delta.toString(),
  accountKeys:keyStrings,loadedAddresses:loaded,resolvedAccountKeys:resolvedKeys,
  computeBudgetInstructions,topLevelInstructions,
  logs:tx.meta?.logMessages||[],
  caution:"Read-only chain evidence. This does not establish exact reviewed-message equality because the original reviewed message bytes are not present in this artifact."
 },null,2));
}
main().catch(e=>{console.error("READ_ONLY_RECONCILIATION_FAILED",e.stack||e);process.exitCode=1;});

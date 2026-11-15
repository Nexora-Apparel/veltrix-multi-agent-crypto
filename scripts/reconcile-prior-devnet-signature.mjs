const RPC="https://api.devnet.solana.com";
const SIGNATURE="ZkB6tUuBzSaeK74pZMig6gToTi4pkmBY1ymG3PHX9g2EBMr8GLWukqQNaeR1gmnuR9auQY7rCEy3eizfjBG8zdS";
const WALLET="FQtHmufHsELvyMHvQctu1kqZQ2zY8wDCJ4KpdxTQKb9U";
const USDC="4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU";
const GENESIS="EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG";
const COMPUTE_BUDGET="ComputeBudget111111111111111111111111111111";
// Candidate CI artifact from the same 0.005 SOL / 10,631-minimum build. It is
// not asserted to be the user's exact reviewed bytes; it permits a diagnostic
// comparison until the browser's original reviewed-message evidence is recovered.
const CI_REFERENCE_UNSIGNED_BASE64="AQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAgP1iH37evKtt4GFe+4Xt+0FsGGoovmpAYUIgnc0DCWbvNBdqKMsUSWqm7TkM99HWfnA1A80wFXCZDsv84dxOYkLomdICj/VhkhRmJ2yl4rCGw/zYUNiNbSzwlU5Hmk79iExmI8tqLWRc1WCQUJl2BC8AWWcHt0a/tCcHhs8Jr0NFfUI+o9Us0A4e5v03Vy1ejYcLFRPblMrZLPyWIirNiWKOhjQD5QbfzExtWAt5R2pIpMBsVjAzh4HrIqCTs9F+xqBjMp9211RucEytooVXoDvZofrqLhnwwOefdT7oM3X5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADtELLORIVfxOpM9ATQoLQMrX/7NAaLb8bd5BgjfAC6nq1OrdyckyvC2oVDVXOLIzzAuUaGur8rFQoyadDPZ9yq4mJl5LcpSNHlv53RisDHfRj9f/q42fFwP+yRuHLfODM4VG678yTugj6GbB1vIj5K7B19l/pTrb86LcQbV4l5bBpuIV/6rgYT7aH9jRhjANdrEOdwa6ztVmKDwAAAAAAEGp9UXGSxcUSGMyUw9SvF/WNruCJuh/UTj29mKAAAAAAbd9uHXZaGT2cvhRs7reawctIXtX1s3kTqM9YV+/wCp/Qu7uxHRZ7SbWm9xeyjz2/2sBugdM+nAWOHveCQU96gEBwIABXwDAAAA1iH37evKtt4GFe+4Xt+0FsGGoovmpAYUIgnc0DCWbvMgAAAAAAAAADd4Q1lSb0tFM0RSbkJtWUp5UHR4ZDZpY05Iemh0VVZHeAFjAAAAAAClAAAAAAAAAAbd9uHXZaGT2cvhRs7reawctIXtX1s3kTqM9YV+/wCpDgQFDAANAQEKDQAJCwEFBgMEDg4MCAIYj75a2sQeM95AS0wAAAAAAIcpAAAAAAAADgMFAAABCQ==";
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
function readShortVec(bytes,state){
 let value=0,shift=0;
 for(let i=0;i<5;i++){if(state.offset>=bytes.length)throw new Error("Truncated shortvec");const b=bytes[state.offset++];value|=(b&127)<<shift;if(!(b&128))return value;shift+=7;}
 throw new Error("Invalid shortvec");
}
function extractMessage(wire){
 const state={offset:0},signatureCount=readShortVec(wire,state);
 const messageOffset=state.offset+signatureCount*64;
 if(messageOffset>=wire.length)throw new Error("Malformed transaction signature vector");
 return wire.subarray(messageOffset);
}
function summarizeLegacyMessage(message){
 const state={offset:0};
 if(message.length<3||(message[0]&128)!==0)throw new Error("Reference comparison only supports legacy messages");
 const header={requiredSignatures:message[state.offset++],readonlySigned:message[state.offset++],readonlyUnsigned:message[state.offset++]};
 const count=readShortVec(message,state),keys=[];
 for(let i=0;i<count;i++){if(state.offset+32>message.length)throw new Error("Truncated account keys");keys.push(Buffer.from(message.subarray(state.offset,state.offset+32)).toString("hex"));state.offset+=32;}
 if(state.offset+32>message.length)throw new Error("Truncated blockhash");
 const blockhash=Buffer.from(message.subarray(state.offset,state.offset+32)).toString("hex");state.offset+=32;
 const instructionCount=readShortVec(message,state),instructions=[];
 for(let i=0;i<instructionCount;i++){
  const programIdIndex=message[state.offset++],accountCount=readShortVec(message,state),accounts=Array.from(message.subarray(state.offset,state.offset+accountCount));state.offset+=accountCount;
  const dataLength=readShortVec(message,state),data=Buffer.from(message.subarray(state.offset,state.offset+dataLength)).toString("hex");state.offset+=dataLength;
  instructions.push({programIdIndex,accounts,data});
 }
 if(state.offset!==message.length)throw new Error("Trailing message bytes");
 return{length:message.length,header,accountKeyCount:count,accountKeys:keys,blockhash,instructionCount,instructions};
}
function firstDifference(a,b){
 const limit=Math.min(a.length,b.length);for(let i=0;i<limit;i++)if(a[i]!==b[i])return{offset:i,referenceByte:a[i],confirmedByte:b[i]};
 return a.length===b.length?null:{offset:limit,referenceByte:a[limit]??null,confirmedByte:b[limit]??null};
}
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
 const wireTx=await rpc("getTransaction",[SIGNATURE,{encoding:"base64",commitment:"confirmed",maxSupportedTransactionVersion:0}]);
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
 const confirmedWire=Buffer.from(wireTx.transaction?.[0]||"","base64");
 const confirmedMessage=extractMessage(confirmedWire);
 const ciReferenceMessage=extractMessage(Buffer.from(CI_REFERENCE_UNSIGNED_BASE64,"base64"));
 const confirmedSummary=summarizeLegacyMessage(confirmedMessage);
 const referenceSummary=summarizeLegacyMessage(ciReferenceMessage);
 console.log(JSON.stringify({
  result:tx.meta?.err===null?"confirmed_transaction_no_execution_error":"transaction_failed",
  signature:SIGNATURE,genesis,slot:tx.slot,blockTime:tx.blockTime,version:tx.version??"legacy",
  confirmationStatus:statuses?.value?.[0]?.confirmationStatus||null,
  executionError:tx.meta?.err??null,feeLamports:tx.meta?.fee,
  wallet:WALLET,usdcMint:USDC,usdcPreRaw:pre.toString(),usdcPostRaw:post.toString(),usdcDeltaRaw:delta.toString(),
  accountKeys:keyStrings,loadedAddresses:loaded,resolvedAccountKeys:resolvedKeys,
  computeBudgetInstructions,topLevelInstructions,
  messageComparison:{
   confirmedWireLength:confirmedWire.length,confirmedMessageLength:confirmedMessage.length,
   ciReferenceWireLength:Buffer.from(CI_REFERENCE_UNSIGNED_BASE64,"base64").length,
   ciReferenceMessageLength:ciReferenceMessage.length,
   firstByteDifferenceFromCiReference:firstDifference(ciReferenceMessage,confirmedMessage),
   referenceLabel:"CI artifact for same amount/minimum/quote build; not the user's original reviewed bytes",
   confirmedHeader:confirmedSummary.header,referenceHeader:referenceSummary.header,
   confirmedInstructionCount:confirmedSummary.instructionCount,referenceInstructionCount:referenceSummary.instructionCount,
   confirmedAccountKeyCount:confirmedSummary.accountKeyCount,referenceAccountKeyCount:referenceSummary.accountKeyCount
  },
  logs:tx.meta?.logMessages||[],
  caution:"Read-only chain evidence. This does not establish exact reviewed-message equality because the original reviewed message bytes are not present in this artifact."
 },null,2));
}
main().catch(e=>{console.error("READ_ONLY_RECONCILIATION_FAILED",e.stack||e);process.exitCode=1;});

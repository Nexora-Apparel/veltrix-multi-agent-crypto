import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { PublicKey, Transaction } from "@solana/web3.js";

const html=fs.readFileSync(new URL("../wallet/phantom-devnet-swap.html",import.meta.url),"utf8");
const script=html.split("<script>",2)[1].split("</script>",1)[0];
const artifactPath=process.env.CPMM_ARTIFACT||"artifacts/devnet-cpmm-unsigned.json";
const artifact=JSON.parse(fs.readFileSync(artifactPath,"utf8"));
assert.equal(artifact.status,"simulation_succeeded");
assert.equal(artifact.cluster,"devnet");
assert.equal(artifact.pool,"5QYRTeSXweZXLRcMocdJcDZ6HCUJCzPgK8HZYvjoMiwo");
assert.equal(artifact.transaction_version,"legacy");
assert.equal(artifact.address_lookup_tables.length,0);
assert.ok(artifact.serialized_size_bytes<=1232);
assert.equal(artifact.transaction_count,1);
assert.equal(artifact.signed,false);
assert.equal(artifact.submitted,false);
assert.equal(artifact.broadcast,false);

const bytes=Uint8Array.from(Buffer.from(artifact.unsigned_transaction_base64,"base64"));
const tx=Transaction.from(bytes);
assert.equal(tx.signatures.length,1);
assert.ok(tx.signatures.every(item=>item.signature===null));
assert.ok(tx.compileMessage().accountKeys.length<=32);
assert.equal(tx.instructions.length,5);

const nodes=new Map();
function node(id){
  if(!nodes.has(id)) nodes.set(id,{
    textContent:"",className:"",disabled:false,value:"0.001",
    classList:{add(){},remove(){}},addEventListener(){}
  });
  return nodes.get(id);
}
const window={__VELTRIX_TEST__:true,confirm:()=>false};
const context={
  window,document:{getElementById:node},console,solanaWeb3:(await import("@solana/web3.js")),
  fetch,URLSearchParams,TextEncoder,TextDecoder,Uint8Array,DataView,Set,Map,BigInt,Number,String,
  Array,Error,Math,JSON,Date,atob,btoa,Buffer
};
vm.runInNewContext(script,context);
const hooks=window.__VELTRIX_TEST_HOOKS__;
assert.ok(hooks,"browser verifier test hooks unavailable");
hooks.setWalletForTest(new PublicKey(artifact.wallet));
const parsedUsdcAccounts = {
  value:[{
    account:{
      owner:hooks.constants.TOKEN,
      data:{program:"spl-token",parsed:{type:"account",info:{
        mint:hooks.constants.USDC,owner:artifact.wallet,
        tokenAmount:{amount:"2460",decimals:6}
      }}}
    }
  }]
};
assert.equal(hooks.sumUsdcTokenAccounts(parsedUsdcAccounts,artifact.wallet),2460n);
assert.throws(()=>hooks.sumUsdcTokenAccounts({value:[{account:{data:{parsed:{info:{tokenAmount:{amount:"1"}}}}}}]},artifact.wallet),
  /unexpected owner, mint, program or amount/);
const reviewedMessage = Uint8Array.from([1,2,3,4]);
const matchingConfirmedTx = {
  meta:{err:null},
  transaction:{
    signatures:["test-signature"],
    message:{serialize:()=>Uint8Array.from(reviewedMessage)}
  }
};
assert.equal(hooks.assertConfirmedTransactionMatchesReviewedMessage(matchingConfirmedTx,reviewedMessage,"test-signature"),true);
assert.throws(()=>hooks.assertConfirmedTransactionMatchesReviewedMessage({
  meta:{err:null},transaction:{signatures:["test-signature"],message:{serialize:()=>Uint8Array.from([1,2,3,5])}}
},reviewedMessage,"test-signature"),/differs from the immutable reviewed message/);
assert.throws(()=>hooks.assertConfirmedTransactionMatchesReviewedMessage(matchingConfirmedTx,reviewedMessage,"other-signature"),
  /does not contain the returned Phantom signature/);
const noAtaCpis={createAccountCount:0,sizeQueryCount:0,immutableOwnerCount:0,initializeAccountCount:0,createAccountLamports:null};
const existingAtaRent=hooks.validateOutputAtaCpiEvidence({outputAtaPresent:true},0n,noAtaCpis);
assert.equal(existingAtaRent.outputAtaPresent,true);
assert.equal(existingAtaRent.persistentAtaRentLamports,"0");

const exactRent=1488440n;
const createdAtaCpis={createAccountCount:1,sizeQueryCount:1,immutableOwnerCount:1,
  initializeAccountCount:1,createAccountLamports:exactRent.toString()};
const newAtaRent=hooks.validateOutputAtaCpiEvidence({outputAtaPresent:false},exactRent,createdAtaCpis);
assert.equal(newAtaRent.outputAtaPresent,false);
assert.equal(newAtaRent.persistentAtaRentLamports,exactRent.toString());
assert.throws(()=>hooks.validateOutputAtaCpiEvidence({outputAtaPresent:true},0n,createdAtaCpis),
  /existing USDC ATA must not incur rent or be recreated/);
assert.throws(()=>hooks.validateOutputAtaCpiEvidence({outputAtaPresent:false},exactRent+1n,createdAtaCpis),
  /funded with exact live rent/);

const audit=await hooks.inspectDirectCpmmTransaction(tx,{
  outputAmount:artifact.quoted_output_raw,
  otherAmountThreshold:artifact.minimum_output_raw
},bytes);
assert.equal(audit.directCpmm,"verified");
assert.equal(audit.temporaryWsolFundingLamports,"2488440");
assert.ok(audit.resolvedAccountCount<=32);
assert.equal(audit.lookupTables.length,0);
assert.equal(audit.runtime.adversarialRejected,true);
assert.equal(audit.runtime.actualOutput,artifact.actual_output_raw);
const output={evidence_type:"veltrix_browser_direct_cpmm_live_audit",status:"passed",
  wallet:artifact.wallet,pool:artifact.pool,input_amount_lamports:artifact.input_amount_lamports,
  minimum_output_raw:artifact.minimum_output_raw,actual_output_raw:audit.runtime.actualOutput,
  temporary_wsol_funding_lamports:audit.temporaryWsolFundingLamports,
  output_ata_present:audit.runtime.outputAtaPresent,
  persistent_ata_rent_lamports:audit.runtime.persistentAtaRentLamports,
  adversarial_minimum_raw:audit.runtime.adversarialMinimum,
  adversarial_rejected:audit.runtime.adversarialRejected,
  resolved_account_count:audit.resolvedAccountCount,lookup_tables:audit.lookupTables,
  privilege_map:audit.privileges,inner_cpi_count:audit.runtime.innerCpiCount,
  transfer_proofs:audit.runtime.transferProofs,finalized_slot:audit.slot,
  exact_transaction_sha256:await (async()=>{const {createHash}=await import("node:crypto");return createHash("sha256").update(bytes).digest("hex")})(),
  signed:false,submitted:false,broadcast:false};
fs.writeFileSync("artifacts/browser-direct-cpmm-audit.json",JSON.stringify(output,null,2)+"\n");
console.log(JSON.stringify({status:output.status,actual_output_raw:output.actual_output_raw,
  minimum_output_raw:output.minimum_output_raw,adversarial_rejected:output.adversarial_rejected,
  resolved_account_count:output.resolved_account_count,inner_cpi_count:output.inner_cpi_count,
  transfer_proofs:output.transfer_proofs,exact_transaction_sha256:output.exact_transaction_sha256},null,2));

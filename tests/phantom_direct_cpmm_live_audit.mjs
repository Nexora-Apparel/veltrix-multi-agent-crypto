import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { PublicKey, VersionedTransaction } from "@solana/web3.js";

const html=fs.readFileSync(new URL("../wallet/phantom-devnet-swap.html",import.meta.url),"utf8");
const script=html.split("<script>",2)[1].split("</script>",1)[0];
const artifactPath=process.env.CPMM_ARTIFACT||"artifacts/devnet-cpmm-unsigned.json";
const artifact=JSON.parse(fs.readFileSync(artifactPath,"utf8"));
assert.equal(artifact.status,"simulation_succeeded");
assert.equal(artifact.cluster,"devnet");
assert.equal(artifact.pool,"5QYRTeSXweZXLRcMocdJcDZ6HCUJCzPgK8HZYvjoMiwo");
assert.equal(artifact.transaction_version,0);
assert.equal(artifact.transaction_count,1);
assert.equal(artifact.signed,false);
assert.equal(artifact.submitted,false);
assert.equal(artifact.broadcast,false);

const bytes=Uint8Array.from(Buffer.from(artifact.unsigned_transaction_base64,"base64"));
const tx=VersionedTransaction.deserialize(bytes);
assert.equal(tx.version,0);
assert.equal(tx.signatures.length,1);
assert.ok(tx.signatures.every(sig=>sig.every(byte=>byte===0)));

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
const audit=await hooks.inspectDirectCpmmTransaction(tx,{
  outputAmount:artifact.quoted_output_raw,
  otherAmountThreshold:artifact.minimum_output_raw
},bytes);
assert.equal(audit.directCpmm,"verified");
assert.equal(audit.resolvedAccountCount,16);
assert.equal(audit.runtime.adversarialRejected,true);
assert.equal(audit.runtime.actualOutput,artifact.actual_output_raw);
const output={evidence_type:"veltrix_browser_direct_cpmm_live_audit",status:"passed",
  wallet:artifact.wallet,pool:artifact.pool,input_amount_lamports:artifact.input_amount_lamports,
  minimum_output_raw:artifact.minimum_output_raw,actual_output_raw:audit.runtime.actualOutput,
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

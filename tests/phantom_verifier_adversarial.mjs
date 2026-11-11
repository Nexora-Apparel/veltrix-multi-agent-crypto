import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const html = fs.readFileSync(new URL("../wallet/phantom-devnet-swap.html", import.meta.url), "utf8");
const script = html.split("<script>", 2)[1].split("</script>", 1)[0];
const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) nodes.set(id, {
    textContent:"",className:"",disabled:false,value:"0.001",
    classList:{add(){},remove(){}},addEventListener(){},
  });
  return nodes.get(id);
}
const window = {__VELTRIX_TEST__:true,confirm:()=>false};
const context = {
  window,document:{getElementById:node},console,Uint8Array,Array,BigInt,Number,String,
  TextEncoder,DataView,Set,Map,Error,Math,atob, btoa,
  solanaWeb3:{Connection:class { constructor(){} }},
};
vm.runInNewContext(script,context);
const h = window.__VELTRIX_TEST_HOOKS__;
assert.ok(h,"guarded browser verifier hooks are exposed in test mode");
const C = h.constants;
const wallet = {toBase58:()=>C.WALLET};
h.setWalletForTest(wallet);

function u64(value) {
  let n=BigInt(value); const out=new Uint8Array(8);
  for(let i=0;i<8;i++){out[i]=Number(n&255n);n>>=8n;}
  return out;
}
function ix(programIdIndex, accountKeyIndexes, data) {
  return {programIdIndex,accountKeyIndexes,data:Uint8Array.from(data)};
}
function makeMessage() {
  const keys=[C.WALLET,"input-ata",C.SOL,"output-ata",C.USDC,"authority",C.POOL,
    "vault-in","vault-out","observation",C.ROUTER,C.TOKEN,C.TOKEN_2022,C.ATA_PROGRAM,
    C.SYSTEM,C.COMPUTE_BUDGET,"amm-config",C.CPMM];
  const setup=[5,...u64(C.TEST_LAMPORTS)];
  const route=[0,...u64(C.TEST_LAMPORTS),...u64(3110)];
  const cleanup=[6], price=[3,...u64(50000)], limit=[2,0x40,0x0d,0x03,0];
  const resolved=keys.map(value=>({toBase58:()=>value,equals(other){return this.toBase58()===other.toBase58();}}));
  const message={
    header:{numRequiredSignatures:1,numReadonlySignedAccounts:0,numReadonlyUnsignedAccounts:6},
    staticAccountKeys:resolved.slice(0,16),
    addressTableLookups:[{writableIndexes:[0],readonlyIndexes:[1]}],
    compiledInstructions:[
      ix(10,[0,1,2,11,13,14],setup),
      ix(10,[11,12,13,14,0,1,3,17,3,2,4,5,16,6,7,8,9],route),
      ix(10,[0,1,0,11,13,14],cleanup),
      ix(15,[],price),ix(15,[],limit),
    ],
  };
  return {message,resolved,keys};
}
function rejected(fn, label) {
  assert.throws(fn,/Transaction intent rejected:/,label);
}

// Primitive and ALT adversarial cases.
const raw=Uint8Array.from([0,1,2,3,4,5,6,7,8]);
assert.equal(h.readU64(raw,1),0x0807060504030201n);
const sample=Uint8Array.from([0,0,1,2,3,255]);
assert.deepEqual([...h.b58decode(h.b58encode(sample))],[...sample]);
const goodState={deactivationSlot:(1n<<64n)-1n,lastExtendedSlot:10n,lastExtendedSlotStartIndex:2,addresses:["A","B","C"]};
const goodSelection=h.validateLookupSelection({writableIndexes:[0],readonlyIndexes:[2]},goodState,11);
assert.deepEqual(goodSelection.writable,["A"]);
assert.deepEqual(goodSelection.readonly,["C"]);
rejected(()=>h.validateLookupSelection({writableIndexes:[0],readonlyIndexes:[]},{...goodState,deactivationSlot:9n},11),"deactivated ALT");
rejected(()=>h.validateLookupSelection({writableIndexes:[0],readonlyIndexes:[]},{...goodState,lastExtendedSlot:12n},11),"future ALT extension");
rejected(()=>h.validateLookupSelection({writableIndexes:[3],readonlyIndexes:[]},goodState,11),"out-of-range ALT index");
rejected(()=>h.validateLookupSelection({writableIndexes:[2],readonlyIndexes:[]},goodState,10),"same-slot ALT extension");

// Baseline router transaction passes the structural policy.
const base=makeMessage();
const baseline=h.decodeRouterPolicy(base.message,base.resolved,{otherAmountThreshold:"3110"});
assert.equal(baseline.minOut,3110n);
assert.equal(baseline.programs.length,5);

// Each altered intent must be rejected.
{
  const x=makeMessage(); x.message.compiledInstructions[1].data[1]++;
  rejected(()=>h.decodeRouterPolicy(x.message,x.resolved,{otherAmountThreshold:"3110"}),"altered input amount");
}
{
  const x=makeMessage(); x.message.compiledInstructions[1].data.set(u64(3109),9);
  rejected(()=>h.decodeRouterPolicy(x.message,x.resolved,{otherAmountThreshold:"3110"}),"reduced minimum output");
}
{
  const x=makeMessage(); x.message.compiledInstructions[0].accountKeyIndexes.reverse();
  rejected(()=>h.decodeRouterPolicy(x.message,x.resolved,{otherAmountThreshold:"3110"}),"altered setup recipient/account order");
}
{
  const x=makeMessage(); x.message.compiledInstructions[1].accountKeyIndexes[15]=6;
  rejected(()=>h.decodeRouterPolicy(x.message,x.resolved,{otherAmountThreshold:"3110"}),"altered route vault account");
}
{
  const x=makeMessage(); x.message.compiledInstructions[2].data[0]=5;
  rejected(()=>h.decodeRouterPolicy(x.message,x.resolved,{otherAmountThreshold:"3110"}),"altered cleanup tag");
}
{
  const x=makeMessage(); x.message.compiledInstructions[0].programIdIndex=11;
  rejected(()=>h.decodeRouterPolicy(x.message,x.resolved,{otherAmountThreshold:"3110"}),"unknown setup program");
}
{
  const x=makeMessage(); x.message.header.numReadonlyUnsignedAccounts=5;
  rejected(()=>h.decodeRouterPolicy(x.message,x.resolved,{otherAmountThreshold:"3110"}),"router signer/writable privilege escalation");
}
{
  const x=makeMessage(); x.message.compiledInstructions[3].data.set(u64(1000001),1);
  rejected(()=>h.decodeRouterPolicy(x.message,x.resolved,{otherAmountThreshold:"3110"}),"excessive compute-unit price");
}
{
  const x=makeMessage(); x.message.compiledInstructions[4].data.set([0xff,0xff,0xff,0x7f],1);
  rejected(()=>h.decodeRouterPolicy(x.message,x.resolved,{otherAmountThreshold:"3110"}),"excessive compute-unit limit");
}
console.log("Phantom browser verifier adversarial tests passed: ALT lifecycle/index mutations, byte primitives, router setup/route/cleanup, amount/minimum, account order, program identity, privilege escalation and compute-budget caps.");

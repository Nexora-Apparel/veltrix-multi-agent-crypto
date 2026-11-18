(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const tokenKey = "veltrix_dashboard_api_token";
  const money = (value) => new Intl.NumberFormat(undefined,{style:"currency",currency:"USD",maximumFractionDigits:2}).format(Number(value||0));
  const signed = (value) => (Number(value)>0?"+":"")+money(value);
  const number = (value,digits=2) => Number(value||0).toLocaleString(undefined,{maximumFractionDigits:digits});
  const date = (value) => value ? new Date(value).toLocaleString([], {month:"short",day:"numeric",hour:"numeric",minute:"2-digit"}) : "—";
  const age = (value) => { if(!value) return "time unknown"; const stamp=new Date(value).getTime(); if(!Number.isFinite(stamp)) return "time invalid"; const delta=Date.now()-stamp; if(delta < -60000) return "clock ahead"; if(delta < 60000) return "just now"; const minutes=Math.floor(delta/60000); if(minutes<60) return minutes+"m ago"; const hours=Math.floor(minutes/60); if(hours<48) return hours+"h ago"; return Math.floor(hours/24)+"d ago"; };
  const short = (value) => value && value.length>18 ? value.slice(0,7)+"…"+value.slice(-5) : (value||"—");
  let refreshing=false, chartPoints=[], allTrades=[], allPositions=[], allConsensus=[], allMarkets=[], allChainMarkets={}, runtimeSnapshot=null, currentRange="ALL";

  async function api(path){
    const token=sessionStorage.getItem(tokenKey)||"";
    const response=await fetch(path,{headers:token?{Authorization:"Bearer "+token}:{},cache:"no-store"});
    if(response.status===401){ sessionStorage.removeItem(tokenKey); $("access-gate").hidden=false; $("access-error").textContent="Token expired or was revoked. Enter the current dashboard token."; throw new Error("Dashboard access token expired or was revoked."); }
    if(!response.ok) throw new Error("Dashboard request failed ("+response.status+").");
    return response.json();
  }
  function element(tag,className,text){
    const node=document.createElement(tag);
    if(className) node.className=className;
    if(text!==undefined) node.textContent=text;
    return node;
  }
  function recordRow(title,detail,value,valueDetail,positive){
    const row=element("article","record-row");
    const main=element("div","record-main");
    main.append(element("b","",title),element("small","",detail));
    const amount=element("div","record-value"+(positive===true?" good":positive===false?" bad":""),value);
    if(valueDetail) amount.append(element("small","",valueDetail));
    row.append(main,amount);
    return row;
  }
  function renderClose(item){
    const row=element("article","close-row");
    const market=allMarkets.find(m=>m.mint===item.token)||{};
    const symbol=item.symbol||market.symbol;
    const avatar=element("div","token-avatar",(symbol||item.token||"?").replace(/[^a-z0-9]/gi,"").slice(0,4).toUpperCase()||"?");
    const main=element("div","close-main");
    main.append(element("b","",symbol?"$"+symbol.replace(/^\$/,""):short(item.token)),element("small","", "Closed · "+date(item.created_at_iso)));
    const profit=element("div","close-profit",signed(item.realized_pnl));
    const pct=Number(item.realized_pnl_pct);
    if(Number.isFinite(pct)&&item.realized_pnl_pct!==undefined) profit.append(element("small","",(pct>0?"+":"")+number(pct)+"%"));
    row.append(avatar,main,profit);
    return row;
  }
  function renderHome(overview){
    const a=overview.account||{}, p=overview.performance||{};
    $("equity").textContent=money(a.equity_usd);
    $("return-pill").textContent=(Number(a.total_return_pct)>=0?"▲ ":"▼ ")+signed(Number(a.equity_usd||0)-Number(a.starting_balance_usd||0))+" · "+number(a.total_return_pct)+"%";
    $("trade-count").textContent=number(p.exit_fills,0);
    $("win-rate").textContent=number(p.win_rate_pct,0)+"%";
    const closes=allTrades.filter(t=>t.side==="SELL");
    const best=closes.length?closes.reduce((max,t)=>Math.max(max,Number(t.realized_pnl||0)),-Infinity):0;
    $("best-close").textContent=closes.length?signed(best):"—";
    $("best-close").className=best>=0?"positive":"negative";
    const recent=$("recent-closes");recent.replaceChildren();
    if(!closes.length) recent.append(element("div","empty-state","No settled paper closes yet."));
    else closes.slice(0,5).forEach(t=>recent.append(renderClose(t)));
    const latestPosition=allPositions[0];
    const latestMarket=latestPosition ? (allMarkets.find(m=>m.mint===latestPosition.token)||{}) : {};
    const latestSymbol=latestPosition && (latestPosition.symbol||latestMarket.symbol);
    $("activity-line").hidden=!latestPosition;
    if(latestPosition) $("latest-activity").textContent="Bought "+(latestSymbol?"$"+latestSymbol:short(latestPosition.token))+" · open paper position";
    $("wallet-equity").textContent=money(a.equity_usd);
    $("wallet-return").textContent=number(a.total_return_pct)+"% total return";
    $("cash").textContent=money(a.cash_usd);
    $("realized").textContent=signed(a.realized_pnl_usd);
    $("wallet-realized").textContent=signed(a.realized_pnl_usd);
    $("unrealized").textContent=signed(a.unrealized_pnl_usd);
    $("exposure").textContent=money(a.portfolio_exposure_usd);
    $("open-count").textContent=number(a.open_positions,0);
    $("wallet-open-count").textContent=number(a.open_positions,0);
  }
  function renderPositions(){
    const host=$("open-positions");host.replaceChildren();
    if(!allPositions.length){host.append(element("div","empty-state","No open paper positions."));return;}
    allPositions.forEach(p=>{
      const name=p.symbol?"$"+p.symbol:short(p.token);
      const pnl=Number(p.unrealized_pnl_usd||0);
      host.append(recordRow(name,"Entry "+number(p.entry_price,8)+" · Mark "+(p.mark_status||"UNKNOWN"),signed(pnl),p.mark_status||"Unmarked",p.mark_status==="FRESH"?(pnl>=0):null));
    });
  }
  function renderTrades(){
    const host=$("all-trades");host.replaceChildren();
    if(!allTrades.length){host.append(element("div","empty-state","No paper fills recorded."));return;}
    allTrades.slice(0,100).forEach(t=>{
      const market=allMarkets.find(m=>m.mint===t.token)||{};
      const symbol=t.symbol||market.symbol;
      const value=t.side==="SELL"?signed(t.realized_pnl):money(t.notional);
      const detail=(t.side||"FILL")+" · "+date(t.created_at_iso)+" · "+number(t.price,8);
      host.append(recordRow(symbol?"$"+symbol:short(t.token),detail,value,"Fee "+money(t.fee_usd),t.side==="SELL"?(Number(t.realized_pnl)>=0):null));
    });
  }
  function renderAgents(){
    const lineup=$("agent-lineup"),host=$("agent-decisions");
    lineup.replaceChildren();host.replaceChildren();
    const runtimeAgents=Array.isArray(runtimeSnapshot?.agents?.items)?runtimeSnapshot.agents.items:[];
    const byName=new Map(runtimeAgents.filter(item=>item&&typeof item.name==="string").map(item=>[item.name.toLowerCase(),item]));
    const consensusNames=allConsensus.flatMap(item=>Array.isArray(item.supporting_agents)?item.supporting_agents:[]).filter(name=>typeof name==="string"&&name.trim());
    const names=[...new Set([...runtimeAgents.map(item=>item?.name),...consensusNames].filter(name=>typeof name==="string"&&name.trim()))].sort();
    if(!names.length) lineup.append(element("div","empty-state","No persisted agent runtime or consensus identities are available."));
    else names.forEach(name=>{
      const runtime=byName.get(name.toLowerCase());
      const status=runtime?.status||"CONSENSUS-ONLY";
      const heartbeat=runtime?.last_heartbeat? "Heartbeat "+age(runtime.last_heartbeat)+" · "+date(runtime.last_heartbeat):"No heartbeat recorded";
      const item=element("article",""),symbol=element("span","agent-symbol",name.trim().slice(0,1).toUpperCase()),copy=element("div","");
      copy.append(element("b","",name.trim().toUpperCase()),element("small","",status+" · "+heartbeat));
      item.append(symbol,copy);lineup.append(item);
    });
    $("consensus-count").textContent=number(allConsensus.length,0)+" records";
    if(!allConsensus.length){host.append(element("div","empty-state","No persisted consensus decisions yet."));return;}
    allConsensus.slice(0,60).forEach(item=>{
      const agents=(item.supporting_agents||[]).join(", ")||"Agent attribution missing in this record";
      const reasons=(item.reasons||[]).join(" · ")||"No reason supplied by the API";
      host.append(recordRow(short(item.mint),date(item.created_at)+" · "+agents,item.status||"UNKNOWN",reasons,item.status==="CONSENSUS_APPROVED"));
    });
  }
  function renderRuntime(runtime){
    const agentItems=Array.isArray(runtime?.agents?.items)?runtime.agents.items:[];
    const providerItems=Array.isArray(runtime?.providers?.items)?runtime.providers.items:[];
    const agentHost=$("agent-runtime"),providerHost=$("provider-runtime");
    agentHost.replaceChildren();providerHost.replaceChildren();
    const agentStatus=runtime?.agents?.status||"UNKNOWN";
    const providerStatus=runtime?.providers?.status||"UNKNOWN";
    $("agent-runtime-summary").textContent=agentStatus;
    $("provider-runtime-summary").textContent=providerStatus;
    if(!agentItems.length) agentHost.append(element("div","empty-state","No persisted agent heartbeat telemetry."));
    else agentItems.forEach(item=>{
      const detail=[item.last_heartbeat?"Heartbeat "+age(item.last_heartbeat)+" · "+date(item.last_heartbeat):"No heartbeat recorded",item.last_stance||"",item.error_code||""].filter(Boolean).join(" · ");
      agentHost.append(recordRow(String(item.name||"Agent").toUpperCase(),detail,item.status||"UNKNOWN",item.error_code||"Persisted runtime telemetry",item.status==="FRESH"?true:item.status==="FAILED"?false:null));
    });
    if(!providerItems.length) providerHost.append(element("div","empty-state","No persisted provider probe telemetry."));
    else providerItems.forEach(item=>{
      const detail=[item.chain||"",item.last_probe?"Probe "+age(item.last_probe)+" · "+date(item.last_probe):"No probe recorded",Number.isFinite(Number(item.latency_ms))?number(item.latency_ms,0)+" ms":""].filter(Boolean).join(" · ");
      providerHost.append(recordRow(item.provider||"Provider",detail,item.status||"UNKNOWN",item.error_code||"Persisted provider probe",item.status==="FRESH"?true:item.status==="FAILED"?false:null));
    });
    $("runtime-summary").textContent="Agents "+agentStatus+" · Providers "+providerStatus+" · Live execution "+(runtime?.live_execution===false?"disabled":"not verified");
  }
  function renderNetworks(){
    const labels={solana:"Solana",base:"Base",bsc:"BSC"};
    let total=0;
    for(const chain of Object.keys(labels)){
      const data=allChainMarkets[chain]||{};
      const count=Number(data.observation_count||0);
      total+=count;
      $(chain+"-count").textContent=number(count,0);
      $(chain+"-latest").textContent=data.latest_observed_at?"Last observation "+age(data.latest_observed_at)+" · "+date(data.latest_observed_at):(data.available===true?"No persisted observations":"Ledger unavailable");
    }
    $("network-total").textContent=number(total,0)+" observations";
    $("network-summary").textContent=Object.keys(labels).map(chain=>labels[chain]+" "+Number(allChainMarkets[chain]?.observation_count||0)).join(" · ");
  }
  function renderChart(){
    const svg=$("equity-chart");svg.replaceChildren();
    const now=Date.now(), ranges={"1H":3600000,"4H":14400000,"12H":43200000,"ALL":Infinity};
    let points=chartPoints;
    if(currentRange!=="ALL") points=points.filter(p=>!p.time||now-new Date(p.time).getTime()<=ranges[currentRange]);
    if(points.length<2){$("chart-empty").hidden=false;return;}
    $("chart-empty").hidden=true;
    const ns="http://www.w3.org/2000/svg", values=points.map(p=>Number(p.equity_usd||0));
    const min=Math.min(...values),max=Math.max(...values),range=Math.max(max-min,Math.abs(max)*.0001,0.000001);
    for(let i=1;i<=3;i++){const y=20+i*35;const line=document.createElementNS(ns,"line");line.setAttribute("x1","8");line.setAttribute("x2","592");line.setAttribute("y1",y);line.setAttribute("y2",y);line.setAttribute("class","chart-grid");svg.append(line);}
    const coords=values.map((v,i)=>[10+(580*i/(values.length-1)),145-((v-min)/range)*105]);
    const poly=document.createElementNS(ns,"polyline");poly.setAttribute("points",coords.map(p=>p.join(",")).join(" "));poly.setAttribute("class","chart-line");svg.append(poly);
    const last=coords[coords.length-1],dot=document.createElementNS(ns,"circle");dot.setAttribute("cx",last[0]);dot.setAttribute("cy",last[1]);dot.setAttribute("r","7");dot.setAttribute("class","chart-point");svg.append(dot);
  }
  function renderSearch(query){
    const host=$("search-results");host.replaceChildren();
    const q=query.trim().toLowerCase();
    if(!q){host.append(element("p","","Search loaded ledger and market records."));return;}
    const items=[
      ...allPositions.map(x=>({name:x.symbol||x.token,detail:"Open position · "+x.mark_status})),
      ...allTrades.map(x=>({name:x.symbol||x.token,detail:x.side+" fill · "+date(x.created_at_iso)})),
      ...allMarkets.map(x=>({name:x.symbol||x.mint,detail:"Solana market snapshot · "+date(x.retrieved_at)})),
      ...Object.values(allChainMarkets).flatMap(chain=>Array.isArray(chain.observations)?chain.observations.map(x=>({name:x.symbol||x.token,detail:(x.chain||"Chain")+" market observation · "+date(x.observed_at)})):[]),
      ...allConsensus.map(x=>({name:x.mint,detail:"Agent consensus · "+x.status}))
    ].filter(x=>(x.name||"").toLowerCase().includes(q)).slice(0,30);
    if(!items.length){host.append(element("p","","No matching records in the loaded dashboard data."));return;}
    items.forEach(x=>host.append(recordRow(x.name,x.detail,"","",null)));
  }
  function navigate(screen){
    document.querySelectorAll(".screen").forEach(s=>s.classList.toggle("active",s.id==="screen-"+screen));
    document.querySelectorAll(".nav-item").forEach(b=>{
      const active=b.dataset.screen===screen;b.classList.toggle("active",active);
      if(active)b.setAttribute("aria-current","page");else b.removeAttribute("aria-current");
    });
    window.scrollTo({top:0,behavior:"smooth"});
  }
  async function refresh(){
    if(refreshing)return;refreshing=true;
    try{
      const names=["health","overview","positions","trades","decisions","consensus","markets","equity","runtime","solana","base","bsc"];
      const requests=[
        api("/api/health"),api("/api/overview"),api("/api/positions"),api("/api/trades"),
        api("/api/decisions"),api("/api/consensus"),api("/api/markets"),api("/api/equity-curve"),
        api("/api/runtime-status"),api("/api/chain-markets?chain=solana"),
        api("/api/chain-markets?chain=base"),api("/api/chain-markets?chain=bsc")
      ];
      const results=await Promise.allSettled(requests);
      const failures=results.map((result,index)=>result.status==="rejected"?names[index]+": "+result.reason.message:null).filter(Boolean);
      if(results.some(result=>result.status==="rejected"&&/401|token expired|revoked/i.test(result.reason?.message||""))){
        $("health-label").textContent="Dashboard access required";
        $("health-detail").textContent="The access token was rejected. Enter the current dashboard token.";
        $("last-updated").textContent="Authentication required";
        return;
      }
      const value=(index,fallback)=>results[index].status==="fulfilled"?results[index].value:fallback;
      const health=value(0,{database:"unavailable",missing_paper_tables:[]});
      const overview=value(1,{account:{},performance:{}});
      allPositions=value(2,[]);allTrades=value(3,[]);value(4,[]);
      allConsensus=value(5,[]);allMarkets=value(6,[]);chartPoints=value(7,{points:[]}).points||[];
      runtimeSnapshot=value(8,{agents:{status:"UNKNOWN",items:[]},providers:{status:"UNKNOWN",items:[]}});
      allChainMarkets={solana:value(9,{}),base:value(10,{}),bsc:value(11,{})};
      $("access-gate").hidden=true;$("access-token").value="";
      renderHome(overview);renderPositions();renderTrades();renderAgents();renderRuntime(runtimeSnapshot);renderNetworks();renderChart();
      $("health-label").textContent=health.database==="connected"?"Database connected · read-only": "Database health unavailable";
      const missing=(health.missing_paper_tables||[]);
      const safety=health.live_execution===false&&runtimeSnapshot.live_execution===false?"Live execution disabled":"Execution state not fully verified";
      $("health-detail").textContent=safety+(missing.length?" · Missing tables: "+missing.join(", "):" · "+(health.database_read_only===true?"Read-only access":"Read-only status unavailable"));
      if(failures.length) $("health-detail").textContent+=" · Partial data: "+failures.join("; ");
      $("last-updated").textContent=(failures.length?"Partially updated ":"Updated ")+new Date().toLocaleTimeString();
    }catch(error){
      $("health-label").textContent="Dashboard data unavailable";
      $("health-detail").textContent=error.message;
      $("last-updated").textContent="Refresh failed";
    }finally{refreshing=false;}
  }
  document.querySelectorAll(".nav-item").forEach(b=>b.addEventListener("click",()=>navigate(b.dataset.screen)));
  document.querySelectorAll("[data-go]").forEach(b=>b.addEventListener("click",()=>navigate(b.dataset.go)));
  document.querySelectorAll("[data-range]").forEach(b=>b.addEventListener("click",()=>{
    currentRange=b.dataset.range;
    document.querySelectorAll("[data-range]").forEach(x=>x.classList.toggle("selected",x===b));
    renderChart();
  }));

  $("access-submit").addEventListener("click",()=>{
    const token=$("access-token").value.trim();
    if(token.length<32){$("access-error").textContent="Token must contain at least 32 characters.";return;}
    sessionStorage.setItem(tokenKey,token);$("access-error").textContent="";refresh();
  });
  $("access-token").addEventListener("keydown",e=>{if(e.key==="Enter")$("access-submit").click();});
  $("search-open").addEventListener("click",()=>{$("search-sheet").hidden=false;$("search-input").focus();});
  $("search-close").addEventListener("click",()=>{$("search-sheet").hidden=true;});
  $("search-input").addEventListener("input",e=>renderSearch(e.target.value));
  $("status-toggle").addEventListener("click",()=>{$("status-sheet").hidden=false;});
  $("status-close").addEventListener("click",()=>{$("status-sheet").hidden=true;});
  refresh();
  window.setInterval(refresh,15000);
})();
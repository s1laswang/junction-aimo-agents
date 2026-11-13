(function(){
const $=s=>document.querySelector(s);
const TEXTS={
milk:"I need 1,000 litres of lactose-free milk by Monday. Find an option that meets our requirements based on price and availability.",
school:"Weekly school lunch: 200 kg of gluten-free pasta and 80 litres of oat drink, no nuts, no egg, delivered by Thursday.",
event:"Last-minute event for 120 guests on Friday: 60 kg of salmon fillet, 40 litres of cream and 25 kg of butter."};
const MOCKS={
milk:{need:{customer:"",deliver_by:"2026-11-16",raw_text:"",items:[{query:"lactose-free milk",quantity:1000,unit:"l",constraints:{lactose_free:true,gluten_free:null,exclude_allergens:[],labels:[],origin:null,max_unit_price:null,brands:[]}}]},
 offers:[{need_item:0,offers:[
  {product_id:"mock-400101",product:{name:"Valio Eila lactose-free milk 1 l",brand:"Valio Eila",sales_unit:"PK",unit_size:{value:1,unit:"l"}},units_needed:1000,unit_price_net:1.18,total_net:1180,available_amount:1500,available_date:"2026-11-16",reliability:.97,co2_per_kg:1.1,match_score:.96,tier:"best",notes:["exact: lactose-free, 1 l pack","stock covers full quantity"]},
  {product_id:"mock-400117",product:{name:"Valio lactose-free milk drink 1 l",brand:"Valio",sales_unit:"PK",unit_size:{value:1,unit:"l"}},units_needed:1000,unit_price_net:1.24,total_net:1240,available_amount:1000,available_date:"2026-11-16",reliability:.94,co2_per_kg:1.1,match_score:.9,tier:"better",notes:["higher price"]}],shortfall:0}],
 order:{status:"placed",decision:{outcome:"auto",reasons:[{code:"WITHIN_MANDATE",text:"@WM"},{code:"NO_ALLERGEN_CONFLICT",text:"No excluded allergens in the selected product."}]},substitutions:[]}},
school:{need:{customer:"",deliver_by:"2026-11-20",raw_text:"",items:[
 {query:"gluten-free pasta",quantity:200,unit:"kg",constraints:{lactose_free:null,gluten_free:true,exclude_allergens:["nuts","egg"],labels:[],origin:null,max_unit_price:null,brands:[]}},
 {query:"oat drink",quantity:80,unit:"l",constraints:{lactose_free:null,gluten_free:true,exclude_allergens:["nuts"],labels:[],origin:null,max_unit_price:null,brands:[]}}]},
 offers:[
 {need_item:0,offers:[{product_id:"mock-510220",product:{name:"Gluten-free fusilli 500 g",brand:"Schar",sales_unit:"PK",unit_size:{value:.5,unit:"kg"}},units_needed:400,unit_price_net:2.1,total_net:840,available_amount:400,available_date:"2026-11-19",reliability:.91,co2_per_kg:1.6,match_score:.93,tier:"best",notes:["egg-free, nut-free confirmed"]}],shortfall:0},
 {need_item:1,offers:[{product_id:"mock-520310",product:{name:"Oat drink 1 l",brand:"Alpro",sales_unit:"PK",unit_size:{value:1,unit:"l"}},units_needed:80,unit_price_net:1.45,total_net:116,available_amount:48,available_date:"2026-11-19",reliability:.88,co2_per_kg:.9,match_score:.82,tier:"good",notes:["substitute for requested brand","partial stock"]}],shortfall:32}],
 order:{status:"pending_approval",decision:{outcome:"needs_approval",reasons:[{code:"ORDER_OVER_LIMIT",text:"@OL"},{code:"SUBSTITUTION",text:"Oat drink was substituted and the mandate asks for approval on substitutions."},{code:"PARTIAL_STOCK",text:"Only 48 of 80 units are available by Thursday; 32 are short."}]},substitutions:[{original_id:"mock-520300",substitute_id:"mock-520310",reason:"Original out of stock"}]}},
event:{need:{customer:"",deliver_by:"2026-11-20",raw_text:"",items:[
 {query:"salmon fillet",quantity:60,unit:"kg",constraints:{lactose_free:null,gluten_free:null,exclude_allergens:[],labels:[],origin:null,max_unit_price:null,brands:[]}},
 {query:"cream",quantity:40,unit:"l",constraints:{lactose_free:null,gluten_free:null,exclude_allergens:[],labels:[],origin:null,max_unit_price:null,brands:[]}},
 {query:"butter",quantity:25,unit:"kg",constraints:{lactose_free:null,gluten_free:null,exclude_allergens:[],labels:[],origin:null,max_unit_price:null,brands:[]}}]},
 offers:[
 {need_item:0,offers:[{product_id:"mock-610001",product:{name:"Salmon fillet 1-2 kg",brand:"Fiskars",sales_unit:"KG",unit_size:{value:1,unit:"kg"}},units_needed:60,unit_price_net:17.9,total_net:1074,available_amount:60,available_date:"2026-11-19",reliability:.9,co2_per_kg:5.1,match_score:.9,tier:"best",notes:[]}],shortfall:0},
 {need_item:1,offers:[{product_id:"mock-610002",product:{name:"Cream 35% 1 l",brand:"Valio",sales_unit:"PK",unit_size:{value:1,unit:"l"}},units_needed:40,unit_price_net:3.2,total_net:128,available_amount:40,available_date:"2026-11-19",reliability:.98,co2_per_kg:2.4,match_score:.97,tier:"exact",notes:[]}],shortfall:0},
 {need_item:2,offers:[{product_id:"mock-610003",product:{name:"Butter 500 g",brand:"Valio",sales_unit:"PK",unit_size:{value:.5,unit:"kg"}},units_needed:50,unit_price_net:3.9,total_net:195,available_amount:50,available_date:"2026-11-19",reliability:.98,co2_per_kg:9.2,match_score:.97,tier:"exact",notes:[]}],shortfall:0}],
 order:{status:"blocked",decision:{outcome:"blocked",reasons:[{code:"CATEGORY_NOT_ALLOWED",text:"Fresh fish is outside this agent's allowed categories."},{code:"LEAD_TIME",text:"Delivery in under 48 hours is not covered by the mandate."}]},substitutions:[]}}};
const DEMO=new URLSearchParams(location.search).has("demo");
const S={mock:new URLSearchParams(location.search).has("mock"),seen:new Set(),base:false,sc:"milk"};
const cust=()=>aimo.customer();
const esc=v=>String(v??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const NF=new Intl.NumberFormat("en-GB",{minimumFractionDigits:2,maximumFractionDigits:2});
const eur=v=>v==null?"-":NF.format(v);
const money=v=>eur(v)+" EUR";
const qty=v=>new Intl.NumberFormat("en-GB",{maximumFractionDigits:0}).format(v);
const AVAIL=v=>v>=1e9?"in stock":qty(v);
const lt=s=>{const d=new Date(s);if(isNaN(d))return clock();const p=n=>String(n).padStart(2,"0");return`${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`};
const noteHtml=n=>{n=n||[];if(!n.length)return"";const r=n.slice(1);const sk=r.length&&r.every(x=>/skipped/i.test(x));return" - "+esc(n[0])+(r.length?`<details><summary>${sk?"+"+r.length+" skipped (cutoff passed)":"+"+r.length+" more"}</summary>${r.map(x=>"<div>"+esc(x)+"</div>").join("")}</details>`:"")};
const day=s=>{const d=new Date(s);return isNaN(d)?String(s||""):d.toLocaleDateString("en-GB",{day:"numeric",month:"short"})};
const MANDATES={"hotel-aurora":{o:2000,w:8000},"hospital-tays":{o:1500,w:6000},"school-hervanta":{o:800,w:3000}};
const clock=()=>{const d=new Date(),p=(n,l=2)=>String(n).padStart(l,"0");return`${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(),3)}`};
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function setMock(on){if(on)S.mock=true;aimo.mock(S.mock)}
function line(o){
 $("#trace-empty").hidden=true;
 const li=document.createElement("li");li.className="ln"+(o.ext?" ext":"")+(o.aud?" aud":"");
 li.innerHTML=`<div class="h"><span class="t">${o.t||lt()}</span><span><span class="tool">${esc(o.tool)}</span><span class="st ${o.cls||"run"}">${esc(o.status||"")}</span></span><span class="ms">${o.ms!=null?o.ms+" ms":esc(o.actor||"")}</span><span class="why">${esc(o.why||"")}</span>${o.req||(o.res&&Object.keys(o.res).length)?`<details><summary>request / response</summary><pre>${esc(JSON.stringify({request:o.req,response:o.res},null,2))}</pre></details>`:""}</div>`;
 $("#trace").prepend(li);return li;
}
function upd(li,o){
 const st=li.querySelector(".st");st.className="st "+o.cls;st.textContent=o.status;
 li.querySelector(".ms").textContent=o.ms+" ms";li.querySelector(".why").textContent=o.why;
 const d=document.createElement("details");d.innerHTML=`<summary>request / response</summary><pre>${esc(JSON.stringify({request:o.req,response:o.res},null,2))}</pre>`;li.querySelector(".h").append(d);
}
async function call(tool,method,path,body,mock,why){
 const li=line({tool,status:"running",why:"Calling "+path+"..."});
 const t0=performance.now();let res=null,real=false;
 if(!S.mock){
  res=await aimo.api(path,{method,body});real=res!=null;
  if(!real){S.pend=true}
 }
 if(S.pend){S.pend=false}
 let err=null;
 if(!real&&!S.mock){try{const r=await fetch(path,{method,headers:{"Content-Type":"application/json","X-Agent-Key":localStorage.getItem("aimo.agentKey")||"demo-"+cust()},body:body?JSON.stringify(body):undefined});if(r.status>=400&&r.status<500&&r.status!==404){const e=await r.json().catch(()=>null);if(e&&e.code)err={status:r.status,...e}}}catch(e){}}
 if(err){S.mock=S.fell?S.mock:false;upd(li,{cls:"err",status:String(err.status),ms:Math.round(performance.now()-t0),why:err.code+": "+err.text,req:body,res:err});const x=new Error(err.code+": "+err.text);throw x}
 if(!real){setMock(true);S.fell=true;await sleep(200+Math.random()*400);res=mock()}
 const ms=Math.round(performance.now()-t0);
 upd(li,{cls:"ok",status:real?"200":"mock",ms,why:why(res),req:body,res});
 return res;
}
const tierRank={best:4,better:3,good:2,exact:1};
function best(offers){return [...offers].sort((a,b)=>(tierRank[b.tier]||0)-(tierRank[a.tier]||0)||b.match_score-a.match_score)[0]}
function consChips(c){
 const o=[];if(c.lactose_free)o.push("lactose-free");if(c.gluten_free)o.push("gluten-free");
 (c.exclude_allergens||[]).forEach(a=>o.push("no "+a));(c.labels||[]).forEach(l=>o.push(l));
 if(c.origin)o.push("origin "+c.origin);if(c.max_unit_price!=null)o.push("max "+money(c.max_unit_price));(c.brands||[]).forEach(b=>o.push(b));
 return o.map(x=>`<span class="cons">${esc(x)}</span>`).join(" ");
}
function needHtml(n){
 return`<div class="sec"><h3>Parsed need</h3><div class="kv"><span>Deliver by <b class="mono">${esc(n.deliver_by)}</b></span><span>Customer <b class="mono">${esc(n.customer)}</b></span></div>`+
 n.items.map(i=>`<div class="kv" style="margin-top:6px"><b>${esc(i.query)}</b><span class="mono">${esc(i.quantity)} ${esc(i.unit)}</span>${consChips(i.constraints||{})}</div>`).join("")+`</div>`;
}
function offersHtml(need,off){
 return`<div class="sec"><h3>Offers</h3>`+off.items.map((g,i)=>{
  const ni=need.items[i]||g.need_item||{};
  const pick=best(g.offers||[]);
  const ht=(g.offers||[]).some(o=>o.tier);
  const rows=(g.offers||[]).map(o=>`<tr class="${o===pick?"pick":""}"><td>${esc(o.product.name)}${o===pick?' <span class="tag ok">picked</span>':""}<div class="note mono">${esc(o.product_id)}${noteHtml(o.notes)}</div></td><td>${esc(o.product.brand)}</td><td class="n">${qty(o.units_needed)}</td><td class="n">${eur(o.unit_price_net)}</td><td class="n">${eur(o.total_net)}</td><td class="n">${AVAIL(o.available_amount)}<div class="note">${esc(day(o.available_date))}</div></td><td class="n">${o.reliability!=null?Math.round(o.reliability*100)+"%":"-"}</td><td class="n">${o.co2_per_kg??"-"}</td>${ht?`<td>${o.tier?`<span class="tag">${esc(o.tier)}</span>`:""}</td>`:""}</tr>`).join("");
  const sf=g.shortfall>0?`<tr class="short"><td colspan="${ht?9:8}">Shortfall: ${g.shortfall} units not available by the delivery date.</td></tr>`:"";
  return`<div class="tw"><div class="note" style="margin:6px 0 2px">${esc(ni.query||"")} - ${esc(ni.quantity||"")} ${esc(ni.unit||"")}</div><table><thead><tr><th>Product</th><th>Brand</th><th class="n">Units</th><th class="n">Unit EUR</th><th class="n">Total EUR</th><th class="n">Available</th><th class="n">Reliab.</th><th class="n">CO2/kg</th>${ht?"<th>Tier</th>":""}</tr></thead><tbody>${rows}${sf}</tbody></table></div>`}).join("")+`</div>`;
}
function quoteHtml(q){
 return`<div class="sec quote"><h3>Signed quote</h3><div class="kv"><span class="mono">${esc(q.quote_id)}</span><span>Total <b class="mono">${money(q.total_net)}</b></span><span>Valid until <b class="mono">${esc(q.valid_until)}</b></span><span class="tag ok">signed Ed25519</span><span class="jws" title="${esc(q.jws)}">${esc(q.jws.slice(0,28))}...</span><button class="ghost" type="button" id="copy">Copy</button></div></div>`;
}
function orderHtml(o){
 const d=o.decision,lab={auto:"Auto-approved",needs_approval:"Needs approval",blocked:"Blocked"}[d.outcome];
 return`<div class="sec"><h3>Order</h3><div class="dec ${d.outcome}"><div class="kv"><strong>${lab}</strong><span class="mono">${esc(o.order_id)}</span><span class="tag">${esc(o.status)}</span><span class="mono">${money(o.total_net)}</span>${d.outcome==="needs_approval"?'<a href="/inbox.html">Open in Approvals</a>':""}<a href="/audit.html?order_id=${encodeURIComponent(o.order_id)}">Audit trail</a></div><ul>${d.reasons.map(r=>`<li><code>${esc(r.code)}</code><span>${esc(r.text)}</span></li>`).join("")}</ul></div>${(o.substitutions||[]).map(s=>`<div class="note" style="margin-top:4px">Substitution ${esc(s.original_id)} to ${esc(s.substitute_id)}: ${esc(s.reason)}</div>`).join("")}</div>`;
}
function fillOrder(o,q,c){const L=MANDATES[c]||MANDATES["hotel-aurora"];const t=money(q.total_net);const d={...o.decision,reasons:o.decision.reasons.filter(r=>r.text!=="@OL"||q.total_net>L.o).map(r=>r.text==="@WM"?{...r,text:`Total ${t} is under the per-order limit of ${money(L.o)}.`}:r.text==="@OL"?{...r,text:`Total ${t} is over the per-order limit of ${money(L.o)}.`}:r)};if(o.decision.outcome==="auto"&&q.total_net>L.o){d.outcome="needs_approval";d.reasons=[{code:"ORDER_OVER_LIMIT",text:`Total ${t} is over the per-order limit of ${money(L.o)}.`}]}return{...o,decision:d,status:d.outcome==="needs_approval"&&o.status==="placed"?"pending_approval":o.status}}
const hex=n=>Array.from({length:n},()=>Math.floor(Math.random()*16).toString(16)).join("");
function demoBtns(out,ord){
 out.insertAdjacentHTML("beforeend",'<div class="row"><button type="button" class="ghost" id="sim">Simulate stock-out</button><button type="button" class="ghost" id="clr">Clear</button></div>');
 const pid=String(((ord.lines||[])[0]||{}).product_id||"");
 const act=async(method,body,msg)=>{const t0=performance.now();const r=await aimo.api("/v1/admin/simulate",{method,admin:true,body});line({tool:"admin/simulate",status:r?"200":"failed",cls:r?"ok":"err",ms:Math.round(performance.now()-t0),why:msg,req:body,res:r})};
 $("#sim").onclick=()=>act("POST",{product_id:pid,available_amount:0},"Simulated stock-out on "+pid);
 $("#clr").onclick=()=>act("DELETE",undefined,"Cleared simulation");
}
async function run(){
 const text=$("#text").value.trim();if(!text)return;
 const sc=/school|allergen|gluten|hospital/i.test(text)?"school":/event|guests|salmon|last-minute/i.test(text)?"event":"milk";
 const M=MOCKS[sc],c=cust();
 $("#run").disabled=true;$("#live").textContent="running";$("#live").classList.add("on");
 const res=$("#result");res.innerHTML='<div class="panel" id="out"></div>';const out=$("#out");
 try{
  const need=await call("parse_request","POST","/v1/needs/parse",{text,customer:c},()=>({...M.need,customer:c,raw_text:text}),
   n=>`Read the request as ${n.items.length} item(s) to deliver by ${n.deliver_by}.`);
  out.insertAdjacentHTML("beforeend",needHtml(need));
  const off=await call("get_offers","POST","/v1/offers",need,()=>({items:M.offers.map(g=>({need_item:need.items[g.need_item],offers:g.offers,shortfall:g.shortfall}))}),
   o=>`Found ${o.items.reduce((a,g)=>a+(g.offers||[]).length,0)} offer(s); ${o.items.filter(g=>g.shortfall>0).length?"some stock is short":"stock covers every item"}.`);
  out.insertAdjacentHTML("beforeend",offersHtml(need,off));
  const picks=off.items.map(g=>best(g.offers||[])).filter(Boolean);
  const lines=picks.map(p=>({product_id:p.product_id,quantity:Math.min(p.units_needed,p.available_amount)}));
  const qm=()=>{const ls=picks.map((p,i)=>({product_id:p.product_id,quantity:lines[i].quantity,sales_unit:p.product.sales_unit,unit_price_net:p.unit_price_net,line_total_net:+(p.unit_price_net*lines[i].quantity).toFixed(2)}));const t=+ls.reduce((a,l)=>a+l.line_total_net,0).toFixed(2);return{quote_id:"q_mock_"+hex(6),customer:c,agent_id:"console",delivery_date:need.deliver_by,lines:ls,total_net:t,total_gross:+(t*1.14).toFixed(2),currency:"EUR",issued_at:new Date().toISOString(),valid_until:new Date(Date.now()+864e5).toISOString().slice(0,16)+"Z",jws:"eyJhbGciOiJFZERTQSJ9."+hex(40)+"."+hex(64)}};
  const q=await call("request_quote","POST","/v1/quotes",{offers:lines,delivery_date:need.deliver_by},qm,
   q=>`Priced ${q.lines.length} line(s) at ${money(q.total_net)} and signed the quote.`);
  out.insertAdjacentHTML("beforeend",quoteHtml(q));
  $("#copy").onclick=()=>{navigator.clipboard&&navigator.clipboard.writeText(q.jws);$("#copy").textContent="Copied"};
  const ord=await call("place_order","POST","/v1/orders",{quote_id:q.quote_id,jws:q.jws},()=>({order_id:"o_mock_"+hex(6),quote_id:q.quote_id,customer:c,agent_id:"console",lines:q.lines,total_net:q.total_net,...fillOrder(M.order,q,c)}),
   o=>`Policy decision: ${o.decision.outcome.replace("_"," ")}. ${o.decision.reasons[0]?o.decision.reasons[0].text:""}`);
  out.insertAdjacentHTML("beforeend",orderHtml(ord));
  if(DEMO)demoBtns(out,ord);
 }catch(e){out.insertAdjacentHTML("beforeend",`<div class="dec blocked">Run failed: ${esc(e.message)}</div>`)}
 $("#run").disabled=false;$("#live").textContent="idle";$("#live").classList.remove("on");
}
async function poll(){
 if(!S.mock){
  const j=await aimo.api("/v1/audit?customer="+encodeURIComponent(cust()));
  if(j){const ev=Array.isArray(j)?j:(j.events||[]);
   ev.sort((a,b)=>a.seq-b.seq).forEach(e=>{const k=cust()+e.seq;if(S.seen.has(k))return;S.seen.add(k);if(S.base)line({t:lt(e.ts),aud:1,tool:e.action,status:"audit",cls:"ok",actor:e.actor,why:e.why,res:e.detail,ext:!/console/.test(e.actor||"")})});
   S.base=true}
 }
 setTimeout(poll,2000);
}
function init(){
 window.addEventListener("customer",()=>{S.base=false});
 $("#scenarios").onclick=e=>{const b=e.target.closest("[data-s]");if(b){$("#text").value=TEXTS[b.dataset.s];$("#text").focus()}};
 $("#req").onsubmit=e=>{e.preventDefault();run()};
 setMock(S.mock);poll();
}
init();
})();

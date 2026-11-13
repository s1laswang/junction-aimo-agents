(function(){
var Q=new URLSearchParams(location.search);
var MODE=Q.get("mock");
var $=function(id){return document.getElementById(id)};
var state={q2o:{},events:[],text:"",view:"ledger",bad:null,open:{}};
var ICON={
shield:'<svg class="i" viewBox="0 0 24 24"><path d="M12 3l8 3v6c0 4.5-3.2 8-8 9-4.8-1-8-4.5-8-9V6z"/><path d="M9 12l2 2 4-4"/></svg>',
alert:'<svg class="i" viewBox="0 0 24 24"><path d="M12 3l8 3v6c0 4.5-3.2 8-8 9-4.8-1-8-4.5-8-9V6z"/><path d="M12 8v5M12 16h.01"/></svg>',
link:'<svg class="i" viewBox="0 0 24 24"><path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/></svg>',
chev:'<svg class="i chev" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg>'
};

function canon(v){
if(v===null||typeof v!=="object")return JSON.stringify(v).replace(/[\u007f-￿]/g,function(c){return "\\u"+("0000"+c.charCodeAt(0).toString(16)).slice(-4)});
if(Array.isArray(v))return "["+v.map(canon).join(",")+"]";
return "{"+Object.keys(v).sort().map(function(k){return canon(k)+":"+canon(v[k])}).join(",")+"}";
}
async function sha(s){
var b=await crypto.subtle.digest("SHA-256",new TextEncoder().encode(s));
return Array.from(new Uint8Array(b)).map(function(x){return x.toString(16).padStart(2,"0")}).join("");
}
var ZERO="0".repeat(64);
async function hashOf(e){return sha(e.prev_hash+canon({action:e.action,actor:e.actor,customer:e.customer,detail:e.detail,seq:e.seq,subject:e.subject,ts:e.ts,why:e.why}))}

async function verify(evs){
var s=evs.slice().sort(function(a,b){return a.seq-b.seq});
for(var i=0;i<s.length;i++){
var e=s[i];
if(await hashOf(e)!==e.hash)return {at:e.seq,why:"hash mismatch"};
if(i===0&&e.seq===1&&e.prev_hash!==ZERO)return {at:e.seq,why:"genesis prev_hash"};
if(i>0&&s[i-1].seq===e.seq-1&&s[i-1].hash!==e.prev_hash)return {at:e.seq,why:"prev_hash does not match #"+s[i-1].seq};
}
return null;
}

window.aimoAudit={verify:verify,hashOf:hashOf};
var MOCK_RAW=[
["agent:buyer-aurora","parse_request","need:n1","Read the request as 2000 L lactose-free milk, delivery by Monday 17 Nov.",{raw_text:"2000 litres lactose-free milk by Monday"},"ord_7f3a"],
["agent:buyer-aurora","get_offers","need:n1","Found 4 lactose-free milk offers; the cheapest in stock covers 2000 L at 1.12 EUR/L.",{offers:4,best:"410008"},"ord_7f3a"],
["agent:buyer-aurora","request_quote","quote:q_91c2","Signed a quote for 2000 L at 2,240.00 EUR net, valid until the 14:00 order cutoff.",{total_net:2240,valid_until:"2026-11-14T14:00:00Z"},"ord_7f3a"],
["system","policy_check","order:ord_7f3a","Order total 2,240.00 EUR is above the 2,000 EUR per-order limit, so a human must approve.",{limit:2000,total:2240,outcome:"needs_approval"},"ord_7f3a"],
["agent:buyer-aurora","place_order","order:ord_7f3a","Placed the order against the signed quote; held for approval.",{quote_id:"q_91c2",status:"pending_approval"},"ord_7f3a"],
["human:kitchen-manager","approve_order","order:ord_7f3a","Approved: the milk is for Monday breakfast service.",{approve:true,note:"ok for Monday"},"ord_7f3a"],
["system","order_placed","order:ord_7f3a","Order passed to fulfilment and confirmed for delivery on 17 Nov.",{status:"confirmed"},"ord_7f3a"],
["agent:seller-valio","stock_alert","product:410008","Stock for lactose-free milk 1 L fell below the weekly average; offering oat drink as an alternative.",{available:620},null],
["agent:buyer-aurora","get_offers","need:n2","Found 3 offers for 40 kg unsalted butter; cheapest in stock 7.40 EUR/kg.",{offers:3},"ord_8b21"],
["system","policy_check","order:ord_8b21","Order is within the 2,000 EUR limit and all allergen rules; no approval needed.",{outcome:"auto"},"ord_8b21"],
["agent:buyer-aurora","place_order","order:ord_8b21","Placed the order automatically within the mandate.",{status:"placed"},"ord_8b21"]
];
async function buildMock(broken){
var out=[],prev=ZERO,t=Date.parse("2026-11-13T09:00:00Z");
for(var i=0;i<MOCK_RAW.length;i++){
var r=MOCK_RAW[i],d=Object.assign({},r[4]);if(r[5])d.order_id=r[5];
var e={seq:i+1,ts:new Date(t+i*61000).toISOString().replace(/\.\d+Z$/,"Z"),customer:i<8?"hotel-aurora":i===8?"hospital-tays":"hotel-aurora",actor:r[0],action:r[1],subject:r[2],detail:d,why:r[3],prev_hash:prev};
e.hash=await hashOf(e);prev=e.hash;out.push(e);
}
if(broken)out[4].detail.status="placed";
return out;
}
function plainText(evs){
return evs.map(function(e){return "#"+e.seq+"  "+e.ts+"  "+e.actor+"  "+e.action+"\n    "+e.why+"\n    ref "+e.subject}).join("\n\n");
}

function orderOf(e){
var d=e.detail||{};if(d.order_id)return d.order_id;
var m=/^order:(.+)$/.exec(e.subject||"");if(m)return m[1];
var q=state.q2o[d.quote_id||e.subject];return q||null;
}
function esc(s){return String(s).replace(/[&<>"]/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]})}
function short(h){return h?h.slice(0,8)+"…"+h.slice(-4):"none"}
function tm(ts){var d=new Date(ts);return isNaN(d)?ts:d.toISOString().slice(11,19)}
function dt(ts){var d=new Date(ts);return isNaN(d)?"":d.toISOString().slice(0,10)}

function filtered(){
var a=$("f-actor").value,x=$("f-action").value,c=$("f-customer").value,o=$("f-order").value.trim();
return state.events.filter(function(e){return (!c||e.customer===c)&&(!o||orderOf(e)===o)&&(!a||e.actor.split(":")[0]===a)&&(!x||e.action===x)});
}
function badge(){
$("how-n").textContent=state.events.length;
var b=$("badge"),n=state.events.length;
if(state.bad===undefined||state.bad==="pending"){b.className="badge idle";b.textContent="Verifying chain";return}
if(state.bad){b.className="badge stop";b.innerHTML=ICON.alert+'Chain broken at #'+state.bad.at+' <a href="#e'+state.bad.at+'" id="jump">Jump to #'+state.bad.at+'</a>';
$("jump").addEventListener("click",function(){state.open[state.bad.at]=true;render();var el=$("e"+state.bad.at);if(el)el.scrollIntoView({block:"center"})});return}
var cp=state.cp&&state.cp.jws?", checkpoint #"+state.cp.seq+" signed":"";
if(state.mode==="unknown"){b.className="badge idle";b.innerHTML=ICON.alert+"Chain check inconclusive in browser, "+n+" events";return}
b.className="badge ok";b.innerHTML=ICON.shield+(state.mode==="gateway"?"Chain verified by gateway, ":state.srv?"Chain verified in your browser and by gateway, ":"Chain verified in your browser, ")+n+" event"+(n===1?"":"s")+cp;if(state.cp&&state.cp.hash)b.title="checkpoint "+state.cp.hash+" at "+state.cp.ts;
}
var tseq=0;
async function scopedText(evs){
var my=++tseq,pl=$("plain"),c=$("f-customer").value,o=$("f-order").value.trim();
pl.textContent=state.live?"Loading":plainText(evs);
if(!state.live)return;
var d=await aimo.api("/v1/audit?"+(c?"customer="+encodeURIComponent(c)+"&":"")+(o?"order_id="+encodeURIComponent(o):""),{headers:{"X-Admin-Key":"demo-admin"}});
if(my!==tseq)return;
var t=d&&d.text;
pl.textContent=t&&(evs.length||!t)?t:(evs.length?plainText(evs):"No audit events match these filters.");
}
function render(){
var evs=filtered(),L=$("ledger");
$("plain").hidden=state.view!=="text";L.hidden=state.view==="text";
$("v-ledger").setAttribute("aria-pressed",state.view==="ledger");$("v-text").setAttribute("aria-pressed",state.view==="text");
if(state.view==="text")scopedText(evs);
if(!evs.length){L.innerHTML='<div class="empty">No audit events match these filters.</div>';return}
var groups=[],idx={};
evs.forEach(function(e){var k=orderOf(e)||"";if(!(k in idx)){idx[k]=groups.length;groups.push({k:k,items:[]})}groups[idx[k]].items.push(e)});
var byHash={};state.events.forEach(function(e){byHash[e.seq]=e});
L.innerHTML=groups.map(function(g){
var first=g.items[0];
return '<div class="grp"><div class="grp-h"><h3>'+(g.k?"Order":"No order")+'</h3>'+(g.k?'<span class="mono">'+esc(g.k)+'</span>':'')+'<span class="small muted">'+dt(first.ts)+" &middot; "+g.items.length+' event'+(g.items.length===1?"":"s")+'</span></div>'+
g.items.map(function(e){
var bad=state.bad&&state.bad!=="pending"&&state.bad.at===e.seq,open=!!state.open[e.seq],kind=e.actor.split(":")[0];
var pe=byHash[e.seq-1];
return '<div class="row'+(bad?" bad":"")+'" id="e'+e.seq+'" data-open="'+open+'">'+
'<button type="button" class="line" aria-expanded="'+open+'" data-seq="'+e.seq+'"><span class="seq"><b>#'+e.seq+'</b></span><span class="ts">'+tm(e.ts)+'</span><span class="actor '+kind+'" title="'+esc(e.actor)+'">'+esc(e.actor)+'</span><span class="what"><span class="act">'+esc(e.action)+'</span><span class="why">'+esc(e.why)+'</span></span><span class="subj" title="'+esc(e.subject)+'">'+esc(e.subject)+'</span>'+ICON.chev+'</button>'+
'<div class="detail"><pre>'+esc(JSON.stringify(e.detail,null,2))+'</pre><div class="hashes"><span class="h'+(bad?" bad":"")+'" title="'+esc(e.prev_hash)+'">'+short(e.prev_hash)+'</span>'+ICON.link+'<span class="h'+(bad?" bad":"")+'" title="'+esc(e.hash)+'">'+short(e.hash)+'</span>'+(pe?'<span class="muted">follows #'+pe.seq+'</span>':'')+'</div></div></div>';
}).join("")+'</div>';
}).join("");
}
function fillActions(){
var sel=$("f-action"),cur=sel.value,acts=Array.from(new Set(state.events.map(function(e){return e.action}))).sort();
sel.innerHTML='<option value="">all</option>'+acts.map(function(a){return '<option>'+esc(a)+'</option>'}).join("");
sel.value=acts.indexOf(cur)<0?"":cur;
}
async function load(){
state.bad="pending";badge();
var data=null;
if(!MODE)data=await aimo.api("/v1/audit",{headers:{"X-Admin-Key":"demo-admin"}});
var evs,text="";
if(data){evs=Array.isArray(data)?data:(data.events||data.trail||[]);text=Array.isArray(data)?"":(data.text||"");aimo.mock(false)}
else{evs=await buildMock(MODE==="broken");aimo.mock(true)}
state.q2o={};evs.forEach(function(e){var d=e.detail||{};if(d.order_id&&d.quote_id)state.q2o[d.quote_id]=d.order_id});state.events=evs;state.text=text;state.cp=data&&!Array.isArray(data)?data.checkpoint:null;var sv=data&&!Array.isArray(data)?data.verified:null;
fillActions();
var br=await verify(evs);
if(!sv&&data&&!MODE){var v=await aimo.api("/v1/audit/verify",{headers:{"X-Admin-Key":"demo-admin"}});if(v&&typeof v.ok==="boolean")sv={ok:v.ok,bad_seq:v.broken_at||v.bad_seq}}
state.bad=null;state.mode="browser";state.srv=!!(sv&&sv.ok);
if(sv&&sv.ok===false)state.bad={at:sv.bad_seq};
else if(br){if(sv&&sv.ok)state.mode="gateway";else if(data)state.mode="unknown";else state.bad=br}
badge();scope();render();state.live=!!data;receipt(data);
}
var MOCK_ORDER={order_id:"ord_7f3a",quote_id:"q_91c2",customer:"hotel-aurora",agent_id:"buyer-aurora",total_net:2240,status:"confirmed",delivery_date:"2026-11-17",decision:{outcome:"needs_approval",reasons:[{code:"per_order_limit",text:"Order total 2,240.00 EUR is above the 2,000 EUR per-order limit."}]},substitutions:[{original_id:"410008",substitute_id:"410112",reason:"Same lactose-free 1 L milk in a different pack, in stock for the full quantity."}],lines:[]};
var TAG={confirmed:"ok",placed:"ok",approved:"ok",pending_approval:"warn",draft:"neutral",rejected:"stop",blocked:"stop",expired:"stop"};
function eur(v){return Number(v).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2})+" EUR"}
function row(k,v){return '<div class="rc-k">'+k+'</div><div class="rc-v">'+v+'</div>'}
async function receipt(data){
var el=$("receipt"),id=$("f-order").value.trim();
if(!id){el.hidden=true;return}
var o=null;
if(data&&!MODE)o=await aimo.api("/v1/orders/"+encodeURIComponent(id),{headers:{"X-Admin-Key":"demo-admin"}});
else if(id===MOCK_ORDER.order_id)o=MOCK_ORDER;
if(!o){el.hidden=false;el.innerHTML='<div class="panel-head"><h2>Order receipt</h2></div><p class="muted">No order found for '+esc(id)+'.</p>';return}
var evs=state.events.filter(function(e){return orderOf(e)===id||e.subject===o.quote_id||(e.detail&&e.detail.quote_id===o.quote_id)});
var ap=evs.filter(function(e){return e.actor.indexOf("human:")===0&&/approv|reject/.test(e.action)}).pop();
var red=evs.some(function(e){return /redeem/.test(e.action)||e.action==="place_order"||/^order\./.test(e.action)});
var dec=o.decision||{};
var out={auto:"Placed automatically within the mandate",needs_approval:"Needed human approval",blocked:"Blocked by policy"}[dec.outcome]||dec.outcome;
var h='<div class="panel-head"><h2>Order receipt <span class="mono muted">'+esc(o.order_id)+'</span></h2><span class="tag '+(TAG[o.status]||"neutral")+'">'+esc(String(o.status).replace(/_/g," "))+'</span></div><div class="rc">';
h+=row("Customer",esc(o.customer))+row("Buyer agent",'<span class="mono">'+esc(o.agent_id)+'</span>')+row("Total net",'<span class="mono">'+eur(o.total_net)+'</span>')+row("Delivery",o.delivery_date?'<span class="mono">'+esc(String(o.delivery_date).slice(0,10))+'</span>':'<span class="muted">not set</span>');
h+=row("Decision",esc(out)+(dec.reasons&&dec.reasons.length?'<ul>'+dec.reasons.map(function(r){return '<li>'+esc(r.text||r.code)+'</li>'}).join("")+'</ul>':""));
h+=row("Approval",ap?esc(/reject/.test(ap.action)?"Rejected":"Approved")+" by <span class=\"mono\">"+esc(ap.actor.replace(/^human:/,""))+"</span> at <span class=\"mono\">"+esc(ap.ts.replace("T"," ").replace("Z"," UTC"))+"</span>"+(ap.why?'<div class="muted small">'+esc(ap.why)+'</div>':''):'<span class="muted">'+(dec.outcome==="needs_approval"?"Awaiting a human decision":"No human approval required")+'</span>');
var subs=o.substitutions||[];
h+=row("Substitutions",subs.length?'<ul>'+subs.map(function(x){return '<li><span class="mono">'+esc(x.original_id)+'</span> &rarr; <span class="mono">'+esc(x.substitute_id)+'</span>: '+esc(x.reason)+'</li>'}).join("")+'</ul>':'<span class="muted">none</span>');
h+=row("Quote",'<span class="mono">'+esc(o.quote_id)+'</span> '+(red?'<span class="tag ok">signature verified at placement</span>':'<span class="tag neutral">not yet redeemed</span>'));
el.innerHTML=h+'</div>';el.hidden=false;
}
function scope(){var c=$("f-customer").value,o=$("f-order").value.trim(),v=filtered().length,n=state.events.length;$("scope").textContent=(c||"all customers")+(o?" / "+o:"")+" \u00b7 showing "+v+" of "+n+" events"}
function refilter(){scope();render()}
function exportJson(){
var b=new Blob([JSON.stringify(state.events,null,2)],{type:"application/json"}),a=document.createElement("a");
a.href=URL.createObjectURL(b);a.download="audit-"+aimo.customer()+".json";document.body.appendChild(a);a.click();a.remove();
}
$("f-order").value=Q.get("order_id")||"";
$("filters").addEventListener("submit",function(e){e.preventDefault()});
$("f-order").addEventListener("input",function(){var u=new URL(location);if(this.value.trim())u.searchParams.set("order_id",this.value.trim());else u.searchParams.delete("order_id");history.replaceState(null,"",u);refilter()});
$("f-order").addEventListener("change",function(){receipt(state.live?{}:null)});
["f-actor","f-action","f-customer"].forEach(function(i){$(i).addEventListener("change",refilter)});
$("v-ledger").addEventListener("click",function(){state.view="ledger";render()});
$("v-text").addEventListener("click",function(){state.view="text";render()});
$("export").addEventListener("click",exportJson);
$("print").addEventListener("click",function(){window.print()});
$("ledger").addEventListener("click",function(e){var b=e.target.closest(".line");if(!b)return;var s=b.dataset.seq;state.open[s]=!state.open[s];var r=b.parentNode;r.dataset.open=!!state.open[s];b.setAttribute("aria-expanded",!!state.open[s])});
$("f-customer").innerHTML='<option value="">all customers</option>'+aimo.CUSTOMERS.map(function(c){return "<option>"+c+"</option>"}).join("");
$("f-customer").value=aimo.customer();
window.addEventListener("customer",function(e){$("f-customer").value=e.detail;refilter()});
load();
})();

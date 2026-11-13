(function(){
var Q=new URLSearchParams(location.search);
var MODE=Q.get("mock");
var $=function(id){return document.getElementById(id)};
var state={events:[],text:"",view:"ledger",bad:null,open:{}};
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

var MOCK_RAW=[
["agent:buyer-aurora","parse_request","need:n1","Read the request as 1000 L lactose-free milk, delivery by Monday 17 Nov.",{raw_text:"1000 litres lactose-free milk by Monday"},"ord_7f3a"],
["agent:buyer-aurora","get_offers","need:n1","Found 4 lactose-free milk offers; the cheapest in stock covers 1000 L at 1.12 EUR/L.",{offers:4,best:"410008"},"ord_7f3a"],
["agent:buyer-aurora","request_quote","quote:q_91c2","Signed a quote for 1000 L at 1,120.00 EUR net, valid until the 14:00 order cutoff.",{total_net:1120,valid_until:"2026-11-14T14:00:00Z"},"ord_7f3a"],
["system","policy_check","order:ord_7f3a","Order total 1,120.00 EUR is above the 800 EUR per-order limit, so a human must approve.",{limit:800,total:1120,outcome:"needs_approval"},"ord_7f3a"],
["agent:buyer-aurora","place_order","order:ord_7f3a","Placed the order against the signed quote; held for approval.",{quote_id:"q_91c2",status:"pending_approval"},"ord_7f3a"],
["human:kitchen-manager","approve_order","order:ord_7f3a","Approved: the milk is for Monday breakfast service.",{approve:true,note:"ok for Monday"},"ord_7f3a"],
["system","order_placed","order:ord_7f3a","Order passed to fulfilment and confirmed for delivery on 17 Nov.",{status:"confirmed"},"ord_7f3a"],
["agent:seller-valio","stock_alert","product:410008","Stock for lactose-free milk 1 L fell below the weekly average; offering oat drink as an alternative.",{available:620},null],
["agent:buyer-aurora","get_offers","need:n2","Found 3 offers for 40 kg unsalted butter; cheapest in stock 7.40 EUR/kg.",{offers:3},"ord_8b21"],
["system","policy_check","order:ord_8b21","Order is within the 800 EUR limit and all allergen rules; no approval needed.",{outcome:"auto"},"ord_8b21"],
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
var m=/^order:(.+)$/.exec(e.subject||"");return m?m[1]:null;
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
var b=$("badge"),n=state.events.length;
if(state.bad===undefined||state.bad==="pending"){b.className="badge idle";b.textContent="Verifying chain";return}
if(state.bad){b.className="badge stop";b.innerHTML=ICON.alert+'Chain broken at #'+state.bad.at+' <a href="#e'+state.bad.at+'" id="jump">Jump to #'+state.bad.at+'</a>';
$("jump").addEventListener("click",function(){state.open[state.bad.at]=true;render();var el=$("e"+state.bad.at);if(el)el.scrollIntoView({block:"center"})});return}
b.className="badge ok";b.innerHTML=ICON.shield+"Chain verified, "+n+" event"+(n===1?"":"s");
}
function render(){
var evs=filtered(),L=$("ledger");
$("plain").hidden=state.view!=="text";L.hidden=state.view==="text";
$("v-ledger").setAttribute("aria-pressed",state.view==="ledger");$("v-text").setAttribute("aria-pressed",state.view==="text");
$("plain").textContent=state.view==="text"?(state.text||plainText(evs)):"";
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
'<button type="button" class="line" aria-expanded="'+open+'" data-seq="'+e.seq+'"><span class="seq"><b>#'+e.seq+'</b></span><span class="ts">'+tm(e.ts)+'</span><span class="actor '+kind+'">'+esc(e.actor)+'</span><span class="what"><span class="act">'+esc(e.action)+'</span><span class="why">'+esc(e.why)+'</span></span><span class="subj" title="'+esc(e.subject)+'">'+esc(e.subject)+'</span>'+ICON.chev+'</button>'+
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
state.events=evs;state.text=text;
fillActions();
state.bad=await verify(evs);
badge();scope();render();
}
function scope(){var c=$("f-customer").value,o=$("f-order").value.trim();$("scope").textContent=(c||"all customers")+(o?" / "+o:"")}
function refilter(){scope();render()}
function exportJson(){
var b=new Blob([JSON.stringify(state.events,null,2)],{type:"application/json"}),a=document.createElement("a");
a.href=URL.createObjectURL(b);a.download="audit-"+aimo.customer()+".json";document.body.appendChild(a);a.click();a.remove();
}
$("f-order").value=Q.get("order_id")||"";
$("filters").addEventListener("submit",function(e){e.preventDefault()});
$("f-order").addEventListener("input",function(){var u=new URL(location);if(this.value.trim())u.searchParams.set("order_id",this.value.trim());else u.searchParams.delete("order_id");history.replaceState(null,"",u);refilter()});
["f-actor","f-action","f-customer"].forEach(function(i){$(i).addEventListener("change",refilter)});
$("v-ledger").addEventListener("click",function(){state.view="ledger";render()});
$("v-text").addEventListener("click",function(){state.view="text";render()});
$("export").addEventListener("click",exportJson);
$("ledger").addEventListener("click",function(e){var b=e.target.closest(".line");if(!b)return;var s=b.dataset.seq;state.open[s]=!state.open[s];var r=b.parentNode;r.dataset.open=!!state.open[s];b.setAttribute("aria-expanded",!!state.open[s])});
$("f-customer").innerHTML='<option value="">all customers</option>'+aimo.CUSTOMERS.map(function(c){return "<option>"+c+"</option>"}).join("");
$("f-customer").value=aimo.customer();
window.addEventListener("customer",function(e){$("f-customer").value=e.detail;refilter()});
load();
})();
